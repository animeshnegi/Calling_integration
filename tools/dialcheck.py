"""Dial-by-extension proof, without an Asterisk.

    python tools/dialcheck.py

Builds the scenario from the field: two customers, the first with two phone
numbers and 35 extensions between them, and renders the *real* Asterisk dial
plan and PJSIP endpoints for it. Then it shows which extensions each number
space can dial, and where a cross-organisation misdial ends up.

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
    return sorted(
        line.split("exten => ")[1].split(",")[0]
        for line in context.splitlines()
        if line.startswith("exten => ") and line.split("exten => ")[1].split(",")[0].isdigit()
    )


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

    # Meridian owns two numbers: +1 302 555 0098 with 20 extensions and
    # +1 302 555 0067 with 15. Northwind has two extensions of its own.
    for index in range(20):
        store.save_extension({"extension": f"{101 + index}", "display_name": f"Meridian line 0098 #{index + 1}"}, meridian)
    for index in range(15):
        store.save_extension({"extension": f"{121 + index}", "display_name": f"Meridian line 0067 #{index + 1}"}, meridian)
    for extension in ("301", "302"):
        store.save_extension({"extension": extension, "display_name": f"Northwind desk {extension}"}, northwind)
    store.save_number({"number": "+13025550098", "provider": "IPComms", "inbound_extension": "101",
                       "owner_user_id": meridian, "default_outbound": True, "active": True})
    store.save_number({"number": "+13025550067", "provider": "IPComms", "inbound_extension": "121",
                       "owner_user_id": meridian, "active": True})
    store.save_number({"number": "+13025550011", "provider": "IPComms", "inbound_extension": "301",
                       "owner_user_id": northwind, "default_outbound": True, "active": True})

    dialplan, pjsip = render(store, workdir)
    meridian_exts = [row["extension"] for row in store.list_extensions(meridian) if row["active"]]
    northwind_exts = [row["extension"] for row in store.list_extensions(northwind) if row["active"]]
    meridian_context = context_of(dialplan, TelephonyConfigSync.tenant_context(meridian))
    northwind_context = context_of(dialplan, TelephonyConfigSync.tenant_context(northwind))

    print("=== what each organisation can dial ===")
    print(f"Meridian Health   (numbers +13025550098, +13025550067): "
          f"{len(meridian_exts)} extensions, {len(extensions_in(meridian_context))} dialable")
    print(f"Northwind Trading (number  +13025550011): {len(northwind_exts)} extensions, "
          f"{len(extensions_in(northwind_context))} dialable")
    print(f"Meridian's numbers see: {', '.join(extensions_in(meridian_context))}")
    print(f"Northwind's numbers see: {', '.join(extensions_in(northwind_context))}")

    print("\n=== the field example ===")
    checks = [
        ("105 dials 117 (both under +13025550098)", "117" in extensions_in(meridian_context)),
        ("108 dials 102 (under the customer's other number)", "102" in extensions_in(meridian_context)),
        ("a Meridian phone cannot dial Northwind's 301", "301" not in extensions_in(meridian_context)),
        ("a Northwind phone cannot dial Meridian's 101", "101" not in extensions_in(northwind_context)),
        ("an unknown 3-digit number is answered with \"not in service\"",
         "exten => _XXX,1" in meridian_context and "Playback(ss-noservice)" in meridian_context),
        ("both of Meridian's numbers ring a Meridian extension",
         all(f"Stasis(engineerip,inbound,{did},{extension})" in dialplan
             for did, extension in (("13025550098", "101"), ("13025550067", "121")))),
        ("outbound dialling still leaves every organisation",
         "Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)" in meridian_context),
    ]
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print(f"\nrendered configuration: {workdir}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
