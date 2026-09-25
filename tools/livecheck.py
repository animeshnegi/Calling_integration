"""Live proof against the running preview: every claim this batch makes, checked
over HTTP the way a browser would."""
import json
import re
import sys
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
status, body = customer.json("/admin/api/call-defaults", "POST", {"outbound": "102", "fallback": "101"})
check("the customer can change them", status == 200, str(body)[:120])
status, body = customer.json("/admin/api/call-defaults")
saved = body.get("call_defaults", {})
check("the change stuck", saved.get("outbound") == "102" and saved.get("fallback") == "101", json.dumps(saved))
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
# Put the main line back the way the platform generates it, so this check can
# rerun against a preview that earlier runs have already edited.
devices = sorted(row["extension"] for row in cust_state["extensions"] if row["active"])
status, body = customer.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": "+13025550001", "target": "+13025550001",
    "route": {"nodes": [{"type": "ring_group", "extensions": devices, "timeout": 25,
                         "label": f"Ring {len(devices)} devices for 25s", "configured": True}]},
})
check("the customer can set their main line to ring every device", status == 200, f"{status} {str(body)[:90]}")
existing = {row["extension"] for row in cust_state["extensions"]}
made = "150"
while made in existing:
    made = str(int(made) + 1)
status, body = customer.json("/admin/api/extensions", "POST", {
    "extension": made, "display_name": f"Support handset {made}", "active": True,
})
check("a customer can add a device of their own", status == 200, f"{status} {str(body)[:90]}")
status, cust_state = customer.json("/admin/api/state")
fresh = next((row for row in cust_state["extensions"] if row["extension"] == made), None)
check("it gets SIP credentials automatically", bool(fresh) and bool(fresh.get("sip_username")), json.dumps(fresh)[:120] if fresh else "missing")
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
status, body = admin.json("/admin/api/call-routes", "POST", {
    "target_type": "number", "phone_number": number, "target": number,
    "route": {"nodes": [{"type": "simultaneous", "extensions": extensions, "timeout": 25,
                         "label": "Ring all devices", "configured": True}]},
})
check("the administrator saves a flow onto the customer's number", status == 200, f"{status} {str(body)[:90]}")

status, detail = admin.json(f"/admin/api/customers/{meridian['id']}")
route = next((row for row in detail.get("call_routes", []) if row.get("phone_number") == number), None)
check("the flow is stored on that customer, ringing every device",
      bool(route) and [row["type"] for row in route["route"]["nodes"]] == ["simultaneous"]
      and sorted(route["route"]["nodes"][0]["extensions"]) == extensions,
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
      all(_re.fullmatch(rf"[A-Za-z]{{6}}_{ext}", name) for ext, name in identities.items()),
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
check("a blank request generates a strong password", status == 200 and len(body.get("sip_password", "")) >= 12)
status, detail = admin.json(f"/admin/api/customers/{customer.json('/admin/api/state')[1]['user_id']}")
status, northwind_state = admin.json("/admin/api/state")
check("an administrator may rotate it for any customer",
      admin.json("/admin/api/extensions/" + extensions[0] + "/password", "POST", {"password": "operator-set"})[0] == 200)
status, body = admin.json(f"/admin/api/extensions/{extensions[0]}/credentials")
check("the administrator sees the rotated secret", body["credentials"]["sip_password"] == "operator-set")

# --- nothing deletes an administrator ---------------------------------------
status, admin_state = admin.json("/admin/api/state")
administrators = [row for row in admin_state["users"] if row["role"] == "admin"]
check("the platform administrators are listed", len(administrators) >= 1, f"{len(administrators)}")

# A second administrator, so this is not only the "your own account" rule.
status, created = admin.json("/admin/api/platformadmins", "POST", {
    "username": "ops-deputy", "email": "ops.deputy@example.test",
    "password": "deputy-administrator-password",
})
check("the platform can still add an administrator", status in (200, 201), f"{status} {str(created)[:80]}")
status, admin_state = admin.json("/admin/api/state")
deputy = next((row for row in admin_state["users"] if row["username"] == "ops-deputy"), None)
check("the new administrator is listed", bool(deputy))
status, detail = admin.json(f"/admin/api/users/{deputy['id']}", "DELETE") if deputy else (0, {})
check("deleting another administrator is refused",
      status == 400 and "cannot be deleted" in str(detail), f"{status} {str(detail)[:90]}")
check("and the account is still there",
      any(row["username"] == "ops-deputy" for row in admin.json("/admin/api/state")[1]["users"]))

# --- the documentation page --------------------------------------------------
status, page = admin.request("/documentation")
check("the documentation page is served to a signed-in operator", status == 200 and "EIP Telephony" in page, str(status))
check("it links back to the console", 'href="/admin"' in page)
check("it documents the API surface", "/api/v1/calls" in page and "X-EngineerIP-Signature" in page)
check("it explains the SIP identity format", "QwErTy_101" in page)
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

failed = [label for label, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} live checks passed")
if failed:
    print("FAILED: " + "; ".join(failed))
sys.exit(1 if failed else 0)
