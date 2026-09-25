import re
import secrets
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Response, g, jsonify, request, send_file, send_from_directory, stream_with_context

from .config import Config

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
CALL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_request_times: dict[str, deque[float]] = defaultdict(deque)
WINDOW_SECONDS = 60
MAX_REQUESTS_PER_WINDOW = 120
MAX_OUTBOUND_CALLS_PER_WINDOW = 30


def _client_key(scope: str = "api") -> str:
    # Do not trust a caller-supplied X-Forwarded-For header for security controls.
    return f"{scope}:{request.remote_addr or 'unknown'}"


def _rate_limited(scope: str = "api", limit: int = MAX_REQUESTS_PER_WINDOW) -> bool:
    now = time.monotonic()
    bucket = _request_times[_client_key(scope)]
    while bucket and now - bucket[0] >= WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


def require_token(scope_or_fn=None):
    """Authenticate a scoped database API key or the legacy environment master token."""
    required_scope = scope_or_fn if isinstance(scope_or_fn, str) else None

    def decorator(fn: Callable):
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any):
            if _rate_limited():
                return jsonify({"error": "rate limit exceeded"}), 429
            from flask import current_app
            service = current_app.extensions.get("telephony_service")
            config = getattr(service, "config", Config)
            supplied = request.headers.get("Authorization", "")
            token = supplied[7:] if supplied.startswith("Bearer ") else ""
            if not token:
                return jsonify({"error": "unauthorized"}), 401
            if config.TELEPHONY_TOKEN and secrets.compare_digest(token, config.TELEPHONY_TOKEN):
                g.api_client = "legacy-master"
                g.api_owner_id = None
                return fn(*args, **kwargs)
            identity = service.settings_store.authenticate_api_key(token, required_scope) if service.settings_store else None
            if identity is False:
                return jsonify({"error": "insufficient API key scope"}), 403
            if not identity:
                return jsonify({"error": "unauthorized"}), 401
            g.api_client = f"key:{identity['id']}"
            g.api_owner_id = identity.get("owner_user_id")
            return fn(*args, **kwargs)
        return wrapped

    return decorator(scope_or_fn) if callable(scope_or_fn) else decorator


def _configured_default_extension(service) -> str:
    if service.settings_store:
        configured = str(service.settings_store.get_settings().get("default_extension", "")).strip()
        if configured.isdigit() and 100 <= int(configured) <= 999:
            if any(row["extension"] == configured and row["active"] for row in service.settings_store.list_extensions()):
                return configured
    return service.config.DEFAULT_EXTENSION


