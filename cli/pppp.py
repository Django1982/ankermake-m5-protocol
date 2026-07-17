import json
import random
import time
import uuid
import logging as log

from datetime import datetime, timedelta
from tqdm import tqdm

import cli.util

from libflagship.pktdump import PacketWriter
from libflagship.pppp import Duid, P2PCmdType, FileTransfer, PktClose, PktLanSearch, PktPunchPkt
from libflagship.ppppapi import AnkerPPPPApi, AnkerPPPPAsyncApi, PPPPState

_PPPP_PROBE_ATTEMPTS = 2
_PPPP_LAN_SEARCH_RETRIES = 3
_PPPP_LAN_SEARCH_MIN_WINDOW = 0.35
_PPPP_LAN_SEARCH_RETRY_GAP = 0.15
_PPPP_OPEN_RETRY_GAP = 0.3
_PPPP_OPEN_ATTEMPT_WINDOW = 3.0


def _pppp_dumpfile(api, dumpfile):
    if dumpfile:
        log.info(f"Logging all pppp traffic to {dumpfile!r}")
        pktwr = PacketWriter.open(dumpfile)
        api.set_dumper(pktwr)


def pppp_open(config, printer_index, timeout=None, dumpfile=None):
    deadline = datetime.now() + timedelta(seconds=timeout) if timeout else None

    with config.open() as cfg:
        if printer_index < 0 or printer_index >= len(cfg.printers):
            raise ValueError(f"Printer number {printer_index} out of range, must be in 0..{len(cfg.printers)-1}")
        printer = cfg.printers[printer_index]
        ip_addr = pppp_resolve_printer_ip(config, printer, printer_index, dumpfile=dumpfile)
        if not ip_addr:
            raise ConnectionRefusedError("No printer IP found; ensure printer is online on the same network")

    attempt = 0
    while True:
        attempt += 1
        api = AnkerPPPPApi.open_lan(Duid.from_string(printer.p2p_duid), host=ip_addr)
        _pppp_dumpfile(api, dumpfile)

        log.info(
            f"Trying connect to printer {printer.name} ({printer.p2p_duid}) over pppp using ip {ip_addr}"
            + (f" (attempt {attempt})" if attempt > 1 else "")
        )

        api.connect_lan_search()
        api.start()

        # Cap each attempt to a sub-window of the overall deadline. A silent
        # non-completing attempt (no CLOSE, connection just never establishes)
        # must not consume the whole budget, or the retry loop below never
        # gets a second attempt.
        attempt_deadline = None
        if deadline:
            attempt_deadline = min(
                deadline, datetime.now() + timedelta(seconds=_PPPP_OPEN_ATTEMPT_WINDOW)
            )

        while api.state != PPPPState.Connected and not api.stopped.is_set():
            if attempt_deadline and datetime.now() > attempt_deadline:
                break
            time.sleep(0.1)

        if api.state == PPPPState.Connected:
            log.info("Established pppp connection")
            return api

        # Rejected by the printer (or timed out establishing this attempt).
        # The printer can take a moment to free up its single PPPP session
        # slot after a previous connection closes, so retry within the
        # deadline instead of failing on the first rejection.
        api.stop()
        try:
            api.sock.close()
        except OSError as exc:
            log.debug(f"PPPP socket close failed: {exc}")

        if not deadline or datetime.now() >= deadline:
            raise ConnectionRefusedError("Connection rejected by device")

        time.sleep(_PPPP_OPEN_RETRY_GAP)


def pppp_open_broadcast(dumpfile=None):
    api = AnkerPPPPApi.open_broadcast()
    api.state = PPPPState.Connected
    _pppp_dumpfile(api, dumpfile)
    return api


def probe_printer_ip(printer, ip_addr, timeout=2.0, attempts=_PPPP_PROBE_ATTEMPTS):
    """Check if a printer is reachable at a specific IP via a lightweight LAN search.

    Sends a directed PktLanSearch and waits for a PktPunchPkt response.
    Does NOT establish a full PPPP session, so it won't interfere with
    subsequent connection attempts.
    """
    if not ip_addr:
        return False

    api = AnkerPPPPAsyncApi.open_lan(Duid.from_string(printer.p2p_duid), host=ip_addr)
    try:
        total_timeout = max(float(timeout or 0.0), 0.1)
        max_attempts = max(1, int(attempts or 1))
        per_attempt_timeout = max(total_timeout / max_attempts, _PPPP_LAN_SEARCH_MIN_WINDOW)
        deadline = time.monotonic() + total_timeout

        for _ in range(max_attempts):
            api.connect_lan_search()
            attempt_deadline = min(deadline, time.monotonic() + per_attempt_timeout)
            while time.monotonic() < attempt_deadline:
                remaining = max(0.05, attempt_deadline - time.monotonic())
                try:
                    msg = api.recv(timeout=remaining)
                except (TimeoutError, ConnectionResetError, StopIteration):
                    break
                if isinstance(msg, PktPunchPkt):
                    return True
            if time.monotonic() >= deadline:
                break
        return False
    finally:
        try:
            api.sock.close()
        except Exception as exc:
            log.debug(f"PPPP socket close failed: {exc}")


