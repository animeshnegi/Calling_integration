from __future__ import annotations

import json
import threading
import uuid
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

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            auth=(self.user, self.password),
            timeout=10,
            **kwargs,
        )
        if not response.ok:
            raise AsteriskError(f"Asterisk API {response.status_code}: {response.text[:500]}")
        if not response.content:
            return None
        return response.json()

    def create_outbound_call(self, extension: str, phone: str, metadata: dict[str, Any] | None = None) -> str:
        call_id = str(uuid.uuid4())
        variables = {"EIP_CALL_ID": call_id}
        if metadata:
            variables["EIP_CONTACT_ID"] = str(metadata.get("contact_id", ""))
            variables["EIP_MEMBER_ID"] = str(metadata.get("member_id", ""))
        # Dialplan/Local channel is the portable hand-off point; the application
        # records the UUID so provider channel IDs can later be correlated.
        self._request(
            "POST",
            "/channels",
            params={
                "endpoint": f"Local/{phone}@web-outbound/n",
                "app": self.app,
                "appArgs": f"{extension},{phone}",
                "channelId": call_id,
                "variables": json.dumps(variables),
            },
        )
        return call_id

    def hangup(self, channel_id: str) -> None:
        self._request("DELETE", f"/channels/{channel_id}")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/asterisk/info")

    def event_url(self) -> str:
        scheme = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{scheme}/events?api_key={self.user}:{self.password}&app={self.app}"


def ari_event_loop(on_event, config: type[Config] = Config) -> threading.Thread:
    client = AsteriskClient(config)

    def run() -> None:
        backoff = 2
        while True:
            ws = None
            try:
                ws = websocket.create_connection(client.event_url(), timeout=30)
                backoff = 2
                while True:
                    raw = ws.recv()
                    if not raw:
                        break
                    on_event(json.loads(raw))
            except Exception:
                threading.Event().wait(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

    thread = threading.Thread(target=run, name="asterisk-ari-events", daemon=True)
    thread.start()
    return thread
