import importlib
import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from cli.model import Account, Config, Printer
from simple_websocket.errors import ConnectionClosed

import web as web_module
from web.lib.service import RunState


API_KEY = "secret-key-123456"


def _printer(sn="SN1", name="Printer", model="V8111"):
    return Printer(
        id=sn,
        sn=sn,
        name=name,
        model=model,
        create_time=datetime(2024, 1, 1, 12, 0, 0),
        update_time=datetime(2024, 1, 1, 12, 0, 0),
        wifi_mac="aabbccddeeff",
        ip_addr="192.168.1.10",
        mqtt_key=b"\x01\x02",
        api_hosts=["api.example"],
        p2p_hosts=["p2p.example"],
        p2p_duid=f"duid-{sn}",
        p2p_key="secret",
    )


class FakeConfigManager:
    def __init__(self, cfg):
        self.cfg = cfg

    @contextmanager
    def open(self):
        yield self.cfg

    @contextmanager
    def modify(self):
        yield self.cfg


class FakeSock:
    def __init__(self, receives=None, close_after_sends=None):
        self.receives = list(receives or [])
        self.sent = []
        self.close_after_sends = close_after_sends

    def send(self, data):
        self.sent.append(data)
        if self.close_after_sends is not None and len(self.sent) >= self.close_after_sends:
            raise ConnectionClosed()

    def receive(self):
        if not self.receives:
            return None
        value = self.receives.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeServices:
    def __init__(self, *, streams=None, borrowed=None, svcs=None, refs=None):
        self._streams = streams or {}
        self._borrowed = borrowed or {}
        self.svcs = svcs or {}
        self.refs = refs or {}

    def stream(self, name, **kwargs):
        yield from self._streams.get(name, [])

    @contextmanager
    def borrow(self, name):
        yield self._borrowed.get(name)


def _base_config():
    return Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer("SN1", "Printer 1"), _printer("SN2", "Printer 2")],
    )


def _ws_handler(module, name):
    return module.app.view_functions[name].__closure__[0].cell_contents


def _install_app_state(module, **values):
    app = module.app
    keys = [
        "config",
        "api_key",
        "login",
        "printer_index",
        "video_supported",
        "unsupported_device",
    ]
    old_values = {key: app.config.get(key) for key in keys}
    old_svc = app.svc
    app.config.update({
        "config": FakeConfigManager(_base_config()),
        "api_key": None,
        "login": True,
        "printer_index": 0,
        "video_supported": True,
        "unsupported_device": False,
    })
    for key, value in values.items():
        if key == "svc":
            app.svc = value
        else:
            app.config[key] = value
    return old_values, old_svc


def _restore_app_state(module, old_values, old_svc):
    module.app.svc = old_svc
    for key, value in old_values.items():
        module.app.config[key] = value


def test_mqtt_and_upload_websockets_forward_stream_messages():
    sock = FakeSock()
    mqtt_name = web_module.mqtt_service_name(0)
    services = FakeServices(
        streams={
            mqtt_name: [{"hello": "mqtt"}],
            "filetransfer": [{"status": "done"}],
        }
    )
    old_values, old_svc = _install_app_state(web_module, svc=services)

    try:
        _ws_handler(web_module, "mqtt")(sock)
        _ws_handler(web_module, "upload")(sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)

    assert sock.sent == [
        json.dumps({"hello": "mqtt"}),
        json.dumps({"status": "done"}),
    ]


