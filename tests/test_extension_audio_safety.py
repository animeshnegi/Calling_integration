"""Portable media builds and extension-call routing/incident safeguards.

These are build-contract/dialplan regressions, not a live SIP/RTP acceptance test.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.admin import SettingsStore, browser_username
from app.telephony_config import TelephonyConfigSync

ROOT = Path(__file__).resolve().parents[1]


def test_portable_compiler_options_are_set_before_any_parallel_build():
    dockerfile = (ROOT / "asterisk/Dockerfile").read_text()
    generate = dockerfile.index("&& make menuselect.makeopts")
    policy = dockerfile.index("--disable BUILD_NATIVE --enable KEEP_FRAME_POINTERS")
    compile_at = dockerfile.index('&& make -j"${ASTERISK_MAKE_JOBS}"')
    assert generate < policy < compile_at
    assert "--enable BUILD_NATIVE" not in dockerfile
    assert "build-portability.txt" in dockerfile
    assert "BUILD_NATIVE=disabled" in dockerfile


@pytest.mark.parametrize("broken_menuselect", [False, True])
def test_docker_build_guard_refuses_to_compile_when_native_option_survives(tmp_path, broken_menuselect):
    """Run the actual Docker RUN fragment with stand-ins for the build tools."""
    dockerfile = (ROOT / "asterisk/Dockerfile").read_text()
    start = dockerfile.index("&& make menuselect.makeopts") + 3
    end = dockerfile.index("&& make install", start)
    fragment = dockerfile[start:end].replace("\\\n", " ").strip()
    fragment = fragment.removesuffix("&&").strip()
    # The final shell line was continued into make install; don't leave a dangling slash.
    fragment = fragment.rstrip("\\ ")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make = bin_dir / "make"
    make.write_text('''#!/bin/sh
if [ "$1" = menuselect.makeopts ]; then
    printf 'MENUSELECT_CFLAGS= OPTIONAL_API BUILD_NATIVE\\n' > menuselect.makeopts
else
    touch compile-was-run
fi
''')
    make.chmod(0o755)
    menu = tmp_path / "menuselect"
    menu.mkdir()
    tool = menu / "menuselect"
    tool.write_text('''#!/bin/sh
[ "$1 $2 $3 $4 $5" = "--disable BUILD_NATIVE --enable KEEP_FRAME_POINTERS menuselect.makeopts" ] || exit 2
if [ "$BROKEN_MENUSELECT" != 1 ]; then
    printf 'MENUSELECT_CFLAGS= OPTIONAL_API KEEP_FRAME_POINTERS\\n' > menuselect.makeopts
fi
''')
    tool.chmod(0o755)
    result = subprocess.run(["sh", "-ec", fragment], cwd=tmp_path, env={
        **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ASTERISK_MAKE_JOBS": "4",
        "BROKEN_MENUSELECT": "1" if broken_menuselect else "0",
    }, capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) is not broken_menuselect, result.stderr
    assert (tmp_path / "compile-was-run").exists() is not broken_menuselect
    if broken_menuselect:
        assert "refusing a CPU-specific export image" in result.stderr


@pytest.mark.parametrize("webrtc", [False, True])
def test_101_to_105_is_one_parallel_dial_with_anchored_media_and_no_config_restart(tmp_path, webrtc):
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    for digits in ("101", "105"):
        store.save_extension({"extension": digits, "sip_username": f"desk{digits}", "sip_password": "test-secret", "webrtc_enabled": webrtc})
    sync = TelephonyConfigSync(store, None, str(tmp_path / "pjsip.dynamic.conf"))
    context = sync.render_dialplan().split("[from-internal]\n", 1)[1].split("\n[", 1)[0]
    pjsip = sync.render_pjsip()
    for row in store.list_extensions():
        digits = row["digits"]
        route = context.split(f"exten => {digits},1,", 1)[1].split("\nexten =>", 1)[0]
        targets = [f"PJSIP/{digits}"]
        if webrtc:
            targets.insert(0, f"PJSIP/{browser_username(row['sip_username'])}")
        assert f"Dial({'&'.join(targets)},30)" in route
        assert route.count("Dial(") == 1
        assert "Stasis(" not in route  # Native extension calls do not run ARI transfer/config sync.
        assert "restart" not in route.lower() and "System(" not in route
        endpoints = [digits, row["sip_username"]]
        if webrtc:
            endpoints.append(browser_username(row["sip_username"]))
        for endpoint in endpoints:
            body = pjsip.split(f"[{endpoint}]\ntype=endpoint\n", 1)[1].split("\n\n", 1)[0]
            for setting in ("direct_media=no", "rtp_symmetric=yes", "force_rport=yes", "rewrite_contact=yes", "allow=g722,ulaw,alaw"):
                assert setting in body
    # The working direct external-call path keeps its trunk, CID, timeout and guard.
    assert 'GotoIf($["${OUTBOUND_TRUNK}"=""]?blocked)' in context
    assert 'Set(CALLERID(num)=${OUTBOUND_CID})' in context
    assert 'Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)' in context


def mock_docker(tmp_path):
    calls = tmp_path / "docker-calls.jsonl"
    tool = tmp_path / "docker"
    tool.write_text('''#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
with open(os.environ['DOCKER_CALLS'], 'a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='inspect':
    if os.environ.get('MISSING_CONTAINER')=='1':sys.exit(1)
    if '--format' in args:print('Running=true ExitCode=0 OOMKilled=false RestartCount=6')
elif args[0]=='events':print('1700000000 action=die exitCode=132')
elif args[0]=='exec' and 'build-portability.txt' in ' '.join(args):
    if os.environ.get('OLD_IMAGE')=='1':sys.exit(1)
    print('BUILD_NATIVE=disabled')
elif args[0]=='exec':print('diagnostic CLI output')
elif args[0]=='logs':print('normal bridge switch before process exit')
else:sys.exit(90)
''')
    tool.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "DOCKER_CALLS": str(calls)}
    return env, calls


def test_incident_snapshot_retains_historical_exit_code_and_only_reads_state(tmp_path):
    env, log = mock_docker(tmp_path)
    result = subprocess.run(["bash", str(ROOT / "scripts/diagnose-extension-audio.sh")], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "ExitCode=0" in result.stdout and "action=die exitCode=132" in result.stdout
    assert "not proof of" in result.stdout and "SIGILL" in result.stdout
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert {args[0] for args in calls} == {"inspect", "events", "exec", "logs"}
    events = next(args for args in calls if args[0] == "events")
    assert "--until" in events and events[events.index("--until") + 1].endswith("Z")
    assert "--since" in events and "event=die" in events and "event=oom" in events
    assert not any(token in " ".join(args) for args in calls for token in ("set logger", "set debug", "core restart", " reload", "/.env", "{{.Config.Env}}"))
    assert any("pjsip show channelstats" in args for args in calls)


def test_old_image_still_collects_crash_events_and_reports_unavailable_metadata(tmp_path):
    env, _ = mock_docker(tmp_path)
    env["OLD_IMAGE"] = "1"
    result = subprocess.run(["bash", str(ROOT / "scripts/diagnose-extension-audio.sh")], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert "diagnostic command failed" in result.stderr
    assert "action=die exitCode=132" in result.stdout


def test_missing_container_fails_before_any_exec_or_log_dump(tmp_path):
    env, log = mock_docker(tmp_path)
    env["MISSING_CONTAINER"] = "1"
    result = subprocess.run(["bash", str(ROOT / "scripts/diagnose-extension-audio.sh")], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and "Cannot inspect" in result.stderr
    assert all(json.loads(line)[0] == "inspect" for line in log.read_text().splitlines())
