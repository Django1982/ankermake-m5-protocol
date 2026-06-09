import atexit
import logging as log
import contextlib
import threading
import time

from enum import Enum
from threading import Thread, Event, Lock, RLock, current_thread
from datetime import datetime, timedelta
import queue
from queue import Queue

_SERVICE_MIN_ITERATION_SEC = 0.010
_START_FAILURE_REPEAT_NOTICE_SECONDS = 10.0
_START_FAILURE_REPEAT_NOTICE_COUNT = 5
_REPLACE_SERVICE_STOP_TIMEOUT_SECONDS = 5.0
_REPLACE_SERVICE_JOIN_TIMEOUT_SECONDS = 1.0


class Holdoff:

    def __init__(self):
        self.deadline = None

    def reset(self, delay=None):
        self.deadline = datetime.now()
        if delay:
            self.deadline += timedelta(seconds=delay)

    @property
    def passed(self):
        return self.deadline is None or datetime.now() > self.deadline


class ServiceError(Exception):
    pass


class ServiceStoppedError(ServiceError):
    pass


class ServiceSignal(Exception):
    pass


class ServiceRestartSignal(ServiceSignal):
    def __init__(self, msg="", delay: float = 1.0):
        super().__init__(msg)
        self.delay = delay


class RunState(Enum):
    Starting = 2
    Running  = 3
    # Idle     = 4
    Stopping = 5
    Stopped  = 6


