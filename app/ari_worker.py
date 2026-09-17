from __future__ import annotations

import time

from . import create_app
from .config import Config


def _sync_asterisk_config(app) -> None:
    """Wait for AMI to become usable before starting the ARI event loop."""
    sync = app.extensions["telephony_config_sync"]
    delay = 2.0
    for attempt in range(1, 11):
        try:
            sync.apply()
            app.logger.info("Initial Asterisk database configuration sync completed")
            return
        except Exception:
            if attempt == 10:
                raise
            app.logger.warning(
                "Asterisk configuration sync attempt %s/10 failed; retrying in %.1fs",
                attempt,
                delay,
                exc_info=True,
            )
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)


def main() -> None:
    # Asterisk is health-gated by docker-compose, but AMI can still need a
    # short additional startup window. Do the sync explicitly with retries
    # instead of allowing one transient AMI failure to leave the worker
    # running with stale database configuration.
    app = create_app(Config, start_ari=False, sync_config=False)
    _sync_asterisk_config(app)

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
