from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .admin import SettingsStore, same_account
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

    def _extension_reachable_by(self, extension: str, owner: Any, owners: dict[str, Any]) -> bool:
        """May a device belonging to `owner` ring this extension?

        Yes for the platform's own lines and for every extension of the same
        customer account; no for another customer's - so dialling an extension
        number can never drop a call on a different organisation's desk.
        """
        if owner is None or str(owner).strip() == "":
            return True
        extension_owner = owners.get(str(extension))
        if extension_owner is None or str(extension_owner).strip() == "":
            return True
        return self._same_owner(extension_owner, owner)

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
        if configured and self._valid_extension(configured) and configured in active_extensions:
            return configured
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
        for ext in self.store.list_extensions():
            if not ext["active"]:
                continue
            extension = self._clean(ext["extension"])
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
            aors = [extension]
            alias_aor = []
            if username != extension:
                aors.append(username)
                alias_aor = [f"[{username}]", "type=aor", "max_contacts=5", "remove_existing=yes", ""]
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
                # hardware phones, but make the alias WebRTC-capable for the
                # browser. Both endpoints use the same auth and AOR contacts.
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
                    f"[{username}]", "type=endpoint", f"aors={','.join(aors)}", f"auth=auth-{extension}",
                    f"callerid=<{extension}>", *browser_endpoint_body, "",
                ]
            lines.extend([
                f"; Extension {extension}",
                f"[{extension}]", "type=aor", "max_contacts=5", "remove_existing=yes", "",
                *alias_aor,
                f"[auth-{extension}]", "type=auth", "auth_type=userpass",
                f"username={username}", f"password={password}", *auth_digest, "",
                f"[{extension}]", "type=endpoint", f"aors={','.join(aors)}", f"auth=auth-{extension}",
                *endpoint_body, "",
                *alias_endpoint,
            ])

        extension_owners = self._extension_owners()
        for account in sip_accounts:
            extension = self._clean(str(account.get("extension") or ""))
            if extension in configured_extensions:
                continue
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
            number = self._clean(extension["extension"])
            pin = self._clean(self.store.get_voicemail_pin(number))
            display_name = self._clean(extension.get("display_name") or f"Extension {number}")
            lines.append(f"{number} => {pin},{display_name},,,attach=no|delete=no")
        lines.append("")
        return "\n".join(lines)

    def render_dialplan(self) -> str:
        settings = self.store.get_settings()
        extensions = [row for row in self.store.list_extensions() if row["active"]]
        active_exts = [row["extension"] for row in extensions]
        voicemail_exts = {row["extension"] for row in extensions if row.get("voicemail_enabled")}
        owners = self._extension_owners()
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
        platform_exts = [row["extension"] for row in extensions if row.get("owner_user_id") in (None, "")]
        tenant_exts: dict[int, list[str]] = {}
        for row in extensions:
            if row.get("owner_user_id") in (None, ""):
                continue
            tenant_exts.setdefault(int(row["owner_user_id"]), []).append(row["extension"])
        # A device that is not linked to an extension still answers in its own
        # account's context, so that context has to exist even without extensions.
        for account in self.store.list_sip_accounts():
            owner = account.get("owner_user_id")
            if owner not in (None, "") and str(owner).strip().isdigit() and int(owner) not in tenant_exts:
                tenant_exts[int(owner)] = []

        def extension_route(number: str) -> list[str]:
            return [
                f"exten => {number},1,NoOp(EngineerIP extension {number})",
                f" same => n,Dial(PJSIP/{number},30)",
                *([f' same => n,ExecIf($["${{DIALSTATUS}}" != "ANSWER"]?VoiceMail({number}@engineerip,u))'] if number in voicemail_exts else []),
                " same => n,Hangup()",
            ]

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
            "; Extension numbers are three digits and unique platform-wide. Dial-by-",
            "; extension uses one context per customer account, so a phone can reach",
            "; every extension of its own account - whichever DID that extension",
            "; belongs to - and never another account's.",
            ";",
            "; [from-internal]: the platform's own devices; they may ring any extension.",
            "[from-internal]",
            *voicemail_login,
        ]
        for extension in [row["extension"] for row in extensions]:
            lines.extend(extension_route(extension))
        lines.extend(unknown_extension)
        lines.extend(outbound)
        for owner_id in sorted(tenant_exts):
            context = self.tenant_context(owner_id)
            lines.extend([
                "",
                f"; {context}: extensions of customer account {owner_id}, plus the platform's.",
                f"[{context}]",
                *voicemail_login,
            ])
            for extension in [*tenant_exts[owner_id], *platform_exts]:
                lines.extend(extension_route(extension))
            lines.extend(unknown_extension)
            lines.extend(outbound)
        lines.extend(["", "[voicemail-inbound]"])
        for extension in sorted(voicemail_exts):
            lines.extend([
                f"exten => {extension},1,VoiceMail({extension}@engineerip,u)",
                " same => n,Hangup()",
            ])
        lines.extend(["", "[from-provider]"])
        for number in self.store.list_numbers():
            if not number["active"]:
                continue
            did = re.sub(r"[^0-9]", "", number["number"])
            if not did:
                continue
            # A DID rings an extension of the organisation it belongs to - its own
            # link, that customer's fallback, or the platform's last resort.
            extension = ""
            for candidate in (str(number["inbound_extension"] or ""), did_extension(number), inbound_fallback):
                if (
                    candidate and self._valid_extension(candidate) and candidate in active_exts
                    and self._extension_reachable_by(candidate, number.get("owner_user_id"), owners)
                ):
                    extension = candidate
                    break
            for dialed_number in (did, f"+{did}"):
                if not extension:
                    lines.extend([
                        f"exten => {dialed_number},1,NoOp(Inbound DID {dialed_number} has no reachable extension)",
                        " same => n,Playback(ss-noservice)",
                        " same => n,Hangup()",
                    ])
                    continue
                lines.extend([
                    f"exten => {dialed_number},1,NoOp(Inbound DID {dialed_number} owned by extension {extension})",
                    f" same => n,Stasis(engineerip,inbound,{dialed_number},{extension})",
                    *([f" same => n,VoiceMail({extension}@engineerip,u)"] if extension in voicemail_exts else []),
                    " same => n,Hangup()",
                ])
        lines.extend([
            "exten => s,1,NoOp(Inbound provider call ${CALLERID(all)})",
            f" same => n,Dial(PJSIP/{default_ext},30)",
            *([f' same => n,ExecIf($["${{DIALSTATUS}}" != "ANSWER"]?VoiceMail({default_ext}@engineerip,u))'] if default_ext in voicemail_exts else []),
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
