from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

from .admin import (
    browser_username, endpoint_name as extension_endpoint, extension_digits, extension_key, extension_mailbox,
    extension_scope,
)
from .config import Config
from .models import Call, CallStore


ARI_READY_STALE_SECONDS = 30
logger = logging.getLogger(__name__)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_setting(settings: dict[str, str], key: str, default: bool) -> bool:
    value = settings.get(key)
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


class TelephonyService:
    def __init__(self, asterisk, config: type[Config] = Config, store: CallStore | None = None, settings_store=None):
        self.asterisk = asterisk
        self.config = config
        self.store = store or CallStore(getattr(config, "DATABASE_URI", "") or config.CALLS_DB_PATH)
        self.settings_store = settings_store
        self._finalizing: set[str] = set()
        self._finalize_lock = threading.Lock()
        # Channel id -> call id for every leg of a call that is ringing several
        # devices; rebuilt from the stored call record after a worker restart.
        self._leg_index: dict[str, str] = {}
        # When the endpoint states below were fetched, and what came back.
        self._endpoint_states: tuple[float, dict[str, str]] = (0.0, {})

    def ari_ready(self) -> bool:
        try:
            ready = Path(self.config.ARI_READY_PATH)
            return ready.exists() and (datetime.now(timezone.utc).timestamp() - ready.stat().st_mtime) <= ARI_READY_STALE_SECONDS
        except OSError:
            return False

    # How long an answer from Asterisk about registered endpoints is reused.
    # Every console refresh asks, and Asterisk should not be asked twice for the
    # same state.
    ENDPOINT_STATE_SECONDS = 5

    def endpoint_states(self) -> dict[str, str]:
        """Which PJSIP endpoints are signed in right now, from ARI.

        Registration is what makes a phone ring, so the console shows it against
        every extension and device. An empty answer means Asterisk could not be
        asked - which callers must not read as "nothing is registered".
        """
        now = time.monotonic()
        stamped, states = self._endpoint_states
        if now - stamped < self.ENDPOINT_STATE_SECONDS:
            return states
        collected: dict[str, str] = {}
        if self.ari_ready():
            try:
                collected = {
                    str(row.get("resource") or ""): str(row.get("state") or "offline").lower()
                    for row in self.asterisk.list_endpoints()
                    if str(row.get("technology") or "").lower() == "pjsip"
                }
            except Exception:
                collected = {}
        self._endpoint_states = (now, collected)
        return collected


    def dial_endpoints(self, extension: str, number: str = "") -> list[str]:
        """Every PJSIP endpoint one extension's devices live on.

        Media is negotiated by the endpoint a call is dialled on, so this is
        what decides whether a device can answer with audio: a browser needs
        the WebRTC endpoint (DTLS-SRTP, ICE, RTCP-mux) and a hardware phone or
        softphone the plain one - a WebRTC offer sent to a hardware phone is
        refused, and a plain offer sent to a browser the same. The two device
        kinds therefore live on two endpoints with two SIP identities (see
        `browser_username`), and an extension marked as answering in the
        browser rings BOTH: the browser endpoint first, then the plain one, so
        a Zoiper on a WebRTC-ticked extension is rung with the media it speaks.

        `extension` is the stored key (`104@+13025550001`) whenever the caller
        knows which number the call is on, which is what keeps two lines' 104s
        apart even when a row is stale.
        """
        digits = extension_digits(extension)
        # The name is built the same way the renderer builds it: the extension's
        # globally unique identity, digits plus the line they belong to
        # (`101-13025550001`), because a PJSIP section name cannot hold the `@`
        # the stored key has. The digits alone name nothing here.
        plain = f"PJSIP/{extension_endpoint(extension)}" if digits else ""
        if not self.settings_store or not digits:
            return [plain] if plain else []
        try:
            row = None
            if extension_scope(extension):
                row = self.settings_store._extension_row(str(extension))
            if row is None and number:
                # Only the digits, or a stale reference: they are read against
                # the number the call is on and nothing else.
                row = self.settings_store.resolve_extension(number, digits)
            if row is None and not number:
                # No number was named: the digits are read only while exactly one
                # extension carries them. Several mean the caller has to say
                # which line, so nothing is guessed here.
                matches = [
                    item for item in self.settings_store.list_extensions()
                    if extension_digits(item.get("key") or item.get("extension")) == digits
                ]
                if len(matches) > 1:
                    # A row with no number of its own is named by its digits -
                    # that is its key - so the plain digits are exactly that row.
                    unscoped = [item for item in matches if not item.get("number")]
                    matches = unscoped
                row = matches[0] if len(matches) == 1 else None
        except Exception:
            return [plain] if plain else []
        if row is None:
            return [plain] if plain else []
        plain = f"PJSIP/{extension_endpoint(str(row['key']))}"
        username = str(row.get("sip_username") or "")
        if row.get("active") and row.get("webrtc_enabled") and username:
            web = f"PJSIP/{browser_username(username)}"
            return [web, plain]
        return [plain]

    def dial_endpoint(self, extension: str, number: str = "") -> str:
        """The endpoint a single leg rings: the browser's when there is one."""
        for target in self.dial_endpoints(extension, number):
            return target
        return ""

    def _provider_endpoint(self, provider_name: str | None) -> tuple[str, str]:
        if not self.settings_store:
            return "ipcomms", "IPComms"
        provider = self.settings_store.get_provider(provider_name)
        if not provider:
            raise RuntimeError("No active SIP provider is configured")
        endpoint = f"provider-{hashlib.sha256(provider['name'].encode()).hexdigest()[:12]}"
        return endpoint, provider["name"]

    def _recording_settings(self, extension: str | None = None) -> dict[str, Any]:
        settings = self.settings_store.get_settings() if self.settings_store else {}
        fmt = str(settings.get("recording_format", "wav")).lower().strip()
        if fmt not in {"wav", "wav49", "gsm", "slin16"}:
            fmt = "wav"
        try:
            max_duration = max(0, int(settings.get("recording_max_duration_seconds", "0")))
        except (TypeError, ValueError):
            max_duration = 0
        try:
            retention = max(1, int(settings.get("recording_retention_days", "90")))
        except (TypeError, ValueError):
            retention = 90
        # Two switches, and both have to agree. The customer decides whether a
        # device records, per extension; the administrator keeps the platform
        # switch, which stops every recording when it is off. Neither replaces
        # the other: the platform switch cannot start a recording, and a
        # customer's opt-in cannot overrule it.
        extension_enabled = False
        if extension and self.settings_store:
            # The console and the API speak the three digits a person dials; the
            # row is stored under its key (`101@+13025550001`). A store that only
            # holds the bare digits still answers, by exact name.
            resolver = getattr(self.settings_store, "extension_row", None)
            row = resolver(extension) if resolver else None
            if row is None:
                # The digits name one extension while exactly one of them carries
                # them; with several, the phone number has to be named.
                rows = self.settings_store.list_extensions()
                if extension_scope(extension):
                    row = next(
                        (item for item in rows
                         if str(item.get("key") or item.get("extension")) == str(extension)),
                        None,
                    )
                else:
                    matches = [
                        item for item in rows
                        if extension_digits(item.get("key") or item.get("extension")) == extension_digits(extension)
                    ]
                    row = matches[0] if len(matches) == 1 else None
            extension_enabled = bool(row and row.get("active") and row.get("recording_enabled"))
        platform_enabled = bool(self.settings_store and self.settings_store.recording_platform_enabled())
        return {
            "enabled": extension_enabled and platform_enabled,
            "platform_enabled": platform_enabled,
            "extension_enabled": extension_enabled,
            "format": fmt,
            "beep": _bool_setting(settings, "recording_beep", False),
            "announcement": _bool_setting(settings, "recording_announcement", False),
            "announcement_media": str(settings.get("recording_announcement_media", "")).strip(),
            "retention_days": retention,
            "max_duration": max_duration,
        }

    @staticmethod
    def _send_webhook(url: str, token: str, payload: dict[str, Any], delivery_id: str | None = None) -> tuple[bool, int | None, str | None]:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        timestamp = str(int(time.time()))
        delivery_id = delivery_id or str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json", "User-Agent": "EngineerIP-Telephony/1.0",
            "X-EngineerIP-Delivery": delivery_id, "X-EngineerIP-Timestamp": timestamp,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
            signature = hmac.new(token.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
            headers["X-EngineerIP-Signature"] = f"sha256={signature}"
        try:
            response = requests.post(url, data=body, headers=headers, timeout=5)
            return response.ok, response.status_code, None if response.ok else f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            return False, None, exc.__class__.__name__

    def _webhook_endpoints(self, event: str, owner_user_id: int | None = None) -> list[dict[str, Any]]:
        endpoints = self.settings_store.list_webhooks(include_tokens=True) if self.settings_store else []
        endpoints = [endpoint for endpoint in endpoints if endpoint.get("owner_user_id") == owner_user_id]
        selected = []
        for endpoint in endpoints:
            subscribed = {item.strip() for item in endpoint["events"].split(",")}
            if endpoint["active"] and ("*" in subscribed or event in subscribed):
                selected.append(endpoint)
        # Keep the environment webhook as a backwards-compatible bootstrap path.
        if not endpoints and self.config.CRM_WEBHOOK_URL:
            selected.append({"id": None, "url": self.config.CRM_WEBHOOK_URL, "token": self.config.CRM_WEBHOOK_TOKEN})
        return selected

    def notify_crm(self, event: str, call: Call | None, extra: dict[str, Any] | None = None) -> None:
        if call is None:
            return
        payload = {"event": event, "call": call.to_dict()}
        if extra:
            payload.update(extra)
        owner = self.settings_store.get_extension_owner(call.extension) if self.settings_store else None
        for endpoint in self._webhook_endpoints(event, owner):
            if endpoint.get("id") is not None and self.settings_store:
                try:
                    self.settings_store.enqueue_webhook(endpoint["id"], event, payload)
                except Exception:
                    # Webhook storage failure must not interrupt a live/paid call.
                    logger.exception("Failed to enqueue webhook event %s", event)
            else:
                self._send_webhook(endpoint["url"], endpoint.get("token", ""), payload)

    def process_webhook_deliveries(self) -> int:
        if not self.settings_store:
            return 0
        delivered = 0
        for item in self.settings_store.pending_webhook_deliveries():
            endpoint = item["endpoint"]
            ok, _, error = self._send_webhook(endpoint["url"], endpoint.get("token", ""), item["payload"], item["id"])
            self.settings_store.finish_webhook_delivery(item["id"], ok, error)
            if not ok and endpoint.get("owner_user_id") and hasattr(self.settings_store, "add_notification"):
                self.settings_store.add_notification(
                    int(endpoint["owner_user_id"]), "webhook_failure", "Webhook delivery failed",
                    f"{endpoint.get('name') or 'Webhook'} could not be delivered. Automatic retries are scheduled.",
                )
            delivered += int(ok)
        return delivered

    def test_webhook(self, webhook_id: int) -> dict[str, Any]:
        endpoints = self.settings_store.list_webhooks(include_tokens=True) if self.settings_store else []
        endpoint = next((item for item in endpoints if item["id"] == webhook_id), None)
        if not endpoint:
            return {"ok": False, "error": "webhook not found"}
        payload = {"event": "webhook.test", "sent_at": iso_now(), "source": "engineerip-telephony"}
        ok, status, error = self._send_webhook(endpoint["url"], endpoint.get("token", ""), payload)
        return {"ok": ok, "status_code": status, "error": error}

    def start_outbound(self, *, phone: str, extension: str, contact_id=None, member_id=None, provider=None, caller_id_number=None) -> Call:
        if not self.ari_ready():
            raise RuntimeError("ARI event worker is not ready")
        source = self.settings_store.get_outbound_number(extension, caller_id_number) if self.settings_store else None
        if self.settings_store and not source:
            raise RuntimeError("No active callback number is assigned to this extension")
        if source and provider and source["provider"] != provider:
            raise RuntimeError("The selected callback number belongs to a different provider")
        provider_endpoint, provider_name = self._provider_endpoint(source["provider"] if source else provider)
        call_id = str(uuid.uuid4())
        call = Call(
            call_id=call_id,
            contact_id=str(contact_id) if contact_id is not None else None,
            member_id=str(member_id) if member_id is not None else None,
            extension=str(extension),
            phone=phone,
            caller_id_number=source["number"] if source else None,
            provider=provider_name,
            employee_channel_id=f"{call_id}-employee",
            status="initiated",
        )
        # Persist the state before originate so an immediate StasisStart/StateChange
        # event can always be correlated by the ARI worker.
        self.store.create(call)
        self.notify_crm("call.started", call)
        # One leg per endpoint the extension's devices live on: the browser of
        # a WebRTC extension rings on its own endpoint and a desk phone on the
        # plain one, each with the media it can speak. Whoever answers first
        # takes the call and the rest are cancelled.
        legs: list[str] = []
        failure: Exception | None = None
        for index, target in enumerate(self.dial_endpoints(extension, source["number"] if source else "")):
            try:
                self.asterisk.create_outbound_call(
                    call_id, extension, phone, provider_endpoint,
                    {"contact_id": contact_id, "member_id": member_id},
                    endpoint=target, index=index,
                )
            except Exception as exc:
                failure = failure or exc
                continue
            leg = f"{call_id}-employee" if index <= 0 else f"{call_id}-employee-{index}"
            legs.append(leg)
            self._leg_index[leg] = call_id
        if not legs:
            current = self.store.get(call_id)
            if current and current.status not in {"completed", "failed"}:
                updated = self.store.update(call_id, status="failed", ended_at=iso_now())
                self.notify_crm("call.failed", updated, {"reason": "asterisk_originate_failed"})
            raise failure or RuntimeError("Asterisk originate failed")
        self.store.update(call_id, employee_channel_ids=",".join(f"{leg}|{extension}" for leg in legs))
        current = self.store.get(call_id)
        if current and current.status == "initiated":
            current = self.store.update(call_id, status="ringing")
            self.notify_crm("call.ringing", current)
        return self.store.get(call_id) or call

    @staticmethod
    def _leg_pairs(call: Call) -> list[tuple[str, str]]:
        """Every rung leg with its extension: `channel|extension` pairs."""
        pairs = []
        for token in str(getattr(call, "employee_channel_ids", "") or "").split(","):
            token = token.strip()
            if not token:
                continue
            channel, _, ext = token.partition("|")
            if channel:
                pairs.append((channel, ext or str(call.extension)))
        if not pairs and call.employee_channel_id:
            pairs.append((str(call.employee_channel_id), str(call.extension)))
        return pairs

    def _call_for_channel(self, channel_id: str) -> Call | None:
        """Resolve a channel to its call, including the extra legs of a ring group."""
        known = self._leg_index.get(str(channel_id))
        if known:
            call = self.store.get(known)
            if call:
                return call
        return self.store.find_by_channel(channel_id)

    def _live_channel_ids(self) -> set[str]:
        try:
            return {str(item.get("id")) for item in self.asterisk.list_channels() if item.get("id")}
        except Exception:
            return set()

    def _legs_still_ringing(self, call: Call) -> list[str]:
        live = self._live_channel_ids()
        return [channel for channel, _ in self._leg_pairs(call) if channel in live]

    def _customer_leg_ids(self, call: Call, channel_id: str = "") -> list[str]:
        """The far-side channels of a call, alive now and other than `channel_id`.

        One extension may be rung on several endpoints (browser and plain), so
        the customer side of a call can be more than one channel. The first is
        the historical `{call}-customer`; the rest are numbered beside it.
        """
        head = str(call.customer_channel_id or "")
        prefix = f"{call.call_id}-customer"
        return [
            channel for channel in self._live_channel_ids()
            if channel != channel_id and (channel == head or channel.startswith(prefix))
        ]

    def _cancel_legs(self, channels: list[str], keep: str = "") -> None:
        for channel in channels:
            if channel and channel != keep:
                self.asterisk.hangup(channel)

    def _hangup_call_channels(self, call: Call, keep: str = "") -> None:
        """Hang up every live channel this call still owns.

        Not just the first leg: a call may ring several devices, and a call
        that ends while some of them still ring leaves orphans. Answering an
        orphan finds the far side long gone and the call cuts the instant it
        is picked up - while the leftover devices keep ringing forever, which
        looks like endless incoming calls.
        """
        for channel, _ in self._leg_pairs(call):
            if channel and channel != keep:
                self._hangup_quietly(channel)
        head = str(call.customer_channel_id or "")
        if head and head != keep:
            self._hangup_quietly(head)
        for channel in self._customer_leg_ids(call, keep):
            self._hangup_quietly(channel)

    def _hangup_quietly(self, channel: str) -> None:
        try:
            self.asterisk.hangup(channel)
        except Exception:
            pass

    # Callers being asked for an extension, keyed by the channel they are on:
    # the prompt they are hearing, the digits collected so far and the clock.
    # In memory on purpose - a session belongs to a live channel.
    _ivr_sessions: dict[str, dict[str, Any]]

    def _ivr_sessions_map(self) -> dict[str, dict[str, Any]]:
        sessions = getattr(self, "_ivr_sessions", None)
        if sessions is None:
            sessions = {}
            self._ivr_sessions = sessions
        return sessions

    def _sequential_sessions_map(self) -> dict[str, dict[str, Any]]:
        """Live sequential-ring state, keyed by call ID, for the ARI worker."""
        sessions = getattr(self, "_sequential_inbound", None)
        if sessions is None:
            sessions = {}
            self._sequential_inbound = sessions
        return sessions

    def _start_next_sequential_destination(self, call_id: str) -> bool:
        """Ring the next destination in a sequential number-flow step.

        Devices belonging to one extension may ring together, but the next
        extension is not started until every device for the current one ends.
        """
        sessions = self._sequential_sessions_map()
        session = sessions.get(str(call_id))
        call = self.store.get(str(call_id))
        if not session or not call or call.ended_at or call.answered:
            sessions.pop(str(call_id), None)
            return False

        destinations = session["destinations"]
        while session["next_index"] < len(destinations):
            destination_index = session["next_index"]
            session["next_index"] += 1
            destination = str(destinations[destination_index])
            endpoints = self.dial_endpoints(destination, session["number"])
            legs: list[str] = []
            timeout = int((session["timeouts"] or {}).get(destination, session["timeout"]))
            for endpoint_index, endpoint in enumerate(endpoints):
                try:
                    leg = self.asterisk.create_inbound_employee_leg(
                        call_id, destination, session["customer_channel_id"],
                        index=destination_index * 10 + endpoint_index, endpoint=endpoint,
                        timeout=timeout,
                    )
                except Exception:
                    continue
                legs.append(str(leg))
                self._leg_index[str(leg)] = str(call_id)
            if not legs:
                continue

            # Old legs have all ended before this method is called. Drop their
            # indexes and keep only the current destination in the call record.
            for old_leg, _ in self._leg_pairs(call):
                self._leg_index.pop(old_leg, None)
            updated = self.store.update(
                str(call_id), extension=destination, status="ringing",
                employee_channel_id=legs[0],
                employee_channel_ids=",".join(f"{leg}|{destination}" for leg in legs),
            )
            self.notify_crm(
                "call.employee_ringing", updated,
                {"sequential_index": destination_index + 1, "destination": destination},
            )
            return True

        sessions.pop(str(call_id), None)
        return False

    # How long a caller has to press the next digit when what they typed could
    # still grow into a longer extension.
    IVR_INTERDIGIT = 1.4
    # What to play when the chosen voice has no recording installed yet: the
    # stock Asterisk prompt asks the caller the same thing, so the menu works
    # before an operator drops in a custom greeting.
    IVR_STOCK_PROMPT = "sound:vm-enter-num-to-call"

    def has_ivr_sessions(self) -> bool:
        """True while a caller is hearing a prompt, so the worker can wake
        promptly instead of every thirty seconds."""
        return bool(self._ivr_sessions_map())

    def _resolve_inbound_extension(self, did: str, extension: str) -> dict | None:
        """The device the dial plan named, by key or by the digits a caller reads.

        The generated plan passes the stored key. A hand-written entry, a plan
        from before this change, or an operator's test passes the three digits -
        and those are read against the number that received the call, which is
        what keeps one line's 101 apart from another's.
        """
        if not self.settings_store:
            return None
        wanted = str(extension or "").strip()
        if not wanted:
            return None
        rows = self.settings_store.list_extensions()
        exact = next((row for row in rows if str(row["key"]) == wanted), None)
        if exact is not None:
            return exact
        number = next(
            (row for row in self.settings_store.list_numbers()
             if str(row["number"]).lstrip("+") == str(did).lstrip("+")),
            None,
        )
        if wanted.isdigit() and number is not None:
            # Three digits are read against the number that received the call -
            # and only against it: another line's 104, the account's lowest line
            # and another customer's desk are all wrong answers here.
            resolved = self.settings_store.resolve_extension(number["number"], wanted, number.get("owner_user_id"))
            if resolved is not None:
                return resolved
        return None

    def start_inbound(self, channel: dict[str, Any], did: str, extension: str) -> Call | None:
        current = self._resolve_inbound_extension(did, extension)
        if current is not None and not current["active"]:
            current = None
        if current is None:
            self.asterisk.hangup(str(channel.get("id") or ""))
            return None
        extension = str(current["key"])
        # The DID decides which organisation this call belongs to, and it may only
        # ring that organisation's phones. A stale link pointing a number at
        # another customer's extension is refused here as well as in the dialplan,
        # so a mis-routed DID is a missed call and never a crossed line.
        number = next(
            (row for row in (self.settings_store.list_numbers() if self.settings_store else [])
             if str(row["number"]).lstrip("+") == str(did).lstrip("+")),
            None,
        )
        if (
            number is not None and current.get("number") not in (None, "")
            and str(current.get("number")) != str(number.get("number"))
        ):
            # A shared DID with no desk of its own may enter through the
            # anchor selected from its explicitly saved, same-owner journey.
            anchor = self.settings_store.number_flow_anchor(str(number["number"]))
            if str(current.get("key")) != anchor:
                self.asterisk.hangup(str(channel.get("id") or ""))
                return None
        if (
            number and number.get("owner_user_id") is not None and current.get("owner_user_id") is not None
            and int(number["owner_user_id"]) != int(current["owner_user_id"])
        ):
            self.asterisk.hangup(str(channel.get("id") or ""))
            return None
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            return None
        existing = self.store.find_by_channel(channel_id)
        if existing:
            return existing
        call_id = str(uuid.uuid4())
        caller = str((channel.get("caller") or {}).get("number") or "unknown")[:32]
        # The link is stored as the key - `101@+13025550001` - so it is the key
        # that says which line this call arrived on, and the record carries that
        # line's own spelling of the number.
        owned = next(
            (row for row in self.settings_store.list_numbers()
             if str(row["inbound_extension"]) == str(current["key"])
             and did.lstrip("+") == row["number"].lstrip("+")),
            None,
        )
        incoming_number = number
        number = owned["number"] if owned else did
        # The stored flow decides what happens next: a menu asks the caller for
        # an extension, anything else rings the devices it names - one device for
        # an extension's own number, every device for a customer's main line, a
        # whole group when the customer built one.
        plan = self.settings_store.inbound_plan(number, extension)
        destinations = list(plan.get("destinations", [extension]))
        call = Call(
            call_id=call_id, contact_id=None, member_id=None, extension=extension, phone=caller,
            caller_id_number=number, provider=(owned or incoming_number or {}).get("provider"),
            direction="inbound", status="ringing", customer_channel_id=channel_id,
            employee_channel_id=f"{call_id}-employee",
        )
        self.store.create(call)
        self.notify_crm("call.started", call)
        if str(plan.get("kind") or "") == "ivr":
            # The menu answers the call; nobody rings until the caller asks for
            # an extension, so there is no employee to report as ringing yet.
            return self._start_ivr(call, channel, plan)
        if plan.get("ring_strategy") == "sequential" and destinations:
            self._sequential_sessions_map()[call_id] = {
                "destinations": destinations,
                "next_index": 0,
                "number": number,
                "customer_channel_id": channel_id,
                "timeout": int(plan.get("timeout") or 30),
                "timeouts": dict(plan.get("timeouts") or {}),
            }
            if self._start_next_sequential_destination(call_id):
                return self.store.get(call_id)
            self._inbound_no_answer(self.store.get(call_id), plan, "inbound_no_ringing_device")
            return self.store.get(call_id)
        self.notify_crm("call.employee_ringing", call)
        legs: list[tuple[str, str]] = []
        ring_timeout = int(plan.get("timeout") or 30)
        desk_timeouts = plan.get("timeouts") or {}
        dial_plan = [
            (destination, target)
            for destination in destinations
            for target in self.dial_endpoints(destination, number)
        ]
        for index, (destination, target) in enumerate(dial_plan):
            try:
                leg = self.asterisk.create_inbound_employee_leg(
                    call_id, destination, channel_id, index=index, endpoint=target,
                    timeout=int(desk_timeouts.get(destination, ring_timeout)),
                )
            except Exception:
                continue
            legs.append((str(leg), destination))
            self._leg_index[str(leg)] = call_id
        if not legs:
            self._inbound_no_answer(self.store.get(call_id), plan, "inbound_no_ringing_device")
            return self.store.get(call_id)
        self.store.update(call_id, employee_channel_id=legs[0][0], employee_channel_ids=",".join(f"{leg}|{ext}" for leg, ext in legs))
        return self.store.get(call_id)

    def _inbound_no_answer(self, call: Call, plan: dict, reason: str = "inbound_not_answered") -> None:
        """One incoming no-answer path, including desks with ringing disabled."""
        self._sequential_sessions_map().pop(call.call_id, None)
        if call.ended_at:
            return
        if call.status not in {"forwarding", "ringing_fallback"} and self._ring_fallbacks(call, plan):
            return
        mailbox = extension_mailbox(plan.get("voicemail") or "")
        if mailbox:
            handed = str(call.customer_channel_id or "")
            try:
                self.asterisk.continue_in_dialplan(handed, "voicemail-inbound", mailbox)
                self.notify_crm("call.voicemail", call, {"reason": reason})
            except Exception:
                logger.exception("Voicemail handoff failed for call %s, mailbox %s", call.call_id, mailbox)
                handed = ""
                self.asterisk.hangup(call.customer_channel_id)
            self._finalize(call.call_id, reason, keep=handed)
            return
        if call.status != "forwarding" and self._forward_call(call, plan):
            return
        self.notify_crm("call.missed", call, {"reason": reason})
        self.asterisk.hangup(call.customer_channel_id)
        self._finalize(call.call_id, reason)

    def _forward_call(self, call: Call, plan: dict) -> bool:
        """Nobody answered the desk: ring the journey's forward number instead.

        The caller is still on the line here - that is why this path hangs them
        up. Instead, the forward number is dialled as one more leg of the same
        call: if it answers it is bridged to the caller exactly like a desk that
        picked up, and when the call finally ends every channel it owns goes
        down together. A forward that cannot be placed lets the ordinary
        missed-call ending carry on. One forward per call: a forward leg that
        dies unanswered never starts another.
        """
        phone = str(plan.get("forward") or "").strip()
        if not phone:
            return False
        timeout = int(plan.get("forward_timeout") or 25)
        try:
            endpoint, _ = self._provider_endpoint(call.provider or "")
            forward_channel = self.asterisk.create_forward_leg(
                call.call_id, phone, endpoint, call.customer_channel_id or "",
                call.caller_id_number, timeout=timeout, index=1,
            )
        except Exception:
            return False
        self._leg_index[str(forward_channel)] = call.call_id
        self.store.update(
            call.call_id,
            status="forwarding",
            employee_channel_id=str(forward_channel),
            employee_channel_ids=f"{forward_channel}|{call.extension}",
        )
        self.notify_crm("call.forwarding", self.store.get(call.call_id), {"phone": phone})
        return True

    def _ring_fallbacks(self, call: Call, plan: dict) -> bool:
        """A rung desk may hand an unanswered call to another desk first.

        The desk's call handling names an unanswered destination; once every
        rung device has died unanswered, that destination is given its own
        chance to pick up - its own ring timeout, its own handling after - and
        only then does the journey's ending (voicemail or forward) apply. One
        round per call: a fallback that rings out never chains again.
        """
        fallbacks = [str(value) for value in (plan.get("fallbacks") or []) if str(value)]
        if not fallbacks:
            return False
        next_plan = {**plan, "destinations": fallbacks}
        if self.settings_store and hasattr(self.settings_store, "apply_desk_handling"):
            row = self.settings_store.extension_row(fallbacks[0])
            next_plan = self.settings_store.apply_desk_handling(next_plan, fallbacks, call.caller_id_number or "", row.get("owner_user_id") if row else None)
        if not next_plan["destinations"]:
            updated = self.store.update(call.call_id, extension=fallbacks[0], status="ringing_fallback")
            self._inbound_no_answer(updated, next_plan)
            return True
        ring_timeout = int(next_plan.get("timeout") or 30)
        desk_timeouts = next_plan.get("timeouts") or {}
        legs: list[tuple[str, str]] = []
        for destination in next_plan["destinations"]:
            for index, target in enumerate(self.dial_endpoints(destination, call.caller_id_number or "")):
                try:
                    leg = self.asterisk.create_inbound_employee_leg(
                        call.call_id, destination, call.customer_channel_id or "",
                        index=10 + len(legs) + index, endpoint=target,
                        timeout=int(desk_timeouts.get(destination, ring_timeout)),
                    )
                except Exception:
                    continue
                legs.append((str(leg), destination))
                self._leg_index[str(leg)] = call.call_id
        if not legs:
            updated = self.store.update(call.call_id, extension=fallbacks[0], status="ringing_fallback")
            self._inbound_no_answer(updated, next_plan)
            return True
        prior = [token for token in str(call.employee_channel_ids or "").split(",") if token]
        pairs = [f"{leg}|{ext}" for leg, ext in legs]
        for old_leg, _ in self._leg_pairs(call):
            self._leg_index.pop(old_leg, None)
        self.store.update(
            call.call_id,
            status="ringing_fallback",
            employee_channel_id=legs[0][0], extension=legs[0][1],
            employee_channel_ids=",".join([*prior, *pairs]),
        )
        self.notify_crm("call.employee_ringing", self.store.get(call.call_id), {"fallbacks": fallbacks})
        return True

    # ---------------------------------------------------------------- the menu
    def _start_ivr(self, call: Call, channel: dict[str, Any], plan: dict[str, Any]) -> Call | None:
        """Answer, play the prompt, and wait for digits."""
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            return None
        self._ivr_sessions_map()[channel_id] = {
            "call_id": call.call_id, "channel_id": channel_id,
            "extension": call.extension or "", "number": call.caller_id_number or "", "digits": "", "tries": 0,
            "attempts": max(1, int(plan.get("attempts") or 2)),
            "input_timeout": max(2, int(plan.get("input_timeout") or 6)),
            # Single-key mappings ("press 1 for the front desk") when the menu
            # has them; without them the caller types the full extension.
            "key_map": dict(plan.get("key_map") or {}),
            # The digits a caller may type on this line, in order. A menu never
            # hears a key: it hears 104, which is that number's 104.
            "keys": [str(value) for value in (plan.get("extensions") or [])],
            "extensions": [extension_digits(value) for value in (plan.get("extensions") or [])],
            "plan": plan, "resolve_after": 0.0,
        }
        self._ivr_prompt(channel_id)
        return call

    def _ivr_prompt(self, channel_id: str) -> None:
        """Answer if needed, play the prompt, and start this attempt's clock."""
        session = self._ivr_sessions_map().get(channel_id)
        if not session:
            return
        media = str(session["plan"].get("media") or "sound:custom/ivr-welcome")
        try:
            self.asterisk.answer_channel(channel_id)
        except Exception:
            pass
        playback_id = f"ivr-{session['call_id']}-{session['tries']}"
        try:
            self.asterisk.play_channel_media(channel_id, media, playback_id=playback_id)
        except Exception:
            # No recording for that voice (yet): the caller still has to hear
            # what is expected of them, so the stock prompt stands in. If even
            # that fails the menu stays silent - the timeout and the
            # retry/fallback path are unchanged either way.
            try:
                self.asterisk.play_channel_media(channel_id, self.IVR_STOCK_PROMPT, playback_id=playback_id)
            except Exception:
                pass
        session["deadline"] = time.monotonic() + session["input_timeout"]
        session["resolve_after"] = 0.0

    def _ivr_digit(self, channel_id: str, digit: str) -> None:
        session = self._ivr_sessions_map().get(channel_id)
        if not session or not digit or not digit.isdigit():
            return
        session["digits"] = (session["digits"] + digit)[:6]
        self._ivr_resolve(session, final=False)

    def _ivr_resolve(self, session: dict[str, Any], final: bool) -> None:
        """Decide what the digits collected so far mean."""
        digits = str(session["digits"])
        if not digits:
            return
        key_map = dict(session.get("key_map") or {})
        if key_map:
            # A key menu resolves the moment the mapped key is pressed; any
            # key that maps nowhere is invalid input, handled by retry/fallback.
            if digits in key_map:
                return self._ivr_dial(session, key_map[digits])
            if not final and any(str(key).startswith(digits) for key in key_map):
                session["resolve_after"] = time.monotonic() + self.IVR_INTERDIGIT
                return
            return self._ivr_retry(session, digits)
        keys = [str(key) for key in (session.get("keys") or [])]
        if not keys and self.settings_store:
            row = self._extension_row(session["extension"])
            owner = row.get("owner_user_id") if row else None
            number = str(session.get("number") or "")
            if owner is not None and number:
                # The menu of one line offers that line's extensions and nothing
                # else: 104 is this number's 104.
                keys = self.settings_store.scoped_extension_keys(number, int(owner))
                session["keys"] = keys
                session["extensions"] = [extension_digits(key) for key in keys]
        extensions = [extension_digits(key) for key in keys]
        candidates = [ext for ext in extensions if ext.startswith(digits)]
        longest = max((len(ext) for ext in extensions), default=0)
        if digits in extensions and (len(candidates) == 1 or final or len(digits) >= longest):
            key = next((item for item in keys if extension_digits(item) == digits), digits)
            return self._ivr_dial(session, key)
        if candidates and not final:
            # "1" cannot be dialled while "101" and "106" both exist: give the
            # caller a moment for the next digit.
            session["resolve_after"] = time.monotonic() + self.IVR_INTERDIGIT
            return
        return self._ivr_retry(session, digits)

    def _ivr_retry(self, session: dict[str, Any], digits: str) -> None:
        """Nothing valid was entered: ask again, or run the fallback."""
        if session["tries"] + 1 < session["attempts"]:
            session["tries"] += 1
            session["digits"] = ""
            self._ivr_prompt(session["channel_id"])
            return
        return self._ivr_fallback(session, digits)

    def _ivr_dial(self, session: dict[str, Any], extension: str) -> None:
        """The caller typed an extension that exists: ring it."""
        channel_id = session["channel_id"]
        self._ivr_sessions_map().pop(channel_id, None)
        self.asterisk.stop_playback(f"ivr-{session['call_id']}-{session['tries']}")
        call = self.store.get(session["call_id"])
        plan = dict(session["plan"])
        plan["destinations"] = [extension]
        if self.settings_store and hasattr(self.settings_store, "apply_desk_handling"):
            row = self.settings_store.extension_row(extension)
            owner = row.get("owner_user_id") if row else None
            plan = self.settings_store.apply_desk_handling(plan, [extension], session.get("number") or "", owner)
        self.store.update(call.call_id, extension=extension)
        if not plan.get("destinations"):
            self._inbound_no_answer(self.store.get(call.call_id), plan)
            return
        legs: list[str] = []
        for index, target in enumerate(self.dial_endpoints(extension, session.get("number") or "")):
            try:
                leg = self.asterisk.create_inbound_employee_leg(
                    session["call_id"], extension, channel_id, index=index, endpoint=target,
                    timeout=int((plan.get("timeouts") or {}).get(extension, plan.get("timeout") or 30)),
                )
            except Exception:
                continue
            legs.append(str(leg))
            self._leg_index[str(leg)] = session["call_id"]
        if not legs:
            self._inbound_no_answer(self.store.get(session["call_id"]), plan, "ivr_extension_unavailable")
            return
        self.store.update(
            session["call_id"], extension=extension, employee_channel_id=legs[0],
            employee_channel_ids=",".join(f"{leg}|{extension}" for leg in legs),
        )
        self.notify_crm("call.ivr_extension_selected", self.store.get(session["call_id"]), {"digits": session["digits"], "extension": extension})

    def _ivr_fallback(self, session: dict[str, Any], digits: str) -> None:
        """No valid extension: the node's own fallback, the rest of the flow, the
        mailbox, or a missed call."""
        channel_id = session["channel_id"]
        self._ivr_sessions_map().pop(channel_id, None)
        self.asterisk.stop_playback(f"ivr-{session['call_id']}-{session['tries']}")
        plan = session["plan"]
        fallback = str(plan.get("fallback") or "")
        if fallback:
            return self._ivr_dial({**session, "digits": digits}, fallback)
        destinations = [str(value) for value in (plan.get("fallback_destinations") or [])]
        if self.settings_store and hasattr(self.settings_store, "apply_desk_handling"):
            number = session.get("number") or ""
            owner = next((row.get("owner_user_id") for row in self.settings_store.list_numbers() if row["number"] == number), None)
            plan = self.settings_store.apply_desk_handling({**plan, "destinations": destinations}, destinations, number, owner)
            destinations = plan["destinations"]
        if destinations:
            legs: list[tuple[str, str]] = []
            dial_plan = [
                (destination, target)
                for destination in destinations
                for target in self.dial_endpoints(destination, session.get("number") or "")
            ]
            for index, (destination, target) in enumerate(dial_plan):
                try:
                    leg = self.asterisk.create_inbound_employee_leg(
                        session["call_id"], destination, channel_id, index=index, endpoint=target,
                    )
                except Exception:
                    continue
                legs.append((str(leg), destination))
                self._leg_index[str(leg)] = session["call_id"]
            if legs:
                self.store.update(
                    session["call_id"], extension=legs[0][1],
                    employee_channel_id=legs[0][0], employee_channel_ids=",".join(f"{leg}|{ext}" for leg, ext in legs),
                )
                self.notify_crm("call.ivr_fallback", self.store.get(session["call_id"]), {"digits": digits})
                return
        call = self.store.get(session["call_id"])
        self._inbound_no_answer(call, plan, "ivr_no_selection")

    def process_ivr_timeouts(self, now: float | None = None) -> int:
        """Called from the worker loop: move every waiting caller along."""
        clock = time.monotonic() if now is None else now
        handled = 0
        for channel_id, session in list(self._ivr_sessions_map().items()):
            if session.get("resolve_after") and clock >= session["resolve_after"]:
                handled += 1
                self._ivr_resolve(session, final=True)
                continue
            if clock < session.get("deadline", 0):
                continue
            handled += 1
            if session["digits"]:
                self._ivr_resolve(session, final=True)
            else:
                self._ivr_retry(session, "")
        return handled

    def hangup(self, call_id: str) -> Call | None:
        call = self.store.get(call_id)
        if not call:
            return None
        self._cancel_legs([leg for leg, _ in self._leg_pairs(call)])
        self.asterisk.hangup_call(call.employee_channel_id, call.customer_channel_id)
        self._finalize(call_id, "hangup_requested")
        return self.store.get(call_id)

    def _platform_line(self, number: str) -> dict | None:
        """The active phone number the platform owns with these digits, whoever holds it.

        Typing a full number reaches it - the same customer's or another's - so
        this is the test for "internally owned" that decides between the platform
        and the carrier. A number the platform does not own returns None.
        """
        store = self.settings_store
        digits = re.sub(r"[^0-9]", "", str(number or ""))
        if not store or not digits:
            return None
        try:
            return next(
                (
                    row for row in store.list_numbers()
                    if row.get("active") and re.sub(r"[^0-9]", "", str(row["number"])) == digits
                ),
                None,
            )
        except Exception:
            return None

    def _local_target(self, number_row: dict) -> str:
        """The extension that answers when a full number of the platform is dialled, or "".

        It is the same answer the dial plan gives for that number
        (TelephonyConfigSync.number_route): the number's own inbound extension,
        then its account's fallback, then the operator's last resort - each
        read against that very number, so a line can never ring another line's
        device. "" means the number has no reachable extension: NOT IN SERVICE,
        never the carrier, because the number is one the platform owns.
        """
        store = self.settings_store
        if not store or not number_row:
            return ""
        number = str(number_row["number"])
        owner = number_row.get("owner_user_id")
        owner_id = int(owner) if str(owner or "").strip().isdigit() else None
        try:
            candidates = [str(number_row.get("inbound_extension") or "")]
            if owner_id is not None:
                candidates.append(str(store.customer_call_defaults(owner_id).get("fallback") or ""))
            candidates.append(str(store.get_settings().get("inbound_fallback_extension") or ""))
            for candidate in candidates:
                found = store.resolve_extension(number, extension_digits(candidate), owner_id)
                if found is not None:
                    return str(found["key"])
        except Exception:
            return ""
        return ""

    def _is_local_extension(self, extension: str, owner: int, number: str = "") -> bool:
        """True when this extension is active, on this number, and this account's."""
        try:
            rows = self.settings_store.list_extensions()
        except Exception:
            return False
        key = str(extension)
        row = next((item for item in rows if str(item["key"]) == key), None)
        if row is None and not extension_scope(key):
            matches = [item for item in rows if item["digits"] == extension_digits(key)]
            row = matches[0] if len(matches) == 1 else None
        if not row or not row["active"]:
            return False
        if number and str(row["number"]) != str(number):
            return False
        found = row.get("owner_user_id")
        return found in (None, "") or str(found) == str(owner)

    def _start_customer(self, call: Call) -> None:
        if call.customer_channel_id or call.status in {"completed", "failed"}:
            return
        # A full number the platform owns - of this customer or of another - is
        # reached on the platform, never over the carrier. If it has no reachable
        # extension it is NOT IN SERVICE, and the call stops here.
        line = self._platform_line(call.phone)
        local = self._local_target(line) if line else ""
        if line is not None and not local:
            updated = self.store.update(call.call_id, status="failed", ended_at=iso_now())
            self.notify_crm("call.failed", updated, {"reason": "number_not_in_service"})
            self.asterisk.hangup(call.employee_channel_id or "")
            return
        customer_channel = f"{call.call_id}-customer"
        prepared = self.store.update(call.call_id, customer_channel_id=customer_channel, status="dialing_customer")
        if not prepared:
            return
        self.notify_crm("call.customer_dialing", prepared)
        try:
            if local:
                created = False
                for index, target in enumerate(self.dial_endpoints(local, call.phone)):
                    try:
                        self.asterisk.create_local_leg(
                            call.call_id, local, call.employee_channel_id or "", call.caller_id_number,
                            endpoint=target, index=index,
                        )
                        created = True
                    except Exception:
                        continue
                if not created:
                    raise RuntimeError("customer_originate_failed")
            else:
                endpoint, _ = self._provider_endpoint(call.provider)
                self.asterisk.create_customer_leg(
                    call.call_id, call.phone, endpoint, call.employee_channel_id or "", call.caller_id_number
                )
        except Exception:
            self.asterisk.hangup(call.employee_channel_id or "")
            updated = self.store.update(call.call_id, status="failed", ended_at=iso_now())
            self.notify_crm("call.failed", updated, {"reason": "customer_originate_failed"})

    def _start_bridge(self, call: Call) -> None:
        current = self.store.get(call.call_id)
        if not current or current.bridge_id or not current.employee_channel_id or not current.customer_channel_id:
            return
        bridge_id = f"bridge-{current.call_id}"
        try:
            self.asterisk.create_bridge(current.call_id)
            self.asterisk.add_channel_to_bridge(bridge_id, current.employee_channel_id)
            self.asterisk.add_channel_to_bridge(bridge_id, current.customer_channel_id)
        except Exception:
            # The bridge itself could not be built: the call cannot run. Take
            # down everything it still owns - every ringing leg included - so
            # nothing keeps ringing a call that no longer exists.
            try:
                self.asterisk.destroy_bridge(bridge_id)
            except Exception:
                pass
            self._hangup_call_channels(current)
            updated = self.store.update(current.call_id, status="failed", ended_at=iso_now(), bridge_id=None, recording_status="failed")
            self.notify_crm("call.failed", updated, {"reason": "bridge_setup_failed"})
            return
        updated = self.store.update(current.call_id, bridge_id=bridge_id, status="bridged")
        self.notify_crm("call.bridged", updated)
        # Everything below is enhancement on top of a live, bridged call. A
        # failure here - a recorder that cannot write, a notification that
        # cannot queue - must never hang up the people already talking: the
        # recording is marked failed and the call carries on.
        try:
            recording = self._recording_settings(updated.extension)
            if not recording["enabled"]:
                return
            name = f"call-{updated.call_id}"
            # Persist intent first: a fast finish event must already be
            # correlatable, and a failed start must remain visible in history.
            updated = self.store.update(updated.call_id, recording_name=name, recording_format=recording["format"], recording_status="recording")
            self.asterisk.start_bridge_recording(bridge_id, name, recording["format"], recording["beep"], recording["max_duration"])
            self.notify_crm("call.recording_started", updated)
            if recording["announcement"] and recording["announcement_media"]:
                try:
                    self.asterisk.play_bridge_media(bridge_id, recording["announcement_media"])
                except Exception:
                    self.notify_crm("call.recording_announcement_failed", updated)
        except Exception:
            logger.exception("Recording setup failed for call %s", current.call_id)
            updated = self.store.update(current.call_id, recording_status="failed")
            self.notify_crm("call.recording_failed", updated)

    def _finalize(self, call_id: str, reason: str, keep: str = "") -> None:
        self._sequential_sessions_map().pop(str(call_id), None)
        with self._finalize_lock:
            if call_id in self._finalizing:
                return
            self._finalizing.add(call_id)
        try:
            call = self.store.get(call_id)
            if not call or call.status == "completed":
                return
            ended = iso_now()
            # Publish the transition before hangup/stop can emit a finish event.
            # The SQL guard preserves a finish event that already arrived.
            if call.recording_name:
                self.store.mark_recording_finalizing(call_id)
            # The call is over: nothing it owns may keep ringing. Anything
            # still up is an orphan of the same call and answers into a void.
            # `keep` names a channel deliberately handed on - a caller leaving
            # a voicemail message is still on the line, talking to a mailbox.
            self._hangup_call_channels(call, keep=keep)
            duration_start = call.answered_at or call.started_at
            try:
                started = datetime.fromisoformat(duration_start)
                duration = max(0, int((datetime.fromisoformat(ended) - started).total_seconds()))
            except (ValueError, TypeError):
                duration = 0
            recording_status = call.recording_status
            if call.recording_name and recording_status == "recording":
                self.asterisk.stop_recording(call.recording_name)
            if call.bridge_id:
                self.asterisk.destroy_bridge(call.bridge_id)
            terminal_status = "failed" if call.status == "failed" else ("completed" if call.answered else "failed")
            updated = self.store.update(call_id, status=terminal_status, ended_at=ended, duration_seconds=duration)
            if terminal_status == "completed":
                self.notify_crm("call.completed", updated, {"reason": reason})
            else:
                self.notify_crm("call.failed", updated, {"reason": "call_ended_before_answer" if reason == "channel_destroyed" else reason})
                if call.direction == "inbound" and self.settings_store and hasattr(self.settings_store, "add_notification"):
                    number = next((row for row in self.settings_store.list_numbers() if row["number"] == call.caller_id_number), None)
                    if number and number.get("owner_user_id"):
                        self.settings_store.add_notification(
                            int(number["owner_user_id"]), "missed_call", "Missed call",
                            f"Missed inbound call from {call.phone} to {call.caller_id_number}.",
                        )
        finally:
            with self._finalize_lock:
                self._finalizing.discard(call_id)

    def cleanup_recordings(self) -> None:
        """Delete managed recordings using the persisted call end time.

        ARI's StoredRecording model exposes name and format, but no creation/completion
        timestamp, so retention cannot safely be calculated from the ARI listing alone.
        Calls are persisted in the configured database, making ended_at the authoritative retention clock.
        """
        retention_days = self._recording_settings()["retention_days"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        for call in self.store.all():
            if not call.recording_name or not call.ended_at or call.recording_status == "deleted":
                continue
            try:
                ended = datetime.fromisoformat(call.ended_at)
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if ended >= cutoff:
                continue
            if self.asterisk.delete_stored_recording(call.recording_name):
                updated = self.store.update(
                    call.call_id,
                    recording_status="deleted",
                    recording_path=None,
                )
                self.notify_crm("call.recording_deleted", updated, {"reason": "retention_policy"})

    def handle_ari_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type in {"RecordingFinished", "RecordingFailed"}:
            recording = event.get("recording") or {}
            name = str(recording.get("name") or "")
            call = self.store.find_by_recording(name) if name else None
            if call:
                changes: dict[str, Any] = {
                    "recording_status": "finalized" if event_type == "RecordingFinished" else "failed"
                }
                if recording.get("format") and not call.recording_format:
                    changes["recording_format"] = str(recording["format"])
                if event_type == "RecordingFinished":
                    stored = self.asterisk.get_stored_recording(name) if name else None
                    if stored:
                        filename = stored.get("filename")
                        if filename:
                            changes["recording_path"] = str(filename)
                        if stored.get("format") and not call.recording_format:
                            changes["recording_format"] = str(stored["format"])
                updated = self.store.update(call.call_id, **changes)
                self.notify_crm(f"call.recording_{'finished' if event_type == 'RecordingFinished' else 'failed'}", updated)
            return

        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            return
        if event_type == "ChannelDtmfReceived":
            # A caller answering the menu: no call lookup needed, the session
            # already knows which call the channel belongs to.
            self._ivr_digit(str(channel_id), str(event.get("digit") or ""))
            return
        call = self._call_for_channel(channel_id)
        if not call and event_type == "StasisStart":
            args = event.get("args") or []
            if len(args) >= 3 and args[0] == "inbound":
                self.start_inbound(channel, str(args[1]), str(args[2]))
            return
        if not call:
            return

        if event_type == "StasisStart":
            if channel_id == call.employee_channel_id or channel_id in [leg for leg, _ in self._leg_pairs(call)]:
                current = self.store.get(call.call_id)
                if current and current.status == "initiated":
                    updated = self.store.update(call.call_id, status="ringing")
                    self.notify_crm("call.employee_ringing", updated)
            return

        if event_type == "ChannelStateChange":
            if str(channel.get("state", "")).lower() != "up":
                return
            current = self.store.get(call.call_id)
            if not current:
                return
            if current.status in {"completed", "failed"}:
                # A device of a call that already ended picked up. There is
                # nothing to bridge it to; hang the straggler up instead of
                # building a bridge to a far side that is long gone.
                self._hangup_quietly(str(channel_id))
                return
            employee_legs = [leg for leg, _ in self._leg_pairs(current)]
            if current.direction == "inbound" and channel_id in employee_legs:
                self._sequential_sessions_map().pop(current.call_id, None)
                answered_extension = next((ext for leg, ext in self._leg_pairs(current) if leg == channel_id), current.extension)
                others = [leg for leg in employee_legs if leg != channel_id]
                updated = self.store.update(
                    current.call_id, status="employee_answered", employee_channel_id=channel_id,
                    extension=answered_extension or current.extension,
                )
                self.notify_crm("call.employee_answered", updated)
                # Whoever picks up first takes the call; the other devices stop ringing.
                self._cancel_legs(others)
                self._start_bridge(updated)
                updated = self.store.get(current.call_id)
                if updated and updated.status != "failed" and not updated.answered:
                    updated = self.store.update(updated.call_id, status="answered", answered=True, answered_at=iso_now())
                    self.notify_crm("call.answered", updated)
            elif (channel_id in employee_legs or channel_id == current.employee_channel_id) and not current.customer_channel_id:
                # An employee device answered the panel's call: ring the far
                # side - once - and stop the employee's other devices.
                others = [leg for leg in employee_legs if leg != channel_id]
                self._cancel_legs(others)
                updated = self.store.update(current.call_id, status="employee_answered", employee_channel_id=channel_id)
                self.notify_crm("call.employee_answered", updated)
                self._start_customer(updated)
            elif channel_id == current.customer_channel_id or channel_id.startswith(f"{current.call_id}-customer"):
                # The far side answered. An internal destination rings on every
                # endpoint of the extension - browser and plain phone - so the
                # rest are cancelled and the bridge names the one that answered.
                self._cancel_legs(self._customer_leg_ids(current, channel_id))
                if channel_id != current.customer_channel_id:
                    current = self.store.update(current.call_id, customer_channel_id=channel_id) or current
                self._start_bridge(current)
                updated = self.store.get(current.call_id)
                if updated and updated.status != "failed" and not updated.answered:
                    updated = self.store.update(updated.call_id, status="answered", answered=True, answered_at=iso_now())
                    self.notify_crm("call.answered", updated)
            return

        if event_type == "ChannelDestroyed":
            # All ring legs can disappear before their queued events arrive.
            # After a handoff the first event has ended our ownership; another
            # leg must not continue the caller out of VoiceMail a second time
            # (ARI rejects it, and the transfer-error path would hang them up).
            if call.ended_at is not None:
                self._leg_index.pop(str(channel_id), None)
                return
            # A caller who gives up during the prompt leaves nothing behind.
            self._ivr_sessions_map().pop(str(channel_id), None)
            legs = [leg for leg, _ in self._leg_pairs(call)]
            employee_leg = channel_id in legs or channel_id == (call.employee_channel_id or "")
            customer_leg = (not employee_leg) and (
                channel_id == (call.customer_channel_id or "") or channel_id.startswith(f"{call.call_id}-customer")
            )
            # A device that lost the race to answer is cancelled once the call
            # runs on another channel. Its destroy is bookkeeping, not a hangup -
            # tearing the live call down here used to drop the call the moment
            # the other phone stopped ringing.
            if call.answered and (
                (employee_leg and channel_id != call.employee_channel_id)
                or (customer_leg and channel_id != call.customer_channel_id)
            ):
                self._leg_index.pop(str(channel_id), None)
                return
            if (employee_leg or customer_leg) and not call.answered:
                remaining = (
                    [leg for leg in self._legs_still_ringing(call) if leg != channel_id]
                    if employee_leg else self._customer_leg_ids(call, channel_id)
                )
                if remaining:
                    # Other devices are still ringing: the call is not missed yet.
                    self._leg_index.pop(str(channel_id), None)
                    return
                self._leg_index.pop(str(channel_id), None)
                if employee_leg and call.direction == "inbound" and call.customer_channel_id:
                    if self._start_next_sequential_destination(call.call_id):
                        return
                    # Nobody picked up. A journey that ends in voicemail keeps
                    # the caller; one that ends in a forward number rings it
                    # with the caller still waiting; the default flow simply
                    # ends the call.
                    plan = self.settings_store.inbound_plan(call.caller_id_number or "", call.extension) if self.settings_store else {}
                    if (plan.get("kind") == "ivr" or call.status == "ringing_fallback") and self.settings_store:
                        row = self.settings_store.extension_row(call.extension)
                        plan = self.settings_store.apply_desk_handling(plan, [call.extension], call.caller_id_number or "", row.get("owner_user_id") if row else None)
                    self._inbound_no_answer(call, plan)
                    return
                for leg in legs:
                    self._leg_index.pop(leg, None)
                self._hangup_call_channels(call, keep=channel_id)
                self._finalize(call.call_id, "channel_destroyed")
                return
            for leg in legs:
                self._leg_index.pop(leg, None)
            other = call.customer_channel_id if employee_leg else call.employee_channel_id
            if other and other != channel_id:
                self.asterisk.hangup(other)
            self._finalize(call.call_id, "channel_destroyed")

    def reconcile_recordings(self) -> None:
        """Recover missed finish events using positive evidence from ARI.

        A missing stored response may mean ARI is down or still flushing; it
        must not be interpreted as a failed recording or downgrade a live one.
        """
        for call in self.store.stale_recordings():
            if not call.ended_at:
                continue
            try:
                stored = self.asterisk.get_stored_recording(call.recording_name or "")
            except Exception:
                continue
            if stored:
                updated = self.store.update(
                    call.call_id, recording_status="finalized",
                    recording_format=stored.get("format") or call.recording_format,
                    recording_path=stored.get("filename") or call.recording_path,
                )
                self.notify_crm("call.recording_finished", updated, {"reason": "reconciled"})

    def recover_incomplete_calls(self) -> None:
        """Reconcile persisted calls with channels that survived an ARI worker restart."""
        try:
            self.reconcile_recordings()
        except Exception:
            logger.exception("Recording reconciliation during recovery failed")
        try:
            live = {str(channel.get("id")): channel for channel in self.asterisk.list_channels() if channel.get("id")}
        except Exception:
            return
        for call in self.store.all():
            if call.status in {"completed", "failed"} or call.ended_at is not None:
                continue
            # Re-register every leg of a ring group so its events still land here.
            for leg, _ in self._leg_pairs(call):
                self._leg_index[leg] = call.call_id
            employee = live.get(call.employee_channel_id or "")
            customer = live.get(call.customer_channel_id or "") if call.customer_channel_id else None
            if not employee and not customer:
                updated = self.store.update(call.call_id, status="failed", ended_at=iso_now())
                self.notify_crm("call.failed", updated, {"reason": "worker_restart_no_live_channel"})
                continue
            if employee and not customer and str(employee.get("state", "")).lower() == "up" and not call.customer_channel_id:
                self._start_customer(call)
                continue
            if customer and employee and str(customer.get("state", "")).lower() == "up":
                current = self.store.get(call.call_id)
                if current and not current.bridge_id:
                    self._start_bridge(current)
                current = self.store.get(call.call_id)
                if current and current.status != "failed" and not current.answered:
                    updated = self.store.update(current.call_id, status="answered", answered=True, answered_at=iso_now())
                    self.notify_crm("call.answered", updated)
