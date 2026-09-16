from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from typing import Any

import requests
import websocket

from .config import Config


class AsteriskError(RuntimeError):
    pass


class AsteriskClient:
    def __init__(self, config: type[Config] = Config):
        self.base_url = config.ASTERISK_ARI_URL.rstrip("/")
        self.user = config.ASTERISK_ARI_USER
        self.password = config.ASTERISK_ARI_PASSWORD
        self.app = config.ASTERISK_ARI_APP
        self.recording_path = getattr(config, "ASTERISK_RECORDING_PATH", "/var/spool/asterisk/recording")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            auth=(self.user, self.password),
            timeout=10,
            **kwargs,
        )
        if not response.ok:
            raise AsteriskError(f"Asterisk API {response.status_code}")
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def _variables(call_id: str, metadata: dict[str, Any] | None = None) -> dict[str, str]:
        variables = {"EIP_CALL_ID": call_id}
        if metadata:
            for key in ("contact_id", "member_id"):
                if metadata.get(key) is not None:
                    variables[f"EIP_{key.upper()}"] = str(metadata[key])
        return variables

    def create_outbound_call(
        self,
        call_id: str,
        extension: str,
        phone: str,
        provider_endpoint: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Ring the employee first; the customer leg is created after answer."""
        employee_channel = f"{call_id}-employee"
        self._request(
            "POST",
            "/channels",
            params={
                "endpoint": f"PJSIP/{extension}",
                "app": self.app,
                "appArgs": f"employee,{call_id},{provider_endpoint},{phone}",
                "channelId": employee_channel,
                "timeout": 30,
            },
            json={"variables": self._variables(call_id, metadata)},
        )
        return call_id

    def create_customer_leg(self, call_id: str, phone: str, provider_endpoint: str, employee_channel_id: str) -> str:
        customer_channel = f"{call_id}-customer"
        self._request(
            "POST",
            "/channels",
            params={
                "endpoint": f"PJSIP/{phone}@{provider_endpoint}",
                "app": self.app,
                "appArgs": f"customer,{call_id}",
                "channelId": customer_channel,
                "originator": employee_channel_id,
                "timeout": 60,
            },
        )
        return customer_channel

    def list_channels(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/channels")
        return result if isinstance(result, list) else []

    def create_bridge(self, call_id: str) -> str:
        bridge_id = f"bridge-{call_id}"
        self._request(
            "POST",
            "/bridges",
            params={"type": "mixing", "bridgeId": bridge_id, "name": f"EngineerIP {call_id}"},
        )
        return bridge_id

    def add_channel_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        self._request("POST", f"/bridges/{bridge_id}/addChannel", params={"channel": channel_id})

    def start_bridge_recording(self, bridge_id: str, name: str, fmt: str, beep: bool, max_duration: int = 0) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/bridges/{bridge_id}/record",
            params={
                "name": name,
                "format": fmt,
                "ifExists": "fail",
                "beep": "true" if beep else "false",
                "maxDurationSeconds": max_duration,
                "terminateOn": "none",
            },
        )

    def play_bridge_media(self, bridge_id: str, media: str) -> dict[str, Any]:
        if not media or len(media) > 256 or "\r" in media or "\n" in media:
            raise ValueError("Invalid announcement media")
        if not media.startswith(("sound:", "recording:")):
            raise ValueError("Announcement media must use sound: or recording:")
        return self._request("POST", f"/bridges/{bridge_id}/play", params={"media": media})

    def stop_recording(self, name: str) -> None:
        try:
            self._request("POST", f"/recordings/live/{name}/stop")
        except AsteriskError:
            pass

    def get_stored_recording(self, name: str) -> dict[str, Any] | None:
        if not name or len(name) > 128 or any(char in name for char in "/\\\r\n"):
            return None
        try:
            result = self._request("GET", f"/recordings/stored/{name}")
            if not isinstance(result, dict):
                return None
            # StoredRecording exposes name/format, not a filesystem filename.
            # Keep the canonical Asterisk-side path as derived metadata for CRM/admin use.
            fmt = str(result.get("format") or "").strip()
            if fmt and name:
                result.setdefault("filename", str(Path(self.recording_path) / f"{name}.{fmt}"))
            return result
        except AsteriskError:
            return None

    def list_stored_recordings(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/recordings/stored")
        return result if isinstance(result, list) else []

    def delete_stored_recording(self, name: str) -> bool:
        if not name or len(name) > 128 or any(char in name for char in "/\\\r\n"):
            return False
        try:
            self._request("DELETE", f"/recordings/stored/{name}")
            return True
        except AsteriskError:
            return False

    def cleanup_old_recordings(self, retention_days: int) -> int:
        """Deprecated compatibility helper.

        ARI StoredRecording does not expose creation/completion timestamps, so age-based
        retention is enforced by TelephonyService from persisted call ended_at timestamps.
        """
        return 0

    def destroy_bridge(self, bridge_id: str) -> None:
        try:
            self._request("DELETE", f"/bridges/{bridge_id}")
        except AsteriskError:
            pass

    def hangup(self, channel_id: str) -> None:
        if not channel_id:
            return
        try:
            self._request("DELETE", f"/channels/{channel_id}")
        except AsteriskError:
            pass

    def hangup_call(self, employee_channel_id: str | None, customer_channel_id: str | None) -> None:
        for channel_id in (employee_channel_id, customer_channel_id):
            if channel_id:
                self.hangup(channel_id)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/asterisk/info")

    def event_url(self) -> str:
        scheme = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{scheme}/events?app={self.app}"

    def event_headers(self) -> list[str]:
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return [f"Authorization: Basic {token}"]


def _set_ready(path: str) -> None:
    ready = Path(path)
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.touch()


def _clear_ready(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def ari_event_loop(on_event, config: type[Config] = Config) -> None:
    client = AsteriskClient(config)
    ready_path = config.ARI_READY_PATH
    _clear_ready(ready_path)

    def run() -> None:
        backoff = 2
        while True:
            ws = None
            try:
                ws = websocket.create_connection(client.event_url(), timeout=5, header=client.event_headers())
                backoff = 2
                _set_ready(ready_path)
                while True:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        _set_ready(ready_path)
                        continue
                    if not raw:
                        break
                    _set_ready(ready_path)
                    on_event(json.loads(raw))
            except Exception:
                _clear_ready(ready_path)
                threading.Event().wait(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                _clear_ready(ready_path)

    thread = threading.Thread(target=run, name="asterisk-ari-events", daemon=True)
    thread.start()
