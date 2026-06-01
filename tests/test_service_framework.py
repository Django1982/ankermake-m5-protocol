from contextlib import contextmanager
from queue import Queue
from threading import Timer
from types import SimpleNamespace

import pytest

from web.lib.service import RunState, Service, ServiceError, ServiceManager, ServiceStoppedError


class FakeManagedService:
    def __init__(self, *, wanted=False, state=RunState.Stopped, ready_error=None, video_enabled=False):
        self.wanted = wanted
        self.state = state
        self.running = True
        self.video_enabled = video_enabled
        self.ready_error = ready_error
        self.start_calls = 0
        self.stop_calls = 0
        self.await_ready_calls = 0
        self.await_stopped_calls = 0
        self.shutdown_calls = 0

    def start(self):
        self.start_calls += 1
        self.wanted = True

    def stop(self):
        self.stop_calls += 1
        self.wanted = False

    def await_ready(self):
        self.await_ready_calls += 1
        if self.ready_error:
            raise self.ready_error
        self.state = RunState.Running
        return True

    def await_stopped(self, timeout=None):
        self.await_stopped_calls += 1
        self.state = RunState.Stopped
        return True

    def shutdown(self):
        self.shutdown_calls += 1

    @contextmanager
    def tap(self, handler):
        handler({"status": "frame"})
        Timer(0.01, lambda: setattr(self, "state", RunState.Stopped)).start()
        yield self


class DummyService(Service):
    def worker_init(self):
        pass

    def worker_start(self):
        pass

    def worker_run(self, timeout):
        pass

    def worker_stop(self):
        pass


def test_service_manager_register_get_put_and_unregister():
    manager = ServiceManager()
    svc = FakeManagedService()

    manager.register("mqttqueue", svc)
    with pytest.raises(KeyError):
        manager.register("mqttqueue", svc)

    fetched = manager.get("mqttqueue")
    assert fetched is svc
    assert manager.refs["mqttqueue"] == 1
    assert svc.start_calls == 1
    assert svc.await_ready_calls == 1

    manager.put("mqttqueue")
    assert manager.refs["mqttqueue"] == 0
    assert svc.stop_calls == 1

    manager.unregister("mqttqueue")
    assert "mqttqueue" not in manager


def test_service_manager_get_rolls_back_on_ready_failure_and_video_put_keeps_running():
    manager = ServiceManager()
    broken = FakeManagedService(ready_error=ServiceError("boom"))
    video = FakeManagedService(state=RunState.Running, video_enabled=True)
    video.persistent = True  # video_enabled=True keeps service alive (via persistent flag)
    manager.register("broken", broken)
    manager.register("videoqueue", video)

    with pytest.raises(ServiceError, match="boom"):
        manager.get("broken")
    assert manager.refs["broken"] == 0
    assert broken.stop_calls == 1

    manager.refs["videoqueue"] = 1
    manager.put("videoqueue")
    assert manager.refs["videoqueue"] == 0
    assert video.stop_calls == 0


def test_service_manager_restart_all_stream_and_atexit():
    manager = ServiceManager()
    wanted = FakeManagedService(wanted=True, state=RunState.Running)
    wanted_stops = FakeManagedService(wanted=True, state=RunState.Running, ready_error=ServiceStoppedError("stopped"))
    idle = FakeManagedService(wanted=False, state=RunState.Stopped)

    manager.register("wanted", wanted)
    manager.register("wanted_stops", wanted_stops)
    manager.register("idle", idle)

    manager.restart_all()
    streamed = list(manager.stream("wanted"))
    manager.atexit()

    assert wanted.stop_calls >= 1
    assert wanted.start_calls == 2
    assert wanted.await_ready_calls == 2
    assert wanted_stops.start_calls == 1
    assert wanted_stops.await_ready_calls == 1
    assert idle.start_calls == 0
    assert streamed == [{"status": "frame"}]
    assert wanted.shutdown_calls == 1
    assert wanted_stops.shutdown_calls == 1
    assert idle.shutdown_calls == 1


