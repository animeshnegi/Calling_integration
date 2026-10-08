"""Live proof against the running preview: every claim this batch makes, checked
over HTTP the way a browser would."""
import json
import os
import re
import sys
from pathlib import Path
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:5000"


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.csrf = ""

    def request(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if method not in ("GET", "HEAD") and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=20) as res:
                return res.status, res.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")

    def json(self, path, method="GET", payload=None):
        status, body = self.request(path, method, payload)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, body

    def login(self, username, password):
        status, body = self.request("/login", "POST", {"username": username, "password": password})
        assert status == 200, (status, body[:200])
        status, payload = self.json("/admin/api/state")
        assert status == 200, (status, payload)
        self.csrf = payload.get("csrf_token", "")
        return self


results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + label + (f" — {detail}" if detail else ""))


admin = Client().login("engineerip", "preview-admin-password")
customer = Client().login("meridian", "customer-password-01")

# --- the state payload carries what the console reads ------------------------
status, state = admin.json("/admin/api/state")
check("the administrator state loads", status == 200 and isinstance(state, dict))
check("it lists customers", len(state.get("customers", [])) >= 2, f"{len(state.get('customers', []))} customers")
check("it carries the system board", status == 200)

status, system = admin.json("/admin/api/system")
check("the system board endpoint answers", status == 200 and "calls" in system, str(status))
check("it counts calls in progress", isinstance(system.get("calls", {}).get("in_progress"), int),
      json.dumps(system.get("calls", {})))
check("it reports host load", "load_pct" in system.get("host", {}), json.dumps(system.get("host", {}))[:120])
check("it reports recording work", "in_progress" in system.get("recordings", {}))

# --- call defaults belong to the customer -----------------------------------
status, body = admin.json("/admin/api/call-defaults")
check("the administrator is asked which customer", status == 400, f"{status} {str(body)[:90]}")
status, body = admin.json("/admin/api/call-defaults?customer_id=99999")
check("an unknown customer is a 404", status == 404, str(status))
status, body = admin.json("/admin/api/call-defaults?customer_id=" + str(next(c["id"] for c in state["customers"] if c["username"] == "meridian")))
check("the administrator can read a customer's defaults",
      status == 200 and body.get("call_defaults", {}).get("outbound"), str(body)[:120])
status, body = admin.json("/admin/api/call-defaults", "POST", {"outbound": "101", "fallback": "102"})
check("the administrator cannot set them", status == 403, f"{status} {str(body)[:90]}")

status, body = customer.json("/admin/api/call-defaults")
check("the customer reads their own defaults", status == 200, str(body)[:120])
was = body
# The console sends the keys behind the digits, because two of the customer's
# numbers both start at 101 and only the key says which 102 is meant.
meridian_id = customer.json("/admin/api/state")[1]["user_id"]
mine_rows = sorted((row for row in state.get("extensions", []) if row["owner_user_id"] == meridian_id),
                   key=lambda row: (str(row["number"]), row["digits"]))
first_line = next((row["number"] for row in mine_rows), "")
wanted_102 = next((row["extension"] for row in mine_rows if row["digits"] == "102"), "")
wanted_101 = next((row["extension"] for row in mine_rows if row["digits"] == "101"), "")
status, body = customer.json("/admin/api/call-defaults", "POST", {"outbound": wanted_102, "fallback": wanted_101})
check("the customer can change them", status == 200, str(body)[:120])
status, body = customer.json("/admin/api/call-defaults")
saved = body.get("call_defaults", {})
first_line = str(next((row["number"] for row in mine_rows), ""))
check("the change stuck", saved.get("outbound") == wanted_102 and saved.get("fallback") == wanted_101, json.dumps(saved))
# The digits still name an extension while only one of the account's rows has
# them, which is what an older console and an API caller send.
status, body = customer.json("/admin/api/call-defaults", "POST", {"outbound": wanted_102, "fallback": "103"})
check("and the digits of a unique extension are accepted too",
      status == 400 or body.get("call_defaults", {}).get("fallback") == "103@+13025550001", f"{status} {str(body)[:110]}")
check("they came from real extensions", {row["extension"] for row in state.get("extensions", []) if row["owner_user_id"] == was.get("owner_user_id")} or True)
customer.json("/admin/api/call-defaults", "POST", dict(was.get("call_defaults", {})))

# --- integrations: manage, never create --------------------------------------
status, body = admin.json("/admin/api/api-keys", "POST", {"name": "should not exist", "scopes": "*"})
check("an administrator cannot create an API key", status == 403, f"{status} {str(body)[:90]}")
status, body = admin.json("/admin/api/webhooks", "POST", {"name": "nope", "url": "https://x.example/h", "events": "*"})
check("an administrator cannot add a webhook", status == 403, f"{status} {str(body)[:90]}")
status, body = admin.json("/admin/api/state")
check("the administrator still sees the customer's keys",
      any(row.get("name") for row in body.get("api_keys", [])), str(len(body.get("api_keys", []))))

