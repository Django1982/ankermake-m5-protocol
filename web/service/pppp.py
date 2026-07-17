import contextlib
import json
import logging
import threading
import time

from collections import OrderedDict
from datetime import datetime, timedelta

from ..lib.service import Service, ServiceRestartSignal, ServiceStoppedError
from .. import app

from libflagship.pktdump import PacketWriter
from libflagship.pppp import P2PCmdType, PktClose, Duid, Type, Xzyh, Aabb
from libflagship.ppppapi import AnkerPPPPAsyncApi, PPPPState

import cli.pppp

log = logging.getLogger("pppp")

# Some printers take a few extra seconds to accept a fresh PPPP session after
# a video/timelapse recovery. A slightly longer deadline avoids unnecessary
# reconnect loops on otherwise healthy links.
_CONNECT_DEADLINE_SEC = 8.0
_REPEATED_LOG_COOLDOWN_SEC = 10.0
_REPEATED_LOG_NOTICE_COUNT = 5
_LOG_REPEAT_STATE_MAX = 256

# Reconnect backoff for real link flakiness (e.g. brief printer WiFi drops).
# Without this, a burst of remote CLOSEs/timeouts was retried every ~1s with
# no growth, hammering the printer for minutes during an outage. A session
# only "earns back" a clean slate once it has stayed up for a while — a
# connect that succeeds and immediately drops again keeps escalating.
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 8.0
_STABLE_CONNECTION_SEC = 10.0

# The printer only accepts a single active PPPP session. File transfers open
# their own dedicated connection (see filetransfer.py) instead of sharing
# this service's, so without coordination a transfer starting while
# video/timelapse holds a session causes both sides to race for the
# printer's one session slot — seen as a "received CLOSE from remote peer"
# reconnect storm on both ends and the transfer failing outright. These
# per-service-name locks let a transfer reserve the slot: it stops this
# service for the duration, and worker_start() below refuses to reconnect
# while the reservation is held.
_session_locks = {}
_session_locks_guard = threading.Lock()


def _get_session_lock(service_name):
    with _session_locks_guard:
        lock = _session_locks.get(service_name)
        if lock is None:
            lock = threading.Lock()
            _session_locks[service_name] = lock
        return lock


@contextlib.contextmanager
def reserve_session(printer_index=None):
    """Exclusively reserve a printer's PPPP session for a file transfer.

    Stops the shared PPPPService for the printer (if it currently wants to
    be running) so a dedicated file-transfer connection doesn't collide with
    it, and restarts it afterward. See the module-level comment above
    _session_locks for why this is necessary.
    """
    import web

    service_name = web.resolve_pppp_service_name(printer_index)
    lock = _get_session_lock(service_name)
    svc = None
    was_wanted = False
    try:
        with lock:
            svc = getattr(app.svc, "svcs", {}).get(service_name)
            was_wanted = bool(svc is not None and svc.wanted)
            if was_wanted:
                # Intentionally ignores app.svc.refs (unlike register_services):
                # the printer only supports one PPPP session, so a file transfer
                # must preempt video/timelapse holders. VideoQueue self-heals
                # once the service restarts below.
                svc.stop()
                # Field data: an actively streaming video session can take
                # longer than 5s to fully relinquish its hold; proceeding
                # before the stop is confirmed makes the transfer connect
                # against a still-occupied printer session slot.
                svc.await_stopped(timeout=15.0)
            yield
    finally:
        # Restart outside the lock: worker_start() probes this same lock, and
        # starting while still holding it makes the service thread lose the
        # race and eat its default holdoff (~1s) on every upload.
        if was_wanted:
            svc.start()


def probe_pppp(config, printer_index) -> bool:
    """Try a PPPP LAN connection. Returns True if handshake succeeds, False otherwise.

    This intentionally does not use pppp_resolve_printer_ip(). The normal
    resolver may fall back to the last saved IP so real connection attempts can
    still try a known-good address, but a status probe must only report green
    after the printer responds to a directed probe or LAN discovery.
    """
    try:
        with config.open() as cfg:
            if not cfg:
                return False
            printer = cfg.printers[printer_index]

        ip_addr = (printer.ip_addr or "").strip()
        if ip_addr and cli.pppp.probe_printer_ip(printer, ip_addr):
            return True

        discovered = cli.pppp.lan_search(config)
        return any(result.get("duid") == printer.p2p_duid for result in discovered)
    except Exception:
        return False


