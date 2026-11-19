from __future__ import annotations

import time
import threading

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


def _ivr_clock(service, logger, stopped, wake, lock):
    """Independent prompt clock: outbound network maintenance cannot delay it."""
    while not stopped.is_set():
        wake.wait(1)
        wake.clear()
        if stopped.is_set():
            break
        try:
            with lock:
                service.process_ivr_timeouts()
        except Exception:
            logger.exception("IVR prompt timeout processing failed")


def main() -> None:
    # Asterisk is health-gated by docker-compose, but AMI can still need a
    # short additional startup window. Do the sync explicitly with retries
    # instead of allowing one transient AMI failure to leave the worker
    # running with stale database configuration.
    app = create_app(Config, start_ari=False, sync_config=False)
    _sync_asterisk_config(app)

    service = app.extensions["telephony_service"]
    service.recover_incomplete_calls()

    stopped, wake = threading.Event(), threading.Event()
    call_lock = threading.RLock()

    def on_event(event: dict):
        try:
            with call_lock:
                service.handle_ari_event(event)
            wake.set()
        except Exception:
            app.logger.exception("Failed to process Asterisk ARI event")

    from .asterisk_client import ari_event_loop

    ari_event_loop(on_event, Config)
    clock = threading.Thread(target=_ivr_clock, args=(service, app.logger, stopped, wake, call_lock), daemon=True, name="ivr-clock")
    clock.start()
    last_cleanup = 0.0
    last_voicemail_scan = 0.0
    last_webhook_scan = 0.0
    last_recording_scan = 0.0
    last_config_retry = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_config_retry >= 30:
                try:
                    app.extensions["telephony_config_sync"].retry_pending()
                except Exception:
                    app.logger.exception("Pending Asterisk RTP/configuration retry failed")
                last_config_retry = now
            if now - last_recording_scan >= 30:
                try:
                    service.reconcile_recordings()
                except Exception:
                    app.logger.exception("Recording reconciliation failed")
                last_recording_scan = now
            if now - last_webhook_scan >= 5:
                try:
                    service.process_webhook_deliveries()
                except Exception:
                    app.logger.exception("Webhook delivery processing failed")
                last_webhook_scan = now
            if now - last_voicemail_scan >= 30:
                try:
                    app.extensions["voicemail_notifier"].process_background()
                except Exception:
                    app.logger.exception("Voicemail email processing failed")
                last_voicemail_scan = now
            if now - last_cleanup >= 3600:
                try:
                    service.cleanup_recordings()
                except Exception:
                    app.logger.exception("Recording retention cleanup failed")
                last_cleanup = now
            time.sleep(5)
    finally:
        stopped.set()
        wake.set()
        clock.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Same reasoning as wsgi.py: the container restarts immediately on
        # exit, so a persistent boot failure must not become a hot loop.
        import logging

        logging.getLogger(__name__).exception("Fatal boot failure; pausing before exit to avoid a hot restart loop")
        time.sleep(10)
        raise
