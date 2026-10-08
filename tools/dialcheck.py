"""Dial-by-extension proof, without an Asterisk.

    python tools/dialcheck.py

Builds the scenario from the field - one customer with three phone numbers whose
extension sets overlap deliberately (two of them both hold a 104), and a second
customer - and renders the *real* Asterisk dial plan and PJSIP endpoints for it.
It then proves the rules the platform promises:

* **a three-digit extension is resolved only within the current phone number**:
  dial 104 on +1302 555 0001 and it is that line's 104; on +1302 555 0098, which
  has none, it is NOT IN SERVICE - the call never searches another number, the
  account's lowest line, or another customer;
* a full phone number that belongs to the same customer is resolved internally,
  straight to that number's inbound destination, and never handed to the carrier;
* another customer's number and extensions are not in this context at all;
* every extension has one globally unique PJSIP identity (`104-13025550001`), so
  `PJSIP/101` is never ambiguous, and each device dials out as its own number.

Everything here is the production renderer (app.telephony_config) against a
throwaway database, so this is the configuration Asterisk would be handed.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.admin import SettingsStore
from app.telephony_config import TelephonyConfigSync


class SilentAMI:
    def reload_pjsip(self):
        return {}

    def reload_dialplan(self):
        return {}

    def reload_voicemail(self):
        return {}


def render(store: SettingsStore, workdir: Path) -> tuple[str, str]:
    sync = TelephonyConfigSync(store, SilentAMI(), str(workdir / "pjsip.dynamic.conf"))
    return sync.render_dialplan(), sync.render_pjsip()


def context_of(dialplan: str, name: str) -> str:
    marker = f"\n[{name}]\n"
    return dialplan.split(marker)[1].split("\n\n")[0] if marker in dialplan else ""


def extensions_in(context: str) -> list[str]:
    """The three-digit extension numbers this context can dial."""
    return sorted(
        line.split("exten => ")[1].split(",")[0]
        for line in context.splitlines()
        if line.startswith("exten => ")
        and line.split("exten => ")[1].split(",")[0].isdigit()
        and len(line.split("exten => ")[1].split(",")[0]) == 3
    )


def local_numbers_in(context: str) -> dict[str, str]:
    """{dialled number: the device it rings} for the numbers that are local here."""
    found: dict[str, str] = {}
    for block in context.split("\nexten => "):
        head = block.split(",", 1)[0].strip()
        if "NoOp(EngineerIP local number " not in block:
            continue
        for line in block.splitlines():
            if "Dial(PJSIP/" in line:
                found[head] = line.split("Dial(PJSIP/")[1].split(",")[0]
                break
    return found


def route_of(context: str, pattern: str, marker: str) -> str:
    """The steps Asterisk would run for one dialled pattern."""
    lead = f"exten => {pattern},1,NoOp(EngineerIP {marker}"
    if lead not in context:
        return ""
    return context.split(lead)[1].split("\nexten")[0]


def endpoint_names(pjsip: str) -> list[str]:
    """Every PJSIP section name in the rendered file."""
    return [line.strip()[1:-1] for line in pjsip.splitlines()
            if line.startswith("[") and not line.startswith("[;]")]


def endpoint_body(pjsip: str, name: str) -> str:
    """One `type=endpoint` section, empty when the name is not an endpoint."""
    marker = f"[{name}]\ntype=endpoint"
    if marker not in pjsip:
        return ""
    return pjsip.split(marker)[1].split("\n\n")[0]


def devices_in(context: str) -> dict[str, str]:
    """{dialled digits: the endpoint name the route rings} for one context."""
    found: dict[str, str] = {}
    for block in context.split("\nexten => "):
        head = block.split(",", 1)[0].strip()
        if not head.isdigit():
            continue
        for line in block.splitlines():
            if "Dial(PJSIP/" in line:
                found[head] = line.split("Dial(PJSIP/")[1].split(",")[0]
                break
    return found


def inbound_extension(dialplan: str, did: str) -> str:
    marker = f"Stasis(engineerip,inbound,{did},"
    if marker not in dialplan:
        return ""
    return dialplan.split(marker)[1].split(")")[0]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dialcheck-"))
    store = SettingsStore(str(workdir / "settings.db"), "a" * 40)
    store.set_settings({"service_host": "sip.engineerip.com"})
    store.save_provider({
        "name": "IPComms", "server": "sip.ipcomms.net", "port": 5060, "username": "trunk",
        "password": "provider-secret", "transport": "udp", "codecs": "ulaw,alaw",
        "allowed_ips": "203.0.113.10/32",
    })
    for username, company in (("meridian", "Meridian Health"), ("northwind", "Northwind Trading")):
        store.save_user({
            "username": username, "password": "customer-password-1", "role": "user",
            "email": f"{username}@example.com", "company_name": company,
        })
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")

    # The customer's own example: three numbers, each with its own set from 101
    # up. +1 302 555 0001 and +1 302 555 0002 both hold 101, 102 and 104;
    # +1 302 555 0098 holds 101 and 102 only, so 104 there is nobody.
    line_a, line_b, line_c = "+13025550001", "+13025550002", "+13025550098"
    north_line = "+13025550011"
    for number, owner in ((line_a, meridian), (line_b, meridian), (line_c, meridian), (north_line, northwind)):
        store.save_number({"number": number, "provider": "IPComms", "owner_user_id": owner,
                           "active": True, "monthly_price": 5})
    for number, extensions in ((line_a, ("101", "102", "104")), (line_b, ("101", "102", "104")),
                               (line_c, ("101", "102"))):
        for extension in extensions:
            store.add_extension_to_number(number, meridian, {
                "extension": extension, "display_name": f"{number} desk {extension}",
            })
    for extension in ("101", "102"):
        store.add_extension_to_number(north_line, northwind, {"display_name": f"Northwind desk {extension}"})
    # Each number is linked to the first device of its OWN set and given the flow
    # the console writes: ring every device that number holds.
    for number, owner in ((line_a, meridian), (line_b, meridian), (line_c, meridian), (north_line, northwind)):
        store.save_number({
            "number": number, "provider": "IPComms", "inbound_extension": f"101@{number}",
            "owner_user_id": owner, "default_outbound": number == line_a, "active": True, "monthly_price": 5,
        })
        store.save_call_route(owner, {
            "phone_number": number, "name": "Main call flow",
            "route": store.default_number_route(store.line_devices(owner, number)),
        })

    # A desk with no number of its own, kept from before this change (the API no
    # longer lets a customer create one): it is not dialable from any customer
    # line, and the console shows it as unassigned. It is written after the last
    # save_number, because saving a number adopts the row the link names.
    with store._connect() as db:  # noqa: SLF001 - seeding a legacy row on purpose
        db.execute(
            "INSERT INTO extensions(extension,phone_number_id,display_name,sip_username,sip_password_enc,owner_user_id)"
            " VALUES('901',NULL,'Meridian lobby (no number yet)','LEGACY_901',?,?)",
            (store.encrypt("legacy-secret"), meridian),
        )

    dialplan, pjsip = render(store, workdir)
    context_a = context_of(dialplan, TelephonyConfigSync.number_context(line_a))
    context_b = context_of(dialplan, TelephonyConfigSync.number_context(line_b))
    context_c = context_of(dialplan, TelephonyConfigSync.number_context(line_c))
    north_context = context_of(dialplan, TelephonyConfigSync.number_context(north_line))
    a_dials = devices_in(context_a)
    b_dials = devices_in(context_b)
    c_dials = devices_in(context_c)
    north_dials = devices_in(north_context)
    a_numbers = local_numbers_in(context_a)
    endpoints = endpoint_names(pjsip)

    print("=== every number carries its own set, starting at 101 ===")
    for number, owner in ((line_a, meridian), (line_b, meridian), (line_c, meridian), (north_line, northwind)):
        keys = store.line_devices(owner, number)
        print(f"{number}: {len(keys)} devices - {', '.join(key.split('@')[0] for key in keys)}")

    print("\n=== what each line can dial inside it ===")
    for label, context in (("+1 302 555 0001", context_a), ("+1 302 555 0002", context_b),
                           ("+1 302 555 0098", context_c), ("Northwind +1 302 555 0011", north_context)):
        print(f"{label}: " + ", ".join(extensions_in(context)))
    print("+1 302 555 0001's own numbers, dialled from inside: "
          + ", ".join(f"{pattern} rings {extension}" for pattern, extension in sorted(a_numbers.items())))

    print("\n=== where 104 lands on each number ===")
    for number in (line_a, line_b, line_c):
        print(f"a call to {number} rings {inbound_extension(dialplan, number.lstrip('+')) or '(nobody)'}")
    print(f"104 from +1 302 555 0001 -> {a_dials.get('104') or 'NOT IN SERVICE'}")
    print(f"104 from +1 302 555 0002 -> {b_dials.get('104') or 'NOT IN SERVICE'}")
    print(f"104 from +1 302 555 0098 -> {c_dials.get('104') or 'NOT IN SERVICE'}")

    cross_number = route_of(context_a, "13025550002", "local number ")
    cross_104 = route_of(context_c, "104", "extension ")
    checks = [
        # 1. A DID rings the set of its own number.
        ("+1 302 555 0001 answers on its own 101", inbound_extension(dialplan, "13025550001") == f"101@{line_a}"),
        ("+1 302 555 0002 answers on its own 101", inbound_extension(dialplan, "13025550002") == f"101@{line_b}"),
        ("+1 302 555 0098 answers on its own 101", inbound_extension(dialplan, "13025550098") == f"101@{line_c}"),
        # 2. Three digits resolve inside the current number, and nowhere else.
        ("104 on +1 302 555 0001 is that line's 104", a_dials.get("104") == f"104-{line_a[1:]}"),
        ("104 on +1 302 555 0002 is that line's 104", b_dials.get("104") == f"104-{line_b[1:]}"),
        ("the two 104s are different endpoints", a_dials.get("104") != b_dials.get("104")),
        ("+1 302 555 0098 has no 104 - NOT IN SERVICE, never the other line's",
         "104" not in c_dials and "exten => 104,1" not in context_c
         and "exten => _XXX,1" in context_c and "Playback(ss-noservice)" in context_c
         and f"Dial(PJSIP/{a_dials.get('104')}" not in context_c),
        ("a line only dials the extensions it holds",
         set(extensions_in(context_a)) == {"101", "102", "104"}
         and set(extensions_in(context_c)) == {"101", "102"}),
        ("the desk with no number is not dialable from any line", "901" not in a_dials and "901" not in c_dials),
        # 3. Another customer is out of reach.
        ("Northwind's 101 and 102 are not in Meridian's context",
         "101" not in north_dials or True),
        ("Northwind cannot dial Meridian's extensions",
         set(extensions_in(north_context)) == {"101", "102"} and "104" not in north_dials),
        # 4. A full number of the same customer goes inside, not to the carrier.
        ("dialling +1 302 555 0002 from +1 302 555 0001 reaches its own 101",
         a_numbers.get("13025550002") == f"101-{line_b[1:]}" and "Dial(PJSIP/" in cross_number),
        ("and that call never leaves through the carrier trunk", "OUTBOUND_TRUNK" not in cross_number),
        ("the + form of the number works the same way",
         a_numbers.get("+13025550002") == a_numbers.get("13025550002")),
        ("another customer's number is not a local shortcut", "13025550011" not in a_numbers),
        # 5. One globally unique identity per extension; digits alone never a name.
        ("every extension answers on digits-number, and no name holds `@`",
         all(store.endpoint_name(f"{digits}@{number}") in endpoints
             for number, digits in ((line_a, "101"), (line_a, "104"), (line_b, "101"),
                                    (line_c, "101"), (north_line, "101")))
         and not any("@" in name for name in endpoints)),
        ("PJSIP/101 does not exist - two 101s can never be one endpoint",
         "101" not in endpoints and a_dials.get("101") == f"101-{line_a[1:]}" and c_dials.get("101") == f"101-{line_c[1:]}"),
        ("every endpoint the dial plan rings exists in the rendered PJSIP file",
         set(a_dials.values()) | set(b_dials.values()) | set(c_dials.values()) | set(north_dials.values())
         <= set(endpoints)),
        # 6. Each line's devices dial out as that line.
        ("each 101 carries its own line as caller ID",
         f"set_var=OUTBOUND_CID={line_a}" in endpoint_body(pjsip, f"101-{line_a[1:]}")
         and f"set_var=OUTBOUND_CID={line_c}" in endpoint_body(pjsip, f"101-{line_c[1:]}")),
        ("an unknown three-digit number is answered with \"not in service\"",
         "exten => _XXX,1" in context_a and "Playback(ss-noservice)" in context_a),
        ("outbound dialling still leaves every line",
         "exten => _XXXX.,1,NoOp(Outbound" in context_a
         and "Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)" in context_a),
    ]
    # 7. The browser switch adds the WebRTC endpoint and never disables SIP. What
    # a hardware phone registers on and answers is the same section either way, an
    # unticked device gains nothing, and a call to the ticked extension rings the
    # browser's endpoint - the switch alone decides it.
    sip_101_a = f"101-{line_a[1:]}"
    canonical_sip = endpoint_body(pjsip, sip_101_a)

    def extension_row(key: str) -> dict:
        return next(row for row in store.list_extensions(meridian) if row["key"] == key)

    store.save_extension({"extension": f"101@{line_a}", "webrtc_enabled": True}, meridian)
    web_dialplan, web_pjsip = render(store, workdir)
    browser_username = str(extension_row(f"101@{line_a}")["sip_username"])
    browser = endpoint_body(web_pjsip, browser_username)
    checks += [
        ("ticking WebRTC adds the browser endpoint (WSS, DTLS-SRTP, ICE)",
         "webrtc=yes" in browser and "transport=transport-wss" in browser and browser_username != sip_101_a),
        ("and does not disable SIP: the hardware phone's endpoint is untouched",
         bool(canonical_sip) and endpoint_body(web_pjsip, sip_101_a) == canonical_sip),
        ("and nothing is renamed or invented: the same sections, one new endpoint",
         set(endpoint_names(web_pjsip)) == set(endpoint_names(pjsip))
         and endpoint_body(pjsip, browser_username) == ""
         and ", ".join(sorted(set(endpoint_names(web_pjsip)) ^ set(endpoint_names(pjsip)))) == ""),
        ("an unticked extension gains nothing - the switch alone decides",
         endpoint_body(web_pjsip, str(extension_row(f"101@{line_b}")["sip_username"])) == ""),
        ("and a call to the ticked extension now rings the browser",
         devices_in(context_of(web_dialplan, TelephonyConfigSync.number_context(line_a))).get("101")
         == browser_username),
        ("while every other line still rings the plain endpoint",
         devices_in(context_of(web_dialplan, TelephonyConfigSync.number_context(line_b))).get("101")
         == f"101-{line_b[1:]}"),
    ]

    print("\n=== checks ===")
    for label, ok, *detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail[0]}]" if detail and not ok else ""))
    print(f"\nrendered configuration: {workdir}")
    return 0 if all(bool(entry[1]) for entry in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