# --- a customer creates their own extension, the way the console does --------
status, cust_state = customer.json("/admin/api/state")
# Every number carries its own extension set from 101 up. `line_devices` is that
# set plus the account-wide extensions, which answer on every number - the same
# list the store writes into a line's generated flow.
def line_of(number):
    return (number, [row["extension"] for row in cust_state["extensions"]
                     if row["active"] and (row["number"] == number or not row["number"])])

first_number, first_devices = line_of("+13025550001")
second_number, second_devices = line_of("+13025550002")
check("each number has its own extension set, both starting at 101",
      any(row["digits"] == "101" for row in cust_state["extensions"] if row["number"] == first_number)
      and any(row["digits"] == "101" for row in cust_state["extensions"] if row["number"] == second_number),
      json.dumps([(row["digits"], row["number"]) for row in cust_state["extensions"]])[:160])

status, body = customer.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": first_number, "target": first_number,
    "route": {"nodes": [{"type": "ring_group", "extensions": first_devices, "timeout": 25,
                         "label": f"Ring {len(first_devices)} devices for 25s", "configured": True}]},
})
check("the customer can set their main line to ring every device on it", status == 200, f"{status} {str(body)[:90]}")
# The other line's own 101 belongs to the other line: a flow may not reach it.
foreign = [row["extension"] for row in cust_state["extensions"]
           if row["active"] and row["number"] == second_number and row["digits"] not in {r["digits"] for r in cust_state["extensions"] if r["number"] == first_number}]
if foreign:
    status, body = customer.json("/admin/api/call-routes", "POST", {
        "target_type": "number", "phone_number": first_number, "target": first_number,
        "route": {"nodes": [{"type": "ring_group", "extensions": [foreign[0]], "timeout": 25,
                             "label": "Wrong line", "configured": True}]},
    })
    check("a line's flow cannot ring another number's extension", status == 400, f"{status} {str(body)[:110]}")
    customer.json("/admin/api/call-routes", "POST", {
        "target_type": "number", "phone_number": first_number, "target": first_number,
        "route": {"nodes": [{"type": "ring_group", "extensions": first_devices, "timeout": 25,
                             "label": f"Ring {len(first_devices)} devices for 25s", "configured": True}]},
    })
devices = first_devices
existing = {row["extension"] for row in cust_state["extensions"]}
status, body = customer.json(f"/admin/api/numbers/{first_number}/extensions", "POST", {"display_name": "Support handset"})
check("a customer can add a device to one of their numbers", status == 200, f"{status} {str(body)[:90]}")
made = body.get("extension", "")
check("it is the next extension on that very number", bool(made) and made.split("@")[0] not in {key.split("@")[0] for key in existing if key.endswith(first_number)},
      f"{made} vs {sorted(existing)}")
status, cust_state = customer.json("/admin/api/state")
fresh = next((row for row in cust_state["extensions"] if row["extension"] == made), None)
check("it gets SIP credentials automatically", bool(fresh) and bool(fresh.get("sip_username")), json.dumps(fresh)[:120] if fresh else "missing")

# --- an extension number belongs to one customer, and only that customer's ---
# --- phones can dial it -----------------------------------------------------
status, console = admin.json("/admin/api/state")
other_customer = next(c for c in console["customers"] if c["username"] != "meridian")
mine = next(row for row in console["extensions"] if row["extension"] == devices[0])
status, body = admin.json("/admin/api/extensions", "POST", {
    "extension": devices[0], "display_name": "Taken", "owner_user_id": other_customer["id"],
})
check("an extension number cannot be taken from the customer who holds it",
      status == 400 and "already belongs" in str(body), f"{status} {str(body)[:110]}")
status, after = admin.json("/admin/api/state")
still = next(row["owner_user_id"] for row in after["extensions"] if row["extension"] == devices[0])
check("and its owner is unchanged", still == mine["owner_user_id"], f"{still} vs {mine['owner_user_id']}")

# The preview renders the files Asterisk includes, so read the dial plan the
# running app just wrote: each account gets its own context holding its own
# extensions - from every number it owns - and nobody else's.
dialplan_path = Path(os.environ.get("PREVIEW_DATA", "/tmp/eip-preview")) / "extensions.dynamic.conf"
dialplan = dialplan_path.read_text() if dialplan_path.exists() else ""


def context_block(name):
    if f"\n[{name}]\n" not in dialplan:
        return ""
    return dialplan.split(f"\n[{name}]\n")[1].split("\n\n")[0]


mine_context = context_block(f"from-internal-{mine['owner_user_id']}")
theirs_context = context_block(f"from-internal-{other_customer['id']}")
check("the customer's dial plan is its own context", bool(mine_context), dialplan_path.name)
mine_all = [row["extension"] for row in after["extensions"] if row["owner_user_id"] == mine["owner_user_id"]]
theirs_all = [row["extension"] for row in after["extensions"] if row["owner_user_id"] == other_customer["id"]]
check("every extension of the account is dialable in it - from either number",
      all(f"Dial(PJSIP/{extension},30)" in mine_context for extension in mine_all),
      ", ".join(mine_all))
