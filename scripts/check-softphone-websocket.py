#!/usr/bin/env python3
"""Verify a SIP WebSocket upgrade, without SIP credentials or placing a call.

Run once on the VM against ws://127.0.0.1:8088/ws, then against the public
wss://WEB_HOST/ws. The public check verifies TLS normally; no insecure fallback.
Only a valid RFC 6455 HTTP 101 response selecting the SIP subprotocol passes.
"""
import argparse
import base64
import hashlib
import hmac
import http.client
import math
import os
import ssl
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def check_websocket(url, *, timeout=5, origin=None):
    connection = None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("use ws://HOST/ws or wss://HOST/ws")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("do not put credentials, query strings or fragments in the WebSocket URL")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        if parsed.scheme == "wss":
            connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout, context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        connection.request("GET", parsed.path or "/ws", headers={
            "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Protocol": "sip",
            "Origin": origin or f"{'https' if parsed.scheme == 'wss' else 'http'}://{parsed.netloc}",
        })
        response = connection.getresponse()
        if response.status != 101:
            explanation = {
                404: "No working WebSocket handler at this path. Check the active nginx vhost and Asterisk's HTTP URI/modules; 404 alone does not identify which hop failed.",
                400: "Upgrade rejected. Check forwarding of Upgrade, Connection and Sec-WebSocket-Protocol: sip, then inspect Asterisk's logs.",
                426: "Upgrade required/rejected. Check the WebSocket headers and SIP subprotocol reach Asterisk.",
                502: "The gateway could not reach its upstream Asterisk listener.",
                503: "A gateway or upstream service is unavailable.",
                504: "The gateway timed out reaching its upstream.",
            }.get(response.status, "Expected HTTP 101 Switching Protocols. Check the active proxy route and upstream logs.")
            return False, f"HTTP {response.status}: {explanation}"
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if not hmac.compare_digest(response.getheader("Sec-WebSocket-Accept", ""), expected):
            return False, "HTTP 101 with invalid Sec-WebSocket-Accept (not a valid WebSocket handshake)."
        if response.getheader("Upgrade", "").lower().strip() != "websocket":
            return False, "HTTP 101 without Upgrade: websocket."
        tokens = {token.strip().lower() for token in response.getheader("Connection", "").split(",")}
        if "upgrade" not in tokens:
            return False, "HTTP 101 without Connection: Upgrade."
        if response.getheader("Sec-WebSocket-Protocol", "").strip() != "sip":
            return False, "HTTP 101 without the selected SIP subprotocol. The browser's SIP transport cannot use it."
        return True, "HTTP 101, valid WebSocket accept and SIP subprotocol. Transport upgrade works; SIP registration and call media are separate checks."
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="wss://WEB_HOST/ws, or ws://127.0.0.1:8088/ws on the VM")
    parser.add_argument("--timeout", type=float, default=5, help="network timeout in seconds (default: 5)")
    parser.add_argument("--origin", help="optional browser origin, e.g. https://tel.engineerip.com")
    args = parser.parse_args(argv)
    ok, message = check_websocket(args.url, timeout=args.timeout, origin=args.origin)
    print(f"{'PASS' if ok else 'FAIL'}: {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
