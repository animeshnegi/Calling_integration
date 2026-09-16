from __future__ import annotations

import time

from . import create_app
from .config import Config


def main() -> None:
    app = create_app(Config, start_ari=False, sync_config=True)
    service = app.extensions["telephony_service"]
    service.recover_incomplete_calls()

    def on_event(event: dict):
        try:
            service.handle_ari_event(event)
        except Exception:
            app.logger.exception("Failed to process Asterisk ARI event")

    from .asterisk_client import ari_event_loop

    ari_event_loop(on_event, Config)
    last_cleanup = 0.0
    while True:
        now = time.monotonic()
        if now - last_cleanup >= 3600:
            try:
                service.cleanup_recordings()
            except Exception:
                app.logger.exception("Recording retention cleanup failed")
            last_cleanup = now
        time.sleep(30)


if __name__ == "__main__":
    main()
