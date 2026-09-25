"""Local preview server with a seeded database. Development only.

    .venv/bin/python tools/devpreview.py

Seeds customers, extensions, numbers, devices, call flows, a ring group, calls,
recordings, voicemails and requests so every console page has something real to
show, then serves the app on 0.0.0.0:5000. Data lives in PREVIEW_DATA (default
/tmp/eip-preview) and is only seeded when the database has no customers.

Accounts created here:
    administrator   engineerip / preview-admin-password
    customers       meridian | northwind | acme | bluewave / customer-password-0N
"""
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402 - the path has to be set first
from app.config import Config  # noqa: E402
from app.models import Call  # noqa: E402

DATA = Path(os.environ.get("PREVIEW_DATA", "/tmp/eip-preview"))

ADMIN_USERNAME = os.environ.get("PREVIEW_ADMIN", "engineerip")
ADMIN_PASSWORD = os.environ.get("PREVIEW_ADMIN_PASSWORD", "preview-admin-password")
CUSTOMER_PASSWORD = os.environ.get("PREVIEW_CUSTOMER_PASSWORD", "customer-password-01")

CUSTOMERS = [
    ("meridian", "Meridian Health", "Dana Whitfield", "Practice Manager"),
    ("northwind", "Northwind Logistics", "Ravi Menon", "Operations Lead"),
    ("acme", "Acme Legal", "Sofia Alvarez", "Managing Partner"),
    ("bluewave", "Bluewave Studio", "Tom Fischer", "Founder"),
]

# extensions and how many numbers each customer gets
LAYOUT = {
    "meridian": dict(extensions=["101", "102"], numbers=2, group="Front desk"),
    "northwind": dict(extensions=["201"], numbers=1, group=""),
    "acme": dict(extensions=["301"], numbers=1, group=""),
    "bluewave": dict(extensions=[], numbers=0, group=""),
}


def reset():
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True, exist_ok=True)


