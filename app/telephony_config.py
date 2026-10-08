from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .admin import (
    SettingsStore, endpoint_name as extension_endpoint, extension_digits, extension_key, extension_mailbox,
    extension_scope, same_account,
)
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
    def _section_name(cls, extension: Any) -> str:
        """The PJSIP section name of one extension: its globally unique identity.

        `101-13025550001` - digits plus the line they belong to, so two 101s can
        never collide and `PJSIP/101` is never ambiguous. A section name cannot
        contain `@` (Asterisk parses it as a key/value pair), which is why the
        identity is not the stored key.
        """
        return extension_endpoint(extension)

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
    def number_context(number: Any) -> str:
        """The dialplan context the devices of one phone number answer in.

        A three-digit extension is resolved only inside the current phone number,
        so the context is the number, not the account and not the platform.
        Dialling 104 on +13025550001 rings that line's 104; the 104 of another
        number of the same customer, or of another customer, is not merely
        refused here - it does not exist in this context.
        """
        digits = re.sub(r"[^0-9]", "", str(number or ""))
        return f"from-number-{digits}" if digits else "from-internal"

    @staticmethod
    def tenant_context(owner_user_id: Any) -> str:
        """The context a device with no phone number of its own answers in.

        The platform's own devices; the dial plan renders that context from the
        platform's legacy rows and numbers, and a customer's device always has a
        number context instead.
        """
        return "from-internal"

    @staticmethod
    def _same_owner(left: Any, right: Any) -> bool:
        return same_account(left, right)

    def _extension_owners(self) -> dict[str, Any]:
        """extension key -> the customer account that owns it (None = the platform's)."""
        return {str(row["key"]): row.get("owner_user_id") for row in self.store.list_extensions()}

    def _extension_reachable_by(self, extension: str, number: Any, owners: dict[str, Any]) -> bool:
        """May a call on this phone number ring this extension?

        One answer: only when the extension belongs to that very number and to
        the account that holds the number. Another number's 101 - another of the
        customer's lines, or another customer's - is not reachable here, which is
        what makes a missing extension NOT IN SERVICE instead of somebody else's
        desk.
        """
        if str(number or "").strip() == "":
            # A platform-owned DID with no line of its own: the platform's rows.
            return owners.get(str(extension)) in (None, "")
        return next(
            (
                item for item in self.store.list_extensions()
                if str(item["key"]) == str(extension) and item["number"] == str(number)
            ),
            None,
        ) is not None

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

    def _fallback_digits(self, configured: str) -> str:
        """The digits an operator configured as a last resort, as digits.

        They are read against the current phone number like any other three
        digits: they name a device only where that number has such an extension.
        """
        digits = extension_digits(configured)
        return digits if digits.isdigit() else ""

    def _outbound_identity(self, extension: str, number: str = "") -> tuple[str, str]:
        """Caller ID and carrier trunk one device dials out with.

        The caller ID is the *phone number* the device answers on - 101 on
        +13025550001 presents that line, 101 on +13025550002 presents the other
        one - so it travels with the line, not with the digits. A device account
        with its own `number` presents that line. Empty strings mean the device
        has no number at all: the dial plan refuses its outbound calls instead of
        leaking somebody else's identity.
        """
        assigned = self.store.active_number(number) if str(number or "").strip() else self.store.get_outbound_number(extension)
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
        # A device account answers for one extension of one phone number: the
        # pair is what identifies it, because two numbers may both hold a 101.
        sip_by_extension = {
            (extension_digits(row["extension"]), str(row.get("phone_number") or "")): row
            for row in sip_accounts if row.get("extension")
        }
        sip_by_digits: dict[str, Any] = {}
        for row in sip_accounts:
            if row.get("extension"):
                sip_by_digits.setdefault(extension_digits(row["extension"]), row)
        # Admin-chosen digest challenge (md5/sha256/both), same for every account.
        auth_digest = self._auth_digest_lines()
        configured_extensions: set[str] = set()
        # Every extension answers on its globally unique identity: the digits plus
        # the phone number they belong to (`101-13025550001`). `PJSIP/101` is not
        # ambiguous because nothing is named `101` unless it is a platform row -
        # and a platform row answers only in the operator's own context.
        all_extensions = self.store.list_extensions()
        for ext in all_extensions:
            if not ext["active"]:
                continue
            extension = self._clean(ext["key"])
            digits = extension_digits(extension)
            scope = str(ext["number"] or "")
            linked = sip_by_extension.get((digits, scope)) or (sip_by_digits.get(digits) if not scope else None)
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
            outbound_cid, outbound_trunk = self._outbound_identity(extension, scope)
            # The device dials in the context of the phone number it answers on,
            # so a three-digit extension is resolved inside that number only. A
            # platform row (the operator's own phone) answers in the operator's
            # context, which is where its digits are unique.
            context = self.number_context(scope) if scope else self.tenant_context(ext.get("owner_user_id"))
            # Endpoint names may not contain `@`: a PJSIP section name with one is
            # parsed as a key/value pair. The dial plan and ARI both dial this
            # section name - see `_section_name` and `SettingsStore.endpoint_name`.
            section = self._section_name(extension)
            mailbox = str(ext["mailbox"])
            aors = [section]
            alias_aor: list[str] = []
            if username and username != section:
                # The registration identity: a phone signs in with its SIP
                # username, and the registrar matches that against an AOR, so the
                # AOR exists whatever the WebRTC switch says - a hardware phone
                # or softphone must keep registering with the credential on its
                # sheet.
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
                *([f"mailboxes={mailbox}@engineerip"] if ext.get("voicemail_enabled") else []),
            ]
            alias_endpoint = []
            if ext.get("webrtc_enabled") and username and username != section:
                # A browser needs a WebRTC-capable endpoint. It is created only
                # for an extension whose WebRTC switch is on, so switching WebRTC
                # on - or off - never touches the plain UDP/TCP endpoint above:
                # hardware phones and softphones keep registering and answering
                # either way. Both endpoints share the same auth and AORs, so a
                # device signed in on the browser is reached whichever of the two
                # is dialled.
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
            phone = self._clean(str(account.get("phone_number") or ""))
            if extension and extension_key(extension_digits(extension), phone) in configured_extensions:
                # Already rendered above as that extension's own identity.
                continue
            if extension and not any(
                row["digits"] == extension_digits(extension) and (not phone or row["number"] == phone)
                for row in all_extensions
            ):
                extension = ""
            endpoint = self._id("device", str(account["sip_username"]))
            username = self._clean(account["sip_username"])
            password = self._clean(account["sip_password"])
            transport = self._transport(account["transport"])
            # A device answers in its own account's context even when the
            # extension it was keyed to is gone or belongs somewhere else, and it
            # may only present the caller ID of an extension of that account.
            account_owner = account.get("owner_user_id")
            extension_owner = extension_owners.get(extension_key(extension_digits(extension), phone)) if extension else None
            foreign_extension = bool(
                extension and account_owner not in (None, "") and extension_owner not in (None, "")
                and not self._same_owner(extension_owner, account_owner)
            )
            scope_owner = account_owner if foreign_extension or extension_owner in (None, "") else extension_owner
            # A device keyed to an extension dials in that extension's number
            # context; one with no extension keeps the account's context (its own
            # number's, falling back to the platform's).
            device_number = phone or ""
            if not device_number and extension and not foreign_extension:
                device_number = str(self.store.primary_number(int(scope_owner)) if str(scope_owner or "").strip().isdigit() else "")
            context = self.number_context(device_number) if device_number else self.tenant_context(scope_owner)
            if foreign_extension:
                extension = ""
            # Same registration contract as extensions: the device signs in
            # with its SIP username, so that name must exist both as an AOR
            # (for the registrar) and as an alias ENDPOINT (for endpoint
            # identification - PJSIP matches usernames against endpoint
            # names, and this endpoint's own name is an internal hash).
            outbound_cid, outbound_trunk = self._outbound_identity(extension, phone) if extension else ("", "")
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
            key = self._clean(extension["key"])
            # Every extension has its own mailbox, and two 101s on different
            # numbers have two separate boxes: the mailbox is the digits plus the
            # number they belong to (`101-13025550001`), never the digits alone
            # and never `101@engineerip`. The pin is the one stored for the row.
            mailbox = extension_mailbox(key)
            self.store.alias_voicemail_pin(mailbox, extension_digits(key))
            pin = self._clean(self.store.get_voicemail_pin(mailbox) or self.store.get_voicemail_pin(key))
            display_name = self._clean(extension.get("display_name") or f"Extension {extension_digits(key)}")
            lines.append(f"{mailbox} => {pin},{display_name},,,attach=no|delete=no")
        lines.append("")
        return "\n".join(lines)

    def render_dialplan(self) -> str:
        """The dial plan: which digits ring which device, and in which context.

        One rule shapes everything here: *a three-digit extension is resolved
        only within the current phone number*. Every number renders its own
        context, holding exactly its own extension set, so 104 on +13025550001
        rings that line's 104 - and a line that has no 104 plays NOT IN SERVICE,
        never another number's 104, the account's lowest line, or another
        customer's desk.

        Reaching another line is done by dialling its full number. Any number the
        platform owns - of the same customer or of another one - is routed
        internally, straight to the extension or flow that number answers with,
        and never leaves through the carrier. A number's other extensions are
        reached as `<number>*<digits>` by its own customer only. Everything the
        platform does not own is an ordinary outbound call, placed with the caller
        ID of the number the device answers on.
        """
        settings = self.store.get_settings()
        extensions = [row for row in self.store.list_extensions() if row["active"]]
        voicemail_exts = {row["key"] for row in extensions if row.get("voicemail_enabled")}
        numbers = [number for number in self.store.list_numbers() if number["active"]]
        default_digits = self._fallback_digits(str(settings.get("default_extension", "")).strip())
        fallback_digits = self._fallback_digits(str(settings.get("inbound_fallback_extension", "")).strip())

        def dialable(number) -> list[dict]:
            """The extensions of one phone number, in dialling order."""
            text = str((number or {}).get("number") or "").strip()
            rows = [row for row in extensions if row["number"] == text]
            return sorted(rows, key=lambda row: int(row["digits"]) if row["digits"].isdigit() else 0)

        def key_for(number, digits: Any) -> str:
            """The extension that answers these digits on this number, or ""."""
            text = str((number or {}).get("number") or "").strip()
            return next(
                (str(row["key"]) for row in extensions
                 if row["number"] == text and row["digits"] == extension_digits(digits)),
                "",
            )

        def number_extension(number) -> str:
            """The extension that answers when this number is called.

            One answer for both ways in - a call arriving from the carrier and a
            call dialled internally reach the same device. The number's own link
            is read against this very number, then that customer's own fallback,
            then the operator's configured last resort; every candidate is a
            three-digit number *of this line*, so a line with none of them is
            simply not in service rather than somebody else's extension.
            """
            candidates = [str(number.get("inbound_extension") or "")]
            owner = number.get("owner_user_id")
            if owner is not None:
                candidates.append(str(self.store.customer_call_defaults(int(owner)).get("fallback") or ""))
            candidates.append(fallback_digits)
            for candidate in candidates:
                key = key_for(number, candidate)
                if key:
                    return key
            return ""

        # An extension that answers in the browser (WebRTC enabled) is dialled on
        # its WebRTC alias endpoint: media is negotiated by the endpoint the call
        # is dialled *towards*, so a plain RTP offer to a browser arrives with no
        # audio. Only extensions whose switch is on are affected - and their plain
        # endpoint keeps working, so hardware phones are never disabled by it.
        web_endpoints: dict[str, str] = {}
        for row in self.store.list_extensions():
            username = self._clean(row.get("sip_username"))
            if row.get("active") and row.get("webrtc_enabled") and username and username != extension_endpoint(str(row["key"])):
                web_endpoints[str(row["key"])] = username

        def dial_target(key: str) -> str:
            return web_endpoints.get(str(key)) or extension_endpoint(key)

        def ring_extension(key: str, spoken: str = "") -> list[str]:
            """What it takes to ring one extension, wherever it was reached from.

            `spoken` is what the caller dialled, which is what the mailbox is
            named after. A registered phone that does not answer is left to ring
            out, or to that extension's voicemail. An extension with nobody
            signed in gets a spoken answer instead of the silence a failed Dial
            leaves behind.
            """
            target = dial_target(key)
            mailbox = extension_mailbox(key)
            if key in voicemail_exts:
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

        def extension_route(dialed: str, key: str) -> list[str]:
            """One dialable three-digit number in this context, ringing one device."""
            return [
                f"exten => {dialed},1,NoOp(EngineerIP extension {dialed} on {extension_scope(key) or 'the platform'})",
                *ring_extension(key, spoken=dialed),
            ]

        def number_route(number, pattern: str) -> list[str]:
            """A full number of the same customer, dialled from inside.

            The platform rings whatever that number answers with - the same
            destination the carrier would have reached - so a call between two of
            a customer's own numbers never leaves the platform. A number that
            answers with nothing is not in service here either.
            """
            key = number_extension(number)
            if not key:
                return [
                    f"exten => {pattern},1,NoOp(EngineerIP local number {pattern} has no reachable extension)",
                    " same => n,Playback(ss-noservice)",
                    " same => n,Hangup()",
                ]
            return [
                f"exten => {pattern},1,NoOp(EngineerIP local number {pattern} rings extension {extension_digits(key)})",
                *ring_extension(key),
            ]

        def number_extension_route(number, pattern: str, row: dict) -> list[str]:
            """`<number>*<digits>`: one of a number's own extensions, by a caller of its account."""
            digits = str(row["digits"])
            return [
                f"exten => {pattern}*{digits},1,NoOp(EngineerIP {pattern}*{digits} rings extension {digits} on {number['number']})",
                *ring_extension(str(row["key"]), spoken=digits),
            ]

        def local_number_routes(context_owner: Any) -> list[str]:
            """Every number the platform owns, dialled by its full number from this context.

            Typing a full number reaches that number whoever holds it - a number of
            the same customer or of another one - and never leaves through the
            carrier. The caller reaches the number's own inbound destination (the
            extension or flow it answers with, see number_route). A number's other
            extensions are reached as `<number>*<digits>`, and only by a caller of
            that number's own account; for another account those digits are NOT IN
            SERVICE, so one customer's desks are never dialable by another.
            """
            lines: list[str] = []
            for number in numbers:
                digits = re.sub(r"[^0-9]", "", str(number["number"]))
                if not digits:
                    continue
                same = self._same_owner(number.get("owner_user_id"), context_owner)
                for pattern in (digits, f"+{digits}"):
                    lines.extend(number_route(number, pattern))
                    if same:
                        for row in dialable(number):
                            lines.extend(number_extension_route(number, pattern, row))
                    else:
                        lines.extend([
                            f"exten => _{pattern}*X.,1,NoOp(EngineerIP {pattern}* is another customer's extension: not dialable)",
                            " same => n,Playback(ss-noservice)",
                            " same => n,Hangup()",
                        ])
            return lines

        voicemail_login = [
            "exten => *97,1,NoOp(EngineerIP voicemail login)",
            " same => n,VoiceMailMain(@engineerip)",
            " same => n,Hangup()",
        ]
        # A three-digit number that is not on this line is nobody's extension
        # here: another line's, another customer's or a typo. This is the
        # NOT IN SERVICE answer, and an exact extension or a more specific
        # pattern always wins over it.
        unknown_extension = [
            "exten => _XXX,1,NoOp(${EXTEN} is not an extension of this phone number)",
            " same => n,Playback(ss-noservice)",
            " same => n,Hangup()",
        ]
        # Direct outbound dialling from a registered phone. The endpoint carries
        # its own caller ID and carrier trunk as channel variables (set_var in
        # pjsip, see render_pjsip), and they are the *current phone number's* - so
        # 101 on +13025550001 and 101 on +13025550002 present their own line. A
        # device with no number is politely refused instead of leaking another
        # customer's identity.
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
            "; A three-digit extension is resolved only within the current phone",
            "; number: every number renders its own context from 101 up, so 104 on",
            "; +13025550001 rings that line's 104 and a line without a 104 plays",
            "; NOT IN SERVICE. No context falls back to another number, to the",
            "; account's lowest line, or to another customer's extensions.",
            ";",
            "; A number of the same customer is dialled by its full number and rings",
            "; internally, straight to that number's own extension or flow - of this",
            "; customer or of another one. A number's other extensions are dialled as",
            "; <number>*<digits> by its own customer only. Anything the platform does",
            "; not own is dialled out through the carrier with the caller ID of the",
            "; number the device answers on.",
            ";",
            "; [from-internal]: the platform's own devices; any platform number is reachable.",
            "[from-internal]",
            *voicemail_login,
        ]
        # The operator's own extensions: no customer, no line, so their digits
        # are unique in this one context.
        for row in sorted(
            (item for item in extensions if not item["number"] and item.get("owner_user_id") in (None, "")),
            key=lambda item: int(item["digits"]) if item["digits"].isdigit() else 0,
        ):
            lines.extend(extension_route(str(row["digits"]), str(row["key"])))
        lines.extend(local_number_routes(None))
        lines.extend(unknown_extension)
        lines.extend(outbound)
        for number in numbers:
            context = self.number_context(number["number"])
            owner = number.get("owner_user_id")
            who = "the platform" if owner in (None, "") else f"customer account {int(owner)}"
            lines.extend([
                "",
                f"; {context}: the extensions of {number['number']} ({who}), and nothing else.",
                f"[{context}]",
                *voicemail_login,
            ])
            for row in dialable(number):
                lines.extend(extension_route(str(row["digits"]), str(row["key"])))
            # Full numbers of the same customer first: a literal beats the
            # outbound patterns below, so dialling one of the customer's own
            # numbers never reaches the carrier.
            lines.extend(local_number_routes(int(owner) if str(owner or "").strip().isdigit() else None))
            lines.extend(unknown_extension)
            lines.extend(outbound)
        lines.extend(["", "[voicemail-inbound]"])
        for key in sorted(voicemail_exts):
            mailbox = extension_mailbox(key)
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
            # A DID rings an extension of the organisation it belongs to: the
            # device its own link names on this very number, that customer's
            # fallback, or the operator's last resort - never another line's 101.
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
                    f"exten => {dialed_number},1,NoOp(Inbound DID {dialed_number} rings extension {extension_digits(extension)})",
                    f" same => n,Stasis(engineerip,inbound,{dialed_number},{extension})",
                    *([f" same => n,VoiceMail({extension_mailbox(extension)}@engineerip,u)"] if extension in voicemail_exts else []),
                    " same => n,Hangup()",
                ])
        # A call from the carrier that names no DID of ours: the operator's own
        # last-resort extension, which has no number of its own.
        default_key = next(
            (str(row["key"]) for row in extensions
             if not row["number"] and row.get("owner_user_id") in (None, "") and row["digits"] == default_digits),
            "",
        )
        lines.extend([
            "exten => s,1,NoOp(Inbound provider call ${CALLERID(all)})",
            *(
                [f" same => n,Dial(PJSIP/{dial_target(default_key)},30)"]
                if default_key else
                [" same => n,Playback(ss-noservice)"]
            ),
            *([f' same => n,ExecIf($["${{DIALSTATUS}}" != "ANSWER"]?VoiceMail({extension_mailbox(default_key)}@engineerip,u))'] if default_key in voicemail_exts else []),
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
