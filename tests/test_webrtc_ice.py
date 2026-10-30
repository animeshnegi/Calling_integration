"""Regression tests for Asterisk's browser ICE candidate mapping.

These exercise the shipped entrypoint helper and the real configuration sync,
without requiring a live Asterisk container or public network.
"""
from pathlib import Path
import fcntl
import ipaddress
import os
import socket
import subprocess
import threading
import time

import pytest

from app.admin import SettingsStore
from app.telephony_config import TelephonyConfigSync


ROOT = Path(__file__).resolve().parents[1]
HEADER = "; Managed for browser WebRTC; Docker publishes the same RTP ports.\n[ice_host_candidates]\n"


class AMI:
    def __init__(self, fail_reload=False):
        self.rtp_reloads = 0
        self.restarts = 0
        self.fail_reload = fail_reload

    def reload_rtp(self):
        self.rtp_reloads += 1
        if self.fail_reload:
            self.fail_reload = False
            raise OSError("AMI unavailable")

    def reload_pjsip(self): pass
    def reload_dialplan(self): pass
    def reload_voicemail(self): pass
    def restart(self): self.restarts += 1


def setup_sync(tmp_path, monkeypatch, ami=None):
    monkeypatch.delenv("ASTERISK_EXTERNAL_ADDRESS", raising=False)
    store = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    store.set_settings({"service_host": "198.51.100.42"})
    sync = TelephonyConfigSync(store, ami or AMI(), str(tmp_path / "dynamic" / "pjsip.dynamic.conf"))
    sync.ice_local_path.write_text("172.21.0.2\n127.0.0.1\ninvalid\n")
    return sync


def test_public_rtp_candidate_is_written_and_reloaded_without_changing_dialplan(tmp_path, monkeypatch):
    sync = setup_sync(tmp_path, monkeypatch)
    expected_dialplan = sync.render_dialplan()
    sync.apply()
    assert sync.ice_public_path.read_text() == "198.51.100.42\n"
    assert sync.ice_config_path.read_text() == HEADER + "172.21.0.2 => 198.51.100.42\n"
    assert sync.dialplan_path.read_text() == expected_dialplan
    assert sync.ami.rtp_reloads == 1 and sync.ami.restarts == 1
    sync.apply()
    assert sync.ami.rtp_reloads == 1 and sync.ami.restarts == 1

    # A container recreation can change its bridge IP without changing SIP
    # transports or interrupting established calls with a restart.
    sync.ice_local_path.write_text("172.21.0.7\n")
    sync.apply()
    assert sync.ice_config_path.read_text() == HEADER + "172.21.0.7 => 198.51.100.42\n"
    assert sync.ami.rtp_reloads == 2 and sync.ami.restarts == 1
    assert sync.dialplan_path.read_text() == expected_dialplan


def test_rtp_reload_and_transport_restart_are_both_retried_after_failure(tmp_path, monkeypatch):
    ami = AMI(fail_reload=True)
    sync = setup_sync(tmp_path, monkeypatch, ami)
    with pytest.raises(OSError, match="AMI unavailable"):
        sync.apply()
    assert sync.ice_reload_marker.exists() and sync.restart_marker.exists()
    assert sync.retry_pending() is True
    assert ami.rtp_reloads == 2 and ami.restarts == 1
    assert not sync.ice_reload_marker.exists() and not sync.restart_marker.exists()
    assert sync.retry_pending() is False
    sync.apply()
    assert ami.rtp_reloads == 2 and ami.restarts == 1


def test_failed_ice_only_reload_retries_without_restarting_active_calls(tmp_path, monkeypatch):
    sync = setup_sync(tmp_path, monkeypatch)
    sync.apply()
    sync.ice_local_path.write_text("172.21.0.7\n")
    sync.ami.fail_reload = True
    with pytest.raises(OSError, match="AMI unavailable"):
        sync.apply()
    assert sync.ice_reload_marker.exists() and not sync.restart_marker.exists()
    assert sync.retry_pending() is True
    assert sync.ami.rtp_reloads == 3 and sync.ami.restarts == 1
    assert sync.ice_config_path.read_text() == HEADER + "172.21.0.7 => 198.51.100.42\n"