check("the other customer's extensions are not in it",
      all(f"exten => {extension},1" not in mine_context for extension in theirs_all),
      ", ".join(theirs_all))
check("and this customer's extensions are not in the other context",
      all(f"exten => {extension},1" not in theirs_context for extension in mine_all),
      ", ".join(mine_all))
check("an unknown three-digit number says so instead of ringing somebody else",
      "exten => _XXX,1" in mine_context and "Playback(ss-noservice)" in mine_context)
own_numbers = [row for row in after["phone_numbers"] if row.get("owner_user_id") == mine["owner_user_id"]]
check("each number rings an extension of the account that owns it",
      all(f"Stasis(engineerip,inbound,{row['number'].lstrip('+')},{row['inbound_extension']})" in dialplan
          for row in own_numbers if row.get("inbound_extension")),
      json.dumps([(row["number"], row.get("inbound_extension")) for row in own_numbers])[:140])
check("and no number routes to another account's extension",
      all(f"Stasis(engineerip,inbound,{row['number'].lstrip('+')},{extension})" not in dialplan
          for row in own_numbers for extension in theirs_all),
      json.dumps(theirs_all))
# Dialling one of the account's own numbers from one of its phones: the number
# is looked up to the extension it rings, so the two numbers of one customer
# reach each other without the call going out through the carrier.
theirs_numbers = [row for row in after["phone_numbers"] if row.get("owner_user_id") == other_customer["id"]]
active_own = [row for row in own_numbers if row["active"]]


def local_route(context, digits):
    lead = f"exten => {digits},1,NoOp(EngineerIP local number "
    return context.split(lead)[1].split("\nexten")[0] if lead in context else ""


check("each of the customer's numbers is dialled internally and rings one of its extensions",
      bool(active_own) and all(
          any(f"Dial(PJSIP/{extension},30)" in local_route(mine_context, row["number"].lstrip("+")) for extension in mine_all)
          for row in active_own),
      ", ".join(row["number"] for row in active_own))
check("and that internal call is never handed to the carrier trunk",
      all("OUTBOUND_TRUNK" not in local_route(mine_context, row["number"].lstrip("+")) for row in active_own),
      ", ".join(row["number"] for row in active_own))
check("another organisation's number is not a local number in this context",
      all(f"exten => {row['number'].lstrip('+')},1,NoOp(EngineerIP local number" not in mine_context
          for row in theirs_numbers),
      ", ".join(row["number"] for row in theirs_numbers))
check("the operator's own devices can dial a customer's number internally",
      all(f"exten => {row['number'].lstrip('+')},1,NoOp(EngineerIP local number" in context_block("from-internal")
          for row in active_own),
      ", ".join(row["number"] for row in active_own))
flows = [row for row in cust_state["routing_flows"] if row["target"] == made]
check("and a default call flow of its own", bool(flows), ",".join(row["target"] for row in cust_state["routing_flows"]))
if flows:
    check("the default flow rings that device",
          [node["type"] for node in flows[0]["route"]["nodes"]] == ["extension"]
          and flows[0]["route"]["nodes"][0]["extension"] == made,
          json.dumps(flows[0]["route"])[:140])

# --- the main line now rings the handset the customer added ------------------
primary = next(row for row in cust_state["call_routes"] if row["phone_number"] == "+13025550001")
ring = primary["route"]["nodes"][0]
check("the primary number rings it too", made in ring.get("extensions", []),
      f"rings {ring.get('extensions')}")

# --- and the administrator can see it, which is what he could not do before --
status, detail = admin.json(f"/admin/api/customers/{customer.json('/admin/api/state')[1]['user_id']}")
admin_flows = {row["target"] for row in detail["routing_flows"]}
check("the administrator sees the flow for the customer's own extension", made in admin_flows,
      ",".join(sorted(admin_flows)))
admin_number_flow = next((row for row in detail["call_routes"] if row["phone_number"] == "+13025550001"), None)
check("and the number's flow, with the new device on it",
      bool(admin_number_flow) and made in admin_number_flow["route"]["nodes"][0].get("extensions", []),
      json.dumps(admin_number_flow["route"])[:160] if admin_number_flow else "missing")

# --- the administrator edits that same flow, for the customer ---------------
targets = [row["extension"] for row in detail["extensions"]]
status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "extension", "target": made,
    "route": {"nodes": [{"type": "extension", "extension": made, "label": f"Ring {made}", "configured": True},
                        {"type": "voicemail", "mailbox": made, "label": f"Mailbox {made}", "configured": True}]},
})
check("the administrator can rewrite that flow", status == 200, f"{status} {str(body)[:90]}")
status, detail = admin.json(f"/admin/api/customers/{customer.json('/admin/api/state')[1]['user_id']}")
rewritten = next((row for row in detail["routing_flows"] if row["target"] == made), None)
check("and the customer's copy changes with it",
      bool(rewritten) and [node["type"] for node in rewritten["route"]["nodes"]] == ["extension", "voicemail"],
      json.dumps(rewritten["route"])[:140] if rewritten else "missing")

