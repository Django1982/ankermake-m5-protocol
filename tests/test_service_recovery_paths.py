from contextlib import contextmanager
from datetime import datetime
from threading import Lock
from types import SimpleNamespace

import pytest

from cli.model import Account, Config, Printer
from libflagship.pppp import P2PCmdType, P2PSubCmdType
from libflagship.ppppapi import PPPPError, PPPPState
from web import app
from web.lib.service import RunState, ServiceRestartSignal
from web.service.filetransfer import FileTransferService
from web.service.pppp import PPPPService, probe_pppp
from web.service.video import VideoQueue, _STALL_TIMEOUT


def _config():
    return Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-123456",
            email="user@example.com",
        ),
        printers=[
            Printer(
                id="printer-1",
                sn="SN123",
                name="Printer",
                model="V8111",
                create_time=datetime(2024, 1, 1, 12, 0, 0),
                update_time=datetime(2024, 1, 1, 12, 0, 0),
                wifi_mac="aabbccddeeff",
                ip_addr="192.168.1.10",
                mqtt_key=b"\x01\x02",
                api_hosts=["api.example"],
                p2p_hosts=["p2p.example"],
                p2p_duid="duid-1",
                p2p_key="secret",
            )
        ],
    )


class FakeConfigManager:
    def __init__(self, cfg):
        self.cfg = cfg

    @contextmanager
    def open(self):
        yield self.cfg


@contextmanager
def _borrow(value):
    yield value


class FakeFD:
    def __init__(self, peeks, reads):
        self._peeks = list(peeks)
        self._reads = list(reads)
        self.lock = Lock()

    def peek(self, size, timeout=0):
        if self._peeks:
            return self._peeks.pop(0)
        return b""

    def read(self, size, timeout=0):
        if self._reads:
            return self._reads.pop(0)
        return b""


def test_video_queue_worker_start_stop_and_handler(monkeypatch):
    queue = object.__new__(VideoQueue)
    queue.video_enabled = True
    queue.handlers = []
    queue.state = RunState.Stopped
    queue.saved_light_state = True
    queue.saved_video_mode = None
    queue.saved_video_profile_id = "hd"
    queue.last_frame_at = None
    queue._live_started_at = None
    queue._last_live_refresh_at = 0.0
    queue._last_no_frame_log_at = 0.0
    queue._last_start_live_at = 0.0
    queue._live_active = False
    queue.api_id = None
    queue._enable_generation = 0
    notifications = []
    queue.notify = lambda msg: notifications.append(msg)

    commands = []
    fake_api = object()
    fake_pppp = SimpleNamespace(
        _api=fake_api,
        connected=True,
        xzyh_handlers=[],
        api_command=lambda command, data=None: commands.append((command, data)),
    )

    puts = []
    old_svc = app.svc
    app.svc = SimpleNamespace(
        get=lambda name: fake_pppp,
        put=lambda name: puts.append(name),
    )

    class FakeXzyh:
        def __init__(self, cmd):
            self.cmd = cmd

    monkeypatch.setattr("web.service.video.Xzyh", FakeXzyh)
    monkeypatch.setattr("web.service.video.time.monotonic", lambda: 123.0)

    try:
        queue.worker_start()
        queue._handler((1, FakeXzyh(1)))
        queue._handler((1, FakeXzyh(P2PCmdType.APP_CMD_VIDEO_FRAME)))
        queue.worker_stop()
    finally:
        app.svc = old_svc

    assert queue.pppp is None
    assert queue.api_id is None
    assert puts == ["pppp"]
    assert len(commands) == 4
    assert queue.last_frame_at == 123.0
    assert len(notifications) == 2