class Service(Thread):

    def __init__(self):
        super().__init__()
        self.running = True
        self.persistent = False
        self.deadline = None
        self.state = RunState.Stopped
        self.wanted = False
        self._event = Event()
        self.handlers = []
        self._holdoff = Holdoff()
        self._start_failure_signature = None
        self._start_failure_count = 0
        self._start_failure_last_notice_at = 0.0
        self._start_failure_suppressed = False
        # N-5: Event-based ready/stopped signalling — no polling loops
        self._ready_event = Event()
        self._stopped_event = Event()
        self._stopped_event.set()   # starts in stopped state
        # S-4: snapshot-based handler dispatch to avoid holding lock during callbacks
        self._handlers_lock = Lock()
        self._handlers_snapshot = ()
        self._handlers_dirty = False
        self.daemon = True
        super().start()

    @property
    def name(self):
        return type(self).__name__

    def start(self):
        if self.wanted or self.state in (RunState.Starting, RunState.Running):
            return
        log.info(f"{self.name}: Requesting start")
        self.wanted = True
        self._event.set()

    def stop(self):
        if (not self.wanted) or self.state in (RunState.Stopping, RunState.Stopped):
            return
        log.info(f"{self.name}: Requesting stop")
        self.wanted = False
        self._event.set()

    def wake(self):
        """Cancel the current holdoff and wake the service thread immediately.

        Safe to call from any thread. Used when an external event means the
        service should stop waiting (e.g. a new WebSocket client connected).
        """
        self._holdoff.deadline = datetime.now()
        self._event.set()

    def restart(self):
        log.info(f"{self.name}: Requesting restart")
        wanted = self.wanted
        self.stop()
        self.await_stopped()
        if wanted:
            self.start()
            self.await_ready()

    def shutdown(self):
        if self.state != RunState.Stopped:
            self.stop()
            self.await_stopped()

        self.running = False
        self._event.set()
        return self.join()

    def idle(self, timeout=None):
        if self._event.wait(timeout=timeout):
            self._event.clear()

    def _reset_start_failure_tracking(self):
        self._start_failure_signature = None
        self._start_failure_count = 0
        self._start_failure_last_notice_at = 0.0
        self._start_failure_suppressed = False

    def _log_start_failure(self, exc, *, retrying):
        if isinstance(exc, TimeoutError):
            return
        if (not retrying) and isinstance(exc, ServiceStoppedError):
            return

        signature = (type(exc), str(exc), bool(retrying))
        now = time.monotonic()
        action = "Retrying in 1 second." if retrying else "Shutting down service."

        if signature != self._start_failure_signature:
            self._start_failure_signature = signature
            self._start_failure_count = 1
            self._start_failure_last_notice_at = now
            self._start_failure_suppressed = False
            if retrying and not isinstance(exc, ServiceStoppedError):
                log.exception(f"{self.name}: Failed to start worker: {exc}. {action}")
            else:
                log.error(f"{self.name}: Failed to start worker: {exc}. {action}")
            return

        self._start_failure_count += 1
        count = self._start_failure_count

        if not self._start_failure_suppressed:
            self._start_failure_suppressed = True
            self._start_failure_last_notice_at = now
            log.warning(
                "%s: Start failure is repeating (%s). Suppressing duplicate start-failure logs while %s",
                self.name,
                exc,
                "retrying" if retrying else "stopping",
            )
            return

        if (
            (now - self._start_failure_last_notice_at) < _START_FAILURE_REPEAT_NOTICE_SECONDS
            and (count % _START_FAILURE_REPEAT_NOTICE_COUNT) != 0
        ):
            return

        self._start_failure_last_notice_at = now
        log.warning(
            "%s: Failed to start worker: %s. %s (seen %s times)",
            self.name,
            exc,
            action,
            count,
        )

    def _attempt_start(self):
        try:
            log.debug(f"{self.name} worker starting..")
            self.worker_start()
        except Exception as E:
            if self.wanted:
                self._log_start_failure(E, retrying=True)
                self._holdoff.reset(delay=1)
            else:
                self._log_start_failure(E, retrying=False)
                self.state = RunState.Stopped
                self._stopped_event.set()
        else:
            self._reset_start_failure_tracking()
            log.info(f"{self.name}: Worker started")
            self.state = RunState.Running
            self._ready_event.set()
            self._stopped_event.clear()

    def _attempt_run(self):
        _t0 = time.monotonic()
        try:
            self.worker_run(timeout=0.1)
        except ServiceRestartSignal as sig:
            log.info(f"{self.name}: Service requested restart.")
            self._holdoff.reset(delay=getattr(sig, 'delay', 1.0))
            self.state = RunState.Stopping
            return
        except Exception:
            log.exception(f"{self.name}: Unexpected exception while running worker")
            log.warning(f"{self.name}: Stopping worker due to exception")
            self._holdoff.reset()
            self.state = RunState.Stopping
            return
        _elapsed = time.monotonic() - _t0
        _floor = getattr(self, '_min_iteration_sec', _SERVICE_MIN_ITERATION_SEC)
        _remaining = _floor - _elapsed
        if _remaining > 0:
            time.sleep(_remaining)

    def _attempt_stop(self):
        try:
            self.worker_stop()
        except Exception as E:
            log.exception(f"{self.name}: Failed to stop worker: {E}. Retrying in 1 second.")
            self._holdoff.reset(delay=1)
        else:
            log.info(f"{self.name}: Worker stopped")
            self.state = RunState.Stopped
            self._ready_event.clear()
            self._stopped_event.set()

    def run(self):
        self.worker_init()

        while self.running:
            if self.state == RunState.Starting:
                if not self.wanted:
                    # stop() called during retry holdoff — don't wait, go directly to Stopped
                    self.state = RunState.Stopped
                    self._ready_event.clear()
                    self._stopped_event.set()
                elif self._holdoff.passed:
                    self._attempt_start()
                else:
                    self.idle(timeout=0.1)

            elif self.state == RunState.Running:
                if self.wanted:
                    self._attempt_run()
                else:
                    log.debug(f"{self.name}: Stopping worker")
                    self._holdoff.reset()
                    self.state = RunState.Stopping

            elif self.state == RunState.Stopping:
                if self._holdoff.passed:
                    self._attempt_stop()
                else:
                    self.idle(timeout=0.1)

            elif self.state == RunState.Stopped:
                if self.wanted:
                    log.debug(f"{self.name}: Starting worker")
                    self._holdoff.reset()
                    self.state = RunState.Starting
                    self._stopped_event.clear()  # service is no longer stopped once it starts trying
                else:
                    self.idle(timeout=0.1)
            else:
                raise ValueError("Unknown state value")

        log.debug(f"{self.name}: Shutting down thread")
        if self.state == RunState.Running:
            self.handlers.clear()
            with self._handlers_lock:
                self._handlers_snapshot = ()
                self._handlers_dirty = False
            self.worker_stop()
        log.debug(f"{self.name}: Thread exit")

    def worker_init(self):
        pass

    def worker_start(self):
        pass

    def worker_run(self, timeout):
        pass

    def worker_stop(self):
        pass

    def notify(self, data):
        # S-4: Use a snapshot of handlers to avoid holding a lock during callbacks.
        # Rebuild the snapshot only when the handler list has changed.
        if self._handlers_dirty:
            with self._handlers_lock:
                self._handlers_snapshot = tuple(self.handlers)
                self._handlers_dirty = False
        for handler in self._handlers_snapshot:
            try:
                handler(data)
            except Exception:
                log.exception("handler error")

    @contextlib.contextmanager
    def tap(self, handler):
        self.handlers.append(handler)
        with self._handlers_lock:
            self._handlers_dirty = True
        try:
            yield self
        finally:
            try:
                self.handlers.remove(handler)
                with self._handlers_lock:
                    self._handlers_dirty = True
            except ValueError:
                pass

    def await_ready(self):
        # N-5: Use Event.wait instead of polling loop
        while True:
            log.debug(f"{self.name}: Awaiting ready ({self.state})")
            if not (self.running and self.wanted):
                raise ServiceStoppedError(f"{self.name}: Waiting for stopped thread")

            if self._ready_event.wait(timeout=0.4):
                log.debug(f"{self.name}: Ready")
                return True

            if self.state == RunState.Running:
                log.debug(f"{self.name}: Ready (state check)")
                return True

    def await_stopped(self, timeout=None):
        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + max(0.0, float(timeout))

        while True:
            if self.wanted:
                log.warning(f"{self.name}: Service started while waiting for it to stop")
                return False

            # N-5: Use Event.wait for stopped state
            wait_timeout = 0.4
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(f"{self.name}: Timed out waiting for service to stop")
                    return False
                wait_timeout = min(wait_timeout, remaining)

            if self._stopped_event.wait(timeout=wait_timeout):
                log.debug(f"{self.name}: Stopped")
                return True

            if self.state == RunState.Stopped:
                log.debug(f"{self.name}: Stopped (state check)")
                return True