def test_video_websocket_toggles_streaming_and_ctrl_dispatches_commands():
    set_enabled = []
    viewer_events = []
    light_calls = []
    profile_calls = []
    quality_calls = []
    videoqueue = SimpleNamespace(
        saved_video_profile_id="balanced",
        set_video_enabled=lambda enabled: set_enabled.append(enabled),
        viewer_connected=lambda: viewer_events.append("connect"),
        viewer_disconnected=lambda: viewer_events.append("disconnect"),
        api_light_state=lambda enabled: light_calls.append(enabled),
        api_video_profile=lambda profile: profile_calls.append(profile),
        api_video_mode=lambda quality: quality_calls.append(quality),
    )
    services = FakeServices(
        streams={"videoqueue": [SimpleNamespace(data=b"frame-1"), SimpleNamespace(data=b"frame-2")]},
        borrowed={"videoqueue": videoqueue},
        svcs={"videoqueue": videoqueue},
        refs={"videoqueue": 0},
    )
    old_values, old_svc = _install_app_state(web_module, svc=services)

    try:
        video_sock = FakeSock()
        _ws_handler(web_module, "video")(video_sock)

        ctrl_sock = FakeSock(receives=[
            json.dumps({"light": True}),
            json.dumps({"video_profile": "smooth"}),
            json.dumps({"quality": 2}),
            json.dumps({"video_enabled": False}),
            None,
        ])
        _ws_handler(web_module, "ctrl")(ctrl_sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)

    assert video_sock.sent == [b"frame-1", b"frame-2"]
    assert viewer_events == ["connect", "disconnect"]
    assert set_enabled == [False]
    assert json.loads(ctrl_sock.sent[0]) == {"ankerctl": 1}
    assert json.loads(ctrl_sock.sent[1]) == {"video_profile": "balanced"}
    assert light_calls == [True]
    assert profile_calls == ["smooth"]
    assert quality_calls == [2]


def test_ctrl_light_prefers_videoqueue_light_path_when_available():
    light_calls = []
    videoqueue_calls = []

    videoqueue = SimpleNamespace(
        saved_video_profile_id="hd",
        api_light_state=lambda enabled: videoqueue_calls.append(enabled),
    )
    pppp = SimpleNamespace(
        name="pppp",
        connected=True,
        api_command=lambda command_type, **kwargs: light_calls.append((command_type, kwargs)),
    )
    services = FakeServices(
        svcs={
            "videoqueue": videoqueue,
            "pppp": pppp,
        },
        refs={
            "videoqueue": 0,
            "pppp": 0,
        },
    )
    old_values, old_svc = _install_app_state(web_module, svc=services)

    try:
        ctrl_sock = FakeSock(receives=[
            json.dumps({"light": True}),
            None,
        ])
        _ws_handler(web_module, "ctrl")(ctrl_sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)

    assert light_calls == []
    assert videoqueue_calls == [True]
    assert videoqueue.saved_light_state is True


def test_ctrl_light_uses_active_pppp_without_videoqueue():
    light_calls = []
    pppp = SimpleNamespace(
        name="pppp",
        connected=True,
        api_command=lambda command_type, **kwargs: light_calls.append((command_type, kwargs)),
    )
    services = FakeServices(
        svcs={
            "pppp": pppp,
        },
        refs={
            "pppp": 0,
        },
    )
    old_values, old_svc = _install_app_state(web_module, svc=services)

    try:
        ctrl_sock = FakeSock(receives=[
            json.dumps({"light": True}),
            None,
        ])
        _ws_handler(web_module, "ctrl")(ctrl_sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)

    assert light_calls == [
        (web_module.P2PSubCmdType.LIGHT_STATE_SWITCH, {"data": {"open": True}})
    ]