def test_video_queue_worker_run_detects_disconnect_api_swap_and_stall(monkeypatch):
    queue = object.__new__(VideoQueue)
    queue.video_enabled = True
    queue.handlers = [lambda _: None]
    queue.idle = lambda timeout=None: None
    queue._live_started_at = 100.0
    queue.last_frame_at = None
    queue._last_live_refresh_at = 0.0
    queue._last_no_frame_log_at = 0.0
    queue._last_start_live_at = 0.0
    queue._live_active = False
    queue.api_id = 1
    queue.pppp = SimpleNamespace(connected=False, _api=object())

    with pytest.raises(ServiceRestartSignal, match="No pppp connection"):
        queue.worker_run(timeout=0.1)

    queue.pppp = SimpleNamespace(connected=True, _api=object())
    with pytest.raises(ServiceRestartSignal, match="New pppp connection"):
        queue.worker_run(timeout=0.1)

    api = object()
    queue.pppp = SimpleNamespace(connected=True, _api=api)
    queue.api_id = id(api)
    commands = []
    queue.pppp.api_command = lambda command, data=None: commands.append((command, data))
    times = iter([100.0 + _STALL_TIMEOUT + 1, 100.0 + _STALL_TIMEOUT + 1, 100.0 + _STALL_TIMEOUT + 1])
    monkeypatch.setattr("web.service.video.time.monotonic", lambda: next(times))
    monkeypatch.setattr("web.service.video.time.sleep", lambda seconds: None)
    queue.worker_run(timeout=0.1)

    assert commands[0][0] == P2PSubCmdType.CLOSE_LIVE
    assert commands[1][0] == P2PSubCmdType.START_LIVE


def test_video_queue_api_profile_and_mode_validation():
    queue = object.__new__(VideoQueue)
    queue.pppp = None
    queue.saved_video_mode = None
    queue.saved_video_profile_id = None

    assert queue.api_video_profile("fhd") is False
    assert queue.saved_video_profile_id == "fhd"
    assert queue.api_video_profile("unknown") is False
    assert queue.api_video_mode("bad") is False