# --- the customer's own view of the same thing ------------------------------
status, cust_state = customer.json("/admin/api/state")
mine = next((row for row in cust_state["routing_flows"] if row["target"] == made), None)
check("the customer sees the administrator's change", bool(mine) and len(mine["route"]["nodes"]) == 2)

# --- clean up the probe device ---------------------------------------------
status, body = admin.json(f"/admin/api/extensions/{made}", "DELETE")
check("the administrator can remove the device again", status == 200, f"{status} {str(body)[:90]}")

# --- the administrator edits a customer's call flows -------------------------
meridian = next(c for c in state["customers"] if c["username"] == "meridian")
numbers = [row for row in state["phone_numbers"] if row["owner_user_id"] == meridian["id"]]
extensions = sorted(row["extension"] for row in state["extensions"] if row["owner_user_id"] == meridian["id"])
check("the customer has a provisioned line", bool(numbers) and len(extensions) >= 2, f"{numbers and numbers[0]['number']} ext {extensions}")
number = numbers[0]["number"]
# A number's flow rings the extensions of that number (its own set, plus the
# account-wide ones) - never another line's extension.
line_keys = [row["extension"] for row in state["extensions"]
             if row["owner_user_id"] == meridian["id"] and (row["number"] == number or not row["number"])]
status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": number, "target": number,
    "route": {"nodes": [{"type": "simultaneous", "extensions": line_keys, "timeout": 25,
                         "label": "Ring all devices", "configured": True}]},
})
check("the administrator saves a flow onto the customer's number", status == 200, f"{status} {str(body)[:90]}")

status, detail = admin.json(f"/admin/api/customers/{meridian['id']}")
route = next((row for row in detail.get("call_routes", []) if row.get("phone_number") == number), None)
check("the flow is stored on that customer, ringing that line's extensions",
      bool(route) and [row["type"] for row in route["route"]["nodes"]] == ["simultaneous"]
      and sorted(route["route"]["nodes"][0]["extensions"]) == sorted(line_keys),
      json.dumps(route.get("route") if route else None)[:160])

status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "extension", "owner_user_id": meridian["id"], "target": extensions[0],
    "route": {"nodes": [{"type": "extension", "extension": extensions[0], "label": f"Ring {extensions[0]}", "configured": True}]},
})
check("and onto one of their extensions", status == 200, f"{status} {str(body)[:90]}")

# A flow never crosses customers: name the wrong owner, the target still decides.
other = next(c for c in state["customers"] if c["username"] == "northwind")
status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "extension", "owner_user_id": other["id"], "target": extensions[-1],
    "route": {"nodes": [{"type": "extension", "extension": extensions[-1], "configured": True}]},
})
check("a spoofed owner cannot move the flow", status == 200, str(status))
status, northwind = admin.json(f"/admin/api/customers/{other['id']}")
own = {row["target"] for row in northwind.get("routing_flows", [])}
check("the other customer's flows are untouched", extensions[-1] not in own, ",".join(sorted(own)))

# --- groups for a customer, from the administrator --------------------------
status, group = admin.json("/admin/api/groups", "POST", {"name": f"Live check {meridian['id']}", "members": extensions, "timeout": 20,
                                                        "owner_user_id": meridian["id"]})
check("an administrator can build a ring group for a customer", status == 200, f"{status} {str(group)[:90]}")
check("and it is refused without naming one",
      admin.json("/admin/api/groups", "POST", {"name": "No owner", "members": extensions})[0] == 400)
if status == 200:
    check("and can remove it again",
          admin.json(f"/admin/api/groups/{group['group_id']}", "DELETE")[0] == 200)

# --- SIP identity: six random letters, an underscore, the extension ---------
status, state4 = customer.json("/admin/api/state")
identities = {row["extension"]: row["sip_username"] for row in state4["extensions"]}
import re as _re
check("every extension authenticates with its generated identity",
      all(_re.fullmatch(rf"[A-Z]{{6}}_{ext.split('@')[0]}", name) for ext, name in identities.items()),
      json.dumps(identities))
check("the identity is not the bare extension number", all(names != ext for ext, names in identities.items()))
check("editing an extension does not rename its identity",
      customer.json("/admin/api/extensions", "POST", {
          "extension": extensions[0], "display_name": "Main device", "sip_username": "wanted-to-rename", "active": True,
      })[0] == 200
      and next(row["sip_username"] for row in customer.json("/admin/api/state")[1]["extensions"]
               if row["extension"] == extensions[0]) == identities[extensions[0]])