def lan_search(config, timeout=1.0, dumpfile=None, retries=_PPPP_LAN_SEARCH_RETRIES):
    discovered = []
    api = pppp_open_broadcast(dumpfile=dumpfile)
    try:
        total_timeout = max(float(timeout or 0.0), _PPPP_LAN_SEARCH_MIN_WINDOW)
        max_attempts = max(1, int(retries or 1))
        per_attempt_timeout = max(total_timeout / max_attempts, _PPPP_LAN_SEARCH_MIN_WINDOW)
        deadline = time.monotonic() + total_timeout
        seen = set()
        for attempt in range(max_attempts):
            api.send(PktLanSearch())
            attempt_deadline = min(deadline, time.monotonic() + per_attempt_timeout)
            while time.monotonic() < attempt_deadline:
                remaining = max(0.05, attempt_deadline - time.monotonic())
                try:
                    resp = api.recv(timeout=remaining)
                except TimeoutError:
                    break

                if not isinstance(resp, PktPunchPkt):
                    continue

                duid = str(resp.duid)
                ip_addr = str(api.addr[0])
                if (duid, ip_addr) in seen:
                    continue

                seen.add((duid, ip_addr))
                discovered.append({
                    "duid": duid,
                    "ip_addr": ip_addr,
                    "persisted": persist_printer_ip(config, duid, ip_addr),
                })

            if time.monotonic() >= deadline:
                break

            if attempt + 1 < max_attempts:
                time.sleep(min(_PPPP_LAN_SEARCH_RETRY_GAP, max(0.0, deadline - time.monotonic())))
    finally:
        try:
            api.sock.close()
        except Exception as exc:
            log.debug(f"PPPP socket close failed: {exc}")

    return discovered


def persist_printer_ip(config, printer_duid, ip_addr, printer_index=None):
    ip_addr = (ip_addr or "").strip()
    if not ip_addr:
        return False

    try:
        with config.open() as cfg:
            printers = getattr(cfg, "printers", []) or []
            if not printers:
                return False

            indexes = []
            if printer_index is not None and 0 <= printer_index < len(printers):
                if printers[printer_index].p2p_duid == printer_duid:
                    indexes.append(printer_index)

            if not indexes:
                indexes = [
                    idx for idx, saved_printer in enumerate(printers)
                    if saved_printer.p2p_duid == printer_duid
                ]

            if not indexes:
                return False

        with config.modify() as cfg:
            for idx in indexes:
                if idx < len(cfg.printers):
                    cfg.printers[idx].ip_addr = ip_addr

        return True
    except Exception as e:
        log.warning(f"Could not persist printer IP: {e}")
        return False


def pppp_resolve_printer_ip(config, printer, printer_index, dumpfile=None, timeout=2.0):
    ip_addr = (printer.ip_addr or "").strip()
    if ip_addr:
        log.info(f"Validating saved printer IP {ip_addr}")
        if probe_printer_ip(printer, ip_addr, timeout=timeout):
            return ip_addr
        log.warning(f"Saved printer IP {ip_addr} is unreachable; attempting LAN search")

    discovered = lan_search(config, timeout=timeout, dumpfile=dumpfile)
    for result in discovered:
        if result["duid"] == printer.p2p_duid:
            ip_addr = result["ip_addr"]
            log.info(f"Discovered printer IP: {ip_addr}")
            if not result["persisted"]:
                persist_printer_ip(config, printer.p2p_duid, ip_addr, printer_index=printer_index)
            return ip_addr

    # Some networks intermittently drop the directed LAN-search probe even
    # though a direct PPPP connection to the saved address still succeeds.
    # Prefer trying the last known good IP over failing fast and forcing the
    # service manager into a noisy reconnect loop.
    if (printer.ip_addr or "").strip():
        fallback_ip = (printer.ip_addr or "").strip()
        log.warning(
            f"PPPP IP validation failed and LAN search found no match; "
            f"falling back to saved printer IP {fallback_ip}"
        )
        return fallback_ip

    return ""


