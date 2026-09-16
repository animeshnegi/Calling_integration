from __future__ import annotations

import re
import secrets
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Callable

from flask import jsonify, request, send_from_directory

from .config import Config

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
CALL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_request_times: dict[str, deque[float]] = defaultdict(deque)
WINDOW_SECONDS = 60
MAX_REQUESTS_PER_WINDOW = 120
MAX_OUTBOUND_CALLS_PER_WINDOW = 30


def _client_key(scope: str = "api") -> str:
    address = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",", 1)[0].strip()
    return f"{scope}:{address}"


def _rate_limited(scope: str = "api", limit: int = MAX_REQUESTS_PER_WINDOW) -> bool:
    now = time.monotonic()
    bucket = _request_times[_client_key(scope)]
    while bucket and now - bucket[0] >= WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


def require_token(fn: Callable):
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any):
        if _rate_limited():
            return jsonify({"error": "rate limit exceeded"}), 429
        expected = Config.TELEPHONY_TOKEN
        supplied = request.headers.get("Authorization", "")
        prefix = "Bearer "
        token = supplied[len(prefix):] if supplied.startswith(prefix) else ""
        if not expected or not token or not secrets.compare_digest(token, expected):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapped


def register_routes(app, service):
    def build_call(data):
        if not isinstance(data, dict):
            return None, (jsonify({"error": "JSON object required"}), 400)
        phone = str(data.get("phone", "")).strip()
        extension = str(data.get("extension") or Config.DEFAULT_EXTENSION).strip()
        if not phone:
            return None, (jsonify({"error": "phone is required"}), 400)
        if not E164_RE.fullmatch(phone):
            return None, (jsonify({"error": "phone must be a valid E.164 number"}), 400)
        if not extension.isdigit() or not 100 <= int(extension) <= 999:
            return None, (jsonify({"error": "extension must be a 3-digit number"}), 400)
        if not Config.is_extension_configured(extension):
            return None, (jsonify({"error": "extension is not configured"}), 400)
        try:
            call = service.start_outbound(
                phone=phone,
                extension=extension,
                contact_id=data.get("contact_id"),
                member_id=data.get("member_id"),
            )
        except Exception:
            app.logger.exception("Asterisk failed to start outbound call")
            return None, (jsonify({"error": "telephony service unavailable"}), 502)
        return call, None

    @app.get("/health")
    def health():
        try:
            info = service.asterisk.health()
            return jsonify({"ok": True, "asterisk": info.get("system", "reachable")})
        except Exception:
            app.logger.exception("Asterisk health check failed")
            return jsonify({"ok": False, "error": "telephony service unavailable"}), 503

    @app.get("/api/v1/extensions")
    @require_token
    def list_extensions():
        return jsonify({"extensions": list(Config.ASTERISK_EXTENSIONS), "default_extension": Config.DEFAULT_EXTENSION})

    @app.get("/api/v1/calls")
    @require_token
    def list_calls():
        return jsonify({"calls": [call.to_dict() for call in service.store.all()]})

    @app.post("/api/v1/calls")
    @require_token
    def create_call():
        if _rate_limited("outbound", MAX_OUTBOUND_CALLS_PER_WINDOW):
            return jsonify({"error": "outbound call rate limit exceeded"}), 429
        call, error = build_call(request.get_json(silent=True))
        if error:
            return error
        return jsonify({"call": call.to_dict()}), 201

    @app.post("/api/v1/browser/call")
    @require_token
    def browser_call():
        if not Config.ENABLE_BROWSER_API:
            return jsonify({"error": "browser call API is disabled"}), 404
        if _rate_limited("outbound", MAX_OUTBOUND_CALLS_PER_WINDOW):
            return jsonify({"error": "outbound call rate limit exceeded"}), 429
        call, error = build_call(request.get_json(silent=True))
        if error:
            return error
        return jsonify({"call": call.to_dict()}), 201

    @app.get("/api/v1/calls/<call_id>")
    @require_token
    def get_call(call_id: str):
        if not CALL_ID_RE.fullmatch(call_id):
            return jsonify({"error": "call not found"}), 404
        call = service.store.get(call_id)
        if not call:
            return jsonify({"error": "call not found"}), 404
        return jsonify({"call": call.to_dict()})

    @app.post("/api/v1/calls/<call_id>/hangup")
    @require_token
    def hangup_call(call_id: str):
        if not CALL_ID_RE.fullmatch(call_id):
            return jsonify({"error": "call not found"}), 404
        call = service.hangup(call_id)
        if not call:
            return jsonify({"error": "call not found"}), 404
        return jsonify({"call": call.to_dict()})

    @app.post("/api/v1/calls/<call_id>/disposition")
    @require_token
    def disposition(call_id: str):
        if not CALL_ID_RE.fullmatch(call_id):
            return jsonify({"error": "call not found"}), 404
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "JSON object required"}), 400
        disposition = str(data.get("disposition", "")).strip()
        notes = str(data.get("notes", "")).strip()
        if len(disposition) > 100 or len(notes) > 2000:
            return jsonify({"error": "disposition or notes too long"}), 400
        call = service.store.update(call_id, disposition=disposition or None, notes=notes or None)
        if not call:
            return jsonify({"error": "call not found"}), 404
        service.notify_crm("call.disposition", call)
        return jsonify({"call": call.to_dict()})

    @app.post("/api/v1/webhooks/ari")
    @require_token
    def ari_webhook():
        event = request.get_json(silent=True) or {}
        if not isinstance(event, dict):
            return jsonify({"error": "JSON object required"}), 400
        service.handle_ari_event(event)
        return jsonify({"ok": True})

    @app.get("/")
    def index():
        if not Config.ENABLE_DIAGNOSTIC_UI:
            return jsonify({"error": "not found"}), 404
        return send_from_directory("../web", "index.html")
