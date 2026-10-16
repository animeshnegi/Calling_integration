from __future__ import annotations

import socket
import time
import uuid


class AMIError(RuntimeError):
    pass


class AsteriskAMI:
    """Small, synchronous AMI client used only for trusted internal reloads."""

    def __init__(self, host: str, port: int, username: str, password: str, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

    def _read_message(self, sock: socket.socket) -> dict[str, str]:
        """Read one AMI reply, including the CLI body of a `Follows` reply.

        `Action: Command` does not answer `Response: Success` like ordinary
        actions: a command that ran answers `Response: Follows` with the CLI
        output and an `--END COMMAND--` end marker - and on some versions no
        blank line separates the header block from that output. Reading stops
        at the header block's blank line (ordinary replies) or at the end
        marker (Follows), then a Follows body is consumed through its marker,
        so both layouts parse and the reply is fully drained.
        """
        data = b""
        while b"\r\n\r\n" not in data and b"--END COMMAND--" not in data and len(data) < 64 * 1024:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        message: dict[str, str] = {}
        for line in data.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                message[key.strip()] = value.strip()
        if message.get("Response") == "Follows" and b"--END COMMAND--" not in data:
            while b"--END COMMAND--" not in data and len(data) < 1024 * 1024:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        return message

    def _login(self, sock: socket.socket) -> None:
        # Asterisk's AMI banner is not a CRLF-terminated AMI message on all
        # versions/configurations. Do not block waiting for a blank-line
        # terminator before sending the Login action. If the banner is
        # present, _read_message() below simply parses it together with the
        # Login response.
        login = (
            "Action: Login\r\n"
            f"ActionID: {uuid.uuid4()}\r\n"
            f"Username: {self.username}\r\n"
            f"Secret: {self.password}\r\n"
            "Events: off\r\n\r\n"
        )
        sock.sendall(login.encode())
        response = self._read_message(sock)
        if response.get("Response") != "Success":
            raise AMIError("AMI authentication failed")

    def _exchange(self, payload: str, accepted: set[str]) -> dict[str, str]:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            self._login(sock)
            sock.sendall(payload.encode())
            response = self._read_message(sock)
        if response.get("Response") not in accepted:
            raise AMIError(response.get("Message", "AMI action failed"))
        return response

    def action(self, action: str, **headers: str) -> dict[str, str]:
        """Run an ordinary AMI action, which answers `Response: Success`."""
        action_id = headers.pop("ActionID", str(uuid.uuid4()))
        payload = [f"Action: {action}", f"ActionID: {action_id}"]
        payload.extend(f"{key}: {value}" for key, value in headers.items())
        payload.append("")
        payload.append("")
        return self._exchange("\r\n".join(payload), accepted={"Success"})

    def command(self, cli: str) -> dict[str, str]:
        """Run a CLI command over AMI.

        `Action: Command` is not an ordinary action: a command that ran
        answers `Response: Follows` with the CLI output ended by
        `--END COMMAND--` - judging it by the `Response: Success` contract
        would call a healthy answer a failure. Both shapes are accepted
        (some versions answer `Success` with `Output:` lines); only a
        refusal (`Response: Error`, or anything else unexpected) raises.
        """
        return self._exchange(
            f"Action: Command\r\nActionID: {uuid.uuid4()}\r\nCommand: {cli}\r\n\r\n",
            accepted={"Success", "Follows"},
        )

    def reload_pjsip(self) -> dict[str, str]:
        return self.action("Reload", Module="res_pjsip.so")

    def reload_dialplan(self) -> dict[str, str]:
        return self.action("Reload", Module="pbx_config.so")

    def reload_voicemail(self) -> dict[str, str]:
        return self.action("Reload", Module="app_voicemail.so")

    def restart(self, verify_timeout: float = 20.0) -> None:
        """Restart Asterisk and verify it comes back.

        Transport changes only take effect at startup, so the configuration
        sync restarts Asterisk when the rendered transports change - and a
        dropped endpoint section can only be cleared that way too. Asterisk
        reads the just-written configuration files as it boots, which is why
        the files are written before this runs.

        The restart is `core restart when convenient`, not `now`: it happens
        at once on an idle PBX but waits for the current calls to finish
        otherwise. A restart in the middle of a call tears it down without a
        BYE - the far end never learns, and the caller hears silence the
        moment the call connects - and no configuration change is worth that.

        The failure modes are not the same and must not be swallowed alike:
        failing to reach or authenticate with AMI means the restart NEVER
        happened - the running Asterisk keeps the old configuration while the
        files on disk hold the new one, which is exactly the split state this
        must prevent - so those raise. The restart itself can drop the AMI
        connection before the reply lands; a lost connection AFTER the command
        was dispatched means the restart is under way. Once it is, AMI is
        polled until Asterisk answers again; if it never does, that is raised
        too rather than assumed away.
        """
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            try:
                self._login(sock)
            except AMIError as exc:
                raise AMIError("AMI authentication failed; Asterisk was not restarted") from exc
            sock.sendall(
                f"Action: Command\r\nActionID: {uuid.uuid4()}\r\nCommand: core restart when convenient\r\n\r\n".encode()
            )
            try:
                reply = self._read_message(sock)
            except OSError:
                reply = {}
            # A command that ran answers `Response: Follows` (with the CLI
            # output) or `Success` - only a real refusal is a failure here.
            if reply and reply.get("Response") not in {"Success", "Follows"}:
                raise AMIError(
                    f"Asterisk refused the restart: {reply.get('Message', 'AMI action failed')}"
                )
        # The command landed - either the reply arrived or the connection died
        # as the process went down. Wait it out and confirm Asterisk returns.
        self._verify_running(verify_timeout)

    def _verify_running(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        time.sleep(min(1.0, timeout))  # let the process actually go down first
        while True:
            try:
                self.command("core show version")
                return
            except (AMIError, OSError) as exc:
                last = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        raise AMIError(
            f"Asterisk did not come back within {timeout:.0f}s after the restart; "
            f"check the asterisk container logs. Last error: {last}"
        )
