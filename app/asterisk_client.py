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


def endpoint_name(extension: Any) -> str:
    """The channel a leg rings when the caller did not name the endpoint.

    The same identity the renderer writes: digits plus the line they belong to
    (`PJSIP/101-13025550001`). A stored key holds an `@`, which a PJSIP section
    name cannot - and the digits alone are never dialled, because every line
    carries its own 101. The call engine always passes an endpoint from
    `dial_endpoint()`; this is the safe floor under it.
    """
    text = str(extension or "").strip()
    digits, _, scope = text.partition("@")
    scope_digits = "".join(character for character in scope if character.isdigit())
    if digits.isdigit() and scope_digits:
        return f"PJSIP/{digits}-{scope_digits}"
    return f"PJSIP/{digits or text}"


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

    def list_endpoints(self) -> list[dict[str, Any]]:
        """Return ARI endpoint/device state for live registration indicators."""
        return self._request("GET", "/endpoints") or []

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
        endpoint: str | None = None,
    ) -> str:
        """Ring the employee first; the customer leg is created after answer.

        `endpoint` overrides the channel to ring: media is negotiated by the
        endpoint a call is placed towards, so an extension that answers in the
        browser is rung on its WebRTC endpoint instead of its plain one.
        """
        employee_channel = f"{call_id}-employee"
        self._request(
            "POST",
            "/channels",
            params={
                "endpoint": endpoint or endpoint_name(extension),
                "app": self.app,
                "appArgs": f"employee,{call_id},{provider_endpoint},{phone}",
                "channelId": employee_channel,
                "timeout": 30,
            },
            json={"variables": self._variables(call_id, metadata)},
        )
        return call_id

    def create_inbound_employee_leg(
        self, call_id: str, extension: str, customer_channel_id: str, index: int = 0, endpoint: str | None = None,
    ) -> str:
        """Ring one device for an inbound call.

        A main line rings several devices at once, so each leg gets its own
        channel id: the first keeps the historical name and the rest are numbered.
        `endpoint` overrides the channel to ring, the same way it does for an
        outbound call - a device signed in on the browser answers on the WebRTC
        endpoint only.
        """
        employee_channel = f"{call_id}-employee" if int(index) <= 0 else f"{call_id}-employee-{int(index)}"
        self._request(
            "POST", "/channels",
            params={
                "endpoint": endpoint or endpoint_name(extension), "app": self.app,
                "appArgs": f"inbound_employee,{call_id}", "channelId": employee_channel,
                "originator": customer_channel_id, "timeout": 30,
            },
            json={"variables": self._variables(call_id)},
        )
        return employee_channel

    def _customer_leg(self, call_id: str, endpoint: str, employee_channel_id: str, caller_id_number: str | None, timeout: int) -> str:
        customer_channel = f"{call_id}-customer"
        self._request(
            "POST",
            "/channels",
            params={
                "endpoint": endpoint,
                "app": self.app,
                "appArgs": f"customer,{call_id}",
                "channelId": customer_channel,
                "originator": employee_channel_id,
                "callerId": caller_id_number or "",
                "timeout": timeout,
            },
        )
        return customer_channel

    def create_customer_leg(
        self, call_id: str, phone: str, provider_endpoint: str, employee_channel_id: str,
        caller_id_number: str | None = None,
    ) -> str:
        return self._customer_leg(
            call_id, f"PJSIP/{phone}@{provider_endpoint}", employee_channel_id, caller_id_number, 60
        )

    def create_local_leg(
        self, call_id: str, extension: str, employee_channel_id: str, caller_id_number: str | None = None,
        endpoint: str | None = None,
    ) -> str:
        """Ring an extension on this platform instead of a carrier number.

        Used when the number being called is one of the customer's own: the
        lookup already resolved it to the extension it is set to ring, so the
        call is an internal one and never leaves through the trunk. `endpoint`
        overrides the channel to ring, the same way it does for an outbound call
        - an extension that belongs to a line is reached on the PJSIP endpoint
        named after its identity, not by the key that holds `@`.
        """
        return self._customer_leg(call_id, endpoint or endpoint_name(extension), employee_channel_id, caller_id_number, 30)

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

    def open_stored_recording(self, name: str, byte_range: str | None = None):
        """Open a stored recording through private ARI without mounting its volume."""
        if not name or len(name) > 128 or any(char in name for char in "/\\\r\n"):
            return None
        headers = {"Range": byte_range} if byte_range and byte_range.startswith("bytes=") else {}
        response = requests.get(
            f"{self.base_url}/recordings/stored/{name}/file",
            auth=(self.user, self.password), headers=headers, timeout=30, stream=True,
        )
        if response.status_code not in {200, 206}:
            response.close()
            return None
        return response

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

    def answer_channel(self, channel_id: str) -> None:
        """Take the call off ringing before a prompt is played to it."""
        self._request("POST", f"/channels/{channel_id}/answer")

    def play_channel_media(self, channel_id: str, media: str, playback_id: str | None = None) -> dict[str, Any]:
        """Play a sound to one channel. `media` is an ARI media URI, e.g.
        `sound:custom/ivr-welcome` for a prompt or `sound:tts/...` where the
        deployment synthesises speech."""
        params: dict[str, Any] = {"media": media}
        if playback_id:
            params["playbackId"] = playback_id
        return self._request("POST", f"/channels/{channel_id}/play", params=params)

    def stop_playback(self, playback_id: str) -> None:
        """Cut a prompt short - the caller has already answered it with digits."""
        try:
            self._request("DELETE", f"/playbacks/{playback_id}")
        except Exception:
            # The playback may already have finished, which is not an error.
            return

    def continue_in_dialplan(self, channel_id: str, context: str, extension: str) -> None:
        self._request("POST", f"/channels/{channel_id}/continue", params={"context": context, "extension": extension, "priority": 1})

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