def test_interrupted_file_write_keeps_restart_and_rtp_reload_due(tmp_path, monkeypatch):
    sync = setup_sync(tmp_path, monkeypatch)
    write = sync._atomic_write
    fail_once = [True]

    def interrupted(path, content):
        if path == sync.path and fail_once[0]:
            fail_once[0] = False
            raise OSError("interrupted after transport write")
        return write(path, content)

    monkeypatch.setattr(sync, "_atomic_write", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        sync.apply()
    assert sync.transports_path.exists()
    assert sync.restart_marker.exists() and sync.ice_reload_marker.exists()
    assert sync.retry_pending() is True
    assert sync.ami.restarts == 1 and sync.ami.rtp_reloads == 1
    assert not sync.restart_marker.exists() and not sync.ice_reload_marker.exists()


def test_bad_or_unresolved_public_address_preserves_last_good_ice_mapping(tmp_path, monkeypatch):
    sync = setup_sync(tmp_path, monkeypatch)
    sync.apply()
    mapping = sync.ice_config_path.read_bytes()
    public = sync.ice_public_path.read_bytes()
    sync.store.set_settings({"service_host": "offline.example.com"})
    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("DNS down")))
    with pytest.raises(ValueError, match="does not resolve"):
        sync.apply()
    assert sync.ice_config_path.read_bytes() == mapping and sync.ice_public_path.read_bytes() == public
    assert sync.ami.rtp_reloads == 1 and sync.ami.restarts == 1


def test_ipv6_only_or_missing_container_ipv4_never_invents_a_mapping(tmp_path, monkeypatch, caplog):
    sync = setup_sync(tmp_path, monkeypatch)
    sync.store.set_settings({"service_host": "v6.example.com"})
    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_kw: [
        (socket.AF_INET6, socket.SOCK_DGRAM, 0, "", ("2001:4860:4860::8888", 0, 0, 0))
    ])
    sync.apply()
    assert sync.ice_public_path.read_text() == ""
    assert sync.ice_config_path.read_text() == HEADER
    assert "IPv6-only" in caplog.text
    assert "external_media_address=2001:4860:4860::8888" in sync.transports_path.read_text()

    sync.store.set_settings({"service_host": "198.51.100.42"})
    sync.ice_local_path.write_text("")
    sync.apply()
    assert sync.ice_config_path.read_text() == HEADER
    assert "No Asterisk container IPv4" in caplog.text


def test_old_asterisk_image_does_not_block_sip_when_it_has_no_ice_handoff(tmp_path, monkeypatch):
    sync = setup_sync(tmp_path, monkeypatch)
    sync.ice_local_path.unlink()
    sync.apply()
    assert not sync.ice_config_path.exists() and sync.ami.rtp_reloads == 0
    assert sync.ice_public_path.read_text() == "198.51.100.42\n"
    assert sync.ami.restarts == 1


def run_helper(tmp_path, ips, public):
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    (fakebin / "hostname").write_text("#!/bin/sh\necho asterisk-container\n")
    (fakebin / "getent").write_text("#!/bin/sh\n" + "".join(f"echo '{ip} STREAM x'\n" for ip in ips))
    for executable in fakebin.iterdir():
        executable.chmod(0o755)
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir(exist_ok=True)
    (dynamic / "rtp.public-address").write_text(public)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}
    result = subprocess.run(["sh", str(ROOT / "asterisk" / "configure-ice.sh"), str(dynamic)], env=env,
                            capture_output=True, text=True, check=True)
    return dynamic, result


def test_asterisk_boot_rebuilds_mapping_when_docker_ip_changes(tmp_path):
    dynamic, _ = run_helper(tmp_path, ["172.21.0.2", "127.0.0.1", "172.21.0.2"], "198.51.100.42\n")
    assert (dynamic / "rtp.local-addresses").read_text() == "172.21.0.2\n"
    assert (dynamic / "rtp.ice.conf").read_text() == HEADER + "172.21.0.2 => 198.51.100.42\n"
    dynamic, _ = run_helper(tmp_path, ["172.21.0.7"], "198.51.100.42\n")
    assert (dynamic / "rtp.ice.conf").read_text() == HEADER + "172.21.0.7 => 198.51.100.42\n"


