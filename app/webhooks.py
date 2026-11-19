"""Webhook endpoint and delivery persistence, separate from HTTP authorization."""
from __future__ import annotations

import re
import uuid


class WebhookStoreMixin:
    def list_webhooks(self, include_tokens: bool = False, owner_user_id: int | None = None):
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(
                f"SELECT id,name,url,token_enc,events,active,created_at,updated_at,owner_user_id FROM webhook_endpoints{where} ORDER BY name",
                (int(owner_user_id),) if owner_user_id is not None else (),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            encrypted = item.pop("token_enc")
            item["has_token"] = bool(encrypted)
            if include_tokens:
                item["token"] = self.decrypt(encrypted) if encrypted else ""
            result.append(item)
        return result

    def save_webhook(self, data, owner_user_id: int | None = None):
        from .webhook_transport import validate_url

        name = self._validate_config_value(data.get("name", ""), "webhook name", 80)
        url = str(data.get("url", "")).strip()
        validate_url(url)
        events = str(data.get("events", "*")).strip() or "*"
        event_items = [item.strip() for item in events.split(",") if item.strip()]
        if len(event_items) > 50 or any(not re.fullmatch(r"(?:\*|call\.[a-z_]+)", item) for item in event_items):
            raise ValueError("Events must be * or comma-separated call event names")
        events = ",".join(dict.fromkeys(event_items))
        token = str(data.get("token") or "")
        if len(token) > 1000 or any(char in token for char in "\r\n"):
            raise ValueError("Invalid webhook token")
        webhook_id = data.get("id")
        with self._connect() as db:
            existing = None
            if webhook_id not in {None, ""}:
                existing = db.execute("SELECT id,token_enc,owner_user_id FROM webhook_endpoints WHERE id=?", (int(webhook_id),)).fetchone()
                if existing and owner_user_id is not None and existing["owner_user_id"] != int(owner_user_id):
                    raise ValueError("Webhook not found")
            if existing is None:
                existing = db.execute("SELECT id,token_enc,owner_user_id FROM webhook_endpoints WHERE name=? AND (owner_user_id=? OR (owner_user_id IS NULL AND ? IS NULL))", (name, owner_user_id, owner_user_id)).fetchone()
            encrypted = self.encrypt(token) if token else (existing["token_enc"] if existing else "")
            conflict = db.execute("SELECT id FROM webhook_endpoints WHERE name=?", (name,)).fetchone()
            if conflict and (not existing or conflict["id"] != existing["id"]):
                raise ValueError("A webhook with this name already exists")
            if existing:
                db.execute(
                    "UPDATE webhook_endpoints SET name=?,url=?,token_enc=?,events=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (name, url, encrypted, events, int(bool(data.get("active", True))), existing["id"]),
                )
                return existing["id"]
            cursor = db.execute(
                "INSERT INTO webhook_endpoints(name,url,token_enc,events,active,owner_user_id) VALUES(?,?,?,?,?,?)",
                (name, url, encrypted, events, int(bool(data.get("active", True))), owner_user_id),
            )
            return cursor.lastrowid

    def enqueue_webhook(self, endpoint_id: int, event: str, payload: dict):
        import json
        delivery_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO webhook_deliveries(id,endpoint_id,event,payload) VALUES(?,?,?,?)",
                       (delivery_id, int(endpoint_id), event, json.dumps(payload, separators=(",", ":"), sort_keys=True)))
        return delivery_id

    def pending_webhook_deliveries(self, limit: int = 100):
        import json
        with self._connect() as db:
            db.execute("DELETE FROM webhook_deliveries WHERE (status='delivered' OR attempts>=5) AND updated_at<datetime('now','-30 days')")
            rows = db.execute("SELECT id,endpoint_id,event,payload,attempts FROM webhook_deliveries WHERE status IN ('pending','failed') AND attempts<5 AND next_attempt_at<=CURRENT_TIMESTAMP ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        result = []
        endpoints = {row["id"]: row for row in self.list_webhooks(include_tokens=True)}
        for row in rows:
            endpoint = endpoints.get(row["endpoint_id"])
            if endpoint and endpoint["active"]:
                item = dict(row); item["payload"] = json.loads(item["payload"]); item["endpoint"] = endpoint; result.append(item)
        return result

    def finish_webhook_delivery(self, delivery_id: str, success: bool, error: str | None):
        with self._connect() as db:
            db.execute("""UPDATE webhook_deliveries SET status=?,attempts=attempts+1,last_error=?,
                delivered_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE delivered_at END,
                next_attempt_at=datetime('now','+' || MIN(300,30*(attempts+1)) || ' seconds'),updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                ("delivered" if success else "failed", error, int(success), delivery_id))

    def list_webhook_deliveries(self, limit: int = 50, owner_user_id: int | None = None):
        with self._connect() as db:
            if owner_user_id is None:
                rows = db.execute("SELECT id,endpoint_id,event,status,attempts,last_error,delivered_at,created_at,updated_at FROM webhook_deliveries ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT d.id,d.endpoint_id,d.event,d.status,d.attempts,d.last_error,d.delivered_at,d.created_at,d.updated_at FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id=d.endpoint_id WHERE e.owner_user_id=? ORDER BY d.created_at DESC LIMIT ?", (int(owner_user_id), limit)).fetchall()
        return [dict(row) for row in rows]

    def delete_webhook(self, webhook_id, owner_user_id: int | None = None):
        with self._connect() as db:
            if owner_user_id is not None and not db.execute("SELECT 1 FROM webhook_endpoints WHERE id=? AND owner_user_id=?", (int(webhook_id), int(owner_user_id))).fetchone():
                raise ValueError("Webhook not found")
            db.execute("UPDATE webhook_deliveries SET status='failed',attempts=5,last_error='webhook deleted',updated_at=CURRENT_TIMESTAMP WHERE endpoint_id=? AND status!='delivered'", (int(webhook_id),))
            result = db.execute("DELETE FROM webhook_endpoints WHERE id=?", (int(webhook_id),))
            if result.rowcount == 0:
                raise ValueError("Webhook not found")

