from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import jsonify, request, send_from_directory

from .config import Config


def require_token(fn: Callable):
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any):
        expected = Config.TELEPHONY_TOKEN
        supplied = request.headers.get("Authorization", "")
        if not supplied.startswith("Bearer ") or supplied[7:] != expected:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapped


def register_routes(app, service):
    def build_call(data):
        phone = str(data.get("phone", "")).strip()
        extension = str(data.get("extension") or Config.DEFAULT_EXTENSION).strip()
        if not phone:
            return None, (jsonify({"error": "phone is required"}), 400)
        if not extension.isdigit():
            return None, (jsonify({"error": "extension must be numeric"}), 400)
        try:
            call = service.start_outbound(
                phone=phone,
                extension=extension,
                contact_id=data.get("contact_id"),
                member_id=data.get("member_id"),
            )
        except Exception as exc:
            return None, (jsonify({"error": str(exc)}), 502)
        return call, None

    @app.get("/health")
    def health():
        try:
            info = service.asterisk.health()
            return jsonify({"ok": True, "asterisk": info.get("system", "reachable")})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503

    @app.get("/api/v1/calls")
    @require_token
    def list_calls():
        return jsonify({"calls": [call.to_dict() for call in service.store.all()]})

    @app.post("/api/v1/calls")
    @require_token
    def create_call():
        call, error = build_call(request.get_json(silent=True) or {})
        if error:
            return error
        return jsonify({"call": call.to_dict()}), 201

    @app.post("/api/v1/browser/call")
    @require_token
    def browser_call():
        call, error = build_call(request.get_json(silent=True) or {})
        if error:
            return error
        return jsonify({"call": call.to_dict()}), 201

    @app.get("/api/v1/calls/<call_id>")
    @require_token
    def get_call(call_id: str):
        call = service.store.get(call_id)
        if not call:
            return jsonify({"error": "call not found"}), 404
        return jsonify({"call": call.to_dict()})

    @app.post("/api/v1/calls/<call_id>/hangup")
    @require_token
    def hangup_call(call_id: str):
        call = service.hangup(call_id)
        if not call:
            return jsonify({"error": "call not found"}), 404
        return jsonify({"call": call.to_dict()})

    @app.post("/api/v1/calls/<call_id>/disposition")
    @require_token
    def disposition(call_id: str):
        data = request.get_json(silent=True) or {}
        call = service.store.update(
            call_id,
            disposition=str(data.get("disposition", "")).strip() or None,
            notes=str(data.get("notes", "")).strip() or None,
        )
        if not call:
            return jsonify({"error": "call not found"}), 404
        service.notify_crm("call.disposition", call)
        return jsonify({"call": call.to_dict()})

    @app.post("/api/v1/webhooks/ari")
    @require_token
    def ari_webhook():
        event = request.get_json(silent=True) or {}
        service.handle_ari_event(event)
        return jsonify({"ok": True})

    @app.get("/")
    def index():
        return send_from_directory("../web", "index.html")