def test_asterisk_boot_with_no_container_ip_clears_stale_mapping_and_warns(tmp_path):
    dynamic, _ = run_helper(tmp_path, ["172.21.0.2"], "198.51.100.42\n")
    assert (dynamic / "rtp.ice.conf").read_text().endswith("172.21.0.2 => 198.51.100.42\n")
    dynamic, result = run_helper(tmp_path, [], "198.51.100.42\n")
    assert (dynamic / "rtp.local-addresses").read_text() == ""
    assert (dynamic / "rtp.ice.conf").read_text() == HEADER
    assert "Could not find this Asterisk container" in result.stderr


@pytest.mark.parametrize("public", [
    "asterisk.local\n", "172.21.0.1\nextra=bad\n", "1.2.3.999\n", "127.0.0.1\n",
    "10.0.0.1\n", "100.64.0.1\n", "192.168.1.1\n", "192.0.0.9\n", "224.0.0.1\n",
    "104.16.100.100\n", "172.64.1.1\n", "2606:4700:4700::1111\n",
])
def test_invalid_cached_public_address_cannot_inject_asterisk_configuration(tmp_path, public):
    dynamic, result = run_helper(tmp_path, ["172.21.0.2"], public)
    assert (dynamic / "rtp.ice.conf").read_text() == HEADER
    assert "Invalid cached WebRTC media address" in result.stderr


def test_boot_validator_agrees_with_api_for_ipv4_policy_boundaries(tmp_path):
    networks = [ipaddress.ip_network(item) for item in (
        TelephonyConfigSync.UNREACHABLE_MEDIA_NETWORKS + TelephonyConfigSync.CLOUDFLARE_MEDIA_NETWORKS
        + ["224.0.0.0/4", "240.0.0.0/4"]
    ) if ":" not in item]
    addresses = {str(boundary) for net in networks for boundary in (net.network_address, net.broadcast_address)}
    addresses.update({"8.8.8.8", "198.51.100.42", "203.0.113.20"})
    for address in sorted(addresses):
        try:
            TelephonyConfigSync._check_media_address(address)
            permitted = True
        except ValueError:
            permitted = False
        dynamic, _ = run_helper(tmp_path, ["172.21.0.2"], address + "\n")
        mapped = "172.21.0.2 => " + address + "\n" in (dynamic / "rtp.ice.conf").read_text()
        assert mapped == permitted, address


def test_config_sync_serializes_shared_volume_writers(tmp_path, monkeypatch):
    sync = setup_sync(tmp_path, monkeypatch)
    started, finished = threading.Event(), threading.Event()
    errors = []

    def apply():
        started.set()
        try:
            sync.apply()
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    with sync.sync_lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        thread = threading.Thread(target=apply)
        thread.start()
        assert started.wait(2)
        assert not finished.wait(0.2)
        assert not sync.ice_config_path.exists()
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert sync.ice_config_path.read_text() == HEADER + "172.21.0.2 => 198.51.100.42\n"


def test_asterisk_boot_helper_uses_the_same_cross_process_lock(tmp_path):
    dynamic, _ = run_helper(tmp_path, ["172.21.0.2"], "198.51.100.42\n")
    lock_path = dynamic / ".config-sync.lock"
    fakebin = tmp_path / "bin"
    (fakebin / "getent").write_text("#!/bin/sh\necho '172.21.0.7 STREAM x'\n")
    (fakebin / "getent").chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        child = subprocess.Popen(["sh", str(ROOT / "asterisk" / "configure-ice.sh"), str(dynamic)], env=env)
        try:
            time.sleep(0.2)
            assert child.poll() is None
            assert (dynamic / "rtp.ice.conf").read_text().endswith("172.21.0.2 => 198.51.100.42\n")
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    assert child.wait(timeout=5) == 0
    assert (dynamic / "rtp.ice.conf").read_text() == HEADER + "172.21.0.7 => 198.51.100.42\n"


def test_rtp_include_is_in_the_image_and_preserves_the_existing_port_range():
    entrypoint = (ROOT / "asterisk" / "entrypoint.sh").read_text()
    dockerfile = (ROOT / "asterisk" / "Dockerfile").read_text()
    assert '/configure-ice.sh "$DYNAMIC_DIR"' in entrypoint
    assert "#include dynamic/rtp.ice.conf" in entrypoint
    assert "rtpstart=$RTP_START" in entrypoint and "rtpend=$RTP_END" in entrypoint
    assert "COPY entrypoint.sh configure-ice.sh /" in dockerfile
