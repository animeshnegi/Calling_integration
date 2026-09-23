"""Secure interactive customer provisioning for a deployed EIP instance."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys

from .admin import SettingsStore


def provision_customer(store: SettingsStore, username: str, email: str, password: str) -> tuple[int, bool]:
    username = str(username or "").strip().lower()
    email = str(email or "").strip().lower()
    existing = next((row for row in store.list_users() if row["username"] == username), None)
    if existing and existing["role"] != "user":
        raise ValueError("That username belongs to a platform administrator")
    data = {
        "username": username,
        "email": email,
        "password": password,
        "role": "user",
        "extension": "",
        "active": True,
    }
    if existing:
        data["id"] = existing["id"]
    return store.save_user(data), existing is None


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update an EIP customer account")
    parser.add_argument("--username", default="engineerip", help="Customer login name (default: engineerip)")
    parser.add_argument("--email", help="Customer email; prompted when omitted")
    args = parser.parse_args()

    email = (args.email or input("Customer email: ")).strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        print("Error: enter a valid customer email.", file=sys.stderr)
        return 2
    password = getpass.getpass("Customer password (minimum 14 characters): ")
    confirmation = getpass.getpass("Confirm customer password: ")
    if password != confirmation:
        print("Error: passwords do not match.", file=sys.stderr)
        return 2

    db_path = os.getenv("DATABASE_URI") or os.getenv("SETTINGS_DB_PATH", "/app/instance/settings.db")
    secret_key = os.getenv("SECRET_KEY", "")
    if not secret_key:
        print("Error: SECRET_KEY is not available in this container.", file=sys.stderr)
        return 2
    try:
        user_id, created = provision_customer(SettingsStore(db_path, secret_key), args.username, email, password)
    except (ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    action = "created" if created else "updated"
    print(f"Customer '{args.username.lower()}' {action} successfully (user ID {user_id}).")
    print("No number or SIP provider was assigned. Assign resources from Customers/Extensions/Phone numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
