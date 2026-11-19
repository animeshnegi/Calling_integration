"""Narrow account operations at the public/administrative trust boundary."""
SIGNUP_FIELDS = frozenset({"username", "email", "password", "full_name", "company_name", "job_role", "phone"})


def create_customer(store, data):
    if not isinstance(data, dict) or set(data) - SIGNUP_FIELDS:
        raise ValueError("Signup accepts only customer profile fields")
    required = {"full_name": "Name", "company_name": "Company name", "job_role": "Role", "phone": "Phone number"}
    missing = [label for field, label in required.items() if not str(data.get(field, "")).strip()]
    if missing:
        raise ValueError(f"Required fields: {', '.join(missing)}")
    return store.save_user({**data, "role": "user", "extension": "", "active": True})


def update_user_as_admin(store, data, actor_id):
    actor = store.get_user(actor_id)
    if not actor or actor["role"] != "admin" or not actor["active"]:
        raise ValueError("Administrator permission required")
    return store.save_user(data, actor_id)