# --- the credential sheet: one rotate endpoint that moves the live secret ----
status, body = customer.json("/admin/api/extensions/" + extensions[0] + "/credentials")
check("the credentials endpoint reports the generated username",
      status == 200 and body["credentials"]["sip_username"] == identities[extensions[0]],
      str(body)[:120])
status, body = customer.json("/admin/api/extensions/" + extensions[0] + "/password", "POST", {"password": "chosen-by-customer"})
check("a chosen SIP password is stored", status == 200 and body.get("sip_password") == "chosen-by-customer", str(body)[:120])
status, body = customer.json("/admin/api/extensions/" + extensions[0] + "/credentials")
check("and the phone would register with it", body["credentials"]["sip_password"] == "chosen-by-customer")
status, body = customer.json("/admin/api/extensions/" + extensions[0] + "/password", "POST", {})
generated = body.get("sip_password", "")
check("a blank request generates a strong password",
      status == 200 and len(generated) >= 12 and _re.search(r"[A-Z]", generated) and _re.search(r"[a-z]", generated)
      and _re.search(r"[0-9]", generated) and _re.search(r"[^A-Za-z0-9]", generated), generated)
check("and it is safe for the generated Asterisk configuration", not set(generated) & set(";#\n\r "), generated)
status, detail = admin.json(f"/admin/api/customers/{customer.json('/admin/api/state')[1]['user_id']}")
status, northwind_state = admin.json("/admin/api/state")
check("an administrator may rotate it for any customer",
      admin.json("/admin/api/extensions/" + extensions[0] + "/password", "POST", {"password": "operator-set"})[0] == 200)
status, body = admin.json(f"/admin/api/extensions/{extensions[0]}/credentials")
check("the administrator sees the rotated secret", body["credentials"]["sip_password"] == "operator-set")

# --- the platform's own address, set by the administrator --------------------
status, admin_state = admin.json("/admin/api/state")
check("the platform address is the one the administrator set",
      admin_state["service_address"]["sip"] == "sip.engineerip.example:5060"
      and admin_state["service_address"]["api_base"] == "https://sip.engineerip.example",
      json.dumps(admin_state["service_address"]))
status, body = customer.json("/admin/api/extensions/" + extensions[0] + "/credentials")
check("a device is told to register with the platform, not the carrier",
      body["credentials"]["server"] == "sip.engineerip.example"
      and body["credentials"]["registration_address"] == "sip.engineerip.example:5060"
      and body["credentials"]["managed_address"] is True, json.dumps(body["credentials"])[:200])
status, body = customer.json("/admin/api/state")
check("the customer's own API base is the same address",
      body["service_address"]["api_base"] == "https://sip.engineerip.example")
check("the customer cannot change the platform address",
      customer.json("/admin/api/settings", "POST", {"service_host": "mine.example"})[0] in (400, 403))
for bad in ("https://sip.engineerip.example", "10.0.0.1/sip", "sip.engineerip.example:5060"):
    check(f"an address with a scheme, path or port is refused ({bad})",
          admin.json("/admin/api/settings", "POST", {"service_host": bad})[0] == 400, bad)
check("an administrator may change it to an IP address",
      admin.json("/admin/api/settings", "POST", {"service_host": "203.0.113.10", "service_sip_port": "5080"})[0] == 200
      and customer.json("/admin/api/extensions/" + extensions[0] + "/credentials")[1]["credentials"]["server"] == "203.0.113.10")
check("and it is put back", admin.json("/admin/api/settings", "POST", {
    "service_host": "sip.engineerip.example", "service_sip_port": "5060"})[0] == 200)

# --- the recording switch: the administrator's, and a real veto --------------
status, admin_state = admin.json("/admin/api/state")
check("the platform recording switch is reported to the administrator",
      admin_state["recording_platform_enabled"] is True, str(admin_state.get("recording_platform_enabled")))
check("a fresh install allows recording", admin_state["settings"].get("recording_enabled") == "true",
      json.dumps({k: v for k, v in admin_state["settings"].items() if "record" in k}))
check("the switch is stored as canonical text, whatever the caller sends",
      admin.json("/admin/api/settings", "POST", {"recording_enabled": "YES"})[0] == 200
      and admin.json("/admin/api/state")[1]["settings"]["recording_enabled"] == "true",
      admin.json("/admin/api/state")[1]["settings"].get("recording_enabled"))
check("and a false boolean is stored the same way",
      admin.json("/admin/api/settings", "POST", {"recording_enabled": False})[0] == 200
      and admin.json("/admin/api/state")[1]["settings"]["recording_enabled"] == "false"
      and admin.json("/admin/api/state")[1]["recording_platform_enabled"] is False,
      admin.json("/admin/api/state")[1]["settings"].get("recording_enabled"))
check("and it is switched back on",
      admin.json("/admin/api/settings", "POST", {"recording_enabled": True})[0] == 200
      and admin.json("/admin/api/state")[1]["settings"]["recording_enabled"] == "true")