def _send_xzyh_with_retry(api, data, cmd, reply_timeout, chan=0, retries=2):
    # The printer's low-level PPPP handshake (P2P_RDY) can complete slightly
    # before its higher-level file-transfer subsystem is ready to receive on
    # this channel, so the very first packet sent right after a session is
    # established can be silently dropped once in a while. Tolerate that the
    # same way _retry_file_transfer_data() already tolerates it on chan=1.
    attempts = retries + 1

    for attempt in range(1, attempts + 1):
        try:
            return api.send_xzyh(data, cmd=cmd, timeout=reply_timeout)
        except TimeoutError as exc:
            is_drw_timeout = "PPPP DRW ACK" in str(exc)
            if not is_drw_timeout or attempt >= attempts:
                raise

            log.warning(
                f"Retrying file transfer handshake send after transport timeout "
                f"(attempt {attempt + 1}/{attempts})"
            )
            api.reset_chan_tx(chan=chan)


def _pppp_send_file_handshake(api, fui, reply_timeout=2.0):
    file_uuid = (fui.machine_id or "").strip()
    if not file_uuid or file_uuid == "-":
        file_uuid = uuid.uuid4().hex.upper()
        fui.machine_id = file_uuid

    payload = {
        "uuid": file_uuid,
        "device": "ankerctl",
        "flag": 0,
        "random": random.getrandbits(64),
        "timeout": 40,
        "total_timeout": 120,
    }

    log.info("Requesting file transfer..")
    _send_xzyh_with_retry(
        api, json.dumps(payload).encode(), cmd=P2PCmdType.P2P_SEND_FILE, reply_timeout=reply_timeout, chan=0
    )

    try:
        reply = api.recv_xzyh(chan=0, timeout=reply_timeout)
    except TimeoutError:
        reply = None

    if not reply:
        log.warning("No P2P_SEND_FILE reply; falling back to legacy handshake")
        api.send_xzyh(file_uuid[:16].encode(), cmd=P2PCmdType.P2P_SEND_FILE, timeout=reply_timeout)
        return

    if reply.data:
        code = int.from_bytes(reply.data[:4], "little", signed=False)
        if code != 0:
            log.warning(f"P2P_SEND_FILE reply error 0x{code:08x}; falling back to legacy handshake")
            api.send_xzyh(file_uuid[:16].encode(), cmd=P2PCmdType.P2P_SEND_FILE, timeout=reply_timeout)


def _retry_file_transfer_data(api, chunk, pos, reply_timeout, retries=2):
    attempts = retries + 1

    for attempt in range(1, attempts + 1):
        try:
            return api.aabb_request(chunk, frametype=FileTransfer.DATA, pos=pos, timeout=reply_timeout)
        except TimeoutError as exc:
            is_drw_timeout = "PPPP DRW ACK" in str(exc)
            if not is_drw_timeout or attempt >= attempts:
                raise

            log.warning(
                f"Retrying file transfer chunk at pos {pos} after transport timeout "
                f"(attempt {attempt + 1}/{attempts})"
            )
            api.reset_chan_tx(chan=1)


def pppp_send_file(api, fui, data, rate_limit_mbps=None, progress_cb=None, show_progress=True):
    reply_timeout = 15.0

    _pppp_send_file_handshake(api, fui)

    log.info("Sending file metadata..")
    api.aabb_request(bytes(fui), frametype=FileTransfer.BEGIN, timeout=reply_timeout)

    log.info("Sending file contents..")
    blocksize = 1024 * 32
    chunks = cli.util.split_chunks(data, blocksize)
    pos = 0
    total = len(data)

    limiter = cli.util.RateLimiter(rate_limit_mbps) if rate_limit_mbps else None
    if progress_cb:
        try:
            progress_cb(0, total)
        except Exception as e:
            log.warning(f"Progress callback failed: {e}")
    with tqdm(
        unit="b",
        total=total,
        unit_scale=True,
        unit_divisor=1024,
        disable=not show_progress,
    ) as bar:
        for chunk in chunks:
            _retry_file_transfer_data(api, chunk, pos, reply_timeout)
            pos += len(chunk)
            if limiter:
                limiter.throttle(len(chunk))
            if progress_cb:
                try:
                    progress_cb(pos, total)
                except Exception as e:
                    log.warning(f"Progress callback failed: {e}")
            bar.update(len(chunk))
