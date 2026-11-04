"""Admin and customer console HTTP routes and session handling."""
from __future__ import annotations

import csv
import io
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import Response, current_app, jsonify, redirect, request, session, send_file, send_from_directory, stream_with_context

from ..voicemail import mailbox_name

from ..models.settings import extension_digits, extension_scope, extension_key, extension_mailbox, device_registration, extension_registration, same_account, SettingsStore, DB_INTEGRITY_ERRORS, INPUT_DB_ERRORS

_LOGIN_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW = 15 * 60
_LOGIN_LIMIT = 8


def register_admin(app, config, on_telephony_change=None):
    store = SettingsStore(config.DATABASE_URI or config.SETTINGS_DB_PATH, config.SECRET_KEY)

    def flow_owner(data: dict, target_type: str, target: str, phone_number: str = "") -> int:
        """Whose call flow a request is about.

        A customer session is always that customer. An administrator session
        names the customer through the flow's own target - the number, extension
        or group decides - and falls back to the customer selected in the
        workspace, so a flow can never be written across customers."""
        if session.get("admin_role") != "admin":
            return int(session["admin_user_id"])
        if target_type == "number":
            number = str(phone_number or target or "").strip()
            owner = next((row["owner_user_id"] for row in store.list_numbers() if row["number"] == number), None)
        elif target_type == "extension":
            # The console speaks the digits a customer reads; the row is stored
            # under its key. A bare `901` still names the row that has no number of
            # its own.
            wanted = str(target)
            owner = next((row["owner_user_id"] for row in store.list_extensions() if str(row["key"]) == wanted), None)
            if owner is None and wanted.isdigit():
                # The digits name one extension while one of the account's rows
                # carries them; where several do, the caller has to name the
                # phone number as well.
                key = store.extension_key_for_digits(wanted)
                owner = next((row["owner_user_id"] for row in store.list_extensions() if str(row["key"]) == key), None)
        else:
            group = store.get_group(target)
            owner = group["owner_user_id"] if group else None
        if owner:
            return int(owner)
        chosen = data.get("owner_user_id")
        if str(chosen or "").strip().isdigit():
            return int(chosen)
        raise ValueError("Choose the customer this call flow belongs to")

    store.ensure_bootstrap_admin(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
    store.bootstrap_telephony(config)
    app.extensions["settings_store"] = store
    # HTML lives in app/templates; CSS/JS/images in app/static.
    template_dir = str(Path(app.root_path) / "templates")
    static_dir = Path(app.root_path) / "static"

    @app.after_request
    def no_stale_admin_ui_after_image_upgrade(response):
        # An old cached admin.js can still reference removed API-key fields and
        # produce misleading save errors after an image-only deployment. Admin
        # pages, assets and API state must be fetched from the running image.
        if (request.path.startswith("/admin") or request.path in {"/login", "/console-check"}):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def apply_change() -> str:
        """Re-render the Asterisk configuration; return why it did not apply.

        A change is saved even when the render fails - a platform that cannot
        reach Asterisk must not refuse a customer - but the failure is never
        silent: it is logged, and the route that knows the caller can pass the
        reason back to the console.
        """
        if on_telephony_change:
            try:
                on_telephony_change()
            except Exception as exc:
                app.logger.exception("Failed to apply telephony configuration change")
                return str(exc) or exc.__class__.__name__
        return ""

    def login_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("admin_user_id"):
                if request.path.startswith("/admin/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect("/login")
            user = store.get_user(int(session["admin_user_id"]))
            if not user or not user["active"]:
                session.clear()
                if request.path.startswith("/admin/api/"):
                    return jsonify({"error": "account disabled"}), 401
                return redirect("/login")
            session["admin_username"], session["admin_email"] = user["username"], user["email"]
            session["admin_extension"], session["admin_role"] = user["extension"], user["role"]
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                token = request.headers.get("X-CSRF-Token", "")
                expected = session.get("csrf_token", "")
                if not expected or not token or not secrets.compare_digest(token, expected):
                    return jsonify({"error": "csrf validation failed"}), 403
            return fn(*args, **kwargs)
        return wrapped

    def admin_required(fn):
        @login_required
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if session.get("admin_role") != "admin":
                return jsonify({"error": "administrator permission required"}), 403
            return fn(*args, **kwargs)
        return wrapped

    def login_rate_limited():
        username = str((request.get_json(silent=True) or request.form).get("username", "")).strip().lower()[:80]
        now = time.monotonic()
        keys = [f"ip:{request.remote_addr or 'unknown'}", f"user:{username}"]
        limited = False
        for key in keys:
            bucket = _LOGIN_BUCKETS[key]
            while bucket and now - bucket[0] >= _LOGIN_WINDOW:
                bucket.popleft()
            if len(bucket) >= _LOGIN_LIMIT:
                limited = True
            else:
                bucket.append(now)
        return limited

    @app.get("/admin-assets/<path:filename>")
    def admin_asset(filename):
        """Legacy flat-layout URLs -> the organised static/ tree.

        Old bookmarks and cached pages ask for /admin-assets/admin.css and
        /admin-assets/admin.js. Both still resolve, from their new homes.
        """
        name = Path(filename).name
        if name.endswith(".css"):
            folder, target = static_dir / "css", "tailwind.css"
        elif name.endswith(".js"):
            folder, target = static_dir / "js", name
        elif name.endswith((".svg", ".png", ".ico", ".webp", ".jpg")):
            folder, target = static_dir / "images", name
        else:
            folder, target = static_dir, name
        if not (folder / target).is_file():
            return jsonify({"error": "not found"}), 404
        return send_from_directory(str(folder), target, max_age=0)

    @app.get("/")
    def landing_page(): return send_from_directory(template_dir, "index.html")

    @app.post("/signup")
    def public_signup():
        if login_rate_limited():
            return jsonify({"error": "too many signup attempts; try again later"}), 429
        try:
            data = request.get_json(silent=True) or {}
            required = {"full_name": "Name", "company_name": "Company name", "job_role": "Role", "phone": "Phone number"}
            missing = [label for field, label in required.items() if not str(data.get(field, "")).strip()]
            if missing:
                raise ValueError(f"Required fields: {', '.join(missing)}")
            data.update({"role": "user", "extension": "", "active": True})
            user_id = store.save_user(data)
            store.add_activity(user_id, user_id, "customer.signup", "customer", user_id, f"{data['company_name']} account created")
            store.add_notification(user_id, "welcome", "Welcome to Telephony Control", "Request a phone number to begin configuring your telephony environment.")
            return jsonify({"ok": True, "user_id": user_id, "message": "Account created. Sign in to continue."}), 201
        except INPUT_DB_ERRORS as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/login")
    @app.get("/admin/login")
    def admin_login_page(): return send_from_directory(template_dir, "admin-login.html")

    @app.get("/console-check")
    @login_required
    def console_check_page():
        """A layout self-check that runs in the browser that uses the console.

        The automated harnesses run in jsdom, which has no layout engine, so the
        geometry of the customer workspace drawer - what stays still, what
        scrolls, what pins - can only be measured where it is really rendered.
        The page reads positions and sizes and shows nothing about any customer.
        """
        return send_from_directory(template_dir, "console-check.html")

    @app.get("/documentation")
    def documentation_page():
        """The setup, API and webhook reference, linked from the public landing page.

        Public on purpose: a developer deciding whether to sign up reads the API
        before they have an account. The page holds no customer data; the only
        things rendered into it are the service addresses an administrator
        configured, which a device needs to register anyway, so every example
        names this deployment rather than a placeholder.
        """
        page_file = Path(template_dir) / "documentation.html"
        page = page_file.read_text(encoding="utf-8")
        service = store.service_address(request.host.split(":")[0] if request.host else "")
        api_base = service["api_base"] or str(request.host_url).rstrip("/")
        sip_host = service["sip"] or (request.host or "")
        page = page.replace("{{API_BASE}}", api_base).replace("{{SIP_HOST}}", sip_host)
        return Response(page, mimetype="text/html")

    @app.post("/login")
    @app.post("/admin/login")
    def admin_login():
        # This endpoint is consumed by JavaScript and must always return JSON.
        # Redirecting an existing session to /admin makes fetch follow the
        # redirect and then attempt to parse the HTML console as JSON.
        if session.get("admin_user_id"):
            return jsonify({"ok": True, "already_authenticated": True})
        if login_rate_limited():
            return jsonify({"error": "too many login attempts"}), 429
        data = request.get_json(silent=True) or request.form
        try:
            user = store.authenticate(str(data.get("username", "")), str(data.get("password", "")))
        except Exception:
            current_app.logger.exception("Administrator login database lookup failed")
            return jsonify({"error": "Login service unavailable; check the database connection"}), 503
        if not user:
            return jsonify({"error": "invalid username or password"}), 401
        session.clear()
        session["admin_user_id"] = user["id"]
        session["admin_username"] = user["username"]
        session["admin_role"] = user["role"]
        session["admin_email"] = user["email"]
        session["admin_extension"] = user["extension"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        return jsonify({"ok": True})

    @app.post("/admin/logout")
    @login_required
    def admin_logout(): session.clear(); return jsonify({"ok": True})

    @app.get("/admin")
    @login_required
    def admin_page(): return send_from_directory(template_dir, "admin.html")

    @app.get("/admin/api/state")
    @login_required
    def admin_state():
        is_admin = session.get("admin_role") == "admin"
        assigned = str(session.get("admin_extension") or "")
        user_id = int(session["admin_user_id"])
        extensions = store.list_extensions() if is_admin else store.list_extensions(user_id)
        owned_extensions = [row["key"] for row in extensions]
        if is_admin:
            voicemail_messages = current_app.extensions["voicemail_store"].list_messages()
            visible_numbers = store.list_numbers()
        else:
            voicemail_messages = [
                message for ext in owned_extensions
                for message in current_app.extensions["voicemail_store"].list_messages(extension_mailbox(ext))
            ]
            visible_numbers = store.list_numbers(user_id)
            for number in visible_numbers:
                number.pop("provider", None)
        all_users = store.list_users() if is_admin else []
        all_sip = store.list_sip_accounts(None if is_admin else user_id)
        # Whether a phone is signed in right now. A phone that is not registered
        # is the first thing to check when a call does not go through, so every
        # extension and device carries it.
        endpoint_states = current_app.extensions["telephony_service"].endpoint_states()
        if endpoint_states:
            for row in all_sip:
                row["registration_status"] = device_registration(row, endpoint_states)
            for row in extensions:
                row["registration_status"] = extension_registration(row, endpoint_states, all_sip)
        customer_metrics = []
        if is_admin:
            all_numbers = store.list_numbers()
            all_extensions = store.list_extensions()
            for customer in (row for row in all_users if row["role"] == "user"):
                customer_metrics.append({
                    **customer,
                    "number_count": sum(row.get("owner_user_id") == customer["id"] for row in all_numbers),
                    "sip_count": sum(row.get("owner_user_id") == customer["id"] for row in all_sip),
                    "extension_count": sum(row.get("owner_user_id") == customer["id"] for row in all_extensions),
                    "payment_status": "overdue" if any(row["user_id"] == customer["id"] and row["status"] == "open" and row["due_at"] < date.today().isoformat() for row in store.list_invoices()) else "current",
                })
        return jsonify({
            "username": session.get("admin_username"), "email": session.get("admin_email", ""),
            "assigned_extension": assigned, "role": session.get("admin_role"), "is_admin": is_admin,
            # The console scopes owner-bound records (flows, groups) with it.
            "user_id": user_id,
            "csrf_token": session.get("csrf_token"), "extensions": extensions,
            "phone_numbers": visible_numbers,
            "providers": store.list_providers() if is_admin else [],
            "webhooks": store.list_webhooks(owner_user_id=None if is_admin else user_id),
            "webhook_deliveries": store.list_webhook_deliveries(owner_user_id=None if is_admin else user_id),
            "users": all_users,
            "customers": customer_metrics,
            "sip_accounts": all_sip,
            "requests": store.list_requests(None if is_admin else user_id),
            "pending_request_count": sum(row["status"] == "pending" for row in store.list_requests(None if is_admin else user_id)),
            "activity": store.list_activity(None if is_admin else user_id),
            "notifications": store.list_notifications(user_id),
            "call_routes": store.list_call_routes(None if is_admin else user_id),
            "routing_flows": store.list_routing_flows(None if is_admin else user_id),
            "groups": store.list_groups(None if is_admin else user_id),
            "api_keys": store.list_api_keys(owner_user_id=None if is_admin else user_id),
            "invoices": store.list_invoices(None if is_admin else user_id),
            "email_config": store.get_email_config() if is_admin else {},
            "email_deliveries": store.list_voicemail_deliveries() if is_admin else [],
            "call_summary": current_app.extensions["telephony_service"].store.summary(
                extensions=store.call_extension_names(user_id) if not is_admin else None,
            ),
            "voicemail_summary": {
                "total": len(voicemail_messages), "new": sum(row["folder"] == "inbox" for row in voicemail_messages),
                "old": sum(row["folder"] == "old" for row in voicemail_messages), "urgent": sum(row["folder"] == "urgent" for row in voicemail_messages),
            },
            "settings": store.get_settings() if is_admin else {},
            # The device provisioning generated with the customer's first
            # number. An administrator owns none, so it is empty for them.
            "primary_extension": store.primary_extension(user_id) if not is_admin else "",
            # The menu a caller can be given: the voices this deployment can
            # play, the default text, and the point at which the platform adds it
            # to a customer's flows by itself.
            "ivr_voices": store.ivr_voices(),
            "ivr_default_prompt": store.IVR_DEFAULT_PROMPT,
            "ivr_auto_above": store.IVR_AUTO_ABOVE,
            # The platform recording switch, so a customer's own switch can say
            # when it cannot take effect.
            "recording_platform_enabled": store.recording_platform_enabled(),
            # Where customers register and what the API examples are built from.
            # The administrator sees the stored value; everybody else sees the
            # address their own devices should use.
            "service_address": store.service_address(request.host.split(":")[0] if request.host else ""),
            "call_defaults": store.customer_call_defaults(user_id) if not is_admin else {},
        })

    @app.get("/admin/api/call-defaults")
    @login_required
    def admin_call_defaults():
        """Where this account's calls go when nothing more specific is chosen."""
        if session.get("admin_role") == "admin":
            requested = str(request.args.get("customer_id") or "")
            if not requested.isdigit():
                return jsonify({"error": "choose a customer"}), 400
            customer_id = int(requested)
            if not store.get_user(customer_id):
                return jsonify({"error": "customer not found"}), 404
        else:
            customer_id = int(session["admin_user_id"])
        return jsonify({
            "call_defaults": store.customer_call_defaults(customer_id),
            "extensions": [row["key"] for row in store.list_extensions(customer_id) if row["active"]],
        })

    @app.post("/admin/api/call-defaults")
    @login_required
    def admin_save_call_defaults():
        """The customer decides where their own calls land, not the platform."""
        if session.get("admin_role") == "admin":
            return jsonify({"error": "call defaults belong to the customer"}), 403
        try:
            data = request.get_json(silent=True) or {}
            saved = store.set_customer_call_defaults(
                int(session["admin_user_id"]),
                str(data.get("outbound") or ""), str(data.get("fallback") or ""),
            )
            apply_change()
            return jsonify({"ok": True, "call_defaults": saved})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/system")
    @admin_required
    def admin_system():
        """What the platform is doing right now: calls, load and recording work.

        Everything here is measured, never estimated. Asterisk is asked for its
        live channels and endpoints, the call store for what is in progress, and
        the host for its load and memory. Anything unreachable reports zero
        rather than guessing.
        """
        service = current_app.extensions["telephony_service"]
        calls = service.store.all()
        in_progress = [call for call in calls if call.status in {"initiated", "ringing", "answered", "dialing_customer"}]
        today = datetime.now(timezone.utc).date().isoformat()
        by_start = [call for call in calls if str(call.started_at or "").startswith(today)]

        def moment(value):
            try:
                return datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return None

        # Peak concurrency today: the most calls overlapping at any instant.
        events = []
        for call in by_start:
            started, ended = moment(call.started_at), moment(call.ended_at)
            if not started:
                continue
            events.append((started, 1))
            events.append((ended or datetime.now(timezone.utc), -1))
        peak, live = 0, 0
        for _, delta in sorted(events, key=lambda item: (item[0], -item[1])):
            live += delta
            peak = max(peak, live)

        try:
            channels = service.asterisk.list_channels() or []
        except Exception:
            channels = []
        try:
            endpoints = [row for row in (service.asterisk.list_endpoints() or [])
                         if str(row.get("technology") or "").lower() == "pjsip"]
        except Exception:
            endpoints = []
        online = sum(1 for row in endpoints if str(row.get("state") or "").lower() in {"online", "available"})

        cpu_count = os.cpu_count() or 1
        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:
            load1 = load5 = load15 = 0.0
        memory_total = memory_available = 0
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        memory_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        memory_available = int(line.split()[1]) * 1024
        except OSError:
            pass

        running = [call for call in calls if call.recording_status == "recording"]
        return jsonify({
            "calls": {
                "in_progress": len(in_progress),
                "ringing": sum(1 for call in in_progress if call.status == "ringing"),
                "connected": sum(1 for call in in_progress if call.status == "answered"),
                "peak_today": peak,
                "today": len(by_start),
                "answered_today": sum(1 for call in by_start if call.answered),
            },
            "channels": {"active": len(channels)},
            "devices": {"online": online, "total": len(endpoints)},
            "recordings": {
                "in_progress": len(running),
                "today": sum(1 for call in by_start if call.recording_name),
            },
            "host": {
                "load_1": round(load1, 2), "load_5": round(load5, 2), "load_15": round(load15, 2),
                "cpu_count": cpu_count, "load_pct": min(100, round((load1 / cpu_count) * 100)),
                "memory_total": memory_total, "memory_available": memory_available,
                "memory_pct": round(((memory_total - memory_available) / memory_total) * 100) if memory_total else 0,
            },
        })

    @app.post("/admin/api/extensions")
    @login_required
    def admin_extension():
        try:
            data = request.get_json(silent=True) or {}
            owner = data.get("owner_user_id") if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            owner_id = int(owner) if str(owner or "").isdigit() else None
            requested = str(data.get("extension", "")).strip()
            # `101` is not an identity any more: it names the line's own 101 when
            # one row carries those digits, so an edit lands on that row instead of
            # inventing a second extension beside it. With a number named, the
            # request already says which line it means.
            scope = str(data.get("number") or "").strip() or extension_scope(requested)
            if requested.isdigit() and not scope:
                # Two of the account's lines may both hold 101: guessing one would
                # put the device on the wrong desk, so the request has to say which
                # number it means.
                if owner_id is not None:
                    holding = [row for row in store.list_extensions(owner_id) if row["digits"] == requested]
                    if len(holding) > 1 and not any(not row["number"] for row in holding):
                        raise ValueError(
                            f"Extension {requested} is on more than one number: choose the number this extension belongs to"
                        )
                existing_key = store.extension_key_for_digits(requested, owner_id)
                if existing_key:
                    data = {**data, "extension": existing_key}
                    requested = existing_key
            scope = str(data.get("number") or "").strip() or extension_scope(requested)
            canonical = extension_key(extension_digits(requested), scope) if extension_digits(requested) else requested
            previous_owner = store.get_extension_owner(canonical) if canonical else None
            existed = any(row["key"] == canonical for row in store.list_extensions())
            result = store.save_extension(data, owner_id, session.get("admin_role") != "admin")
            # A moved extension changes who answers a number every phone already
            # dials, so it is recorded for the operator and the account that lost
            # it is told - their phones can no longer reach it.
            new_owner = store.get_extension_owner(result)
            if previous_owner is not None and not same_account(previous_owner, new_owner):
                store.add_activity(
                    new_owner or int(session["admin_user_id"]), int(session["admin_user_id"]),
                    "extension.reassigned", "extension", result,
                    f"Extension {result} moved from {store._account_label(previous_owner)} "
                    f"to {store._account_label(new_owner)}",
                )
                store.add_notification(
                    int(previous_owner), "extension", "Extension reassigned",
                    f"Extension {result} was moved to another account and is no longer in your dial plan.",
                )
            credentials = None
            if owner_id:
                store.add_activity(owner_id, int(session["admin_user_id"]), "extension.saved", "extension", result, f"Extension {result} configured")
                # The caller created it, so they get the credentials back once:
                # the console shows them with the provisioning summary.
                if not existed:
                    store.add_activity(
                        owner_id, int(session["admin_user_id"]), "extension.provisioned", "extension", result,
                        f"Extension {result} created with SIP credentials and a default call flow",
                    )
                    credentials = store.reveal_extension_credentials(result, owner_id)
                    # Past the fifth device a customer needs a menu more than a
                    # list of extensions; the rule lives in the store.
                    store.sync_auto_ivr(owner_id)
            apply_change()
            return jsonify({"ok": True, "extension": result, "created": not existed, "credentials": credentials})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/extensions/<extension>/password")
    @login_required
    def admin_extension_password(extension):
        """Change the SIP password a device registers with, or generate a new one."""
        try:
            data = request.get_json(silent=True) or {}
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            key, problem = store.resolve_requested_extension(extension, owner)
            if problem:
                return jsonify({"error": problem}), 400
            extension = key
            result = store.set_extension_password(extension, str(data.get("password") or ""), owner)
            if result["owner_user_id"] is not None:
                store.add_activity(
                    result["owner_user_id"], int(session["admin_user_id"]), "extension.password_rotated",
                    "extension", extension, f"SIP password changed for extension {extension}",
                )
            apply_change()
            return jsonify({"ok": True, "sip_password": result["password"], "source": result["source"]})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/extensions/<extension>/credentials")
    @login_required
    def admin_extension_credentials(extension):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            key, problem = store.resolve_requested_extension(extension, owner)
            if problem:
                return jsonify({"error": problem}), 404
            return jsonify({"credentials": store.reveal_extension_credentials(
                key, owner, request.host.split(":")[0] if request.host else "",
            )})
        except ValueError as exc: return jsonify({"error": str(exc)}), 404

    @app.get("/admin/api/extensions/next")
    @login_required
    def admin_next_extension():
        """The extension number a new line would get, for the console to prefill.

        Naming the line answers for that line: every number starts its own set at
        101, so the number the device will live on decides what comes next.
        """
        number = str(request.args.get("number") or "").strip()
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        if number and owner is None:
            owner = store.get_number_owner(number)
        elif number and owner is not None and store.get_number_owner(number) not in (None, owner):
            # Another customer's line: its free numbers are not this caller's to see.
            return jsonify({"error": "Phone number not found"}), 404
        return jsonify({
            "extension": store.next_extension_number(owner, number),
            "number": number,
        })

    @app.delete("/admin/api/extensions/<extension>")
    @login_required
    def admin_delete_extension(extension):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            key, problem = store.resolve_requested_extension(extension, owner)
            if problem:
                return jsonify({"error": problem}), 400
            extension = key
            row = store._extension_row(extension)
            mailboxes = [extension]
            if row:
                mailboxes = [str(row["key"]), str(row["mailbox"]), str(row["digits"])]
            if any(current_app.extensions["voicemail_store"].list_messages(box) for box in dict.fromkeys(mailboxes)):
                raise ValueError("Cannot delete an extension that still has voicemail messages")
            store.delete_extension(extension, owner); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/numbers")
    @admin_required
    def admin_number():
        try:
            data = request.get_json(silent=True) or {}
            owner = int(data["owner_user_id"]) if str(data.get("owner_user_id", "")).isdigit() else None
            inbound = str(data.get("inbound_extension", "")).strip()
            auto = "auto" in {inbound.lower(), str(data.get("auto_provision", "")).lower()} or bool(data.get("auto_provision"))
            if auto:
                # Auto-provisioning builds the line for a customer; without one
                # it would silently store an unlinked number that rings nobody.
                if not owner:
                    raise ValueError("Auto-provisioning needs a customer account: choose who owns this number, or pick an extension explicitly")
                # Assign first, then provision: a carrier or format problem must
                # not leave a half-built line behind.
                data = {**data, "inbound_extension": "", "default_outbound": False}
            result = store.save_number(data)
            provisioned = None
            if auto and owner:
                provisioned = store.provision_number(owner, result, str(data.get("description", "")), int(session["admin_user_id"]))
                store.add_notification(
                    owner, "number", "Phone line ready",
                    f"{result} is live on extension {provisioned['extension']}. SIP credentials and default call flows were created for you.",
                )
            if owner and not provisioned:
                store.add_activity(owner, int(session["admin_user_id"]), "number.assigned", "phone_number", result, f"Number {result} assigned")
                store.add_notification(owner, "number", "Phone number assigned", f"{result} is now available in your account.")
            apply_change(); return jsonify({"ok": True, "number": result, "provisioned": provisioned})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/numbers/<path:number>/extensions")
    @login_required
    def admin_number_extensions(number):
        """Every extension that belongs to one phone number, in dialling order.

        The list is the number's own set and nothing else: another line of the
        same customer has its own 101, and this is what the console draws beside
        one number.
        """
        is_admin = session.get("admin_role") == "admin"
        owner = None if is_admin else int(session["admin_user_id"])
        line = store.extension_number_row(number)
        if line is None:
            return jsonify({"error": "Phone number not found"}), 404
        if not is_admin and line["owner_user_id"] not in (None, owner):
            return jsonify({"error": "Phone number not found"}), 404
        number_owner = line["owner_user_id"] if is_admin else owner
        rows = store.extensions_on_number(number_owner, number)
        live = current_app.extensions["telephony_service"].endpoint_states()
        accounts = store.list_sip_accounts(number_owner)
        for row in rows:
            if live:
                row["registration_status"] = extension_registration(row, live, accounts)
        return jsonify({
            "number": number,
            "inbound_extension": str(line["inbound_extension"] or ""),
            "owner_user_id": line["owner_user_id"],
            "extensions": rows,
        })

    @app.post("/admin/api/numbers/<path:number>/extensions")
    @login_required
    def admin_add_number_extension(number):
        """Add the next extension to one phone number.

        Every number's set starts at 101 and goes on from there, so this is the
        console's "Add extension" on a line: the next free number on this very
        number, with credentials and a default flow.
        """
        try:
            data = request.get_json(silent=True) or {}
            is_admin = session.get("admin_role") == "admin"
            owner = int(session["admin_user_id"]) if not is_admin else None
            if is_admin and str(data.get("owner_user_id", "")).isdigit():
                owner = int(data["owner_user_id"])
            if owner is None:
                owner = store.get_number_owner(number)
            if not is_admin and owner is not None and owner != int(session["admin_user_id"]):
                raise ValueError("Number is not assigned to your account")
            result = store.add_extension_to_number(number, owner, data)
            store.sync_primary_flows(owner, result["extension"])
            store.sync_auto_ivr(owner)
            store.add_activity(
                owner, int(session["admin_user_id"]), "extension.provisioned", "extension", result["extension"],
                f"Extension {result['digits']} added to {number} with SIP credentials and a default call flow",
            )
            apply_change()
            return jsonify({
                "ok": True, "extension": result["key"], "key": result["key"], "digits": result["digits"],
                "number": result["number"], "created": True,
                "credentials": store.reveal_extension_credentials(result["key"], owner),
            })
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/numbers/<path:number>")
    @admin_required
    def admin_delete_number(number):
        try:
            store.delete_number(number); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/providers")
    @admin_required
    def admin_provider():
        try:
            result = store.save_provider(request.get_json(silent=True) or {})
            apply_change(); return jsonify({"ok": True, "provider": result})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/providers/<int:provider_id>")
    @admin_required
    def admin_delete_provider(provider_id):
        try:
            store.delete_provider(provider_id); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/calls")
    @login_required
    def admin_calls():
        extension = request.args.get("extension", "").strip()
        owned_extensions = None
        if session.get("admin_role") != "admin":
            owner = int(session["admin_user_id"])
            # The history reads the exact extension (`101@+13025550001`), while a
            # caller may name the digits - which name one row of this account, and
            # nothing at all when several carry them.
            owned_extensions = store.call_extension_names(owner)
            if extension.isdigit():
                key, problem = store.resolve_requested_extension(extension, owner)
                if problem:
                    return jsonify({"error": problem}), (404 if problem == "Extension not found" else 400)
                extension = key
            if extension and extension not in owned_extensions:
                return jsonify({"error": "extension not found"}), 404
        status = request.args.get("status", "").strip()
        query = request.args.get("q", "").strip()[:100]
        number = request.args.get("number", "").strip()
        recordings_only = request.args.get("recordings", "false").lower() == "true"
        if extension and not (
            (extension.isdigit() and 101 <= int(extension) <= 999) or extension_scope(extension)
        ):
            return jsonify({"error": "invalid extension"}), 400
        if number:
            if not re.fullmatch(r"\+[1-9]\d{7,14}", number):
                return jsonify({"error": "invalid number"}), 400
            visible_numbers = store.list_numbers(None if session.get("admin_role") == "admin" else int(session["admin_user_id"]))
            if not any(str(row["number"]) == number for row in visible_numbers):
                return jsonify({"error": "number not found"}), 404
        if status and not re.fullmatch(r"[a-z_]{1,40}", status):
            return jsonify({"error": "invalid status"}), 400
        try:
            limit = min(100, max(1, int(request.args.get("limit", "50"))))
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            return jsonify({"error": "invalid pagination"}), 400
        calls, total = current_app.extensions["telephony_service"].store.search(
            extension=extension or None, extensions=owned_extensions if not extension else None,
            status=status or None, recordings_only=recordings_only,
            query=query or None, number=number or None, limit=limit, offset=offset,
        )
        return jsonify({"calls": [call.to_dict() for call in calls], "total": total, "limit": limit, "offset": offset})

    def visible_call_rows():
        calls = current_app.extensions["telephony_service"].store.all()
        if session.get("admin_role") == "admin":
            return calls
        owned = set(store.call_extension_names(int(session["admin_user_id"])))
        return [call for call in calls if call.extension in owned]

    @app.get("/admin/api/calls/export.csv")
    @login_required
    def export_calls_csv():
        recordings_only = request.args.get("recordings", "false").lower() == "true"
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Call ID", "Started", "Direction", "Caller / destination", "Assigned number", "Extension", "Status", "Answered", "Duration seconds", "Provider", "Recording status"])
        def csv_safe(value):
            text = str(value or "")
            return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text
        for call in visible_call_rows():
            if recordings_only and not call.recording_name:
                continue
            writer.writerow([csv_safe(value) for value in [call.call_id, call.started_at, call.direction, call.phone, call.caller_id_number, call.extension, call.status, "yes" if call.answered else "no", call.duration_seconds or 0, call.provider or "", call.recording_status or ""]])
        filename = "recordings.csv" if recordings_only else "call-history.csv"
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/admin/api/analytics")
    @login_required
    def call_analytics():
        calls = visible_call_rows()
        cutoff = (date.today() - timedelta(days=13)).isoformat()
        recent = [call for call in calls if str(call.started_at)[:10] >= cutoff]
        answered = sum(bool(call.answered) for call in calls)
        missed = sum(call.direction == "inbound" and not call.answered for call in calls)
        completed_durations = [int(call.duration_seconds or 0) for call in calls if call.answered]
        days = {(date.today() - timedelta(days=offset)).isoformat(): {"total": 0, "answered": 0} for offset in range(13, -1, -1)}
        extensions = {}
        for call in calls:
            bucket = extensions.setdefault(call.extension, {"extension": call.extension, "total": 0, "answered": 0, "duration_seconds": 0})
            bucket["total"] += 1
            bucket["answered"] += int(bool(call.answered))
            bucket["duration_seconds"] += int(call.duration_seconds or 0)
        for call in recent:
            key = str(call.started_at)[:10]
            if key in days:
                days[key]["total"] += 1
                days[key]["answered"] += int(bool(call.answered))
        return jsonify({
            "total": len(calls), "answered": answered, "missed": missed,
            "answer_rate": round(answered * 100 / len(calls), 1) if calls else 0,
            "average_duration_seconds": round(sum(completed_durations) / len(completed_durations)) if completed_durations else 0,
            "inbound": sum(call.direction == "inbound" for call in calls),
            "outbound": sum(call.direction == "outbound" for call in calls),
            "daily": [{"date": key, **value} for key, value in days.items()],
            "extensions": sorted(extensions.values(), key=lambda row: row["total"], reverse=True)[:10],
        })

    def mailbox_allowed(mailbox: str) -> bool:
        """May this session hear that mailbox?

        A mailbox is a key (`101@+13025550001`), its name on disk
        (`101-13025550001`) or the digits a customer reads (`101`), so all three
        are answered - and only for the account that owns the extension.
        """
        if session.get("admin_role") == "admin":
            return True
        wanted = str(mailbox)
        for row in store.list_extensions(int(session["admin_user_id"])):
            key = str(row["key"])
            if wanted in {key, str(row["mailbox"]), extension_digits(key)}:
                return True
        return False

    @app.post("/admin/api/calls")
    @login_required
    def admin_start_call():
        # Placing a call is the customer's own action with their own line: an
        # administrator manages accounts and never dials on somebody's behalf.
        if session.get("admin_role") == "admin":
            return jsonify({"error": "administrators manage the platform and do not place calls"}), 403
        data = request.get_json(silent=True) or {}
        phone = str(data.get("phone", "")).strip()
        if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            return jsonify({"error": "Phone must be a valid E.164 number"}), 400
        extension = str(data.get("extension") or "").strip()
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        available = store.list_extensions(owner)
        if extension.isdigit():
            # The app and the API send the three digits their user dialled. They
            # name one extension while only one of the account's carries them;
            # with several the phone number has to be named as well.
            if len([row for row in available if row["digits"] == extension]) > 1:
                return jsonify({
                    "error": f"{extension} is on more than one of your numbers - name the phone number as well"
                }), 400
            extension = store.extension_key_for_digits(extension, owner)
        if not any(row["key"] == extension and row["active"] for row in available):
            return jsonify({"error": "Active extension is required"}), 400
        try:
            call = current_app.extensions["telephony_service"].start_outbound(
                phone=phone, extension=extension, contact_id=data.get("contact_id"), member_id=data.get("member_id"),
                caller_id_number=str(data.get("caller_id_number") or "").strip() or None,
            )
            return jsonify({"call": call.to_dict()}), 201
        except RuntimeError as exc:
            message = str(exc)
            if message.startswith(("No active callback number", "The selected callback number", "ARI event worker")):
                return jsonify({"error": message}), 400
            current_app.logger.exception("Admin call start failed")
            return jsonify({"error": "telephony service unavailable"}), 502
        except Exception:
            current_app.logger.exception("Admin call start failed")
            return jsonify({"error": "telephony service unavailable"}), 502

    @app.post("/admin/api/numbers/<path:number>/discontinue")
    @login_required
    def customer_discontinue_number(number):
        if session.get("admin_role") == "admin":
            return jsonify({"error": "Set the discontinuation date while editing the number"}), 400
        try:
            end_date = store.request_number_discontinuation(number, int(session["admin_user_id"]))
            apply_change()
            return jsonify({"ok": True, "discontinue_at": end_date})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/numbers/default")
    @login_required
    def admin_default_number():
        data = request.get_json(silent=True) or {}
        extension = str(data.get("extension") or "").strip()
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        # `101` may name a line's own extension: resolve it to the key first, so
        # the caller ID is set on the device the console was looking at.
        if extension.isdigit():
            if len([row for row in store.list_extensions(owner) if row["digits"] == extension]) > 1:
                return jsonify({
                    "error": f"{extension} is on more than one of your numbers - name the phone number as well"
                }), 400
            extension = store.extension_key_for_digits(extension, owner)
        if owner is not None and not any(row["key"] == extension for row in store.list_extensions(owner)):
            return jsonify({"error": "extension not found"}), 404
        try:
            store.set_default_outbound_number(extension, str(data.get("number") or "").strip())
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/voicemails")
    @login_required
    def admin_voicemails():
        mailbox = request.args.get("extension", "").strip() or None
        owned = None
        if session.get("admin_role") != "admin":
            owner = int(session["admin_user_id"])
            owned = [row["key"] for row in store.list_extensions(owner)]
            # The customer picks an extension; its messages live in that
            # extension's own mailbox (`101-13025550001`).
            if mailbox and mailbox.isdigit():
                # Two of the account's lines may both hold 101, and their messages
                # are two mailboxes: guessing one would show the wrong line's
                # voicemail, so the request has to name the mailbox.
                holding = [row for row in store.list_extensions(owner) if row["digits"] == mailbox]
                if len(holding) > 1:
                    return jsonify({
                        "error": f"{mailbox} is on more than one of your numbers - choose the mailbox "
                                 "on the number it belongs to",
                    }), 400
                if holding:
                    mailbox = extension_mailbox(str(holding[0]["key"]))
            if mailbox and mailbox not in owned and mailbox_name(mailbox) not in {
                extension_mailbox(str(ext)) for ext in owned
            }:
                return jsonify({"error": "mailbox not found"}), 404
        folder = request.args.get("folder", "").strip() or None
        try:
            messages = current_app.extensions["voicemail_store"].list_messages(mailbox, folder) if mailbox or owned is None else [message for ext in owned for message in current_app.extensions["voicemail_store"].list_messages(ext, folder)]
            return jsonify({"voicemails": messages, "total": len(messages)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/voicemails/<mailbox>/<folder>/<message>/file")
    @login_required
    def admin_voicemail_file(mailbox, folder, message):
        if not mailbox_allowed(mailbox):
            return jsonify({"error": "forbidden"}), 403
        path = current_app.extensions["voicemail_store"].audio_path(mailbox, folder, message)
        if not path:
            return jsonify({"error": "voicemail not found"}), 404
        return send_file(path, conditional=True, as_attachment=False, download_name=f"voicemail-{mailbox}-{message}{path.suffix}")

    @app.post("/admin/api/voicemails/<mailbox>/<folder>/<message>/read")
    @login_required
    def admin_voicemail_read(mailbox, folder, message):
        if not mailbox_allowed(mailbox):
            return jsonify({"error": "forbidden"}), 403
        if not current_app.extensions["voicemail_store"].mark_read(mailbox, folder, message):
            return jsonify({"error": "new voicemail not found"}), 404
        return jsonify({"ok": True})

    @app.delete("/admin/api/voicemails/<mailbox>/<folder>/<message>")
    @login_required
    def admin_voicemail_delete(mailbox, folder, message):
        if not mailbox_allowed(mailbox):
            return jsonify({"error": "forbidden"}), 403
        if not current_app.extensions["voicemail_store"].delete(mailbox, folder, message):
            return jsonify({"error": "voicemail not found"}), 404
        return jsonify({"ok": True})

    @app.post("/admin/api/webhooks")
    @login_required
    def admin_webhook():
        """Customers create their endpoints; an administrator manages the ones that
        exist - including editing them - because a broken endpoint is a call he
        cannot deliver."""
        try:
            data = request.get_json(silent=True) or {}
            if session.get("admin_role") == "admin":
                given = str(data.get("id") or "").strip()
                existing = next((row for row in store.list_webhooks() if str(row["id"]) == given), None) if given else None
                if not existing:
                    return jsonify({"error": "webhooks are created by the customer who owns them"}), 403
                # The endpoint's own owner decides the record; the request cannot
                # move a customer's endpoint to somebody else.
                result = store.save_webhook({**data, "id": existing["id"]}, existing["owner_user_id"])
                return jsonify({"ok": True, "webhook_id": result})
            owner = int(session["admin_user_id"])
            result = store.save_webhook(data, owner)
            return jsonify({"ok": True, "webhook_id": result})
        except INPUT_DB_ERRORS as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/webhooks/<int:webhook_id>")
    @login_required
    def admin_delete_webhook(webhook_id):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.delete_webhook(webhook_id, owner)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/webhooks/<int:webhook_id>/test")
    @login_required
    def admin_test_webhook(webhook_id):
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        if owner is not None and not any(row["id"] == webhook_id for row in store.list_webhooks(owner_user_id=owner)):
            return jsonify({"error": "webhook not found"}), 404
        service = current_app.extensions["telephony_service"]
        result = service.test_webhook(webhook_id)
        status = 200 if result.get("ok") else (404 if result.get("error") == "webhook not found" else 502)
        return jsonify(result), status

    @app.get("/admin/api/recordings/<call_id>/file")
    @login_required
    def admin_recording_file(call_id):
        service = current_app.extensions["telephony_service"]
        call = service.store.get(call_id)
        if call and session.get("admin_role") != "admin" and not any(
            row["key"] == call.extension for row in store.list_extensions(int(session["admin_user_id"]))
        ):
            return jsonify({"error": "recording not found"}), 404
        if not call or not call.recording_name or call.recording_status not in {"finalized", "available"}:
            return jsonify({"error": "recording not found"}), 404
        upstream = service.asterisk.open_stored_recording(call.recording_name, request.headers.get("Range"))
        if upstream is None:
            return jsonify({"error": "recording not found"}), 404
        headers = {"Content-Disposition": f'inline; filename="{call.recording_name}.{call.recording_format or "wav"}"'}
        for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if upstream.headers.get(header):
                headers[header] = upstream.headers[header]
        def body():
            try:
                yield from upstream.iter_content(64 * 1024)
            finally:
                upstream.close()
        return Response(
            stream_with_context(body()), status=upstream.status_code,
            content_type=upstream.headers.get("Content-Type", "application/octet-stream"), headers=headers,
            direct_passthrough=True,
        )

    @app.post("/admin/api/settings")
    @admin_required
    def admin_settings():
        try:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict): raise ValueError("JSON object required")
            allowed = {
                "recording_enabled", "recording_format", "recording_retention_days", "recording_announcement",
                "recording_announcement_media", "recording_beep", "recording_max_duration_seconds",
                "default_extension", "inbound_fallback_extension", "webrtc_enabled",
                "service_host", "service_web_host", "service_sip_port", "sip_auth_digest",
                "ice_servers",
            }
            store.set_settings({k: data[k] for k in data if k in allowed})
            # A saved address that will not render is not a reason to refuse
            # the save - DNS can come up after it is entered - but the console
            # is told exactly why the running configuration did not change.
            warning = apply_change()
            return jsonify({"ok": True, **({"warning": warning} if warning else {})})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/groups")
    @login_required
    def customer_group():
        """Create or rename a ring group. Customers manage their own; an
        administrator manages one for a customer from the workspace."""
        try:
            data = request.get_json(silent=True) or {}
            if session.get("admin_role") == "admin":
                # The administrator builds groups for the customer whose flow
                # they are editing; the workspace names that customer.
                chosen = str(data.get("owner_user_id") or "").strip()
                owner = int(chosen) if chosen.isdigit() else None
                if not owner:
                    return jsonify({"error": "Choose the customer this group belongs to"}), 400
            else:
                owner = int(session["admin_user_id"])
            data = {**data, "owner_user_id": owner}
            group_id = store.save_group(data, owner)
            store.add_activity(owner, int(session["admin_user_id"]), "group.saved", "extension_group", group_id, f"Group {data.get('name')} saved")
            apply_change(); return jsonify({"ok": True, "group_id": group_id})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/groups/<int:group_id>")
    @login_required
    def customer_group_delete(group_id):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            group = store.get_group(group_id, owner)
            store.delete_group(group_id, owner)
            if group:
                store.add_activity(group["owner_user_id"], int(session["admin_user_id"]), "group.deleted", "extension_group", group_id, f"Group {group['name']} deleted")
            apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 404

    @app.get("/admin/api/customers/<int:customer_id>")
    @admin_required
    def admin_customer_detail(customer_id):
        customer = store.get_user(customer_id)
        if not customer or customer["role"] != "user":
            return jsonify({"error": "customer not found"}), 404
        extensions = store.list_extensions(customer_id)
        numbers = store.list_numbers(customer_id)
        calls, total = current_app.extensions["telephony_service"].store.search(extensions=[row["key"] for row in extensions], limit=20)
        return jsonify({
            "customer": next((row for row in store.list_users() if row["id"] == customer_id), customer),
            "extensions": extensions, "numbers": numbers, "sip_accounts": store.list_sip_accounts(customer_id),
            # Which of those extensions the platform generated first: the
            # workspace marks it the customer's primary device.
            "primary_extension": store.primary_extension(customer_id),
            "invoices": store.list_invoices(customer_id), "requests": store.list_requests(customer_id),
            "activity": store.list_activity(customer_id), "calls": [call.to_dict() for call in calls], "call_total": total,
            # The workspace routing tab manages this customer's flows, so it needs
            # the same three targets the customer sees: numbers, extensions, groups.
            "call_routes": store.list_call_routes(customer_id),
            "routing_flows": store.list_routing_flows(customer_id),
            "groups": store.list_groups(customer_id),
        })

    @app.post("/admin/api/requests")
    @login_required
    def customer_create_request():
        try:
            data = request.get_json(silent=True) or {}
            request_id = store.create_request(int(session["admin_user_id"]), str(data.get("request_type", "number")), str(data.get("details", "")))
            return jsonify({"ok": True, "request_id": request_id}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/requests/<int:request_id>/resolve")
    @admin_required
    def admin_resolve_request(request_id):
        try:
            data = request.get_json(silent=True) or {}
            store.resolve_request(request_id, str(data.get("status", "")), str(data.get("admin_note", "")), int(session["admin_user_id"]))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/sip-accounts")
    @admin_required
    def admin_save_sip_account():
        try:
            data = request.get_json(silent=True) or {}
            owner = int(data.get("owner_user_id"))
            account_id = store.save_sip_account(data, owner)
            store.add_activity(owner, int(session["admin_user_id"]), "sip.saved", "sip_account", account_id, f"SIP account {data.get('label') or data.get('sip_username')} configured")
            store.add_notification(owner, "sip", "SIP credentials available", "A SIP account has been configured for your organization.")
            apply_change()
            return jsonify({"ok": True, "sip_account_id": account_id})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/sip-accounts/<int:account_id>")
    @admin_required
    def admin_delete_sip_account(account_id):
        try:
            store.delete_sip_account(account_id)
            apply_change()
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.get("/admin/api/device-status")
    @login_required
    def admin_device_status():
        # Use the same session-derived identity as the other endpoints: an
        # administrator sees every device, a customer only their own.
        is_admin = session.get("admin_role") == "admin"
        owner = None if is_admin else int(session["admin_user_id"])
        accounts = store.list_sip_accounts(owner)
        live = current_app.extensions["telephony_service"].endpoint_states()
        extensions = store.list_extensions(owner)
        return jsonify({
            "devices": [
                {"id": row["id"], "registration_status": device_registration(row, live) if live else row["registration_status"]}
                for row in accounts
            ],
            # The extensions a customer actually signs in with - most phones
            # register as the extension itself rather than as a device account.
            "extensions": [
                {"extension": row["key"], "registration_status": extension_registration(row, live, accounts)}
                for row in extensions if live
            ],
        })

    @app.get("/admin/api/sip-accounts/<int:account_id>/credentials")
    @login_required
    def sip_account_credentials(account_id):
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        rows = store.list_sip_accounts(owner, include_password=True)
        account = next((row for row in rows if row["id"] == account_id), None)
        if not account:
            return jsonify({"error": "SIP account not found"}), 404
        return jsonify({"sip_account": account})

    @app.post("/admin/api/call-routes")
    @login_required
    def save_customer_call_route():
        """One endpoint for every kind of call flow: a number, an extension or a
        group. The target decides which store call and who owns it."""
        try:
            data = request.get_json(silent=True) or {}
            target_type = str(data.get("target_type") or "number").strip().lower()
            target = str(data.get("target") or "").strip()
            owner = flow_owner(data, target_type, target, data.get("phone_number"))
            if target_type == "number":
                route_id = store.save_call_route(owner, data)
                label = data.get("phone_number")
            else:
                route_id = store.save_routing_flow(owner, data, target_type=target_type, target=target or None)
                label = f"{target_type} {target}"
            # A flow saved by a customer who is past five devices still gets the
            # menu: the rule is about every workflow, not only the ones that
            # existed when the sixth device was created. What was added is
            # reported, so the console can say it instead of changing the stored
            # flow silently.
            added = store.sync_auto_ivr(owner)
            who = "operator" if session.get("admin_role") == "admin" else "customer"
            kind = "call_route" if target_type == "number" else "routing_flow"
            store.add_activity(owner, int(session["admin_user_id"]), "route.saved", kind, route_id, f"Call flow for {label} updated by the {who}")
            # The store is only the plan - the PBX runs the rendered mailboxes
            # and dialplan. Every sibling mutation (extensions, SIP accounts)
            # applies here, and flows used to skip it: a saved voicemail box
            # then existed nowhere Asterisk could see it, so VoiceMail() failed
            # at ring-out while every store-reading test stayed green.
            warning = apply_change()
            return jsonify({"ok": True, "route_id": route_id, "auto_ivr": added, "warning": warning})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/call-routes/<int:route_id>")
    @login_required
    def delete_customer_call_route(route_id):
        """Removes an extension or group flow so it can be rebuilt from the
        default; number flows keep their own endpoint behaviour."""
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.delete_routing_flow(route_id, owner)
            apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/notifications/read-all")
    @login_required
    def read_all_customer_notifications():
        store.mark_all_notifications_read(int(session["admin_user_id"]))
        return jsonify({"ok": True})

    @app.post("/admin/api/notifications/<int:notification_id>/read")
    @login_required
    def read_customer_notification(notification_id):
        try:
            store.mark_notification_read(notification_id, int(session["admin_user_id"]))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/users")
    @admin_required
    def admin_save_user():
        try:
            data = request.get_json(silent=True) or {}
            data.update({"role": "user", "extension": ""})
            user_id = store.save_user(data, int(session["admin_user_id"]))
            return jsonify({"ok": True, "user_id": user_id})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/platformadmins")
    @admin_required
    def admin_save_platform_admin():
        try:
            data = request.get_json(silent=True) or {}
            data.update({"role": "admin", "extension": "", "active": True})
            user_id = store.save_user(data, int(session["admin_user_id"]))
            return jsonify({"ok": True, "user_id": user_id}), 201
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/users/<int:user_id>")
    @admin_required
    def admin_delete_user(user_id):
        try:
            store.delete_user(user_id, int(session["admin_user_id"]))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/api-keys")
    @login_required
    def admin_create_api_key():
        if session.get("admin_role") == "admin":
            return jsonify({"error": "API keys are created by the customer who owns them"}), 403
        try:
            data = request.get_json(silent=True) or {}
            owner = int(session["admin_user_id"])
            key_id, token = store.create_api_key(str(data.get("name", "")), str(data.get("scopes", "*")), owner)
            store.add_activity(owner, int(session["admin_user_id"]), "api_key.created", "api_key", key_id, f"API key {data.get('name')} generated")
            return jsonify({"ok": True, "key_id": key_id, "token": token}), 201
        except (ValueError, sqlite3.IntegrityError, MySQLIntegrityError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/api-keys/<int:key_id>")
    @login_required
    def admin_update_api_key(key_id):
        try:
            data = request.get_json(silent=True) or {}
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.update_api_key(key_id, data, owner)
            key = next((row for row in store.list_api_keys(owner_user_id=owner) if row["id"] == key_id), None)
            if key:
                store.add_activity(key["owner_user_id"], int(session["admin_user_id"]), "api_key.updated",
                                   "api_key", key_id, f"API key {key['name']} updated")
            return jsonify({"ok": True})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/api-keys/<int:key_id>")
    @login_required
    def admin_revoke_api_key(key_id):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.revoke_api_key(key_id, owner)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/invoices/<int:invoice_id>/status")
    @admin_required
    def admin_invoice_status(invoice_id):
        try:
            store.set_invoice_status(invoice_id, str((request.get_json(silent=True) or {}).get("status", "")))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/invoices")
    @admin_required
    def admin_create_invoice():
        try:
            data = request.get_json(silent=True) or {}
            invoice_id = store.create_invoice(
                int(data.get("user_id")), str(data.get("number", "")),
                str(data.get("period_start", "")), str(data.get("period_end", "")),
                int(data.get("amount_cents", 500)), str(data.get("due_at", "")),
            )
            return jsonify({"ok": True, "invoice_id": invoice_id}), 201
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/email-config")
    @admin_required
    def admin_email_config():
        try:
            store.save_email_config(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "email_config": store.get_email_config()})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/email-config/test")
    @admin_required
    def admin_email_test():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "Email test must be an object"}), 400
        email = str(data.get("email", "")).strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            return jsonify({"error": "Valid test email required"}), 400
        success, error = current_app.extensions["voicemail_notifier"].send_test(email)
        # The console asks for a completed test result rather than an upstream
        # 502: some reverse proxies replace 5xx JSON with a generic HTML page.
        # Keep the original 502 contract for older clients that do not opt in.
        status = 200 if success or data.get("result_only") is True else 502
        return jsonify({"ok": success, "error": error}), status

    @app.post("/admin/api/profile/recording")
    @login_required
    def admin_profile_recording():
        data = request.get_json(silent=True) or {}
        extension = str(data.get("extension") or session.get("admin_extension") or "")
        if session.get("admin_role") != "admin":
            owner = int(session["admin_user_id"])
            owned = store.list_extensions(owner)
            if not extension and len(owned) == 1:
                extension = owned[0]["key"]
            if not extension and len(owned) > 1:
                return jsonify({
                    "error": "Name the extension whose recording you want to change - "
                             "this account has more than one"
                }), 400
            if extension.isdigit():
                if len([row for row in owned if row["digits"] == extension]) > 1:
                    return jsonify({
                        "error": f"{extension} is on more than one of your numbers - name the phone number as well"
                    }), 400
                extension = store.extension_key_for_digits(extension, owner)
            if not any(row["key"] == extension for row in owned):
                return jsonify({"error": "Extension not found"}), 404
        if not extension:
            return jsonify({"error": "Choose an extension"}), 400
        try:
            enabled = bool(data.get("enabled", False))
            store.set_extension_recording(extension, enabled)
            return jsonify({"ok": True, "enabled": enabled})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/profile/email")
    @login_required
    def admin_profile_email():
        try:
            email = str((request.get_json(silent=True) or {}).get("email", ""))
            store.update_user_email(int(session["admin_user_id"]), email)
            session["admin_email"] = email.strip().lower()
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/password")
    @login_required
    def admin_password():
        password = str((request.get_json(silent=True) or {}).get("password", ""))
        if len(password) < 14 or len(password) > 256: return jsonify({"error": "Password must be between 14 and 256 characters"}), 400
        store.set_admin_password(int(session["admin_user_id"]), password)
        return jsonify({"ok": True})

    return store
