#!/usr/bin/env python3
"""Optional real-Asterisk voicemail -> offline transcript -> Elastic multipart test.

Uses throwaway SQLite settings, a loopback SIP client, an isolated PBX, a public
speech fixture and a local HTTP server. Nothing reaches Elastic Email or a real
inbox. Linux prerequisites: Asterisk 22 with app_voicemail/format_wav/PJSIP,
pjsua 2.17, app requirements (including PocketSphinx), and a shared library path.

    ASTERISK_BIN=/path/to/asterisk ASTERISK_MODULES=/path/to/modules \
    PJSUA_BIN=/path/to/pjsua PYTHONPATH=. python tools/voicemail-asteriskcheck.py

Optional --shared-group-test additionally needs passwordless sudo and setpriv;
it records with umask 0022 using one UID and reads/moves with another UID in the
same group, exercising old-volume repair. Add --modern-umask to test the new
0007 process mask and freshly created folders without any repair.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import wave
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.admin import SettingsStore
from app.telephony_config import TelephonyConfigSync
from app.voicemail import VoicemailStore
from app.voicemail_email import VoicemailEmailNotifier
import app.voicemail_email as notification

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "deploy/fixtures/goforward-8k.wav"
OPTIONAL_PJSIP = [
    "res_pjsip_aoc", "res_pjsip_dialog_info_body_generator", "res_pjsip_exten_state",
    "res_pjsip_mwi", "res_pjsip_mwi_body_generator", "res_pjsip_outbound_publish",
    "res_pjsip_pidf_body_generator", "res_pjsip_pidf_digium_body_supplement",
    "res_pjsip_pidf_eyebeam_body_supplement", "res_pjsip_publish_asterisk", "res_pjsip_xpidf_body_generator",
    "res_pjsip_one_touch_record_info", "res_pjsip_t38", "res_pjsip_history", "res_pjsip_messaging",
    "res_pjsip_refer", "res_pjsip_sips_contact", "res_pjsip_acl", "res_pjsip_logger",
]


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("PASS " + label, flush=True)


def wait_for(predicate, description, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError(description)


def udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def tone(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    import array
    samples = array.array("h", (int(3000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(800)))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
        output.writeframes(samples.tobytes())


class FakeElastic(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        content = self.rfile.read(int(self.headers["Content-Length"]))
        msg = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + content
        )
        parts = {part.get_param("name", header="content-disposition"): part for part in msg.iter_parts()}
        self.received.append((self.path, parts))
        payload = b'{"success":true,"data":{"messageid":"loopback-test"}}'
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def log_message(self, *args):
        pass


def stop(process, command=None):
    if not process or process.poll() is not None:
        return
    if command and process.stdin:
        try:
            process.stdin.write(command.encode()); process.stdin.flush()
        except BrokenPipeError:
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=6)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-group-test", action="store_true")
    parser.add_argument("--modern-umask", action="store_true")
    args = parser.parse_args()
    ast_bin = os.environ.get("ASTERISK_BIN", "")
    modules = os.environ.get("ASTERISK_MODULES", "")
    pjsua_bin = os.environ.get("PJSUA_BIN", "")
    if not all(Path(value).exists() for value in (ast_bin, modules, pjsua_bin) if value) or not all((ast_bin, modules, pjsua_bin)):
        parser.error("Set ASTERISK_BIN, ASTERISK_MODULES, and PJSUA_BIN")
    if args.shared_group_test and (not shutil.which("sudo") or not shutil.which("setpriv")):
        parser.error("--shared-group-test needs sudo and setpriv")
    tmp = Path(tempfile.mkdtemp(prefix="eip-real-voicemail-"))
    tmp.chmod(0o755)
    etc, data, run, logs, spool = (tmp / name for name in ("etc", "data", "run", "logs", "spool"))
    for path in (etc, data / "keys/keys", run, logs, spool / "voicemail"):
        path.mkdir(parents=True, exist_ok=True)
    if args.shared_group_test:
        subprocess.run(["sudo", "chown", f"{os.getuid()}:2000", str(spool / "voicemail")], check=True)
        subprocess.run(["sudo", "chmod", "2770", str(spool / "voicemail")], check=True)
    ast = client = server = None
    try:
        settings = SettingsStore(str(tmp / "settings.db"), "test-only-secret-" * 4)
        settings.save_email_config({"enabled": True, "api_key": "ELASTIC.loopback-test", "from_email": "sender@example.com", "transcription_enabled": True})
        settings.save_provider({"name": "p", "server": "127.0.0.1", "username": "u", "password": "test-only", "allowed_ips": "127.0.0.1/32"})
        owner = settings.save_user({"username": "vm-test", "email": "owner@example.com", "password": "test-password-12345", "role": "user", "active": True})
        number = "+13025559907"
        settings.save_number({"number": number, "provider": "p", "owner_user_id": owner, "active": True})
        key = settings.provision_number(owner, number)["extension"]
        settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
            {"type": "ring_group", "extensions": [key], "timeout": 25, "configured": True},
            {"type": "voicemail", "mailbox": key, "configured": True},
        ]}})
        extension = settings.list_extensions(owner)[0]
        mailbox, username = extension["mailbox"], extension["sip_username"]
        # Known throwaway password, not a generated or real customer credential.
        settings.save_extension({"extension": key, "sip_password": "test-password"}, owner)
        # No carrier is needed for this loopback test; do not register one or
        # identify the SIP client as a provider based on its loopback IP.
        with settings._connect() as db:
            db.execute("UPDATE sip_providers SET active=0 WHERE name='p'")
        sync = TelephonyConfigSync(settings, None, str(etc / "pjsip.dynamic.conf"))
        write(etc / "pjsip.dynamic.conf", sync.render_pjsip())
        write(etc / "voicemail.dynamic.conf", sync.render_voicemail())
        write(etc / "voicemail.conf", "[general]\nformat=wav\nmaxsecs=30\nminsecs=2\nreview=no\nattach=no\n#include voicemail.dynamic.conf\n")
        write(etc / "extensions.conf", "[default]\nexten => s,1,Hangup()\n")
        sip, local = udp_port(), udp_port()
        rtp = 20000 + 2 * (udp_port() % 10000)
        write(etc / "asterisk.conf", f"[directories]\nastetcdir => {etc}\nastmoddir => {modules}\nastvarlibdir => {data}\nastdbdir => {data}\nastkeydir => {data / 'keys'}\nastdatadir => {data}\nastagidir => {data / 'agi-bin'}\nastspooldir => {spool}\nastrundir => {run}\nastlogdir => {logs}\n[options]\nverbose=1\n")
        write(etc / "modules.conf", (ROOT / "asterisk/config/modules.conf").read_text() + "\n" + "\n".join(f"noload => {name}.so" for name in OPTIONAL_PJSIP) + "\n")
        write(etc / "pjsip.conf", f"[global]\ntype=global\nendpoint_identifier_order=ip,username,auth_username,anonymous\n[transport-udp]\ntype=transport\nprotocol=udp\nbind=127.0.0.1:{sip}\n#include pjsip.dynamic.conf\n")
        write(etc / "rtp.conf", f"[general]\nrtpstart={rtp}\nrtpend={rtp + 100}\n")
        write(etc / "logger.conf", "[general]\n[logfiles]\nconsole => notice,warning,error,verbose\n")
        tone(data / "sounds/beep.wav")
        ast_log, client_log = tmp / "asterisk.log", tmp / "client.log"
        command = [ast_bin, "-C", str(etc / "asterisk.conf"), "-f", "-n", "-v"]
        mask = 0o007 if args.modern_umask else 0o022
        if args.shared_group_test:
            # sudo sanitizes LD_LIBRARY_PATH and can reset umask. Restore both
            # after privilege separation, never credentials or user data.
            command = ["sudo", "setpriv", "--reuid", str(os.getuid()), "--regid", "2000", "--clear-groups", "--",
                       "env", f"LD_LIBRARY_PATH={os.environ.get('LD_LIBRARY_PATH', '')}",
                       "sh", "-c", f"umask {mask:04o}; exec \"$@\"", "sh"] + command
        with ast_log.open("wb") as out:
            ast = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, umask=mask)
        def ast_ready():
            if ast.poll() is not None:
                raise RuntimeError("Asterisk exited during startup")
            return "Asterisk Ready." in ast_log.read_text(errors="replace")
        wait_for(ast_ready, "Asterisk startup")
        def cli(command):
            return subprocess.run([ast_bin, "-C", str(etc / "asterisk.conf"), "-rx", command], check=True, text=True, capture_output=True).stdout
        check("actual Asterisk loaded app_voicemail", "Running" in cli("module show like app_voicemail"))
        check("flow mailbox exists although desk voicemail switch is off", not extension["voicemail_enabled"] and mailbox in cli("voicemail show users"))
        with client_log.open("wb") as out:
            client = subprocess.Popen([pjsua_bin, f"--id=sip:{username}@127.0.0.1:{sip}", f"--registrar=sip:127.0.0.1:{sip}",
                "--realm=*", f"--username={username}", "--password=test-password", f"--local-port={local}",
                "--bound-addr=127.0.0.1", "--auto-answer=200", f"--play-file={SAMPLE}", "--auto-play", "--null-audio",
                "--no-tcp", "--stdout-no-buf", "--log-level=4", "--app-log-level=4"], stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT)
        def registered():
            if client.poll() is not None:
                raise RuntimeError("Loopback SIP client exited")
            return "registration success, status=200" in client_log.read_text(errors="replace").lower()
        wait_for(registered, "SIP registration")
        cli(f"channel originate PJSIP/{mailbox} application VoiceMail {mailbox}@engineerip,s")
        tempdir = spool / "voicemail/engineerip" / mailbox / "tmp"
        wait_for(lambda: next(tempdir.glob("*.wav"), None), "Asterisk starting a real voicemail recording")
        time.sleep(8)  # Play the real speech sample over RTP, through ulaw.
        client.stdin.write(b"h\n"); client.stdin.flush()
        inbox = spool / "voicemail/engineerip" / mailbox / "INBOX"
        audio = wait_for(lambda: next(inbox.glob("msg*.wav"), None), "Asterisk saving the voicemail")
        wait_for(lambda: audio.with_suffix(".txt").exists(), "Asterisk voicemail metadata")
        with wave.open(str(audio), "rb") as recording:
            check("actual Asterisk recorded 8 kHz mono PCM WAV", recording.getframerate() == 8000 and recording.getnchannels() == 1 and recording.getsampwidth() == 2 and recording.getnframes() >= 16000)
        vm = VoicemailStore(str(spool / "voicemail"))
        check("actual Asterisk mailbox has a readable voicemail", len(vm.list_messages(mailbox)) == 1)
        # Wait for the worker's real ten-second recording-stability guard.
        wait_for(lambda: time.time() - audio.stat().st_mtime >= 10, "recording stability", seconds=20)
        FakeElastic.received = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeElastic)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        notification.ELASTIC_EMAIL_URL = f"http://127.0.0.1:{server.server_port}/v2/email/send"
        notifier = VoicemailEmailNotifier(vm, settings)
        check("real recording is accepted once by the local Elastic contract", notifier.process() == 1 and notifier.process() == 0)
        path, parts = FakeElastic.received[0]
        text = parts["bodyText"].get_content()
        markup = parts["bodyHtml"].get_content()
        attachment = parts["file"].get_payload(decode=True)
        check("multipart recipient is the associated account and attachment is exact", path == "/v2/email/send" and parts["to"].get_content() == "owner@example.com" and attachment == audio.read_bytes())
        deliveries = settings.list_voicemail_deliveries()
        # This is a public speech fixture, never customer audio. On failure,
        # show the recognizer status/text to distinguish engine incompatibility
        # from recognition variation or a missing transcript in the message.
        if not (deliveries[0]["transcription_status"] == "transcribed" and "forward" in text.lower() and "transcript" in markup.lower()):
            print("Fixture transcription status:", deliveries[0]["transcription_status"], "message:", text, flush=True)
        check("PocketSphinx transcribed the actual Asterisk recording", deliveries[0]["transcription_status"] == "transcribed" and "forward" in text.lower() and "transcript" in markup.lower())
        check("plain and HTML bodies contain the automated do-not-reply notice", all("do not reply" in value.lower() and "not associated with a user" in value.lower() for value in (text, markup)))
        if args.shared_group_test:
            if args.modern_umask:
                check("Asterisk's new umask creates a group-writable mailbox", bool(inbox.stat().st_mode & stat.S_IWGRP) and bool(inbox.stat().st_mode & stat.S_ISGID))
                # In production both API and worker run as the same app UID.
                # The parent above used the fixture owner's UID instead, so let
                # the simulated API UID create the lock itself for this case.
                (spool / "voicemail/.voicemail.lock").unlink()
            else:
                check("Asterisk really created a legacy group-unwritable directory", not (inbox.stat().st_mode & stat.S_IWGRP))
            # Copy the standalone store so the different UID need not traverse
            # the current user's private home directory.
            module = tmp / "vm_store.py"
            shutil.copyfile(ROOT / "app/voicemail.py", module); module.chmod(0o644)
            reader = "import importlib.util; s=importlib.util.spec_from_file_location('vm',%r); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); v=m.VoicemailStore(%r); assert len(v.list_messages())==1; print('read')" % (str(module), str(spool / "voicemail"))
            low = ["sudo", "setpriv", "--reuid", "22002", "--regid", "2000", "--clear-groups", "--", sys.executable]
            # The API Python may live under a private home; system Python is
            # sufficient for this standalone stdlib-only module.
            low[-1] = "/usr/bin/python3"
            subprocess.run(low + ["-c", reader], check=True, capture_output=True, text=True)
            check("separate API UID can list Asterisk folders without writing", True)
            if not args.modern_umask:
                repair = (ROOT / "asterisk/entrypoint.sh").read_text()
                lines = [line.replace("/var/spool/asterisk/voicemail", str(spool / "voicemail")) for line in repair.splitlines()
                         if line.startswith(("chmod -R g+rwX /var/spool/asterisk/voicemail", "find /var/spool/asterisk/voicemail -type d"))]
                assert len(lines) == 2
                subprocess.run(["sudo", "sh", "-c", "\n".join(lines)], check=True)
            mover = reader + "; assert v.mark_read(%r,'inbox','msg0000'); assert len(v.list_messages(%r,'old'))==1" % (mailbox, mailbox)
            moved = subprocess.run(low + ["-c", mover], capture_output=True, text=True)
            if moved.returncode:
                lock_stat = (spool / "voicemail/.voicemail.lock").stat()
                raise RuntimeError(f"Separate API UID could not move voicemail (lock {lock_stat.st_uid}:{lock_stat.st_gid} {stat.S_IMODE(lock_stat.st_mode):o}): " + moved.stderr)
            check("separate API UID can move the message", True)
            check("mailbox directories retain the shared telephony group", bool(inbox.stat().st_mode & stat.S_IWGRP) and bool(inbox.stat().st_mode & stat.S_ISGID))
            old = inbox.parent / "Old"
            check("API-created Old folder is group-writable and setgid despite umask 0022", bool(old.stat().st_mode & stat.S_IWGRP) and bool(old.stat().st_mode & stat.S_ISGID))
            touch = ["sudo", "setpriv", "--reuid", str(os.getuid()), "--regid", "2000", "--clear-groups", "--", "touch", str(old / "asterisk-write-test")]
            subprocess.run(touch, check=True)
            check("Asterisk UID can write to an API-created folder", True)
    except Exception:
        # Throwaway fixture logs only; the SIP credentials here are test-only.
        for name in ("asterisk.log", "client.log"):
            path = tmp / name
            if path.exists():
                print(f"--- {name} (last lines) ---", file=sys.stderr)
                print("\n".join(path.read_text(errors="replace").splitlines()[-45:]), file=sys.stderr)
        raise
    finally:
        if server:
            server.shutdown(); server.server_close()
        stop(client, "q\n")
        stop(ast)
        if args.shared_group_test:
            # The separate API UID owns its 2770 folders; remove only this
            # securely created temporary fixture, not any real mailbox volume.
            subprocess.run(["sudo", "rm", "-rf", "--", str(tmp)], check=True)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print("Real Asterisk voicemail integration checks passed (loopback only).")


if __name__ == "__main__":
    main()