def test_service_manager_replace_service_waits_for_old_service_stop_before_swap():
    manager = ServiceManager()

    class ReplaceableService(FakeManagedService):
        def __init__(self):
            super().__init__(wanted=True, state=RunState.Running)
            self.force_close_calls = 0
            self.join_calls = 0
            self.join_timeout = None

        def _force_close_api(self):
            self.force_close_calls += 1

        def await_stopped(self, timeout=None):
            self.await_stopped_calls += 1
            assert manager.svcs["pppp:0"] is self
            assert timeout is not None
            self.state = RunState.Stopped
            return True

        def join(self, timeout=None):
            self.join_calls += 1
            self.join_timeout = timeout

    old = ReplaceableService()
    new = FakeManagedService()
    manager.register("pppp:0", old)

    manager.replace_service("pppp:0", new)

    assert old.stop_calls == 1
    assert old.await_stopped_calls == 1
    assert old.force_close_calls == 1
    assert old.running is False
    assert old.join_calls == 1
    assert old.join_timeout is not None
    assert manager.svcs["pppp:0"] is new
    assert manager.refs["pppp:0"] == 0


def test_service_stream_bounded_queue_drops_oldest_items():
    q = Queue(maxsize=2)

    ServiceManager._enqueue_stream_item(q, "one")
    ServiceManager._enqueue_stream_item(q, "two")
    ServiceManager._enqueue_stream_item(q, "three")

    assert q.get_nowait() == "two"
    assert q.get_nowait() == "three"


import time as _time


def test_attempt_run_floor_prevents_busy_loop():
    """_attempt_run must not complete in less than 10 ms even if worker_run is instant."""

    class InstantService(Service):
        def worker_run(self, timeout):
            pass  # returns immediately

    svc = InstantService()
    svc.state = RunState.Running
    svc.wanted = True

    t0 = _time.monotonic()
    svc._attempt_run()
    elapsed = _time.monotonic() - t0

    svc.shutdown()
    assert elapsed >= 0.009, f"_attempt_run returned in {elapsed*1000:.1f} ms, expected >= 10 ms"


def test_service_wake_cancels_holdoff():
    """wake() must expire the holdoff so the service thread doesn't wait."""
    from datetime import datetime, timedelta

    svc = DummyService()
    # Set a long holdoff
    svc._holdoff.reset(delay=300)
    assert not svc._holdoff.passed, "holdoff should not have passed yet"

    svc.wake()
    assert svc._holdoff.passed, "wake() must expire the holdoff immediately"
    svc.shutdown()


def test_service_start_failure_logging_suppresses_duplicate_tracebacks(monkeypatch):
    svc = object.__new__(DummyService)
    svc._reset_start_failure_tracking()

    records = []

    monkeypatch.setattr(
        "web.lib.service.log.exception",
        lambda message: records.append(("exception", message)),
    )
    monkeypatch.setattr(
        "web.lib.service.log.error",
        lambda message: records.append(("error", message)),
    )
    monkeypatch.setattr(
        "web.lib.service.log.warning",
        lambda message, *args: records.append(("warning", message % args if args else message)),
    )

    times = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr("web.lib.service.time.monotonic", lambda: next(times))

    exc = ConnectionRefusedError("No printer IP found")
    for _ in range(5):
        svc._log_start_failure(exc, retrying=True)

    assert records[0] == (
        "exception",
        "DummyService: Failed to start worker: No printer IP found. Retrying in 1 second.",
    )
    assert records[1] == (
        "warning",
        "DummyService: Start failure is repeating (No printer IP found). Suppressing duplicate start-failure logs while retrying",
    )
    assert records[2] == (
        "warning",
        "DummyService: Failed to start worker: No printer IP found. Retrying in 1 second. (seen 5 times)",
    )
    assert len(records) == 3
