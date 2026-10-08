from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .admin import SettingsStore, extension_digits, extension_mailbox, extension_scope, same_account
from .ami import AsteriskAMI


class TelephonyConfigSync:
    """Render DB-managed SIP and DID objects into internal Asterisk include files."""

    def __init__(self, store: SettingsStore, ami: AsteriskAMI, path: str):
        self.store = store
        self.ami = ami
        self.path = Path(path)
        self.dialplan_path = self.path.with_name("extensions.dynamic.conf")
        self.voicemail_path = self.path.with_name("voicemail.dynamic.conf")
        self.transports_path = self.path.with_name("pjsip.transports.conf")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _id(prefix: str, value: str) -> str:
        return f"{prefix}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"

    @classmethod
    def _section_name(cls, extension: Any, plain_names: dict[str, str] | None = None) -> str:
        """The PJSIP section name for an extension key.

        Section names cannot contain `@` (Asterisk parses it as a key/value pair),
        so a scoped extension is named by its identity unless it is the one that
        owns the plain three-digit name.
        """
        key = str(extension or "")
        if extension_scope(key) == "":
            return key
        digits = extension_digits(key)
        # The one extension that owns the plain three-digit name is reached by
        # it - that is the name every hand-written dial plan and every device
        # account already used. The rest are named by their identity, which
        # carries no `@`.
        return digits if (plain_names or {}).get(digits) == key else extension_mailbox(key)

    @staticmethod
    def _clean(value: Any) -> str:
        text = str(value or "")
        if "\n" in text or "\r" in text or ";" in text or "#" in text:
            raise ValueError("Telephony configuration contains an invalid character")
        return text

    @staticmethod
    def _codecs(value: str) -> str:
        allowed = {"ulaw", "alaw", "opus", "g722", "gsm", "slin", "slin16"}
        codecs = [item.strip().lower() for item in value.split(",") if item.strip()]
        if not codecs or any(codec not in allowed for codec in codecs):
            raise ValueError("Unsupported SIP codec configuration")
        if not ({"ulaw", "alaw"} & set(codecs)):
            raise ValueError("Provider must allow ulaw or alaw to interoperate with internal extensions")
        return ",".join(dict.fromkeys(codecs))

    @staticmethod
    def _transport(value: str) -> str:
        value = value.strip().lower()
        if value not in {"udp", "tcp"}:
            raise ValueError("Provider transport must be udp or tcp")
        return value

    @staticmethod
    def tenant_context(owner_user_id: Any) -> str:
        """The dialplan context the phones of one account answer in.

        Extension numbers are three digits and unique platform-wide, but a
        customer's phone must only ever reach the extensions of its own account
        - whichever DID they belong to - plus the platform's own lines. That is
        a property of the dialplan context, not of a per-call check, so another
        customer's extension is not merely refused: it does not exist here.
        """
        owner = str(owner_user_id if owner_user_id is not None else "").strip()
        return f"from-internal-{int(owner)}" if owner.isdigit() else "from-internal"

    @staticmethod
    def _same_owner(left: Any, right: Any) -> bool:
        return same_account(left, right)

    def _extension_owners(self) -> dict[str, Any]:
        """extension -> the customer account that owns it (None = the platform's)."""
        return {str(row["extension"]): row.get("owner_user_id") for row in self.store.list_extensions()}

    def _extension_reachable_by(self, extension: str, owner: Any, owners: dict[str, Any], scope: str = "") -> bool:
        """May a device belonging to `owner` ring this extension?

        Yes for the platform's own lines and for every extension of the same
        customer account; no for another customer's - so dialling an extension
        number can never drop a call on a different organisation's desk. With a
        `scope` (the DID being answered) the rule tightens to that line: an
        extension that belongs to another of the account's numbers is not on
        this line, so it does not answer here.
        """
        if owner is None or str(owner).strip() == "":
            return True
        extension_owner = owners.get(str(extension))
        if extension_owner is None or str(extension_owner).strip() == "":
            return True
        if not self._same_owner(extension_owner, owner):
            return False
        own_scope = extension_scope(extension)
        return not (scope and own_scope and own_scope != str(scope))

    @staticmethod
    def _allowed_ips(value: str) -> list[str]:
        result = []
        for item in str(value or "").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                result.append(str(ipaddress.ip_network(item, strict=False)))
            except ValueError as exc:
                raise ValueError(f"Invalid provider IP/CIDR: {item}") from exc
        return list(dict.fromkeys(result))

    @staticmethod
    def _valid_extension(value: str) -> bool:
        return value.isdigit() and 100 <= int(value) <= 999

    def _fallback_extension(self, configured: str, active_extensions: list[str]) -> str:
        """The platform-wide last resort, as the key of an extension that exists."""
        if configured and any(extension_digits(key) == configured for key in active_extensions):
            return next(key for key in active_extensions if extension_digits(key) == configured)
        if active_extensions:
            return active_extensions[0]
        return "101"

    def _outbound_identity(self, extension: str) -> tuple[str, str]:
        """Caller ID and carrier trunk a phone on this extension dials out
        with: its default outbound DID and that number's provider endpoint.
        Empty strings mean the extension has no number - direct outbound
        calls from it are refused by the dialplan."""
        assigned = self.store.get_outbound_number(extension)
        if not assigned:
            return "", ""
        provider = None
        if assigned.get("provider"):
            provider = self.store.get_provider(str(assigned["provider"]))
        if provider is None:
            provider = self.store.get_provider()
        if not provider:
            return "", ""
        return self._clean(str(assigned["number"])), self._id("provider", self._clean(str(provider["name"])))

    # Traffic to these destinations is never NAT-rewritten to the external
    # address (Docker networks and RFC1918 LANs).
    LOCAL_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

    # What an extension - desk phone, softphone or the browser - may use, in
    # Asterisk's preference order. G.722 is wideband ("HD voice", 16 kHz) and
    # is built into Asterisk, so an internal call between two devices that both
    # support it is HD without transcoding. PCMU/PCMA stay on the list so a
    # device, or a carrier that cannot do wideband, still gets a normal call
    # instead of a failed one. chan_pjsip's incoming_call_offer_pref defaults
    # to "local", which keeps this order when the far end offers its own.
    INTERNAL_CODECS = "g722,ulaw,alaw"

    def external_address(self) -> str:
        """Single source of truth for the public SIP/RTP address: the admin
        panel's Service address (service_host). ASTERISK_EXTERNAL_ADDRESS in
        .env is only the bootstrap fallback until it is configured."""
        configured = self._clean(self.store.get_settings().get("service_host") or "").strip()
        return configured or self._clean(os.environ.get("ASTERISK_EXTERNAL_ADDRESS", "")).strip()

    def render_transports(self) -> str:
        external = self.external_address()
        lines = [
            "; AUTO-GENERATED by EngineerIP Telephony. Do not edit manually.",
            "; The public address comes from the admin panel (Settings -> Service",
            "; address); ASTERISK_EXTERNAL_ADDRESS in .env is only a bootstrap",
            "; fallback. Transport changes are applied when Asterisk restarts.",
            "",
        ]
        for proto in ("udp", "tcp"):
            lines.extend([f"[transport-{proto}]", "type=transport", f"protocol={proto}", "bind=0.0.0.0:5060"])
            if external:
                lines.extend([
                    f"external_media_address={external}",
                    f"external_signaling_address={external}",
                    *[f"local_net={net}" for net in self.LOCAL_NETS],
                ])

        # Browser softphones use Asterisk's HTTP/WebSocket server on 8089.
        # The WSS transport does not bind its own TCP socket; it is carried
        # by res_http_websocket through the HTTPS listener. Signalling needs no
        # public address here (the browser is already connected through the
        # reverse proxy), but the *media* does: ICE candidates for a WebRTC call
        # are built from this transport, so without external_media_address the
        # browser is told to send audio to the container's private address and
        # the call stays silent.
        lines.extend([
            "[transport-wss]",
            "type=transport",
            "protocol=wss",
            "bind=0.0.0.0",
        ])
        if external:
            lines.extend([
                f"external_media_address={external}",
                *[f"local_net={net}" for net in self.LOCAL_NETS],
            ])
        lines.append("")
        return "\n".join(lines)

    def _auth_digest_lines(self) -> list[str]:
        """Digest algorithm(s) Asterisk offers when challenging a phone,
        chosen by the administrator (Settings -> Server addresses).

        md5 (default) omits the option entirely - Asterisk challenges with
        plain MD5, which every softphone supports. sha256 and both emit the
        matching supported_algorithms_uas value; note that some clients
        (e.g. Zoiper 5) abandon a challenge that contains SHA-256."""
        choice = str(self.store.get_settings().get("sip_auth_digest") or "md5").strip().lower()
        if choice == "sha256":
            return ["supported_algorithms_uas=SHA-256"]
        if choice == "both":
            return ["supported_algorithms_uas=SHA-256,MD5"]
        return []

    def render_pjsip(self) -> str:
        lines = [
            "; AUTO-GENERATED by EngineerIP Telephony. Do not edit manually.",
            "; This file contains SIP credentials and is shared only on the internal Docker network.",
            "",
        ]
        # A customer SIP account linked to an extension becomes that extension's
        # live PJSIP credential, so credentials shown in the portal really ring.
        sip_accounts = [row for row in self.store.list_sip_accounts(include_password=True) if row["active"]]
        sip_by_extension = {str(row["extension"]): row for row in sip_accounts if row.get("extension")}
        # Admin-chosen digest challenge (md5/sha256/both), same for every account.
        auth_digest = self._auth_digest_lines()
        configured_extensions: set[str] = set()
        # Which extension keeps the plain three-digit endpoint name. The first
        # row with those digits keeps the name `101` - so a hand-written dial
        # plan, a device account and everything an operator already wrote about
        # it go on working - and a second 101 (another line, or another account)
        # answers on `101-<its number>`, because a PJSIP section name may not
        # contain the `@` its key does.
        all_extensions = self.store.list_extensions()
        plain_names = self.store.plain_endpoint_names()
        for ext in all_extensions:
            if not ext["active"]:
                continue
            extension = self._clean(ext["extension"])
            digits = extension_digits(extension)
            linked = sip_by_extension.get(extension)
            username = self._clean(linked["sip_username"] if linked else ext["sip_username"])
            password = self._clean(linked["sip_password"] if linked else self.store.get_extension_password(extension))
            transport = self._transport(linked["transport"] if linked else "udp")
            configured_extensions.add(extension)
            # A phone signs in with its prefixed SIP username (e.g. AUHFZH_101)
            # in the From/To headers and the Authorization header, while the
            # dialplan dials the endpoint by extension number. PJSIP's
            # username/auth_username identifiers match against the endpoint
            # NAME, so three objects make the login work:
            #   - an AOR named after the username (the registrar resolves the
            #     To-user against the matched endpoint's AOR list),
            #   - an alias ENDPOINT named after the username sharing the same
            #     auth and AORs (so REGISTER/INVITE from the phone are matched
            #     and authenticated at all),
            #   - the canonical [extension] endpoint the dialplan dials; its
            #     aors list includes the alias AOR, so it rings the phone
            #     wherever the contact was registered.
            outbound_cid, outbound_trunk = self._outbound_identity(extension)
            # The device dials in the context of the account that owns it.
            context = self.tenant_context(ext.get("owner_user_id"))
            # Endpoint names may not contain `@`: a PJSIP section name with one is
            # parsed as a key/value pair. The dial plan and ARI both dial this
            # section name - see `_section_name` and `SettingsStore.endpoint_name`.
            section = self._section_name(extension, plain_names)
            aors = [section]
            alias_aor = []
            if username != section:
                aors.append(username)
                alias_aor = [f"[{username}]", "type=aor", "max_contacts=5", "remove_existing=yes", ""]
            # The endpoint the dial plan rings by default: plain RTP, so the
            # hardware phones and softphones the credentials are shared with can
            # answer it. A browser cannot use this media at all, which is why an
            # extension that answers in the browser is dialled on its WebRTC
            # endpoint instead - see `web_endpoints` in render_dialplan.
            endpoint_body = [
                f"context={context}", "disallow=all", f"allow={self.INTERNAL_CODECS}", f"transport=transport-{transport}",
                "direct_media=no", "rtp_symmetric=yes", "force_rport=yes", "rewrite_contact=yes",
                "identify_by=username,auth_username",
                f"set_var=OUTBOUND_CID={outbound_cid}", f"set_var=OUTBOUND_TRUNK={outbound_trunk}",
                f"allow_subscribe={'yes' if ext.get('voicemail_enabled') else 'no'}",
                *([f"mailboxes={extension}@engineerip"] if ext.get("voicemail_enabled") else []),
            ]
            alias_endpoint = []
            if username != extension:
                # The generated SIP username is already an AOR/endpoint alias.
                # Keep the normal UDP/TCP endpoint untouched for Zoiper and
                # hardware phones, and make the alias WebRTC-capable for the
                # browser. Both endpoints use the same auth and AOR contacts, so
                # a phone signed in on the browser is reached whichever of the
                # two is dialled - but only this one carries the WebRTC media.
                browser_endpoint_body = [
                    line for line in endpoint_body
                    if not line.startswith(("transport=", "allow="))
                ]
                browser_endpoint_body = [
                    *browser_endpoint_body,
                    "transport=transport-wss",
                    "webrtc=yes",
                    f"allow={self.INTERNAL_CODECS}",
                ]
                alias_endpoint = [
                    f"[{username}]", "type=endpoint", f"aors={','.join(aors)}", f"auth=auth-{section}",
                    f"callerid=<{digits}>", *browser_endpoint_body, "",
                ]
            lines.extend([
                f"; Extension {extension}",
                f"[{section}]", "type=aor", "max_contacts=5", "remove_existing=yes", "",
                *alias_aor,
                f"[auth-{section}]", "type=auth", "auth_type=userpass",
                f"username={username}", f"password={password}", *auth_digest, "",
                f"[{section}]", "type=endpoint", f"aors={','.join(aors)}", f"auth=auth-{section}",
                *endpoint_body, "",
                *alias_endpoint,
            ])

        extension_owners = self._extension_owners()
        for account in sip_accounts:
            extension = self._clean(str(account.get("extension") or ""))
            if extension and extension in configured_extensions:
                # Already rendered above as that extension's own identity.
                continue
            if extension and extension not in {row["extension"] for row in all_extensions}:
                extension = ""
            endpoint = self._id("device", str(account["sip_username"]))
            username = self._clean(account["sip_username"])
            password = self._clean(account["sip_password"])
            transport = self._transport(account["transport"])
            # A device answers in its own account's context even when the
            # extension it was keyed to is gone or belongs somewhere else, and it
            # may only present the caller ID of an extension of that account.
            account_owner = account.get("owner_user_id")
            extension_owner = extension_owners.get(extension) if extension else None
            foreign_extension = bool(
                extension and account_owner not in (None, "") and extension_owner not in (None, "")
                and not self._same_owner(extension_owner, account_owner)
            )
            scope_owner = account_owner if foreign_extension or extension_owner in (None, "") else extension_owner
            context = self.tenant_context(scope_owner)
            if foreign_extension:
                extension = ""
            # Same registration contract as extensions: the device signs in
            # with its SIP username, so that name must exist both as an AOR
            # (for the registrar) and as an alias ENDPOINT (for endpoint
            # identification - PJSIP matches usernames against endpoint
            # names, and this endpoint's own name is an internal hash).
            outbound_cid, outbound_trunk = self._outbound_identity(extension) if extension else ("", "")
            device_body = [
                f"context={context}", "disallow=all", f"allow={self.INTERNAL_CODECS}", f"transport=transport-{transport}",
                "direct_media=no", "rtp_symmetric=yes", "force_rport=yes", "rewrite_contact=yes",
                "identify_by=username,auth_username",
                f"set_var=OUTBOUND_CID={outbound_cid}", f"set_var=OUTBOUND_TRUNK={outbound_trunk}",
            ]
            lines.extend([
                f"; Customer device {username}",
                f"[{endpoint}]", "type=aor", "max_contacts=5", "remove_existing=yes", "",
                f"[{username}]", "type=aor", "max_contacts=5", "remove_existing=yes", "",
                f"[auth-{endpoint}]", "type=auth", "auth_type=userpass", f"username={username}", f"password={password}", *auth_digest, "",
                f"[{endpoint}]", "type=endpoint", f"aors={endpoint},{username}", f"auth=auth-{endpoint}",
                *device_body, "",
                f"[{username}]", "type=endpoint", f"aors={endpoint},{username}", f"auth=auth-{endpoint}",
                *device_body, "",
            ])

        for provider in self.store.list_provider_details():
            if not provider["active"]:
                continue
            name = self._clean(provider["name"])
            server = self._clean(provider["server"])
            username = self._clean(provider["username"])
            password = self._clean(provider["password"])
            port = int(provider["port"])
            if not 1 <= port <= 65535:
                raise ValueError("Provider port is invalid")
            transport = self._transport(provider["transport"])
            codecs = self._codecs(provider["codecs"])
            endpoint = self._id("provider", name)
            auth = f"{endpoint}-auth"
            registration = f"{endpoint}-reg"
            did = self.store.first_active_number_for_provider(name)
            did_user = re.sub(r"[^0-9]", "", did or "")
            lines.extend([
                f"; SIP provider {name}", f"[{endpoint}]", "type=endpoint", f"transport=transport-{transport}",
                "context=from-provider", "disallow=all", f"allow={codecs}", f"outbound_auth={auth}",
                f"aors={endpoint}", "direct_media=no", "rtp_symmetric=yes", "force_rport=yes", "rewrite_contact=yes",
                "send_pai=yes", "send_rpid=yes", "trust_id_outbound=yes",
                f"from_user={username}", f"from_domain={server}", "",
                f"[{auth}]", "type=auth", "auth_type=userpass", f"username={username}", f"password={password}", "",
                f"[{endpoint}]", "type=aor", f"contact=sip:{server}:{port}", "qualify_frequency=60", "",
                f"[{registration}]", "type=registration", f"transport=transport-{transport}", f"outbound_auth={auth}",
                f"server_uri=sip:{server}:{port}", f"client_uri=sip:{username}@{server}",
                *([f"contact_user={did_user}"] if did_user else []), "retry_interval=30",
                "forbidden_retry_interval=300", "expiration=300", "",
            ])
            allowed_ips = self._allowed_ips(provider.get("allowed_ips", ""))
            if not allowed_ips:
                raise ValueError(f"Provider {name} has no inbound IP/CIDR allowlist")
            for index, match in enumerate(allowed_ips, 1):
                lines.extend([
                    f"[{endpoint}-identify-{index}]", "type=identify", f"endpoint={endpoint}", f"match={match}", "",
                ])
        return "\n".join(lines) + "\n"

    def render_voicemail(self) -> str:
        lines = [
            "; AUTO-GENERATED EngineerIP voicemail mailboxes.",
            "[engineerip]",
        ]
        for extension in self.store.list_extensions():
            if not extension["active"] or not extension.get("voicemail_enabled"):
                continue
            key = self._clean(extension["extension"])
            # The mailbox is read off the sheet and typed into *97, so it stays
            # the digits wherever those alone are unambiguous: only a second
            # extension with the same digits on another number gets the longer
            # name. The stored pin is the same either way.
            mailbox = extension_mailbox(key)
            if mailbox != extension_digits(key):
                self.store.alias_voicemail_pin(mailbox, extension_digits(key))
            pin = self._clean(self.store.get_voicemail_pin(mailbox) or self.store.get_voicemail_pin(key))
            display_name = self._clean(extension.get("display_name") or f"Extension {extension_digits(key)}")
            lines.append(f"{mailbox} => {pin},{display_name},,,attach=no|delete=no")
        lines.append("")
        return "\n".join(lines)

    def render_dialplan(self) -> str:
        """The dial plan: which digits ring which device, and in whose context.

        Two rules shape everything here.

        Extensions belong to a *phone number*: 101 is the first device of a line
        and the rest follow. A call arriving on a DID rings that number's own
        extensions, the account-wide ones and the platform's - never another
        number's, and never another organisation's.

        Inside one account, extension numbers are platform-wide: a phone at
        101 dials 117 and reaches the 117 of that same organisation, whichever
        number it sits on. That is what keeps dialling between teams simple, and
        it is why the context carries the shared numbers and the DID carries the
        line's own.
        """
        settings = self.store.get_settings()
        extensions = [row for row in self.store.list_extensions() if row["active"]]
        active_exts = [row["extension"] for row in extensions]
        voicemail_exts = {row["extension"] for row in extensions if row.get("voicemail_enabled")}
        owners = self._extension_owners()

        def key_for(number, digits: Any) -> str:
            """The stored key that answers these digits on this number."""
            key = self.store.extension_key_for(str(number or ""), digits, None)
            return key or str(digits)

        def key_for_owner(number, digits: Any, owner: Any) -> str:
            owner_id = int(owner) if str(owner or "").strip().isdigit() else None
            key = self.store.extension_key_for(str(number or ""), digits, owner_id)
            return key or str(digits)
        default_ext = self._fallback_extension(str(settings.get("default_extension", "")).strip(), active_exts)
        inbound_fallback = self._fallback_extension(
            str(settings.get("inbound_fallback_extension", "")).strip(), active_exts
        )

        def did_extension(number) -> str:
            """Which extension answers this DID when its own link is missing or stale.

            Each customer chooses their own fallback, so an unassigned DID belonging
            to one customer can never land on another customer's phone. The
            platform-wide values below are only a last resort for rows written
            before customers owned this decision.
            """
            owner = number.get("owner_user_id")
            if owner is not None:
                customer = self.store.customer_call_defaults(int(owner)).get("fallback", "")
                if customer and customer in active_exts:
                    return customer
            return inbound_fallback

        # Dial-by-extension is decided by context. [from-internal] is the
        # platform's own number space - the operator's devices and every row
        # written before extensions had an owner - and each customer account gets
        # `from-internal-<owner>` holding that account's own extensions, every
        # one of them whichever DID it belongs to, plus the platform's lines.
        # Another customer's extension is not merely refused there: it is absent,
        # so dialling 117 can never ring somebody else's desk.
        # [from-internal] is the platform's own number space: the operator's
        # devices, and any row written before extensions had an owner.
        platform_exts = [
            row["extension"] for row in extensions
            if row.get("owner_user_id") in (None, "") and not extension_scope(row["extension"])
        ]
        # One context per *account*, holding every extension of every number it
        # owns, so its phones reach each other by extension. Which line a device
        # is on does not change who it may call inside the organisation.
        tenant_exts: dict[int, list[str]] = {}
        tenant_by_digits: dict[int, dict[str, str]] = {}
        for row in extensions:
            if row.get("owner_user_id") in (None, ""):
                continue
            owner_id = int(row["owner_user_id"])
            tenant_exts.setdefault(owner_id, []).append(row["extension"])
            digits = str(row["digits"])
            existing = tenant_by_digits.setdefault(owner_id, {})
            if digits not in existing or not extension_scope(existing[digits]):
                # A line's own extension wins over an account-wide row with the
                # same digits: it is the more specific answer.
                existing[digits] = str(row["extension"])
        # A device that is not linked to an extension still answers in its own
        # account's context, so that context has to exist even without extensions.
        for account in self.store.list_sip_accounts():
            owner = account.get("owner_user_id")
            if owner not in (None, "") and str(owner).strip().isdigit() and int(owner) not in tenant_exts:
                tenant_exts[int(owner)] = []

        def number_extension(number) -> str:
            """The extension key that answers when this number is called.

            One answer for both ways in: a call arriving from the carrier and a
            call dialled internally reach the same device. The number's own link
            is read against this very number, so 101 is this line's 101; then
            that customer's own fallback, then the platform's last resort.
            """
            candidates = (
                key_for(number.get("number"), str(number["inbound_extension"] or "")),
                key_for(number.get("number"), did_extension(number)),
                key_for(number.get("number"), inbound_fallback),
            )
            for candidate in candidates:
                if (
                    candidate and candidate in active_exts
                    and self._extension_reachable_by(
                        candidate, number.get("owner_user_id"), owners, extension_scope(number.get("number"))
                    )
                ):
                    return candidate
            return ""

        # An extension that answers in the browser (WebRTC enabled) has to be
        # dialled on its WebRTC endpoint: media is negotiated by the endpoint the
        # call is dialled *towards*, so a plain RTP offer to a browser arrives
        # with no audio, and a WebRTC offer sent to a hardware phone is refused.
        # Only extensions the operator marked are affected; everything else is
        # dialled exactly as before.
        web_endpoints: dict[str, str] = {}
        for row in self.store.list_extensions():
            username = self._clean(row.get("sip_username"))
            if row.get("active") and row.get("webrtc_enabled") and username and username != str(row["extension"]):
                web_endpoints[str(row["extension"])] = username
        # The endpoint a device is reached by. A key holds `@`, which a PJSIP
        # section name cannot, so the dial plan dials the name: the plain digits
        # for the extension that owns them, its identity (`101-13025550098`)
        # otherwise, and the WebRTC alias for a browser phone.
        endpoint_names = self.store.plain_endpoint_names()

        def dial_target(extension: str, endpoint: str = "") -> str:
            if endpoint:
                return endpoint
            return self._section_name(extension, endpoint_names)

        def ring_extension(extension: str, spoken: str = "") -> list[str]:
            """What it takes to ring one extension, wherever it was reached from.

            `spoken` is the digits the caller dialled, which is what the mailbox
            is named after. A registered phone that does not answer is left to
            ring out, or to that extension's voicemail. An extension with nobody
            signed in gets a spoken answer instead of the silence a failed Dial
            leaves behind - which is what a caller otherwise hears when the phone
            they are dialling is simply not registered yet.
            """
            digits = spoken or extension_digits(extension)
            target = dial_target(str(extension), web_endpoints.get(str(extension), ""))
            mailbox = extension_mailbox(extension)
            if extension in voicemail_exts:
                return [
                    f" same => n,Dial(PJSIP/{target},30)",
                    f' same => n,ExecIf($["${{DIALSTATUS}}" != "ANSWER"]?VoiceMail({mailbox}@engineerip,u))',
                    " same => n,Hangup()",
                ]
            return [
                f" same => n,Dial(PJSIP/{target},30)",
                ' same => n,ExecIf($["${DIALSTATUS}" = "CHANUNAVAIL"]?Playback(ss-noservice))',
                " same => n,Hangup()",
            ]

        def extension_route(extension: str, dialed: str) -> list[str]:
            """One dialable number in this context, ringing one device.

            `dialed` is what the phone dials (the three digits), `extension` is
            the device that answers it, and they differ whenever a line has its
            own set: dialling 101 rings that line's 101, not another line's.
            """
            return [
                f"exten => {dialed},1,NoOp(EngineerIP extension {dialed})",
                *ring_extension(extension, spoken=dialed),
            ]

        def local_number_route(pattern: str, extension: str) -> list[str]:
            """Dialling one of the organisation's own numbers, from inside.

            The platform looks the number up when it renders this file and rings
            the extension that number is set to ring - the same destination the
            carrier would have reached - so a call between two of a customer's
            own numbers never leaves through the carrier.
            """
            return [
                f"exten => {pattern},1,NoOp(EngineerIP local number {pattern} rings extension {extension_digits(extension)})",
                *ring_extension(extension),
            ]

        local_numbers = [number for number in self.store.list_numbers() if number["active"]]

        def local_number_routes(owner_id: int | None = None) -> list[str]:
            """One account's numbers - or every number - as internal dialling.

            Rendered as literals, which Asterisk prefers over the outbound
            patterns below, so a number dialled by the organisation that owns it
            rings that number's extension instead of being handed to the trunk.
            Another organisation's number is not rendered here at all, so
            dialling it is the ordinary external call it is.
            """
            lines: list[str] = []
            for number in local_numbers:
                if owner_id is not None and not self._same_owner(number.get("owner_user_id"), owner_id):
                    continue
                extension = number_extension(number)
                digits = re.sub(r"[^0-9]", "", str(number["number"]))
                if not extension or not digits:
                    continue
                for pattern in (digits, f"+{digits}"):
                    lines.extend(local_number_route(pattern, extension))
            return lines

        voicemail_login = [
            "exten => *97,1,NoOp(EngineerIP voicemail login)",
            " same => n,VoiceMailMain(@engineerip)",
            " same => n,Hangup()",
        ]
        # A three-digit number that is not in this context is nobody's extension
        # here - another organisation's or a typo. An exact extension or a more
        # specific pattern always wins over this catch-all, so a future short code
        # only has to be added above it.
        unknown_extension = [
            "exten => _XXX,1,NoOp(${EXTEN} is not an extension of this organisation)",
            " same => n,Playback(ss-noservice)",
            " same => n,Hangup()",
        ]
        # Direct outbound dialing from a registered phone. The calling endpoint
        # carries its own caller ID and carrier trunk as channel variables
        # (set_var in pjsip, see render_pjsip) - an endpoint without an assigned
        # number is politely refused instead of leaking another customer's
        # identity. Every context needs these, customers included.
        outbound: list[str] = []
        for pattern in ("_+X.", "_XXXX."):
            outbound.extend([
                f"exten => {pattern},1,NoOp(Outbound ${{EXTEN}} from endpoint ${{CHANNEL(endpoint)}})",
                ' same => n,GotoIf($["${OUTBOUND_TRUNK}"]=""]?blocked)',
                " same => n,Set(CALLERID(num)=${OUTBOUND_CID})",
                " same => n,Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)",
                " same => n,Hangup()",
                " same => n(blocked),NoOp(No outbound number assigned to endpoint ${CHANNEL(endpoint)})",
                " same => n,Playback(ss-noservice)",
                " same => n,Hangup()",
            ])

        lines = [
            "; AUTO-GENERATED EngineerIP DID, extension and voicemail routing.",
            ";",
            "; Extensions belong to phone numbers: every number starts its own set at",
            "; 101, so a call arriving on a DID rings that number's own extension 101",
            "; (and 102, 103 ... as they are added), the account-wide extensions and",
            "; the platform's - never another number's.",
            ";",
            "; Inside one customer account the extension numbers are platform-wide:",
            "; dialling 104 from any of the account's phones reaches the 104 device it",
            "; owns, whichever DID that device sits on. Another account's extensions are",
            "; not merely refused - they are absent from the context.",
            ";",
            "; A number that is one of the caller's own is dialled the same way: the",
            "; literal routes below ring the extension that number is set to ring, so a",
            "; call between two of a customer's numbers never leaves through the carrier.",
            ";",
            "; [from-internal]: the platform's own devices; they may ring any extension.",
            "[from-internal]",
            *voicemail_login,
        ]
        # The platform's own devices may ring every extension on the platform,
        # the way the operator's line always has. Each three-digit number is
        # rendered once: an account-wide row with those digits answers, and where
        # every row with them belongs to a number the first one does - the
        # operator's line is a single context with no DID of its own to scope to.
        platform_dialable: dict[str, str] = {}
        for row in extensions:
            digits = str(row["digits"])
            current = platform_dialable.get(digits)
            if current is None or (not extension_scope(current) and extension_scope(row["extension"])):
                platform_dialable[digits] = str(row["extension"])
        for digits in sorted(platform_dialable, key=lambda value: int(value) if value.isdigit() else 0):
            lines.extend(extension_route(platform_dialable[digits], digits))
        lines.extend(local_number_routes())
        lines.extend(unknown_extension)
        lines.extend(outbound)
        for owner_id in sorted(tenant_exts):
            context = self.tenant_context(owner_id)
            lines.extend([
                "",
                f"; {context}: every extension of customer account {owner_id}, plus the platform's.",
                f"[{context}]",
                *voicemail_login,
            ])
            # One route per dialable three-digit number. Where a line has its own
            # extension with those digits, that is what answers - so a phone here
            # reaching 104 reaches the 104 of this organisation (as the customer
            # asked), while a DID arriving at 104 reaches its own line's 104.
            dialable: dict[str, str] = dict(tenant_by_digits.get(owner_id, {}))
            for key in platform_exts:
                dialable.setdefault(extension_digits(key), key)
            for digits in sorted(dialable, key=int):
                lines.extend(extension_route(dialable[digits], digits))
            lines.extend(local_number_routes(owner_id))
            lines.extend(unknown_extension)
            lines.extend(outbound)
        lines.extend(["", "[voicemail-inbound]"])
        for extension in sorted(voicemail_exts):
            mailbox = extension_mailbox(extension)
            lines.extend([
                f"exten => {mailbox},1,VoiceMail({mailbox}@engineerip,u)",
                " same => n,Hangup()",
            ])
        lines.extend(["", "[from-provider]"])
        for number in self.store.list_numbers():
            if not number["active"]:
                continue
            did = re.sub(r"[^0-9]", "", number["number"])
            if not did:
                continue
            # A DID rings an extension of the organisation it belongs to - the
            # device its own link names on this very number, that customer's
            # fallback, or the platform's last resort.
            extension = number_extension(number)
            for dialed_number in (did, f"+{did}"):
                if not extension:
                    lines.extend([
                        f"exten => {dialed_number},1,NoOp(Inbound DID {dialed_number} has no reachable extension)",
                        " same => n,Playback(ss-noservice)",
                        " same => n,Hangup()",
                    ])
                    continue
                lines.extend([
                    f"exten => {dialed_number},1,NoOp(Inbound DID {dialed_number} owned by extension {extension_digits(extension)})",
                    f" same => n,Stasis(engineerip,inbound,{dialed_number},{extension})",
                    *([f" same => n,VoiceMail({extension_mailbox(extension)}@engineerip,u)"] if extension in voicemail_exts else []),
                    " same => n,Hangup()",
                ])
        lines.extend([
            "exten => s,1,NoOp(Inbound provider call ${CALLERID(all)})",
            f" same => n,Dial(PJSIP/{dial_target(default_ext)},30)",
            *([f' same => n,ExecIf($["${{DIALSTATUS}}" != "ANSWER"]?VoiceMail({extension_mailbox(default_ext)}@engineerip,u))'] if default_ext in voicemail_exts else []),
            " same => n,Hangup()",
            "",
        ])
        return "\n".join(lines)

    def _atomic_write(self, path: Path, content: str) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def apply(self) -> None:
        self._atomic_write(self.transports_path, self.render_transports())
        self._atomic_write(self.path, self.render_pjsip())
        self._atomic_write(self.dialplan_path, self.render_dialplan())
        self._atomic_write(self.voicemail_path, self.render_voicemail())
        self.ami.reload_pjsip()
        self.ami.reload_voicemail()
        self.ami.reload_dialplan()