class ServiceManager:

    def __init__(self):
        self.svcs = {}
        self.refs = {}
        self.shutting_down = False
        # S-3: RLock for nested borrow() calls (e.g. a service borrowing another)
        self._lock = RLock()
        atexit.register(self.atexit)

    def __iter__(self):
        return iter(self.svcs)

    def __contains__(self, name):
        return name in self.svcs

    def atexit(self):
        self.shutting_down = True
        log.debug("ServiceManager: Shutting down threads..")
        self.dump()

        stop_order = [
            *(name for name in self.svcs if name == "videoqueue" or name.startswith("videoqueue:")),
            "filetransfer",
            *(name for name in self.svcs if name.startswith("mqttqueue")),
            *(name for name in self.svcs if name == "pppp" or name.startswith("pppp:")),
        ]
        seen = set()
        ordered_names = []
        for name in stop_order:
            if name in self.svcs and name not in seen:
                ordered_names.append(name)
                seen.add(name)
        for name in self.svcs:
            if name not in seen:
                ordered_names.append(name)
                seen.add(name)

        for name in ordered_names:
            svc = self.svcs[name]
            if svc.state != RunState.Stopped:
                svc.stop()

        log.debug("ServiceManager: Waiting for threads to stop..")
        self.dump()
        for name in ordered_names:
            self.svcs[name].await_stopped()

        log.debug("ServiceManager: Cleaning up threads..")
        self.dump()
        for name in ordered_names:
            self.svcs[name].shutdown()

        log.info("ServiceManager: Shutdown complete")

    def dump(self):
        log.debug("Service state")
        for name in self.svcs:
            svc = self.svcs[name]
            ref = self.refs[name]
            log.debug(f"  [{ref:>4}] {name:20} running={svc.running} state={svc.state} wanted={svc.wanted}")

    def register(self, name: str, svc: Service):
        if name in self:
            raise KeyError(f"Trying to register {name!r} as {svc} while already taken by {self.svcs[name]}")

        self.svcs[name] = svc
        self.refs[name] = 0

    def unregister(self, name: str):
        if name not in self:
            raise KeyError(f"Trying to unregister unknown service {name!r}")

        if self.refs[name]:
            raise ServiceError(f"Trying to unregister service {name!r} with {self.refs[name]} reference(s)")

        del self.svcs[name]
        del self.refs[name]


    def replace_service(self, name: str, svc: Service):
        if name not in self:
            raise KeyError(f"Trying to replace unknown service {name!r}")

        old = self.svcs[name]
        stopped = old.state == RunState.Stopped
        replacing_current_thread = old is current_thread()
        try:
            old.stop()
        except Exception:
            try:
                old.wanted = False
            except Exception:
                pass
        else:
            try:
                old.wanted = False
            except Exception:
                pass
        try:
            if hasattr(old, "_event"):
                old._event.set()
        except Exception:
            pass
        try:
            if hasattr(old, "_force_close_api"):
                old._force_close_api()
        except Exception:
            pass

        if not stopped and replacing_current_thread:
            log.warning("%s: Replacement requested from the old service thread; skipping stop wait", name)
        elif not stopped:
            try:
                stopped = old.await_stopped(timeout=_REPLACE_SERVICE_STOP_TIMEOUT_SECONDS)
            except Exception as exc:
                stopped = False
                log.warning("%s: Failed waiting for old service %r to stop before replacement: %s", name, old, exc)

        if not stopped:
            log.warning(
                "%s: Replacing service before old worker fully stopped after %.1fs",
                name,
                _REPLACE_SERVICE_STOP_TIMEOUT_SECONDS,
            )

        try:
            old.running = False
        except Exception:
            pass
        try:
            if hasattr(old, "_event"):
                old._event.set()
        except Exception:
            pass
        try:
            if hasattr(old, "join") and not replacing_current_thread:
                old.join(timeout=_REPLACE_SERVICE_JOIN_TIMEOUT_SECONDS)
        except RuntimeError:
            pass

        with self._lock:
            self.svcs[name] = svc
            self.refs[name] = 0

    def restart_all(self, await_ready=True):
        wanted = {}

        for name, svc in self.svcs.items():
            wanted[name] = svc.wanted
            svc.stop()

        for name, svc in self.svcs.items():
            svc.await_stopped()

        for name, svc in self.svcs.items():
            if not wanted[name]:
                continue

            svc.start()

            if not await_ready:
                continue

            try:
                svc.await_ready()
            except ServiceStoppedError:
                # ignore service stopped error, since restart_all() is a
                # best-effort function.
                pass

    def get(self, name: str, ready=True) -> Service:
        if name not in self:
            raise KeyError(f"Requested unknown service {name!r}")

        # S-3: Acquire lock to safely increment ref and decide whether to start.
        # await_ready() must be called OUTSIDE the lock to avoid deadlocks when
        # a service calls borrow() on another service during its own startup.
        with self._lock:
            svc = self.svcs[name]
            self.refs[name] += 1

            # Never request a fresh start while the old worker is still stopping.
            # Let it finish that transition first, then start cleanly.
            stopping = svc.state == RunState.Stopping

        if stopping:
            svc.await_stopped()

        with self._lock:
            svc = self.svcs[name]
            should_start = False
            if self.refs[name] == 1:
                should_start = True
            elif not svc.wanted and svc.state == RunState.Stopped:
                should_start = True
            elif svc.state == RunState.Stopped:
                should_start = True

            if should_start:
                svc.start()

        try:
            if ready:
                svc.await_ready()   # OUTSIDE the lock

            return svc

        except ServiceError:
            self.put(name)
            raise

    def put(self, name: str):
        if name not in self:
            raise KeyError(f"Requested unknown service {name!r}")

        with self._lock:
            svc = self.svcs[name]

            assert self.refs[name]

            self.refs[name] -= 1

            if not self.refs[name]:
                if getattr(svc, "persistent", False):
                    return
                svc.stop()

    @contextlib.contextmanager
    def borrow(self, name: str):
        svc = self.get(name)
        try:
            yield svc
        finally:
            self.put(name)

    @staticmethod
    def _enqueue_stream_item(q, data):
        if q.maxsize and q.full():
            try:
                q.get_nowait()
            except queue.Empty:
                pass
        try:
            q.put_nowait(data)
        except queue.Full:
            # If another producer won the race, drop the stale realtime item.
            pass

    def stream(self, name: str, maxsize=0):
        try:
            with self.borrow(name) as svc:
                q = Queue(maxsize=max(0, int(maxsize or 0)))

                with svc.tap(lambda data: self._enqueue_stream_item(q, data)):
                    while svc.state == RunState.Running:
                        try:
                            data = q.get(timeout=1.0)
                        except queue.Empty:
                            continue
                        yield data
        except (EOFError, OSError, ServiceStoppedError):
            return
