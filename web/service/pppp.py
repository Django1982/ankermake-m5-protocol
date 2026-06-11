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
        super().__init__()

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
        # Do not try to send a graceful close packet here. After a video freeze,
        # that send path can block and leave PPPP half-stopped forever. Force the
        # transport down locally so the service thread can complete its stop and
        # be restarted cleanly.
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
        config = app.config["config"]
        printer_index = getattr(self, "printer_index", app.config.get("printer_index", 0))

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
            raise ConnectionRefusedError("No printer IP found; ensure printer is online on the same network")

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
                raise ConnectionRefusedError("Connection rejected by device")
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
                raise ConnectionRefusedError("Connection rejected by device")

        elapsed = (datetime.now() - started_at).total_seconds()
        log.info(
            "%s: established pppp connection to %s in %.1fs",
            self.name,
            printer.name,
            elapsed,
        )
        self._api = api
        self._connected_event.set()

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
                raise ServiceRestartSignal("PPPP API missing while service is wanted", delay=0)
            return

        # A stale/disconnected API object after video recovery is not a usable
        # running PPPP session. Force an internal restart instead of idling
        # forever in a wanted-but-disconnected state.
        if getattr(api, "state", PPPPState.Connected) != PPPPState.Connected:
            if getattr(self, "wanted", True):
                raise ServiceRestartSignal("PPPP API exists but is not connected while service is wanted", delay=0)
            return

        try:
            msg = api.poll(timeout=timeout)
        except (ConnectionResetError, OSError):
            if not getattr(self, "wanted", True):
                return
            raise ServiceRestartSignal(delay=0)

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
                raise ServiceRestartSignal("PPPP API disappeared during worker loop", delay=0)
            return

        if getattr(api, "state", PPPPState.Connected) != PPPPState.Connected:
            if getattr(self, "wanted", True):
                raise ServiceRestartSignal("PPPP API disconnected during worker loop", delay=0)
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
