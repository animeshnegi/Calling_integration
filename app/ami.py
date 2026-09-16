from __future__ import annotations

import socket
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
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 64 * 1024:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        message: dict[str, str] = {}
        for line in data.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                message[key.strip()] = value.strip()
        return message

    def action(self, action: str, **headers: str) -> dict[str, str]:
        action_id = headers.pop("ActionID", str(uuid.uuid4()))
        payload = [f"Action: {action}", f"ActionID: {action_id}"]
        payload.extend(f"{key}: {value}" for key, value in headers.items())
        payload.append("")
        payload.append("")
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            self._read_message(sock)  # banner
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
            sock.sendall("\r\n".join(payload).encode())
            response = self._read_message(sock)
            if response.get("Response") != "Success":
                raise AMIError(response.get("Message", "AMI action failed"))
            return response

    def reload_pjsip(self) -> dict[str, str]:
        return self.action("Reload", Module="res_pjsip.so")

    def reload_dialplan(self) -> dict[str, str]:
        return self.action("Reload", Module="pbx_config.so")
