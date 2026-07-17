import threading

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import cli.pppp

from libflagship.pppp import Duid, PktPunchPkt
from libflagship.ppppapi import AnkerPPPPApi, FileUploadInfo


def _duid():
    return Duid(prefix="ABCDEF1", serial=123456, check="CHK01")


class _FakeClock:
    """Virtual clock so pppp_open retry tests don't take wall-clock time."""

    def __init__(self):
        self._now = datetime(2026, 1, 1)

    def now(self):
        return self._now

    def sleep(self, seconds):
        self._now += timedelta(seconds=seconds)


def _patch_pppp_open_env(monkeypatch, make_api):
    clock = _FakeClock()
    attempts = []

    monkeypatch.setattr("cli.pppp.datetime", SimpleNamespace(now=clock.now))
    monkeypatch.setattr("cli.pppp.time.sleep", clock.sleep)
    monkeypatch.setattr(
        "cli.pppp.pppp_resolve_printer_ip",
        lambda config, printer, printer_index, dumpfile=None: "10.0.0.25",
    )
    monkeypatch.setattr(
        "cli.pppp.AnkerPPPPApi.open_lan",
        lambda duid, host: attempts.append(True) or make_api(),
    )
    return attempts


class _FakeOpenConfig:
    def __init__(self):
        self._printers = [SimpleNamespace(name="p0", p2p_duid=str(_duid()))]

    def open(self):
        return self

    def __enter__(self):
        return SimpleNamespace(printers=self._printers)

    def __exit__(self, *args):
        return False


class _NeverConnectingApi:
    def __init__(self, stopped):
        self.state = None
        self.stopped = SimpleNamespace(is_set=lambda: stopped)
        self.sock = SimpleNamespace(close=lambda: None)

    def connect_lan_search(self):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def test_pppp_open_retries_after_explicit_close(monkeypatch):
    attempts = _patch_pppp_open_env(monkeypatch, lambda: _NeverConnectingApi(stopped=True))

    with pytest.raises(ConnectionRefusedError):
        cli.pppp.pppp_open(_FakeOpenConfig(), printer_index=0, timeout=2.0)

    assert len(attempts) > 1


def test_pppp_open_retries_when_connection_silently_never_completes(monkeypatch):
    # Regression: a silent non-completing attempt (no CLOSE packet, state
    # never reaches Connected) used to consume the entire deadline in the
    # first attempt's wait loop, so the retry loop never got a second try.
    attempts = _patch_pppp_open_env(monkeypatch, lambda: _NeverConnectingApi(stopped=False))

    with pytest.raises(ConnectionRefusedError):
        cli.pppp.pppp_open(_FakeOpenConfig(), printer_index=0, timeout=8.0)

    assert len(attempts) > 1


def test_probe_printer_ip_retries_before_succeeding(monkeypatch):
    connects = []

    class FakeApi:
        def __init__(self):
            self.sock = SimpleNamespace(close=lambda: None)

        def connect_lan_search(self):
            connects.append(True)

        def recv(self, timeout=None):
            if len(connects) == 1:
                raise TimeoutError
            return PktPunchPkt(duid=_duid())

    monkeypatch.setattr(
        "cli.pppp.AnkerPPPPAsyncApi.open_lan",
        lambda duid, host: FakeApi(),
    )

    printer = SimpleNamespace(p2p_duid=str(_duid()))

    assert cli.pppp.probe_printer_ip(printer, "10.0.0.25", timeout=0.8, attempts=2) is True
    assert len(connects) == 2


def test_lan_search_retries_and_deduplicates_replies(monkeypatch):
    persisted = []

    class FakeBroadcastApi:
        def __init__(self):
            self.sock = SimpleNamespace(close=lambda: None)
            self.addr = ("0.0.0.0", 32108)
            self.send_calls = 0
            self._responses = [
                (PktPunchPkt(duid=_duid()), "10.0.0.25"),
                TimeoutError(),
                (PktPunchPkt(duid=_duid()), "10.0.0.25"),
                TimeoutError(),
                TimeoutError(),
            ]

        def send(self, pkt):
            self.send_calls += 1

        def recv(self, timeout=None):
            item = self._responses.pop(0)
            if isinstance(item, tuple):
                msg, ip_addr = item
                self.addr = (ip_addr, 32108)
                return msg
            raise item

    fake_api = FakeBroadcastApi()

    monkeypatch.setattr("cli.pppp.pppp_open_broadcast", lambda dumpfile=None: fake_api)
    monkeypatch.setattr("cli.pppp.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "cli.pppp.persist_printer_ip",
        lambda config, duid, ip_addr, printer_index=None: persisted.append((duid, ip_addr)) or True,
    )

    results = cli.pppp.lan_search(object(), timeout=0.9, retries=3)

    assert fake_api.send_calls == 3
    assert results == [
        {
            "duid": str(_duid()),
            "ip_addr": "10.0.0.25",
            "persisted": True,
        }
    ]
    assert persisted == [(str(_duid()), "10.0.0.25")]


