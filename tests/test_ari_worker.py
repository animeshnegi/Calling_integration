"""Tests for the dedicated ARI event worker.

``app.ari_worker`` owns the single ARI WebSocket connection, the readiness
marker that gates outbound calling, and the periodic maintenance loop. A
regression here is invisible until a real call fails, so the worker is driven
synchronously with a scripted socket instead of a live Asterisk container.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from app import ari_worker, asterisk_client


class _StopLoop(Exception):
    """Sentinel raised from the patched backoff wait to end the retry loop."""


class RecordingLogger:
    def __init__(self):
        self.infos = []
        self.warnings = []
        self.exceptions = []

    def info(self, message, *args, **kwargs):
        self.infos.append(message % args if args else message)

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message % args if args else message)

    def exception(self, message, *args, **kwargs):
        self.exceptions.append(message % args if args else message)


class ScriptedSocket:
    """Return queued frames, raising any queued exception instead of a frame."""

    def __init__(self, script, *, close_error=None):
        self.script = list(script)
        self.close_error = close_error
        self.closed = False

    def recv(self):
        if not self.script:
            raise AssertionError("scripted ARI socket was read more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        return item

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class EventLoopHarness:
    """Drive ``ari_event_loop`` on the calling thread with a scripted websocket."""

    def __init__(self, tmp_path):
        self.ready = tmp_path / "instance" / "ari.ready"
        self.ready.parent.mkdir(parents=True, exist_ok=True)
        self.config = SimpleNamespace(
            ASTERISK_ARI_URL="http://asterisk:8088/ari",
            ASTERISK_ARI_USER="u",
            ASTERISK_ARI_PASSWORD="p",
            ASTERISK_ARI_APP="engineerip",
            ASTERISK_RECORDING_PATH="/var/spool/asterisk/recording",
            ARI_READY_PATH=str(self.ready),
        )
        self.timeout_error = type("WebSocketTimeoutException", (Exception,), {})
        self.events = []
        self.ready_at_dispatch = []
        self.connections = []
        self.waits = []
        self.ready_touches = []
        self.threads = []
        self.sockets = []

    def dispatch(self, event):
        self.events.append(event)
        self.ready_at_dispatch.append(self.ready.exists())

    def install(self, monkeypatch, sockets, *, stop_after_waits=1):
        harness = self
        self.sockets = sockets
        real_set_ready = asterisk_client._set_ready

        def create_connection(url, timeout=None, header=None):
            harness.connections.append(
                {"url": url, "timeout": timeout, "header": header, "ready": harness.ready.exists()}
            )
            if len(harness.connections) > len(harness.sockets):
                raise OSError("ARI websocket is unavailable")
            return harness.sockets[len(harness.connections) - 1]

        def wait(seconds):
            harness.waits.append(seconds)
            if len(harness.waits) >= stop_after_waits:
                raise _StopLoop

        def track_set_ready(path):
            harness.ready_touches.append(path)
            real_set_ready(path)

        def thread_factory(target=None, name=None, daemon=None):
            record = SimpleNamespace(name=name, daemon=daemon, start=lambda: target())
            harness.threads.append(record)
            return record

        monkeypatch.setattr(
            asterisk_client,
            "websocket",
            SimpleNamespace(create_connection=create_connection, WebSocketTimeoutException=self.timeout_error),
        )
        monkeypatch.setattr(
            asterisk_client,
            "threading",
            SimpleNamespace(Thread=thread_factory, Event=lambda: SimpleNamespace(wait=wait)),
        )
        monkeypatch.setattr(asterisk_client, "_set_ready", track_set_ready)

    def run(self, on_event=None):
        with pytest.raises(_StopLoop):
            asterisk_client.ari_event_loop(on_event or self.dispatch, self.config)


def test_config_sync_applies_once_when_ami_is_ready(monkeypatch):
    applied = []
    app = SimpleNamespace(
        extensions={"telephony_config_sync": SimpleNamespace(apply=lambda: applied.append(1))},
        logger=RecordingLogger(),
    )
    sleeps = []
    monkeypatch.setattr(ari_worker, "time", SimpleNamespace(sleep=sleeps.append))

    ari_worker._sync_asterisk_config(app)

    assert applied == [1]
    assert sleeps == []
    assert app.logger.warnings == []
    assert app.logger.infos == ["Initial Asterisk database configuration sync completed"]


def test_config_sync_retries_with_backoff_until_ami_answers(monkeypatch):
    attempts = []

    def apply():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise ConnectionError("AMI is not ready yet")

    app = SimpleNamespace(
        extensions={"telephony_config_sync": SimpleNamespace(apply=apply)},
        logger=RecordingLogger(),
    )
    sleeps = []
    monkeypatch.setattr(ari_worker, "time", SimpleNamespace(sleep=sleeps.append))

    ari_worker._sync_asterisk_config(app)

    assert attempts == [1, 2, 3]
    # A transient AMI failure must not leave the worker on stale configuration.
    assert sleeps == [2.0, 3.0]
    assert len(app.logger.warnings) == 2
    assert app.logger.warnings[0].startswith("Asterisk configuration sync attempt 1/10 failed")
    assert app.logger.infos == ["Initial Asterisk database configuration sync completed"]


def test_config_sync_raises_after_ten_failed_attempts(monkeypatch):
    def apply():
        raise ConnectionError("AMI never became reachable")

    app = SimpleNamespace(
        extensions={"telephony_config_sync": SimpleNamespace(apply=apply)},
        logger=RecordingLogger(),
    )
    sleeps = []
    monkeypatch.setattr(ari_worker, "time", SimpleNamespace(sleep=sleeps.append))

    with pytest.raises(ConnectionError):
        ari_worker._sync_asterisk_config(app)

    # Nine waits, each capped at ten seconds, then the tenth failure propagates.
    assert sleeps == [2.0, 3.0, 4.5, 6.75, 10.0, 10.0, 10.0, 10.0, 10.0]
    assert len(app.logger.warnings) == 9
    assert app.logger.infos == []


def test_ready_flag_helpers_create_and_remove_marker(tmp_path):
    marker = tmp_path / "nested" / "ari.ready"

    asterisk_client._set_ready(str(marker))
    assert marker.exists()

    # Re-touching keeps the marker fresh, which is what stops it going stale.
    asterisk_client._set_ready(str(marker))
    assert marker.exists()

    asterisk_client._clear_ready(str(marker))
    assert not marker.exists()

    # Clearing an already absent marker must not raise.
    asterisk_client._clear_ready(str(marker))


def test_event_loop_dispatches_events_and_manages_ready_flag(monkeypatch, tmp_path):
    harness = EventLoopHarness(tmp_path)
    harness.ready.touch()  # stale marker left behind by a previous worker run
    socket = ScriptedSocket(['{"type": "StasisStart"}', '{"type": "ChannelStateChange"}', ""])
    harness.install(monkeypatch, [socket])

    harness.run()

    assert [event["type"] for event in harness.events] == ["StasisStart", "ChannelStateChange"]
    # The readiness marker must be published before an event is dispatched, because
    # ari_ready() gates whether the API is allowed to originate a call.
    assert harness.ready_at_dispatch == [True, True]

    first = harness.connections[0]
    assert first["ready"] is False  # stale marker cleared before connecting
    assert first["url"] == "ws://asterisk:8088/ari/events?app=engineerip"
    assert first["timeout"] == 5
    assert first["header"] == [f"Authorization: Basic {base64.b64encode(b'u:p').decode()}"]

    assert socket.closed is True
    assert len(harness.connections) == 2  # reconnected after the socket closed
    # Readiness must be withdrawn before the next attempt, so the API stops
    # accepting outbound calls for the whole reconnect gap.
    assert harness.connections[1]["ready"] is False
    assert harness.waits == [2]  # backoff restarts at two seconds after a good connection
    assert harness.ready.exists() is False
    assert harness.threads[0].name == "asterisk-ari-events"
    assert harness.threads[0].daemon is True


def test_event_loop_survives_receive_timeout_without_dropping_readiness(monkeypatch, tmp_path):
    harness = EventLoopHarness(tmp_path)
    socket = ScriptedSocket([harness.timeout_error, '{"type": "StasisStart"}', ""])
    harness.install(monkeypatch, [socket])

    harness.run()

    assert [event["type"] for event in harness.events] == ["StasisStart"]
    # Connect, re-arm after the idle timeout, then refresh before the event.
    assert len(harness.ready_touches) == 3
    assert socket.closed is True


def test_event_loop_drops_connection_on_undecodable_frame(monkeypatch, tmp_path):
    harness = EventLoopHarness(tmp_path)
    socket = ScriptedSocket(["this is not json"])
    harness.install(monkeypatch, [socket])

    harness.run()

    assert harness.events == []
    assert socket.closed is True
    assert harness.waits == [2]
    assert harness.ready.exists() is False


def test_event_loop_ignores_close_failures(monkeypatch, tmp_path):
    harness = EventLoopHarness(tmp_path)
    socket = ScriptedSocket([""], close_error=OSError("socket already gone"))
    harness.install(monkeypatch, [socket])

    harness.run()

    assert socket.closed is True
    assert harness.waits == [2]


def test_event_loop_backs_off_progressively_and_caps_at_thirty_seconds(monkeypatch, tmp_path):
    harness = EventLoopHarness(tmp_path)
    harness.install(monkeypatch, [], stop_after_waits=5)

    harness.run()

    assert harness.waits == [2, 4, 8, 16, 30]
    assert harness.events == []
    assert harness.ready.exists() is False


class FakeTelephonyService:
    def __init__(self):
        self.recovered = 0
        self.events = []
        self.webhook_scans = 0
        self.cleanup_scans = 0

    def recover_incomplete_calls(self):
        self.recovered += 1

    def handle_ari_event(self, event):
        self.events.append(event)

    def process_webhook_deliveries(self):
        self.webhook_scans += 1

    def cleanup_recordings(self):
        self.cleanup_scans += 1


class FailingEventHandlerService(FakeTelephonyService):
    def handle_ari_event(self, event):
        raise RuntimeError("malformed event")


class FailingMaintenanceService(FakeTelephonyService):
    def process_webhook_deliveries(self):
        raise RuntimeError("database unavailable")

    def cleanup_recordings(self):
        raise RuntimeError("asterisk unavailable")


class FakeVoicemailNotifier:
    def __init__(self):
        self.processed = 0

    def process(self):
        self.processed += 1


class FailingVoicemailNotifier:
    def process(self):
        raise RuntimeError("sendgrid unavailable")


def run_main(monkeypatch, service, notifier, clock_values, *, stop_after_sleeps=1):
    """Run ``ari_worker.main`` to the Nth loop sleep and return the wiring."""
    app = SimpleNamespace(
        extensions={
            "telephony_service": service,
            "telephony_config_sync": SimpleNamespace(apply=lambda: None),
            "voicemail_notifier": notifier,
        },
        logger=RecordingLogger(),
    )
    created = []
    synced = []
    handler = {}

    def fake_create_app(*args, **kwargs):
        created.append({"args": args, "kwargs": kwargs})
        return app

    def fake_event_loop(on_event, config):
        handler["func"] = on_event
        handler["config"] = config

    clock = iter(clock_values)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= stop_after_sleeps:
            raise _StopLoop

    monkeypatch.setattr(ari_worker, "create_app", fake_create_app)
    monkeypatch.setattr(ari_worker, "_sync_asterisk_config", synced.append)
    monkeypatch.setattr(asterisk_client, "ari_event_loop", fake_event_loop)
    monkeypatch.setattr(ari_worker, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=sleep))

    with pytest.raises(_StopLoop):
        ari_worker.main()

    return SimpleNamespace(app=app, created=created, synced=synced, handler=handler, sleeps=sleeps)


def test_main_wires_ari_loop_and_runs_maintenance_on_schedule(monkeypatch):
    service = FakeTelephonyService()
    notifier = FakeVoicemailNotifier()

    bundle = run_main(monkeypatch, service, notifier, [10.0, 40.0, 3700.0], stop_after_sleeps=3)

    assert bundle.created == [
        {"args": (ari_worker.Config,), "kwargs": {"start_ari": False, "sync_config": False}}
    ]
    # Configuration is synced explicitly before the event loop, not by create_app.
    assert bundle.synced == [bundle.app]
    assert service.recovered == 1
    assert bundle.handler["config"] is ari_worker.Config

    # Webhook deliveries drain every 5s, voicemail every 30s, retention hourly.
    assert service.webhook_scans == 3
    assert notifier.processed == 2
    assert service.cleanup_scans == 1
    assert bundle.sleeps == [30, 30, 30]


def test_main_event_handler_logs_failures_without_stopping_the_worker(monkeypatch):
    service = FailingEventHandlerService()

    bundle = run_main(monkeypatch, service, FakeVoicemailNotifier(), [10.0])
    bundle.handler["func"]({"type": "StasisStart", "channel": {"id": "channel-1"}})

    # A bad event must not kill the single ARI consumer for every future call.
    assert bundle.app.logger.exceptions == ["Failed to process Asterisk ARI event"]


def test_main_logs_maintenance_failures_and_keeps_scheduling(monkeypatch):
    bundle = run_main(
        monkeypatch,
        FailingMaintenanceService(),
        FailingVoicemailNotifier(),
        [10.0, 40.0, 3700.0],
        stop_after_sleeps=3,
    )

    assert bundle.app.logger.exceptions == [
        "Webhook delivery processing failed",
        "Webhook delivery processing failed",
        "Voicemail email processing failed",
        "Webhook delivery processing failed",
        "Voicemail email processing failed",
        "Recording retention cleanup failed",
    ]
    # The worker keeps its cadence after failures instead of exiting.
    assert bundle.sleeps == [30, 30, 30]
