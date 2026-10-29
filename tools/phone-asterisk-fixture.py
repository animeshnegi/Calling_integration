#!/usr/bin/env python3
"""Create disposable Asterisk phone fixtures from the real configuration renderer.

Used only by phone-asteriskcheck.js. This never reads production settings or
credentials; all names and passwords below belong to the temporary test PBX.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.admin import SettingsStore, browser_username
from app.telephony_config import TelephonyConfigSync


def main(directory: str) -> None:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    store = SettingsStore(str(path / "settings.db"), "test-only-secret-" * 4)
    store.set_settings({"service_host": "sip.test"})
    for extension in ("101", "102"):
        store.save_extension({
            "extension": extension,
            "sip_username": extension,
            "sip_password": "test-password",
            "webrtc_enabled": True,
        })
    sync = TelephonyConfigSync(store, None, str(path / "pjsip.dynamic.conf"))
    (path / "pjsip.dynamic.conf").write_text(sync.render_pjsip(), encoding="utf-8")
    (path / "extensions.conf").write_text(sync.render_dialplan(), encoding="utf-8")
    extensions = {row["extension"]: row["sip_username"] for row in store.list_extensions()}
    print(json.dumps({
        "plain_102": extensions["102"],
        "browser_101": browser_username(extensions["101"]),
        "browser_102": browser_username(extensions["102"]),
    }))


if __name__ == "__main__":
    main(sys.argv[1])