class PPPPService(Service):

    # PPPPService processes UDP packets for H.264 video ACKs. The default 10ms
    # floor from the base class caps ACK rate and causes video stalls; run at
    # full speed to match master behavior.
    _min_iteration_sec = 0.0

    def __init__(self, printer_index=0):
        self.printer_index = 0 if printer_index is None else int(printer_index)
        self.xzyh_handlers = []
        self._handler_lock = threading.Lock()
        self._log_repeat_state = OrderedDict()
        self._connected_event = threading.Event()
        self._connect_failure_count = 0
        self._connected_since = None
        super().__init__()

    def _note_connection_lost(self):
        """Update the reconnect-failure streak and return the backoff delay to use.

        Resets the streak if the connection that was just lost had stayed up
        for at least _STABLE_CONNECTION_SEC (a real recovery), otherwise
        escalates it — so a rapid connect/drop/connect/drop cycle backs off
        instead of hammering the printer every ~1s indefinitely.
        """
        connected_since = getattr(self, "_connected_since", None)
        stable = (
            connected_since is not None
            and (datetime.now() - connected_since).total_seconds() >= _STABLE_CONNECTION_SEC
        )
        self._connected_since = None
        if stable:
            self._connect_failure_count = 0
        else:
            self._connect_failure_count = getattr(self, "_connect_failure_count", 0) + 1
        return min(
            _BACKOFF_BASE_SEC * (2 ** max(0, self._connect_failure_count - 1)),
            _BACKOFF_CAP_SEC,
        )

    def await_connected(self, timeout: float = 10.0) -> bool:
        """Block until the PPPP connection is established or *timeout* seconds elapse."""
        if self.connected:
            return True
        return self._connected_event.wait(timeout)

    @property
    def name(self):
        return f"PPPPService[{self.printer_index}]"

    def _force_close_api(self):
        if not hasattr(self, "_api"):
            return
        api = self._api
        # Best-effort notify the printer that this session is over, so it
        # releases its single PPPP session slot immediately instead of
        # waiting out its own internal keepalive/timeout. Without this, a
        # file transfer starting right after (e.g. reserve_session() above)
        # gets its connect attempts rejected with "received CLOSE from
        # remote peer" for the printer's entire session timeout, because as
        # far as the printer knows the old (video) session is still alive.
        #
        # This is safe to do even mid-freeze-recovery: unlike
        # send_xzyh()/send_aabb(), which route through Channel.write() and
        # can block waiting on a DRW ACK from the printer, AnkerPPPPBaseApi
        # .send() is a raw, non-blocking sock.sendto() — it never waits for
        # any response, so it cannot reintroduce the hang this code used to
        # guard against. We only attempt it while the api still reports
        # itself Connected; send() raises ConnectionError for Idle/
        # Disconnected states, which is also the state an already-broken
        # connection (e.g. one that triggered this force-close) is likely
        # to be in already, so the guard doubles as a no-op skip in that
        # case. The unconditional socket teardown below remains the actual
        # safety net regardless of whether this send succeeds.
        try:
            if getattr(api, "state", None) == PPPPState.Connected:
                api.send(PktClose())
        except Exception:
            pass
        try:
            api.state = PPPPState.Disconnected
        except Exception:
            pass
        try:
            if getattr(api, "sock", None):
                try:
                    api.sock.shutdown(2)
                except Exception:
                    pass
                api.sock.close()
        except Exception:
            pass
        try:
            del self._api
        except Exception:
            try:
                self._api = None
            except Exception:
                pass

    def stop(self):
        was_wanted = self.wanted
        super().stop()
        if was_wanted:
            log.info("%s: forcing socket close to expedite stop", self.name)
            self._force_close_api()

    def _log_repeated(self, level, key, message, *args, cooldown=_REPEATED_LOG_COOLDOWN_SEC):
        now = time.monotonic()
        state = self._log_repeat_state.get(key)
        if state is None:
            if len(self._log_repeat_state) >= _LOG_REPEAT_STATE_MAX:
                self._log_repeat_state.popitem(last=False)
            self._log_repeat_state[key] = {"count": 1, "last_at": now}
            log.log(level, message, *args)
            return

        # Move to end to mark as recently used
        self._log_repeat_state.move_to_end(key)
        state["count"] += 1
        if (
            (now - state["last_at"]) < cooldown
            and (state["count"] % _REPEATED_LOG_NOTICE_COUNT) != 0
        ):
            return

        state["last_at"] = now
        formatted = message % args if args else message
        log.log(level, "%s (seen %s times)", formatted, state["count"])

    def api_command(self, commandType, **kwargs):
        api = getattr(self, "_api", None)
        if api is None or getattr(api, "state", None) != PPPPState.Connected:
            raise ConnectionError("No pppp connection")
        cmd = {
            "commandType": commandType,
            **kwargs
        }
        return api.send_xzyh(
            json.dumps(cmd).encode(),
            cmd=P2PCmdType.P2P_JSON_CMD,
            block=False
        )

    def worker_start(self):
        import web

        printer_index = getattr(self, "printer_index", app.config.get("printer_index", 0))
        service_name = web.resolve_pppp_service_name(printer_index)
        lock = _get_session_lock(service_name)
        if not lock.acquire(blocking=False):
            raise ConnectionError(
                "PPPP session reserved for a file transfer; retrying shortly"
            )
        lock.release()

        config = app.config["config"]

        deadline = datetime.now() + timedelta(seconds=_CONNECT_DEADLINE_SEC)

        with config.open() as cfg:
            if not cfg:
                raise ServiceStoppedError("No config available")
            printer = cfg.printers[printer_index]

        ip_addr = cli.pppp.pppp_resolve_printer_ip(
            config,
            printer,
            printer_index,
            dumpfile=app.config.get("pppp_dump"),
        )
        if not ip_addr:
            self._log_repeated(
                logging.WARNING,
                ("no_ip", printer_index, printer.p2p_duid),
                "%s: PPPP connect aborted because no printer IP was resolved "
                "(printer=%s, duid=%s)",
                self.name,
                printer.name,
                printer.p2p_duid,
            )
            raise ServiceRestartSignal(
                "No printer IP found; ensure printer is online on the same network",
                delay=self._note_connection_lost(),
            )

        api = AnkerPPPPAsyncApi.open_lan(Duid.from_string(printer.p2p_duid), host=ip_addr)
        if app.config["pppp_dump"]:
            dumpfile = app.config["pppp_dump"]
            log.info(f"Logging all pppp traffic to {dumpfile!r}")
            pktwr = PacketWriter.open(dumpfile)
            api.set_dumper(pktwr)

        started_at = datetime.now()
        self._log_repeated(
            logging.INFO,
            ("connect_attempt", printer_index, ip_addr),
            "%s: trying connect to printer %s (%s) over pppp using ip %s "
            "(deadline=%.1fs)",
            self.name,
            printer.name,
            printer.p2p_duid,
            ip_addr,
            _CONNECT_DEADLINE_SEC,
        )

        api.connect_lan_search()

        while api.state != PPPPState.Connected:
            remaining = (deadline - datetime.now()).total_seconds()
            if remaining <= 0:
                elapsed = (datetime.now() - started_at).total_seconds()
                self._log_repeated(
                    logging.WARNING,
                    ("connect_timeout", printer_index, ip_addr),
                    "%s: PPPP connection timed out after %.1fs "
                    "(printer=%s, ip=%s, state=%s)",
                    self.name,
                    elapsed,
                    printer.name,
                    ip_addr,
                    getattr(api, "state", None),
                )
                raise ServiceRestartSignal(
                    "Connection rejected by device", delay=self._note_connection_lost()
                )
            try:
                msg = api.recv(timeout=remaining)
                api.process(msg)
            except ConnectionResetError:
                elapsed = (datetime.now() - started_at).total_seconds()
                self._log_repeated(
                    logging.WARNING,
                    ("connect_reset", printer_index, ip_addr),
                    "%s: PPPP connection reset after %.1fs "
                    "(printer=%s, ip=%s)",
                    self.name,
                    elapsed,
                    printer.name,
                    ip_addr,
                )
                raise ServiceRestartSignal(
                    "Connection rejected by device", delay=self._note_connection_lost()
                )

        elapsed = (datetime.now() - started_at).total_seconds()
        log.info(
            "%s: established pppp connection to %s in %.1fs",
            self.name,
            printer.name,
            elapsed,
        )
        self._api = api
        self._connected_event.set()
        self._connected_since = datetime.now()

    def _drain_xzyh(self, chan):
        api = getattr(self, "_api", None)
        if api is None or not hasattr(api, "chans"):
            return

        if chan < 0 or chan >= len(api.chans):
            return

        fd = api.chans[chan]

        while True:
            with fd.lock:
                hdr = fd.peek(16, timeout=0.0)
                if not hdr:
                    return
                if hdr[:4] != b"XZYH":
                    if self._resync_xzyh(fd, chan):
                        continue
                    return

                xzyh = Xzyh.parse(hdr)[0]
                pkt = fd.read(xzyh.len + 16, timeout=0.0)
                if not pkt:
                    return
                xzyh.data = pkt[16:]

            with self._handler_lock:
                handlers = self.xzyh_handlers[:]
            for handler in handlers:
                try:
                    handler((chan, xzyh))
                except Exception as e:
                    log.warning(f"Handler error: {e}")

    def _resync_xzyh(self, fd, chan):
        rx = getattr(fd, "rx", None)
        buf = getattr(rx, "buf", None)
        if not buf:
            return False

        data = bytes(buf)
        pos = data.find(b"XZYH", 1)
        if pos < 0:
            if len(buf) > 3:
                discarded = len(buf) - 3
                del buf[:-3]
                log.debug(f"PPPPService: discarded {discarded} unsynced channel {chan} byte(s) while looking for XZYH")
            return False

        del buf[:pos]
        log.debug(f"PPPPService: resynced channel {chan} stream at next XZYH boundary")
        return True

    def _recv_aabb(self, fd):
        data = fd.read(12)
        aabb = Aabb.parse(data)[0]
        p = data + fd.read(aabb.len + 2)
        aabb, data = Aabb.parse_with_crc(p)[:2]
        return aabb, data

    def worker_run(self, timeout):
        api = getattr(self, "_api", None)
        if api is None:
            if getattr(self, "wanted", True):
                raise ServiceRestartSignal(
                "PPPP API missing while service is wanted", delay=self._note_connection_lost()
            )
            return

        # A stale/disconnected API object after video recovery is not a usable
        # running PPPP session. Force an internal restart instead of idling
        # forever in a wanted-but-disconnected state.
        if getattr(api, "state", PPPPState.Connected) != PPPPState.Connected:
            if getattr(self, "wanted", True):
                raise ServiceRestartSignal(
                "PPPP API exists but is not connected while service is wanted",
                delay=self._note_connection_lost(),
            )
            return

        try:
            msg = api.poll(timeout=timeout)
        except (ConnectionResetError, OSError):
            if not getattr(self, "wanted", True):
                return
            raise ServiceRestartSignal(delay=self._note_connection_lost())

        # Drain all remaining UDP packets without blocking. The 10ms service
        # floor caps us at 100 poll() calls/second, but H.264 video needs many
        # DRW ACKs/second. Without draining, the printer's send window fills and
        # it stops streaming after ~5 seconds. Limit to 4096 to stay bounded
        # (real OS socket buffers hold far fewer packets).
        try:
            _drain = 4096
            while _drain > 0 and api.poll(timeout=0) is not None:
                _drain -= 1
        except (ConnectionResetError, OSError):
            pass

        api = getattr(self, "_api", None)
        if api is None:
            if getattr(self, "wanted", True):
                raise ServiceRestartSignal(
                "PPPP API disappeared during worker loop", delay=self._note_connection_lost()
            )
            return

        if getattr(api, "state", PPPPState.Connected) != PPPPState.Connected:
            if getattr(self, "wanted", True):
                raise ServiceRestartSignal(
                "PPPP API disconnected during worker loop", delay=self._note_connection_lost()
            )
            return

        chans = getattr(api, "chans", [])
        if len(chans) > 1 and hasattr(chans[1], "skip_rx_gap"):
            if chans[1].skip_rx_gap(max_queued=8):
                self._drain_xzyh(chan=1)

        if not msg or msg.type != Type.DRW:
            return

        ch = api.chans[msg.chan]

        drain_xzyh = False
        with ch.lock:
            header = ch.peek(4, timeout=0)
            if not header:
                return

            if header[:4] == b'XZYH':
                drain_xzyh = True
            elif header[:2] == b'\xAA\xBB':
                aabb_header = ch.peek(12, timeout=0)
                if not aabb_header:
                    return
                aabb = Aabb.parse(aabb_header)[0]
                frame_len = 12 + aabb.len + 2
                if not ch.peek(frame_len, timeout=0):
                    return

                aabb, data = self._recv_aabb(ch)
                if len(data) != 1:
                    raise ValueError(f"Unexpected reply from aabb request: {data}")

                aabb.data = data
                self.notify((msg.chan, aabb))
            else:
                if msg.chan == 1:
                    if self._resync_xzyh(ch, msg.chan):
                        drain_xzyh = True
                    else:
                        return
                else:
                    raise ValueError(f"Unexpected data in stream: {header!r}")

        if drain_xzyh:
            self._drain_xzyh(chan=msg.chan)

    def worker_stop(self):
        if hasattr(self, "_connected_event"):
            self._connected_event.clear()
        self._force_close_api()

    @property
    def connected(self):
        api = getattr(self, "_api", None)
        if api is None:
            return False
        return getattr(api, "state", None) == PPPPState.Connected