def test_pppp_probe_handles_missing_config_and_resolver_errors(monkeypatch):
    assert probe_pppp(FakeConfigManager(None), 0) is False
    monkeypatch.setattr(
        "web.service.pppp.cli.pppp.pppp_resolve_printer_ip",
        lambda config, printer, printer_index: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert probe_pppp(FakeConfigManager(_config()), 0) is False


def test_pppp_service_api_command_and_connected_property():
    svc = object.__new__(PPPPService)
    with pytest.raises(ConnectionError, match="No pppp connection"):
        svc.api_command(123)

    sent = []
    svc._api = SimpleNamespace(
        state=None,
        send_xzyh=lambda payload, cmd=None, block=None: sent.append((payload, cmd, block)) or "ok",
    )
    result = svc.api_command(7, value=9)

    assert result == "ok"
    assert b'"commandType": 7' in sent[0][0]
    assert sent[0][2] is False
    assert svc.connected is False
    svc._api.state = 2
    svc._api.state = PPPPState.Connected
    assert svc.connected is True


def test_pppp_service_drains_xzyh_and_tolerates_handler_errors(monkeypatch):
    svc = object.__new__(PPPPService)
    captured = []
    svc.xzyh_handlers = [
        lambda item: (_ for _ in ()).throw(RuntimeError("ignore")),
        lambda item: captured.append(item),
    ]
    svc._api = SimpleNamespace(
        chans=[
            FakeFD(
                peeks=[b"XZYH" + b"\x00" * 12, b""],
                reads=[b"XZYH" + b"\x00" * 12 + b"DATA"],
            )
        ]
    )
    monkeypatch.setattr("web.service.pppp.Xzyh.parse", lambda hdr: [SimpleNamespace(len=4)])

    svc._drain_xzyh(0)

    assert len(captured) == 1
    assert captured[0][0] == 0
    assert captured[0][1].data == b"DATA"


def test_pppp_service_worker_run_handles_reset_aabb_and_xzyh(monkeypatch):
    svc = object.__new__(PPPPService)
    notifications = []
    drained = []
    svc.notify = lambda item: notifications.append(item)
    svc._drain_xzyh = lambda chan: drained.append(chan)

    svc._api = SimpleNamespace(poll=lambda timeout=0: (_ for _ in ()).throw(ConnectionResetError()))
    with pytest.raises(ServiceRestartSignal):
        svc.worker_run(timeout=0.1)

    channel = FakeFD(
        peeks=[b"\xAA\xBB\x00\x00", b"\xAA\xBB" + b"\x00" * 10, b"\xAA" * 15],
        reads=[],
    )
    svc._api = SimpleNamespace(
        poll=lambda timeout=0: SimpleNamespace(type=208, chan=0),
        chans=[channel],
    )
    monkeypatch.setattr("web.service.pppp.Aabb.parse", lambda data: [SimpleNamespace(len=1)])
    svc._recv_aabb = lambda fd: (SimpleNamespace(data=None), b"x")
    svc.worker_run(timeout=0.1)

    assert notifications[0][0] == 0
    assert notifications[0][1].data == b"x"
    assert drained == [1]

    drained.clear()
    notifications.clear()
    channel = FakeFD(peeks=[b"XZYH" + b"\x00" * 12], reads=[])
    svc._api = SimpleNamespace(
        poll=lambda timeout=0: SimpleNamespace(type=208, chan=0),
        chans=[channel],
    )
    svc.worker_run(timeout=0.1)

    assert drained == [1, 0]


def test_filetransfer_notify_upload_swallow_and_error_paths(monkeypatch):
    svc = object.__new__(FileTransferService)
    svc.PROGRESS_INTERVAL = 0.0
    notifier_events = []
    svc.notify = lambda payload: (_ for _ in ()).throw(RuntimeError("boom"))
    svc._notifier = SimpleNamespace(send=lambda *args, **kwargs: notifier_events.append((args, kwargs)))
    svc._notify_upload({"status": "x"})
    uploads = []
    svc.notify = lambda payload: uploads.append(payload)

    mqtt = SimpleNamespace(set_gcode_layer_count=lambda count: None)
    old_svc = app.svc
    old_config = app.config.get("config")
    old_printer_index = app.config.get("printer_index")
    old_pppp_dump = app.config.get("pppp_dump")
    app.svc = SimpleNamespace(borrow=lambda name: _borrow(mqtt))
    app.config["config"] = FakeConfigManager(_config())
    app.config["printer_index"] = 0
    app.config["pppp_dump"] = None

    fd = SimpleNamespace(read=lambda: b"G28\n", filename="cube.gcode")
    monkeypatch.setattr("web.service.filetransfer.extract_layer_count", lambda raw: None)
    monkeypatch.setattr("web.service.filetransfer.patch_gcode_time", lambda raw: raw)

    try:
        monkeypatch.setattr(
            "web.service.filetransfer.cli.pppp.pppp_open",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        with pytest.raises(ConnectionError, match="No pppp connection"):
            svc.send_file(fd, user_name="alice")

        api = SimpleNamespace(stop=lambda: uploads.append({"status": "stopped"}))
        monkeypatch.setattr("web.service.filetransfer.cli.pppp.pppp_open", lambda *args, **kwargs: api)
        monkeypatch.setattr(
            "web.service.filetransfer.cli.pppp.pppp_send_file",
            lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("broken")),
        )
        with pytest.raises(ConnectionError, match="PPPP transfer failed"):
            svc.send_file(fd, user_name="alice", start_print=False)
    finally:
        app.svc = old_svc
        app.config["config"] = old_config
        app.config["printer_index"] = old_printer_index
        app.config["pppp_dump"] = old_pppp_dump

    assert any(item.get("status") == "error" and "offline" in item.get("error", "") for item in uploads)
    assert any(item.get("status") == "error" and "broken" in item.get("error", "") for item in uploads)
    assert {"status": "stopped"} in uploads
    assert notifier_events == []