def test_nonzero_printer_services_do_not_fallback_to_legacy_instances():
    viewer_events = []
    printer_two_video = SimpleNamespace(
        saved_video_profile_id="hd",
        viewer_connected=lambda: viewer_events.append("connect"),
        viewer_disconnected=lambda: viewer_events.append("disconnect"),
    )
    mqtt_sock = None
    video_sock = None
    services = FakeServices(
        streams={
            "mqttqueue": [{"hello": "legacy-mqtt"}],
            web_module.mqtt_service_name(1): [{"hello": "mqtt-1"}],
            "videoqueue": [SimpleNamespace(data=b"legacy-frame")],
            web_module.video_service_name(1): [SimpleNamespace(data=b"printer-two-frame")],
        },
        borrowed={
            "videoqueue": SimpleNamespace(saved_video_profile_id="legacy"),
            web_module.video_service_name(1): printer_two_video,
        },
        svcs={
            "mqttqueue": SimpleNamespace(name="legacy-mqtt"),
            web_module.mqtt_service_name(1): SimpleNamespace(name="mqtt-1"),
            "pppp": SimpleNamespace(name="legacy-pppp", connected=True, wanted=True),
            web_module.pppp_service_name(1): SimpleNamespace(name="pppp-1", connected=True, wanted=True),
            "videoqueue": SimpleNamespace(name="legacy-video"),
            web_module.video_service_name(1): printer_two_video,
        },
        refs={
            "videoqueue": 0,
            web_module.video_service_name(1): 0,
        },
    )
    old_values, old_svc = _install_app_state(web_module, svc=services)

    try:
        with web_module.app.test_request_context("/ws/mqtt?printer_index=1"):
            mqtt_sock = FakeSock(close_after_sends=1)
            _ws_handler(web_module, "mqtt")(mqtt_sock)
        with web_module.app.test_request_context("/ws/video?printer_index=1"):
            video_sock = FakeSock(close_after_sends=1)
            _ws_handler(web_module, "video")(video_sock)
        mqtt_name = web_module.get_mqtt_service(1).name
        pppp_name = web_module.get_pppp_service(1).name
        resolved_video = web_module.get_video_service(1)
        resolved_video_name = web_module.resolve_video_service_name(1)
        resolved_pppp_name = web_module.resolve_pppp_service_name(1)
    finally:
        _restore_app_state(web_module, old_values, old_svc)

    assert mqtt_sock.sent == [json.dumps({"hello": "mqtt-1"})]
    assert video_sock.sent == [b"printer-two-frame"]
    assert viewer_events == ["connect", "disconnect"]
    assert mqtt_name == "mqtt-1"
    assert pppp_name == "pppp-1"
    assert resolved_video is printer_two_video
    assert resolved_video_name == web_module.video_service_name(1)
    assert resolved_pppp_name == web_module.pppp_service_name(1)


def test_pppp_probe_helper_and_state_websocket_emit_status(monkeypatch):
    with web_module.app.pppp_probe_lock:
        web_module.app.pppp_probe = {
            "result": None,
            "last_time": 0.0,
            "fail_count": 0,
            "thread": None,
            "client_count": 1,
        }

    old_values, old_svc = _install_app_state(
        web_module,
        config=FakeConfigManager(_base_config()),
        printer_index=0,
    )
    monkeypatch.setattr("web.service.pppp.probe_pppp", lambda config, idx: True)
    monkeypatch.setattr("web.time.time", lambda: 100.0)

    try:
        web_module._maybe_start_pppp_probe("test")
        with web_module.app.pppp_probe_lock:
            thread = web_module.app.pppp_probe["thread"]
        if thread is not None:
            thread.join(timeout=1.0)
        sock = FakeSock(close_after_sends=1)
        mqtt_name = web_module.mqtt_service_name(0)
        services = FakeServices(
            svcs={
                "pppp": SimpleNamespace(connected=False, wanted=False),
                mqtt_name: SimpleNamespace(last_message_time=0.0),
            }
        )
        web_module.app.svc = services
        monkeypatch.setattr(
            "web._maybe_start_pppp_probe",
            lambda reason="scheduled", printer_index=None: None,
        )
        monkeypatch.setattr("web.time.sleep", lambda seconds: None)
        _ws_handler(web_module, "pppp_state")(sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)
        with web_module.app.pppp_probe_lock:
            web_module.app.pppp_probe = {
                "result": None,
                "last_time": 0.0,
                "fail_count": 0,
                "thread": None,
                "client_count": 0,
            }

    assert json.loads(sock.sent[0]) == {"status": "connected", "source": "probe"}