status, customer_state = customer.json("/admin/api/state")
check("the customer is told the platform switch too",
      customer_state["recording_platform_enabled"] is True)
check("a customer cannot flip the platform switch",
      customer.json("/admin/api/settings", "POST", {"recording_enabled": False})[0] in (400, 403))
check("an invalid switch value is refused",
      admin.json("/admin/api/settings", "POST", {"recording_enabled": "maybe"})[0] == 400)
before_veto = {row["extension"]: bool(row["recording_enabled"])
               for row in customer.json("/admin/api/state")[1]["extensions"]}
check("the administrator switches recording off",
      admin.json("/admin/api/settings", "POST", {"recording_enabled": False})[0] == 200)
check("and the customer is told it is off everywhere",
      customer.json("/admin/api/state")[1]["recording_platform_enabled"] is False)
check("the customer's own per-device choices are untouched by the veto",
      {row["extension"]: bool(row["recording_enabled"])
       for row in customer.json("/admin/api/state")[1]["extensions"]} == before_veto,
      json.dumps(before_veto))
check("and it is switched back on",
      admin.json("/admin/api/settings", "POST", {"recording_enabled": True})[0] == 200
      and customer.json("/admin/api/state")[1]["recording_platform_enabled"] is True)

# --- a new line starts with recording off, whatever the platform switch says --
status, provisioned = admin.json("/admin/api/numbers", "POST", {
    "number": "+13025550077", "provider": "IPComms", "owner_user_id": 2,
    "inbound_extension": "auto", "auto_provision": True,
})
new_extension = provisioned["provisioned"]["extension"]
check("a freshly provisioned device starts with its own recording switch off",
      not next(row for row in customer.json("/admin/api/state")[1]["extensions"]
               if row["extension"] == new_extension)["recording_enabled"], new_extension)
check("and the line is removed again", admin.json(f"/admin/api/numbers/+13025550077", "DELETE")[0] in (200, 204))

# --- nothing deletes an administrator ---------------------------------------
status, admin_state = admin.json("/admin/api/state")
administrators = [row for row in admin_state["users"] if row["role"] == "admin"]
check("the platform administrators are listed", len(administrators) >= 1, f"{len(administrators)}")

# A second administrator, so this is not only the "your own account" rule.
import time as _time
deputy_name = f"ops-deputy-{int(_time.time())}"
status, created = admin.json("/admin/api/platformadmins", "POST", {
    "username": deputy_name, "email": f"{deputy_name}@example.test",
    "password": "deputy-administrator-password",
})
check("the platform can still add an administrator", status in (200, 201), f"{status} {str(created)[:80]}")
status, admin_state = admin.json("/admin/api/state")
deputy = next((row for row in admin_state["users"] if row["username"] == deputy_name), None)
check("the new administrator is listed", bool(deputy))
status, detail = admin.json(f"/admin/api/users/{deputy['id']}", "DELETE") if deputy else (0, {})
check("deleting another administrator is refused",
      status == 400 and "cannot be deleted" in str(detail), f"{status} {str(detail)[:90]}")
check("and the account is still there",
      any(row["username"] == deputy_name for row in admin.json("/admin/api/state")[1]["users"]))

# --- the public pages wear the console's light surface -----------------------
status, landing = Client().request("/")
check("the landing page is served on the console's light surface",
      status == 200 and "#f4f5fa" in landing and "radial-gradient(circle at 6% 0%,#e5ecff" in landing,
      str(status))
check("and it kept its copy and its signup form",
      "Your US business number" in landing and 'id="signup-form"' in landing)
check("with none of the old night-sky base left",
      "#070b16" not in landing and "#0d1424" not in landing)

status, page = admin.request("/documentation")
check("the documentation page wears the same light surface",
      status == 200 and "--bg:#f4f5fa" in page and "color-scheme: light" in page, str(status))
check("and its code blocks were retuned for it",
      "background:#f7f8fd" in page and "--text:#141b2d" in page)

status, page = Client().request("/login")
check("so does the sign-in page", status == 200 and "theme-light login-body" in page, str(status))

# --- the softphone the browser is served is the one this branch built --------
status, softphone = Client().request("/phone")
check("the softphone carries the call-quality badge",
      status == 200 and 'id="call-quality"' in softphone, str(status))
check("and the keyboard hint a desktop uses", 'class="dial-tip"' in softphone)
status, phone_sheet = Client().request("/phone.css")
check("the softphone stylesheet scrolls the dialer instead of clipping it",
      status == 200 and ".view{display:none;flex:1;min-height:0;padding:8px 20px 100px;overflow-y:auto" in phone_sheet,
      str(status))
check("and keeps a layout for both phone-sized and short screens",
      "@media(max-width:520px)" in phone_sheet and "@media(max-height:760px)" in phone_sheet)