def seed(store):
    ready = DATA / "ari.ready"
    ready.touch()
    store.ensure_bootstrap_admin(ADMIN_USERNAME, ADMIN_PASSWORD)
    # The address customers register with and the API examples are built from:
    # an administrator sets it in Settings -> Server address.
    store.set_settings({"service_host": "sip.engineerip.example", "service_sip_port": "5060"})
    store.save_provider({
        "name": "IPComms", "server": "sip.ipcomms.net", "port": 5060,
        "username": "eip-preview", "password": "provider-preview-secret", "transport": "udp",
        "codecs": "ulaw,alaw", "allowed_ips": "203.0.113.10/32",
    })

    seeded = []
    for index, (username, company, full_name, job_role) in enumerate(CUSTOMERS, start=1):
        store.save_user({
            "username": username, "password": CUSTOMER_PASSWORD, "role": "user",
            "email": f"{username}@example.com", "full_name": full_name, "company_name": company,
            "job_role": job_role, "phone": f"+1 212 555 01{index:02d}",
        })
        user_id = next(row["id"] for row in store.list_users() if row["username"] == username)
        seeded.append((user_id, username, company, index))

    # Extensions, numbers and devices. bluewave stays empty so the empty states
    # and "needs attention" workspace are reachable.
    number_seq = 0
    for user_id, username, company, index in seeded:
        plan = LAYOUT[username]
        for ext in plan["extensions"]:
            store.save_extension({
                "extension": ext, "display_name": f"{company} {ext}", "sip_password": f"secret-{ext}",
                "voicemail_enabled": True, "voicemail_pin": "4321", "active": True,
            }, user_id)
        for position in range(plan["numbers"]):
            number_seq += 1
            number = f"+1302555{number_seq:04d}"
            store.save_number({
                "number": number, "provider": "IPComms", "description": f"{company} line {position + 1}",
                "inbound_extension": "", "default_outbound": False,
                "owner_user_id": user_id, "monthly_price": "5.00",
                "billing_cycle_day": 12, "billing_start": str(date.today().replace(day=1)), "active": True,
            })
            extension = plan["extensions"][position % len(plan["extensions"])] if plan["extensions"] else ""
            if not extension:
                continue
            # Link the DID and give the line its default call flow, exactly as
            # auto-provisioning does in the console.
            store.save_number({
                "number": number, "provider": "IPComms", "description": f"{company} line {position + 1}",
                "inbound_extension": extension, "default_outbound": position == 0,
                "owner_user_id": user_id, "monthly_price": "5.00",
                "billing_cycle_day": 12, "billing_start": str(date.today().replace(day=1)), "active": True,
            })
            # Linked to an extension, so the store gives the account that
            # extension's identity: that is what the device authenticates with.
            account_id = store.save_sip_account({
                "label": f"{company} desk phone {extension}",
                "sip_password": f"handset-{extension}", "server": "sip.ipcomms.net", "port": 5060,
                "transport": "udp", "phone_number": number, "extension": extension, "active": True,
            }, user_id)
            if position == 0:
                store.set_default_outbound_number(extension, number)
            if position == 0 and username in ("meridian", "northwind"):
                with store._connect() as db:  # noqa: SLF001 - preview seeding only
                    db.execute(
                        "UPDATE customer_sip_accounts SET registration_status='online', last_registered_at=CURRENT_TIMESTAMP WHERE id=?",
                        (account_id,),
                    )

    # Call flows per customer: the main line rings every device, a number tied to
    # one extension rings that one, and each extension has its own flow.
    for user_id, username, company, index in seeded:
        plan = LAYOUT[username]
        if not plan["extensions"]:
            continue
        extensions = [row["extension"] for row in store.list_extensions(user_id)]
        primary = store.primary_extension(user_id)
        for row in store.list_numbers(user_id):
            if not row["inbound_extension"]:
                continue
            devices = extensions if row["inbound_extension"] == primary else [row["inbound_extension"]]
            store.save_call_route(user_id, {
                "phone_number": row["number"], "name": "Main call flow",
                "route": store.default_number_route(devices), "active": True,
            })
            store.ensure_extension_flow(user_id, row["inbound_extension"], voicemail=True)
        if plan["group"]:
            group_id = store.save_group({
                "name": plan["group"], "members": extensions, "timeout": 20, "active": True,
            }, user_id)
            store.save_routing_flow(user_id, {
                "name": f"{plan['group']} flow",
                "route": {"nodes": [
                    {"type": "ring_group", "group_id": str(group_id), "extensions": extensions, "timeout": 20,
                     "label": f"{plan['group']} · {len(extensions)} members · 20s", "configured": True},
                    {"type": "voicemail", "mailbox": extensions[0],
                     "label": f"Voicemail {extensions[0]}", "configured": True},
                ]},
            }, target_type="group", target=str(group_id))

    # A number that is assigned but not yet linked to an extension: the state
    # that used to offer "Use for outbound" and then fail.
    meridian_id = next(user_id for user_id, username, _, _ in seeded if username == "meridian")
    store.save_number({
        "number": "+13025550003", "provider": "IPComms", "description": "Meridian spare line (no extension yet)",
        "inbound_extension": "", "owner_user_id": meridian_id, "monthly_price": "5.00", "active": True,
    })
    store.save_number({
        "number": "+13025559999", "provider": "IPComms", "description": "Spare DID (not yet assigned)",
        "inbound_extension": "", "owner_user_id": None, "monthly_price": "0.00", "active": True,
    })

    store.save_webhook({
        "name": "CRM sync", "url": "https://crm.example.com/hooks/telephony",
        "events": "call.started,call.completed", "secret": "hook-secret-1", "owner_user_id": meridian_id, "active": True,
    })
    store.create_api_key("Meridian CRM", "calls:read,calls:write", meridian_id)
    month_start = str(date.today().replace(day=1))
    store.create_invoice(
        meridian_id, "+13025550001", month_start, str(date.today()),
        500, str(date.today() - timedelta(days=3)),
    )
    store.create_invoice(
        meridian_id, "+13025550002", str(date.today().replace(day=1) - timedelta(days=31)), month_start,
        500, str(date.today() - timedelta(days=33)),
    )
    store.create_request(meridian_id, "number", "Please assign a second DID and ring the front desk group.")
    store.add_notification(meridian_id, "billing", "Invoice ready", "Your monthly invoice is available.")
    store.add_activity(meridian_id, meridian_id, "login", "session", None, "Signed in from the console")

    # Call history so the dashboard, analytics and recordings have content.
    service = store_app.extensions["telephony_service"]
    now = datetime.now()
    history = [
        ("+919812345678", "101", "inbound", "completed", True, 184, 2),
        ("+919812345679", "102", "inbound", "failed", False, 0, 5),
        ("+14155550123", "101", "outbound", "completed", True, 96, 26),
        ("+919812345680", "201", "inbound", "completed", True, 42, 30),
        ("+919812345681", "101", "inbound", "completed", True, 311, 51),
        ("+14155550124", "301", "outbound", "failed", False, 0, 74),
    ]
    for phone, extension, direction, status, answered, duration, hours_ago in history:
        started = now - timedelta(hours=hours_ago)
        call = service.store.create(Call(
            call_id=f"seed-{extension}-{hours_ago}", contact_id=None, member_id=None, extension=extension,
            phone=phone, caller_id_number="+13025550001", provider="IPComms", direction=direction,
            status=status, answered=answered, started_at=started.isoformat(),
            answered_at=(started + timedelta(seconds=12)).isoformat() if answered else None,
            ended_at=(started + timedelta(seconds=duration + 12)).isoformat(), duration_seconds=duration,
            employee_channel_id=f"seed-{extension}-{hours_ago}-employee", customer_channel_id=f"seed-{extension}-{hours_ago}-customer",
            recording_name=f"seed-{extension}-{hours_ago}" if answered and direction == "inbound" else None,
            recording_format="wav" if answered and direction == "inbound" else None,
            recording_status="finalized" if answered and direction == "inbound" else None,
        ))
        assert call is not None
    # Voicemail messages are Asterisk files; the preview leaves that box empty
    # so the empty state is reachable.
    print(f"preview: seeded {DATA}")
    print(f"preview: admin {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    for index, (username, company, _, _) in enumerate(CUSTOMERS, start=1):
        print(f"preview: customer {username} / customer-password-0{index}")


def main():
    reset()
    os.environ.setdefault("FLASK_ENV", "development")


    class PreviewConfig(Config):
        FLASK_ENV = "development"
        SECRET_KEY = "preview-secret-key-not-for-production"
        TELEPHONY_TOKEN = "preview-token"
        SETTINGS_DB_PATH = str(DATA / "settings.db")
        CALLS_DB_PATH = str(DATA / "calls.db")
        ARI_READY_PATH = str(DATA / "ari.ready")
        VOICEMAIL_PATH = str(DATA / "voicemail")
        ASTERISK_DYNAMIC_CONFIG_PATH = str(DATA / "pjsip.dynamic.conf")
        DATABASE_URI = ""
        ADMIN_USERNAME = ADMIN_USERNAME
        ADMIN_PASSWORD = ADMIN_PASSWORD
        ASTERISK_EXTENSIONS = ()
        DEFAULT_EXTENSION = ""
        ENABLE_BROWSER_API = False
        ENABLE_ARI_WEBHOOK = False

    global store_app
    store_app = create_app(PreviewConfig)
    seed(store_app.extensions["settings_store"])
    store_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)


if __name__ == "__main__":
    main()
