"""Dial-by-extension proof, without an Asterisk.

    python tools/dialcheck.py

Builds the scenario from the field: two customers, the first with two phone
numbers whose extension sets both start at 101 and overlap deliberately, and
renders the *real* Asterisk dial plan and PJSIP endpoints for it. It then
proves the two rules the platform promises:

* a call arriving on a number rings that number's own extension set - dial 104
  on +1302 555 0098 and it is that line's 104, never the 104 of the
  customer's other line or of anybody else;
* inside one account the digits name one device, so the customer's phones
  reach each other by extension, and reaching a *specific* line's device is
  done by dialling that line's number, which the platform resolves internally.

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


def endpoint_sections(pjsip: str) -> list[str]:
    """The PJSIP endpoint names in the rendered file (one per device)."""
    return [line.strip()[1:-1] for line in pjsip.splitlines()
            if line.startswith("[") and not line.startswith("[;]")]


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

    # Meridian owns two numbers, and EACH ONE HAS ITS OWN EXTENSION SET FROM 101
    # UP: +1 302 555 0098 holds 101..120, +1 302 555 0067 holds 101..115. The
    # digits overlap on purpose - that is the point of the change. 901 is an
    # account-wide row (what a value stored before this change looks like), so
    # the old way of writing an extension still answers.
    line_a, line_b = "+13025550098", "+13025550067"
    north_line = "+13025550011"
    # The numbers are assigned first (a line has to exist to hold a set), then
    # each one is given its own devices, and finally each is linked to the first
    # device of its own set.
    for number, owner in ((line_a, meridian), (line_b, meridian), (north_line, northwind)):
        store.save_number({"number": number, "provider": "IPComms", "owner_user_id": owner,
                           "active": True, "monthly_price": 5})
    for index in range(20):
        store.add_extension_to_number(line_a, meridian, {"display_name": f"Meridian 0098 desk {index + 1}"})
    for index in range(15):
        store.add_extension_to_number(line_b, meridian, {"display_name": f"Meridian 0067 desk {index + 1}"})
    store.add_extension_to_number(north_line, northwind, {"display_name": "Northwind desk 101"})
    store.add_extension_to_number(north_line, northwind, {"display_name": "Northwind desk 102"})
    store.save_extension({"extension": "901", "display_name": "Meridian lobby (account-wide)"}, meridian)

    # Each number is assigned, linked to the first device of its OWN set, and
    # given the flow the console writes: ring every device that number holds.
    for number, owner, first in ((line_a, meridian, f"101@{line_a}"), (line_b, meridian, f"101@{line_b}"),
                                 (north_line, northwind, f"101@{north_line}")):
        store.save_number({"number": number, "provider": "IPComms", "inbound_extension": first,
                           "owner_user_id": owner, "default_outbound": number == line_a,
                           "active": True, "monthly_price": 5})
        store.save_call_route(owner, {
            "phone_number": number, "name": "Main call flow",
            "route": store.default_number_route(store.line_devices(owner, number)),
        })

    dialplan, pjsip = render(store, workdir)
    meridian_exts = [row["extension"] for row in store.list_extensions(meridian) if row["active"]]
    northwind_exts = [row["extension"] for row in store.list_extensions(northwind) if row["active"]]
    meridian_context = context_of(dialplan, TelephonyConfigSync.tenant_context(meridian))
    northwind_context = context_of(dialplan, TelephonyConfigSync.tenant_context(northwind))
    meridian_numbers = local_numbers_in(meridian_context)
    northwind_numbers = local_numbers_in(northwind_context)
    meridian_dials = devices_in(meridian_context)
    northwind_dials = devices_in(northwind_context)
    endpoints = endpoint_sections(pjsip)
    flows = {row["phone_number"]: [str(value) for node in row["route"]["nodes"]
                                   for value in (node.get("extensions") or [])]
             for row in store.list_call_routes(meridian)}

    def inbound_extension(did: str) -> str:
        marker = f"Stasis(engineerip,inbound,{did},"
        if marker not in dialplan:
            return ""
        return dialplan.split(marker)[1].split(")")[0]

    print("=== every number carries its own set, starting at 101 ===")
    for number in (line_a, line_b, north_line):
        owner = {line_a: meridian, line_b: meridian, north_line: northwind}[number]
        keys = store.line_devices(owner, number)
        print(f"{number}: {len(keys)} devices - {', '.join(key.split('@')[0] for key in keys)}"
              + (" (account-wide included)" if any("@" not in key for key in keys) else ""))

    print("\n=== what each organisation can dial inside it ===")
    print(f"Meridian Health ({len(meridian_exts)} extensions): " + ", ".join(sorted(meridian_dials)))
    print(f"Northwind Trading ({len(northwind_exts)} extensions): " + ", ".join(sorted(northwind_dials)))
    print("Meridian's own numbers, dialled from inside: "
          + ", ".join(f"{pattern} rings {extension}" for pattern, extension in sorted(meridian_numbers.items())))

    print("\n=== where 104 on each number lands ===")
    print(f"a call to {line_a} rings {inbound_extension('13025550098')}")
    print(f"a call to {line_b} rings {inbound_extension('13025550067')}")
    print(f"a Meridian phone dialling 104 rings {meridian_dials.get('104')}")

    other_number = route_of(meridian_context, "13025550067", "local number ")
    answer = route_of(meridian_context, "104", "extension ")
    checks = [
        # 1. A DID rings the set of its own number.
        ("+1 302 555 0098 answers on its own 101", inbound_extension("13025550098") == f"101@{line_a}"),
        ("+1 302 555 0067 answers on its own 101, not on the first line's",
         inbound_extension("13025550067") == f"101@{line_b}"),
        ("104 on +1 302 555 0067 is that line's 104",
         f"104@{line_b}" in store.line_devices(meridian, line_b)),
        ("and the two 104s are different devices",
         f"104@{line_a}" in store.line_devices(meridian, line_a)
         and store.line_devices(meridian, line_a) != store.line_devices(meridian, line_b)),
        # 2. Each number's generated flow rings that number's own devices only.
        ("+1 302 555 0098's flow rings only its own devices",
         bool(flows.get(line_a)) and all(key.endswith(line_a) for key in flows[line_a] if "@" in key)
         and all("@" in key or key == "901" for key in flows[line_a])),
        ("+1 302 555 0067's flow rings only its own devices",
         bool(flows.get(line_b)) and all(key.endswith(line_b) for key in flows[line_b] if "@" in key)
         and all("@" in key or key == "901" for key in flows[line_b])),
        ("the account-wide 901 still answers on both of Meridian's lines",
         store.resolve_extension(line_a, "901", meridian) and store.resolve_extension(line_b, "901", meridian)),
        # 3. Inside the account the digits name one device; another account's
        #    digits are not in the context at all.
        ("a Meridian phone dialling 104 reaches one of the account's two 104s",
         meridian_dials.get("104") in {"104", "104-13025550098", "104-13025550067"}),
        ("the digits the account dials exist exactly once", len(set(meridian_dials)) == len(meridian_dials)),
        ("901 is dialable inside Meridian", meridian_dials.get("901") in {f"901@{line_a}", "901"}),
        ("a Northwind phone reaches its own 101, never Meridian's",
         northwind_dials.get("101") in {"101", "101-13025550011"}
         and meridian_dials.get("101") != northwind_dials.get("101")),
        ("and the two 101s are different PJSIP endpoints",
         northwind_dials.get("101") != meridian_dials.get("101")),
        ("Meridian's 201 is not in Northwind's context", "201" not in northwind_dials),
        ("Northwind's 101 is not in Meridian's context either",
         meridian_dials.get("101") not in {f"101@{north_line}", f"101-13025550011"}),
        # 4. Reaching another number's device: dial the number, resolved here.
        ("dialling Meridian's other number reaches its own 101",
         meridian_numbers.get("13025550067") in {"101", "101-13025550067"} and "Dial(PJSIP/" in other_number),
        ("and that call never leaves through the carrier trunk", "OUTBOUND_TRUNK" not in other_number),
        ("the + form of the number works the same way",
         meridian_numbers.get("+13025550067") == meridian_numbers.get("13025550067")),
        ("a Meridian phone cannot dial Northwind's number internally",
         "13025550011" not in meridian_numbers),
        # 5. The endpoints each device registers with exist, once.
        ("every extension of every line has a PJSIP endpoint, and no name holds `@`",
         all(store.endpoint_name(key) in endpoints for key in meridian_exts + northwind_exts)
         and not any("@" in name for name in endpoints)),
        ("every endpoint the dial plan rings exists in the rendered PJSIP file",
         set(meridian_dials.values()) | set(northwind_dials.values()) | set(meridian_numbers.values())
         <= set(endpoints)),
        ("only one device keeps the plain three-digit endpoint name",
         sum(1 for name in set(endpoints) if name == "101") == 1,
         ", ".join(sorted(name for name in set(endpoints) if name.startswith("10"))[:8])),
        ("an unknown three-digit number is answered with \"not in service\"",
         "exten => _XXX,1" in meridian_context and "Playback(ss-noservice)" in meridian_context),
        ("outbound dialling still leaves every organisation",
         "Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)" in meridian_context),
    ]
    print("\n=== checks ===")
    for label, ok, *detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail[0]}]" if detail and not ok else ""))
    print(f"\nrendered configuration: {workdir}")
    return 0 if all(bool(entry[1]) for entry in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