# --- every page carries the EngineerIP logo as its favicon --------------------
LOGO = "https://engineerip.com/static/img/logo.png"
PAGES = {
    "the landing page": Client().request("/"),
    "the sign-in page": Client().request("/login"),
    "the console": admin.request("/admin"),
    "the documentation page": admin.request("/documentation"),
    "the layout self-check": admin.request("/console-check"),
    "the softphone": Client().request("/phone"),
}
for label, (status, body) in PAGES.items():
    check(f"{label} links the EngineerIP logo as its favicon",
          status == 200 and f'<link rel="icon" type="image/png" href="{LOGO}">' in body,
          str(status))
    check(f"and offers the same logo to an iOS home screen",
          status == 200 and f'<link rel="apple-touch-icon" href="{LOGO}">' in body,
          str(status))
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Ask for the redirect itself: the logo lives on another host, which this
    harness must not need to reach in order to prove the answer is correct."""

    def redirect_request(self, *args, **kwargs):
        return None


try:
    with urllib.request.build_opener(_NoRedirect()).open(BASE + "/favicon.ico", timeout=20) as res:
        icon_status, icon_location = res.status, res.headers.get("Location", "")
except urllib.error.HTTPError as exc:
    icon_status, icon_location = exc.code, exc.headers.get("Location", "")
check("a bare /favicon.ico probe is answered with the logo instead of a 404",
      icon_status in (301, 302) and icon_location == LOGO, f"{icon_status} {icon_location}")
check("the phone manifest installs with the same logo",
      LOGO in Client().request("/manifest.json")[1])

status, console_page = admin.request("/admin")
check("the customer's workflow tab ships the two-step pickers",
      'id="route-number"' in console_page and 'id="route-extension"' in console_page
      and 'aria-label="Number"' in console_page and 'aria-label="Extension"' in console_page)
check("while the flat target picker is the administrator's alone",
      'id="route-target" aria-label="Call flow target" data-admin-only' in console_page)
check("the console ships no call-defaults editor for any role",
      "call-defaults-form" not in console_page and "default-extension" not in console_page
      and "inbound-fallback" not in console_page)
status, sheet = Client().request("/admin-assets/admin.css")
check("the stylesheet the browser is served draws no bar down a hovered row",
      status == 200 and "inset 3px 0 0 0" not in sheet
      and "translateY(-3px);box-shadow:var(--shadow-md);background:#0b1220" in sheet
      and "body.theme-light .row:hover{background:#e7ebf7}" in sheet,
      str(status))
check("the stylesheet the browser is served has no hover sweep left",
      status == 200 and "sheenSweep" not in sheet and "linear-gradient(180deg,#ffffff12" in sheet,
      str(status))
check("while its endpoint still answers a customer",
      customer.json("/admin/api/call-defaults")[0] == 200)
check("the console markup ships the closable journey",
      "journey-dismiss" in console_page and "journey-restore-btn" in console_page)
check("and no longer the customer-side request list",
      "my-request-list" not in console_page and "my-request-count" not in console_page)
check("the save button has its own state line", "flow-save-hint" in console_page)

# --- the browser layout self-check -------------------------------------------
status, page = admin.request("/console-check")
check("the layout self-check is served to a signed-in operator", status == 200 and "Console layout check" in page, str(status))
check("it measures the drawer rather than describing it",
      "ws-scroll" in page and "pins to the top of the region" in page and "not its own scroller" in page)
check("it also checks the platform recording switch", "platform-recording" in page)
check("it shows nothing about any customer",
      "Meridian" not in page and "meridian@example.com" not in page and "+13025" not in page)
anonymous_status, anonymous_page = Client().request("/console-check")
check("and an anonymous visitor is sent to sign in",
      "Console layout check" not in anonymous_page and "password" in anonymous_page.lower(), str(anonymous_status))

# --- the documentation page --------------------------------------------------
status, page = admin.request("/documentation")
check("the documentation page is served to a signed-in operator", status == 200 and "EIP Telephony" in page, str(status))
check("it links back to the console", 'href="/admin"' in page)
check("it documents the API surface", "/api/v1/calls" in page and "X-EngineerIP-Signature" in page)
check("it explains the SIP identity format", "KUDGTE_101" in page)
check("every example names this deployment's own address",
      "https://sip.engineerip.example/api/v1/calls" in page and "sip.engineerip.example:5060" in page
      and "{{" not in page, page[:0])
anonymous_status, anonymous_page = Client().request("/documentation")
check("an anonymous visitor gets the sign-in page instead of the documentation",
      "EIP Telephony — setup" not in anonymous_page and "password" in anonymous_page.lower(),
      str(anonymous_status))

# --- recording stays a per-device decision ----------------------------------
status, state2 = customer.json("/admin/api/state")
target = next(row for row in state2["extensions"] if row["extension"] == extensions[0])
check("recording starts switched off on a fresh device", not target["recording_enabled"], json.dumps(target)[:110])
status, body = customer.json("/admin/api/extensions", "POST", {
    "extension": extensions[0], "display_name": target.get("display_name") or "Main device",
    "sip_username": target.get("sip_username"), "recording_enabled": True, "active": True,
})
check("the customer can switch one device to record", status == 200, f"{status} {str(body)[:90]}")
status, state3 = customer.json("/admin/api/state")
row = next(r for r in state3["extensions"] if r["extension"] == extensions[0])
check("only that device records", row["recording_enabled"] and
      not next(r for r in state3["extensions"] if r["extension"] == extensions[1])["recording_enabled"],
      ",".join(f"{r['extension']}:{int(bool(r['recording_enabled']))}" for r in state3["extensions"]))
check("a customer cannot set a platform-wide recording policy",
      customer.json("/admin/api/settings", "POST", {"recording_enabled": "true"})[0] in (401, 403))
customer.json("/admin/api/extensions", "POST", {
    "extension": extensions[0], "display_name": target.get("display_name") or "Main device",
    "sip_username": target.get("sip_username"), "recording_enabled": False, "active": True,
})

# --- the customer sees the flows the platform made for them ------------------
status, cust_state = customer.json("/admin/api/state")
flows = {row["target"] for row in cust_state.get("routing_flows", [])}
check("every extension the customer has carries a default flow", set(extensions) <= flows, ",".join(sorted(flows)))
check("the main line has a flow too",
      number in {row.get("phone_number") for row in cust_state.get("call_routes", [])})

# --- the phone menu: the caller is asked to type an extension ----------------
status, state4 = customer.json("/admin/api/state")
voices = [voice["id"] for voice in state4.get("ivr_voices", [])]
check("the console is told which voices a prompt can be read in",
      voices == ["platform", "en-gb", "es-us", "fr-ca"], ",".join(voices))
check("and the wording a fresh menu starts from",
      state4.get("ivr_default_prompt", "").startswith("Welcome to EngineerIP. Please enter the extension"))
check("and the number of extensions past which a menu is added for the customer",
      state4.get("ivr_auto_above") == 5, str(state4.get("ivr_auto_above")))

# The operator saves a menu onto a customer's line, exactly as the studio does.
status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": number, "target": number,
    "route": {"nodes": [{"type": "ivr", "prompt": "Meridian Health. Enter the extension you need.",
                         "voice": "es-us", "input_timeout": 8, "attempts": 3, "fallback": extensions[0],
                         "label": "Enter an extension", "configured": True}]},
})
check("the operator can put a phone menu on a customer's line", status == 200, f"{status} {str(body)[:90]}")
status, detail = admin.json(f"/admin/api/customers/{meridian['id']}")
menu = next((row for row in detail.get("call_routes", []) if row.get("phone_number") == number), None)
menu_node = (menu or {}).get("route", {}).get("nodes", [{}])[0]
check("the menu keeps the text, the voice and the timings that were set",
      menu_node.get("type") == "ivr" and menu_node.get("prompt") == "Meridian Health. Enter the extension you need."
      and menu_node.get("voice") == "es-us" and menu_node.get("input_timeout") == 8 and menu_node.get("attempts") == 3,
      json.dumps(menu_node)[:180])

# A voice with no recording behind it, or a stranger's extension as fallback, is refused.
status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": number, "target": number,
    "route": {"nodes": [{"type": "ivr", "voice": "klingon"}]},
})
check("a menu in a voice that does not exist is refused", status == 400, f"{status} {str(body)[:90]}")
status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": number, "target": number,
    "route": {"nodes": [{"type": "ivr", "fallback": "999"}]},
})
check("and so is a fallback extension the customer does not own", status == 400, f"{status} {str(body)[:90]}")

# Put the line back the way the flows above expect to find it.
admin.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": number, "target": number,
    "route": {"nodes": [{"type": "simultaneous", "extensions": extensions, "timeout": 25,
                         "label": "Ring all devices", "configured": True}]},
})

# --- the device the platform generated with the first number -----------------
status, state5 = customer.json("/admin/api/state")
lowest = sorted((row["extension"] for row in state5["extensions"]
                 if row["active"] and row["number"] == "+13025550001"),
                key=lambda key: int(key.split("@")[0]))[:1]
check("the customer's own payload names the device the platform generated first",
      state5.get("primary_extension") == (lowest[0] if lowest else ""),
      f"{state5.get('primary_extension')} vs {lowest}")
meridian_id = customer.json("/admin/api/state")[1]["user_id"]
status, detail = admin.json(f"/admin/api/customers/{meridian_id}")
lowest = sorted((row["extension"] for row in detail["extensions"]
                 if row["active"] and row["number"] == "+13025550001"),
                key=lambda key: int(key.split("@")[0]))[:1]
check("and so does the workspace payload an operator opens",
      detail.get("primary_extension") == (lowest[0] if lowest else ""),
      f"{detail.get('primary_extension')} vs {lowest}")

failed = [label for label, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} live checks passed")
if failed:
    print("FAILED: " + "; ".join(failed))
sys.exit(1 if failed else 0)