def test_pppp_state_websocket_marks_stale_probe_success_dormant_without_reprobe(monkeypatch):
    with web_module.app.pppp_probe_lock:
        web_module.app.pppp_probe = {
            "result": True,
            "last_time": 100.0,
            "fail_count": 0,
            "thread": None,
            "client_count": 1,
        }

    old_values, old_svc = _install_app_state(
        web_module,
        config=FakeConfigManager(_base_config()),
        printer_index=0,
    )
    probe_calls = []

    def record_probe(reason="scheduled", printer_index=None):
        probe_calls.append((reason, printer_index))

    try:
        sock = FakeSock(close_after_sends=1)
        mqtt_name = web_module.mqtt_service_name(0)
        services = FakeServices(
            svcs={
                "pppp": SimpleNamespace(connected=False, wanted=False),
                mqtt_name: SimpleNamespace(last_message_time=0.0),
            }
        )
        web_module.app.svc = services
        monkeypatch.setattr("web._maybe_start_pppp_probe", record_probe)
        monkeypatch.setattr("web.time.time", lambda: 120.0)
        monkeypatch.setattr("web.time.sleep", lambda seconds: None)
        _ws_handler(web_module, "pppp_state")(sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)
        with web_module.app.pppp_probe_lock:
            web_module.app.pppp_probe = {
                "result": None,
                "last_time": 0.0,
                "fail_count": 0,
                "thread": None,
                "client_count": 0,
            }

    assert json.loads(sock.sent[0]) == {"status": "dormant", "source": "none"}
    assert probe_calls == []


def test_pppp_probe_helper_skips_when_services_are_already_recovering(monkeypatch):
    old_values, old_svc = _install_app_state(web_module)
    calls = []
    web_module.app.svc = FakeServices(
        svcs={
            web_module.pppp_service_name(0): SimpleNamespace(wanted=True),
            web_module.video_service_name(0): SimpleNamespace(_awaiting_pppp_recycle=True),
        }
    )

    with web_module.app.pppp_probe_lock:
        web_module.app.pppp_probe = {
            "result": None,
            "last_time": 0.0,
            "fail_count": 0,
            "thread": None,
            "client_count": 1,
        }

    monkeypatch.setattr("web.service.pppp.probe_pppp", lambda config, idx: calls.append((config, idx)) or True)

    try:
        web_module._maybe_start_pppp_probe("test", printer_index=0)
    finally:
        _restore_app_state(web_module, old_values, old_svc)
        with web_module.app.pppp_probe_lock:
            web_module.app.pppp_probe = {
                "result": None,
                "last_time": 0.0,
                "fail_count": 0,
                "thread": None,
                "client_count": 0,
            }

    assert calls == []


def test_dev_debug_routes_register_and_dispatch(monkeypatch):
    monkeypatch.setenv("ANKERCTL_DEV_MODE", "true")
    importlib.reload(web_module)

    try:
        app = web_module.app
        client = app.test_client()
        set_debug = []
        simulated = []
        restart_calls = []

        mqtt = SimpleNamespace(
            state=RunState.Running,
            wanted=True,
            get_state=lambda: {"debug_logging": False},
            set_debug_logging=lambda enabled: set_debug.append(enabled),
            simulate_event=lambda event_type, payload: simulated.append((event_type, payload)),
        )
        pppp = SimpleNamespace(
            state=RunState.Running,
            wanted=True,
            restart=lambda: restart_calls.append(True),
        )
        mqtt_name = web_module.mqtt_service_name(0)
        services = SimpleNamespace(
            borrow=lambda name: _borrow_debug(mqtt if name == mqtt_name else None),
            svcs={mqtt_name: mqtt, "pppp": pppp},
            refs={mqtt_name: 1, "pppp": 0},
        )
        app.svc = services
        app.config["api_key"] = API_KEY
        app.config["login"] = True
        app.config["config"] = FakeConfigManager(_base_config())
        app.config["printer_index"] = 0

        class FakeThread:
            def __init__(self, target, daemon=True):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr("web.threading.Thread", FakeThread)
        monkeypatch.setattr("web.service.pppp.probe_pppp", lambda config, idx: True)

        unauthorized = client.get("/api/debug/state")
        state = client.get("/api/debug/state", headers={"X-Api-Key": API_KEY})
        configured = client.post(
            "/api/debug/config",
            json={"debug_logging": True},
            headers={"X-Api-Key": API_KEY},
        )
        simulated_resp = client.post(
            "/api/debug/simulate",
            json={"type": "start", "payload": {"filename": "cube.gcode"}},
            headers={"X-Api-Key": API_KEY},
        )
        services_resp = client.get("/api/debug/services", headers={"X-Api-Key": API_KEY})
        restarted = client.post("/api/debug/services/pppp/restart", headers={"X-Api-Key": API_KEY})
        tested = client.post("/api/debug/services/pppp/test", headers={"X-Api-Key": API_KEY})
    finally:
        monkeypatch.setenv("ANKERCTL_DEV_MODE", "false")
        importlib.reload(web_module)

    assert unauthorized.status_code == 401
    assert state.status_code == 200
    assert state.get_json()["debug_logging"] is False
    assert configured.status_code == 200
    assert simulated_resp.status_code == 200
    assert services_resp.status_code == 200
    assert services_resp.get_json()["services"]["pppp"]["state"] == "Running"
    assert restarted.status_code == 200
    assert tested.get_json() == {"result": "ok"}
    assert set_debug == [True]
    assert simulated == [("start", {"filename": "cube.gcode"})]
    assert restart_calls == [True]