def register_routes(app, service):
    def api_owner_id():
        return getattr(g, "api_owner_id", None)

    def api_extensions():
        return service.settings_store.list_extensions(api_owner_id()) if service.settings_store and api_owner_id() is not None else (service.settings_store.list_extensions() if service.settings_store else [])

    def call_allowed(call):
        owner = api_owner_id()
        return bool(call) and (owner is None or call.extension in {row["extension"] for row in api_extensions()})

    def build_call(data):
        if not isinstance(data, dict):
            return None, (jsonify({"error": "JSON object required"}), 400)
        if not service.ari_ready():
            return None, (jsonify({"error": "telephony event service is not ready"}), 503)
        phone = str(data.get("phone", "")).strip()
        # The customer's own default outbound extension comes first, then the
        # platform's, then whichever active extension they own.
        if api_owner_id() is not None:
            owned_default = ""
            if service.settings_store:
                owned_default = service.settings_store.call_default_for("outbound", api_owner_id())
            owned_default = owned_default or next((row["extension"] for row in api_extensions() if row["active"]), "")
        else:
            owned_default = _configured_default_extension(service)
        extension = str(data.get("extension") or owned_default).strip()
        provider = str(data.get("provider") or "").strip() or None
        if not phone:
            return None, (jsonify({"error": "phone is required"}), 400)
        if not E164_RE.fullmatch(phone):
            return None, (jsonify({"error": "phone must be a valid E.164 number"}), 400)
        if not extension.isdigit() or not 100 <= int(extension) <= 999:
            return None, (jsonify({"error": "extension must be a 3-digit number"}), 400)
        configured = api_extensions()
        if not any(row["extension"] == extension and row["active"] for row in configured):
            return None, (jsonify({"error": "extension is not configured"}), 400)
        try:
            call = service.start_outbound(
                phone=phone,
                extension=extension,
                contact_id=data.get("contact_id"),
                member_id=data.get("member_id"),
                provider=provider,
                caller_id_number=str(data.get("caller_id_number") or "").strip() or None,
            )
        except RuntimeError as exc:
            message = str(exc)
            if message.startswith(("No active callback number", "The selected callback number", "No active SIP provider")):
                return None, (jsonify({"error": message}), 400)
            app.logger.exception("Asterisk failed to start outbound call")
            return None, (jsonify({"error": "telephony service unavailable"}), 502)
        except Exception:
            app.logger.exception("Asterisk failed to start outbound call")
            return None, (jsonify({"error": "telephony service unavailable"}), 502)
        return call, None

    @app.get("/health")
    def health():
        try:
            service.asterisk.health()
            ready = service.ari_ready()
            return jsonify({"ok": ready, "asterisk": "reachable", "ari_ready": ready}), (200 if ready else 503)
        except Exception:
            app.logger.exception("Asterisk health check failed")
            return jsonify({"ok": False, "error": "telephony service unavailable", "ari_ready": service.ari_ready()}), 503

    @app.get("/api/v1/extensions")
    @require_token("config:read")
    def list_extensions():
        if service.settings_store:
            rows = api_extensions()
            active = [row["extension"] for row in rows if row["active"]]
            if api_owner_id() is not None:
                chosen = service.settings_store.call_default_for("outbound", api_owner_id()) if service.settings_store else ""
                return jsonify({"extensions": active, "default_extension": chosen or (active[0] if active else "")})
            return jsonify({"extensions": active, "default_extension": _configured_default_extension(service)})
        return jsonify({"extensions": list(service.config.ASTERISK_EXTENSIONS), "default_extension": service.config.DEFAULT_EXTENSION})

    @app.get("/api/v1/numbers")
    @require_token("config:read")
    def list_numbers():
        extension = request.args.get("extension", "").strip()
        rows = service.settings_store.list_numbers(api_owner_id()) if service.settings_store and api_owner_id() is not None else (service.settings_store.list_numbers() if service.settings_store else [])
        if extension:
            rows = [row for row in rows if row["inbound_extension"] == extension]
        if api_owner_id() is not None:
            for row in rows:
                row.pop("provider", None)
        return jsonify({"numbers": rows})

    @app.get("/api/v1/providers")
    @require_token("config:read")
    def list_providers():
        rows = [] if api_owner_id() is not None else (service.settings_store.list_providers() if service.settings_store else [])
        return jsonify({"providers": rows})

    @app.get("/api/v1/calls")
    @require_token("calls:read")
    def list_calls():
        try:
            limit = min(100, max(1, int(request.args.get("limit", "50"))))
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            return jsonify({"error": "invalid pagination"}), 400
        extension = request.args.get("extension", "").strip() or None
        status = request.args.get("status", "").strip() or None
        query = request.args.get("q", "").strip()[:100] or None
        owned = [row["extension"] for row in api_extensions()] if api_owner_id() is not None else None
        if extension and owned is not None and extension not in owned:
            return jsonify({"error": "extension not found"}), 404
        calls, total = service.store.search(extension=extension, extensions=owned if not extension else None, status=status, query=query, limit=limit, offset=offset)
        return jsonify({"calls": [call.to_dict() for call in calls], "total": total, "limit": limit, "offset": offset})

    @app.post("/api/v1/calls")
    @require_token("calls:write")
    def create_call():
        if _rate_limited("outbound", MAX_OUTBOUND_CALLS_PER_WINDOW):
            return jsonify({"error": "outbound call rate limit exceeded"}), 429
        request_key = request.headers.get("Idempotency-Key", "").strip()
        client = getattr(g, "api_client", "unknown")
        if request_key and (len(request_key) < 8 or len(request_key) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", request_key)):
            return jsonify({"error": "invalid Idempotency-Key"}), 400
        if request_key:
            claimed, prior_call_id = service.settings_store.claim_idempotency(client, request_key)
            if not claimed:
                prior = service.store.get(prior_call_id) if prior_call_id else None
                if prior:
                    return jsonify({"call": prior.to_dict(), "idempotent_replay": True}), 200
                return jsonify({"error": "an identical call request is already in progress"}), 409
        call, error = build_call(request.get_json(silent=True))
        if error:
            if request_key:
                service.settings_store.finish_idempotency(client, request_key, None)
            return error
        if request_key:
            service.settings_store.finish_idempotency(client, request_key, call.call_id)
        return jsonify({"call": call.to_dict(), "idempotent_replay": False}), 201

    @app.post("/api/v1/browser/call")
    @require_token("calls:write")
    def browser_call():
        if not service.config.ENABLE_BROWSER_API:
            return jsonify({"error": "browser call API is disabled"}), 404
        if _rate_limited("outbound", MAX_OUTBOUND_CALLS_PER_WINDOW):
            return jsonify({"error": "outbound call rate limit exceeded"}), 429
        call, error = build_call(request.get_json(silent=True))
        if error:
            return error
        return jsonify({"call": call.to_dict()}), 201

    @app.get("/api/v1/calls/<call_id>")
    @require_token("calls:read")
    def get_call(call_id: str):
        if not CALL_ID_RE.fullmatch(call_id):
            return jsonify({"error": "call not found"}), 404
        call = service.store.get(call_id)
        if not call_allowed(call):
            return jsonify({"error": "call not found"}), 404
        return jsonify({"call": call.to_dict()})

    @app.post("/api/v1/calls/<call_id>/hangup")
    @require_token("calls:write")
    def hangup_call(call_id: str):
        if not CALL_ID_RE.fullmatch(call_id):
            return jsonify({"error": "call not found"}), 404
        existing = service.store.get(call_id)
        if not call_allowed(existing):
            return jsonify({"error": "call not found"}), 404
        call = service.hangup(call_id)
        return jsonify({"call": call.to_dict()})

    @app.get("/api/v1/recordings")
    @require_token("recordings:read")
    def list_recordings():
        calls = [call.to_dict() for call in service.store.all() if call.recording_name and call_allowed(call)]
        return jsonify({"recordings": calls})

    @app.get("/api/v1/recordings/<call_id>/file")
    @require_token("recordings:read")
    def recording_file(call_id: str):
        if not CALL_ID_RE.fullmatch(call_id):
            return jsonify({"error": "recording not found"}), 404
        call = service.store.get(call_id)
        if not call_allowed(call) or not call.recording_name or call.recording_status not in {"finalized", "available"}:
            return jsonify({"error": "recording not found"}), 404
        upstream = service.asterisk.open_stored_recording(call.recording_name, request.headers.get("Range"))
        if upstream is None:
            return jsonify({"error": "recording not found"}), 404

        def body():
            try:
                yield from upstream.iter_content(64 * 1024)
            finally:
                upstream.close()

        headers = {"Content-Disposition": f'inline; filename="{call.recording_name}.{call.recording_format or "wav"}"'}
        for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if upstream.headers.get(header):
                headers[header] = upstream.headers[header]
        return Response(
            stream_with_context(body()), status=upstream.status_code,
            content_type=upstream.headers.get("Content-Type", "application/octet-stream"), headers=headers,
            direct_passthrough=True,
        )

    def api_mailbox_allowed(mailbox: str) -> bool:
        return api_owner_id() is None or mailbox in {row["extension"] for row in api_extensions()}

    @app.get("/api/v1/voicemail/mailboxes")
    @require_token("voicemail:read")
    def list_voicemail_mailboxes():
        messages = app.extensions["voicemail_store"].list_messages()
        if api_owner_id() is not None:
            messages = [message for message in messages if api_mailbox_allowed(message["mailbox"])]
        counts: dict[str, dict[str, int]] = {}
        for message in messages:
            mailbox_counts = counts.setdefault(message["mailbox"], {"new": 0, "old": 0, "urgent": 0, "total": 0})
            mailbox_counts["total"] += 1
            mailbox_counts["new" if message["folder"] == "inbox" else message["folder"]] += 1
        mailboxes = []
        for extension in api_extensions():
            if not extension.get("voicemail_enabled"):
                continue
            mailboxes.append({
                "extension": extension["extension"], "display_name": extension["display_name"],
                "active": bool(extension["active"]), "counts": counts.get(extension["extension"], {"new": 0, "old": 0, "urgent": 0, "total": 0}),
            })
        return jsonify({"mailboxes": mailboxes})

    @app.get("/api/v1/voicemails")
    @require_token("voicemail:read")
    def list_voicemails():
        mailbox = request.args.get("extension", "").strip() or None
        folder = request.args.get("folder", "").strip() or None
        if mailbox and not any(row["extension"] == mailbox and row.get("voicemail_enabled") for row in api_extensions()):
            return jsonify({"error": "voicemail mailbox not found"}), 404
        try:
            messages = app.extensions["voicemail_store"].list_messages(mailbox, folder)
            if api_owner_id() is not None:
                messages = [message for message in messages if api_mailbox_allowed(message["mailbox"])]
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"voicemails": messages, "total": len(messages)})

    @app.get("/api/v1/voicemails/<mailbox>/<folder>/<message>/file")
    @require_token("voicemail:read")
    def voicemail_file(mailbox: str, folder: str, message: str):
        if not api_mailbox_allowed(mailbox):
            return jsonify({"error": "voicemail not found"}), 404
        path = app.extensions["voicemail_store"].audio_path(mailbox, folder, message)
        if not path:
            return jsonify({"error": "voicemail not found"}), 404
        return send_file(path, conditional=True, as_attachment=False, download_name=f"voicemail-{mailbox}-{message}{path.suffix}")

    @app.post("/api/v1/voicemails/<mailbox>/<folder>/<message>/read")
    @require_token("voicemail:write")
    def voicemail_read(mailbox: str, folder: str, message: str):
        if not api_mailbox_allowed(mailbox):
            return jsonify({"error": "new voicemail not found"}), 404
        if not app.extensions["voicemail_store"].mark_read(mailbox, folder, message):
            return jsonify({"error": "new voicemail not found"}), 404
        return jsonify({"ok": True})

    @app.delete("/api/v1/voicemails/<mailbox>/<folder>/<message>")
    @require_token("voicemail:write")
    def voicemail_delete(mailbox: str, folder: str, message: str):
        if not api_mailbox_allowed(mailbox):
            return jsonify({"error": "voicemail not found"}), 404
        if not app.extensions["voicemail_store"].delete(mailbox, folder, message):
            return jsonify({"error": "voicemail not found"}), 404
        return jsonify({"ok": True})

    @app.get("/api/v1/webhooks")
    @require_token("webhooks:manage")
    def list_webhooks():
        return jsonify({"webhooks": service.settings_store.list_webhooks(owner_user_id=api_owner_id())})

    @app.post("/api/v1/webhooks")
    @require_token("webhooks:manage")
    def save_webhook():
        try:
            webhook_id = service.settings_store.save_webhook(request.get_json(silent=True) or {}, api_owner_id())
            return jsonify({"ok": True, "webhook_id": webhook_id}), 201
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/v1/webhooks/<int:webhook_id>")
    @require_token("webhooks:manage")
    def delete_webhook(webhook_id: int):
        try:
            service.settings_store.delete_webhook(webhook_id, api_owner_id())
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/api/v1/webhooks/<int:webhook_id>/test")
    @require_token("webhooks:manage")
    def test_webhook(webhook_id: int):
        result = service.test_webhook(webhook_id)
        status = 200 if result.get("ok") else (404 if result.get("error") == "webhook not found" else 502)
        return jsonify(result), status

    @app.post("/api/v1/calls/<call_id>/disposition")
    @require_token("calls:write")
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
    @require_token("*")
    def ari_webhook():
        if not service.config.ENABLE_ARI_WEBHOOK:
            return jsonify({"error": "not found"}), 404
        event = request.get_json(silent=True) or {}
        if not isinstance(event, dict):
            return jsonify({"error": "JSON object required"}), 400
        service.handle_ari_event(event)
        return jsonify({"ok": True})

    @app.get("/")
    def index():
        if not service.config.ENABLE_DIAGNOSTIC_UI:
            return jsonify({"error": "not found"}), 404
        return send_from_directory(str(Path(app.root_path).parent / "web"), "index.html")