def test_pppp_send_file_handshake_times_out_instead_of_hanging_when_never_acked():
    # Regression (live-hardware bug): _pppp_send_file_handshake used to call
    # api.send_xzyh() without an explicit timeout, so a silently-dropped
    # handshake packet made Channel.write() block on a bare Event.wait() with
    # no deadline -- forever. This left the exclusive PPPP session lock held
    # indefinitely with zero further log output. It must now raise
    # TimeoutError within reply_timeout seconds instead.
    #
    # A real AnkerPPPPApi is used (not a fake) so the test exercises the
    # actual Channel.write() blocking path; the socket is never touched by
    # send_xzyh/recv_xzyh, so a bare SimpleNamespace stands in for it. The
    # handshake is run on a background thread and joined with a generous
    # timeout so this test fails fast (rather than hanging the suite) if a
    # future change reintroduces the unbounded wait.
    #
    # The initial send_xzyh() now retries transient DRW-ACK timeouts (see
    # test_pppp_send_file_handshake_retries_transient_drw_ack_timeout_on_initial_send
    # below), so with nothing ever acking this still must raise TimeoutError
    # once the retry budget (reply_timeout * 3 attempts) is exhausted --
    # bounded, not hanging forever.
    api = AnkerPPPPApi(sock=SimpleNamespace(), duid=_duid(), addr=("10.0.0.25", 32108))

    fui = FileUploadInfo.from_data(
        b"gcode-data",
        "job.gcode",
        user_name="ankerctl",
        user_id="-",
        machine_id="-",
    )

    outcome = {}

    def run():
        try:
            cli.pppp._pppp_send_file_handshake(api, fui, reply_timeout=0.2)
        except BaseException as exc:  # noqa: BLE001 - captured for assertion below
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "handshake hung instead of raising TimeoutError"
    assert isinstance(outcome.get("error"), TimeoutError)
    assert "PPPP DRW ACK" in str(outcome["error"])


def test_pppp_send_file_handshake_retries_transient_drw_ack_timeout_on_initial_send():
    # Live-hardware follow-up bug: right after a fast PPPP reconnect, the very
    # first handshake packet on chan=0 (the initial send_xzyh() call inside
    # _pppp_send_file_handshake) could time out waiting for the DRW ACK once,
    # even though the transport was otherwise healthy -- the printer's
    # file-transfer subsystem wasn't quite ready yet. This mirrors the
    # tolerance _retry_file_transfer_data() already has for chan=1 data
    # chunks; the initial handshake send must retry (with reset_chan_tx on
    # chan=0 between attempts) instead of failing on the first timeout.
    calls = {"send_xzyh": 0, "reset_chan_tx": []}

    class FakeApi:
        def send_xzyh(self, data, cmd, timeout=None, chan=0):
            calls["send_xzyh"] += 1
            if calls["send_xzyh"] == 1:
                raise TimeoutError("Timed out waiting for PPPP DRW ACK")
            return None

        def recv_xzyh(self, chan=0, timeout=None):
            # code=0 -> handshake accepted, no legacy-fallback send needed.
            return SimpleNamespace(data=(0).to_bytes(4, "little"))

        def reset_chan_tx(self, chan=1):
            calls["reset_chan_tx"].append(chan)

    fui = FileUploadInfo.from_data(
        b"gcode-data",
        "job.gcode",
        user_name="ankerctl",
        user_id="-",
        machine_id="-",
    )

    cli.pppp._pppp_send_file_handshake(FakeApi(), fui, reply_timeout=0.2)

    assert calls["send_xzyh"] == 2, "expected one retry after the transient DRW-ACK timeout"
    assert calls["reset_chan_tx"] == [0], "retry must reset chan=0 (the handshake channel), not chan=1"


def test_pppp_resolve_printer_ip_falls_back_to_saved_ip_when_probe_and_search_miss(monkeypatch):
    monkeypatch.setattr("cli.pppp.probe_printer_ip", lambda printer, ip_addr, timeout=2.0: False)
    monkeypatch.setattr("cli.pppp.lan_search", lambda config, timeout=2.0, dumpfile=None: [])

    printer = SimpleNamespace(p2p_duid=str(_duid()), ip_addr="10.0.0.25")

    resolved = cli.pppp.pppp_resolve_printer_ip(object(), printer, printer_index=0, timeout=0.5)

    assert resolved == "10.0.0.25"