def test_ws_ctrl_rejects_without_api_key():
    """WebSocket handlers must reject unauthenticated connections when
    an API key is configured, sending {"error": "unauthorized"} before closing."""
    sock = FakeSock()
    services = FakeServices()
    old_values, old_svc = _install_app_state(
        web_module, svc=services, api_key=API_KEY
    )

    try:
        with web_module.app.test_request_context():
            _ws_handler(web_module, "ctrl")(sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)

    assert len(sock.sent) == 1
    msg = json.loads(sock.sent[0])
    assert msg == {"error": "unauthorized"}


def test_ws_ctrl_allows_with_session():
    """WebSocket handlers should allow access when session is authenticated."""
    ctrl_msg = json.dumps({"light": True})
    sock = FakeSock(receives=[ctrl_msg])
    light_calls = []
    vq = SimpleNamespace(
        saved_video_profile_id="hd",
        api_light_state=lambda v: light_calls.append(v),
        api_video_profile=lambda v: None,
    )
    services = FakeServices(
        svcs={"videoqueue": vq},
        borrowed={"videoqueue": vq},
    )
    old_values, old_svc = _install_app_state(
        web_module, svc=services, api_key=API_KEY
    )

    try:
        with web_module.app.test_request_context():
            from flask import session as flask_session
            flask_session["authenticated"] = True
            _ws_handler(web_module, "ctrl")(sock)
    finally:
        _restore_app_state(web_module, old_values, old_svc)

    # Should have received ankerctl handshake + video_profile + processed light command
    assert any('"ankerctl"' in s for s in sock.sent)
    assert light_calls == [True]


@contextmanager
def _borrow_debug(value):
    yield value


# ---------------------------------------------------------------------------
# _pppp_probe_interval tests
# ---------------------------------------------------------------------------

from web import _pppp_probe_interval


def test_pppp_probe_interval_fast_retries():
    assert _pppp_probe_interval(0) == 15.0
    assert _pppp_probe_interval(1) == 15.0
    assert _pppp_probe_interval(2) == 15.0


def test_pppp_probe_interval_steady_state():
    assert _pppp_probe_interval(3) == 60.0
    assert _pppp_probe_interval(10) == 60.0


def test_pppp_probe_interval_exponential_backoff():
    assert _pppp_probe_interval(13) == 120.0   # 1 doubling
    assert _pppp_probe_interval(23) == 240.0   # 2 doublings
    assert _pppp_probe_interval(33) == 300.0   # capped
    assert _pppp_probe_interval(9999) == 300.0
