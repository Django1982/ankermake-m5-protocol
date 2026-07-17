import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cli.model import Account, Config, Printer
from web import (
    _read_bed_leveling_grid,
    _read_printer_report,
    _read_printer_settings_summary,
    app,
    register_services,
    webserver,
)
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
    def __init__(self, cfg, config_root=None):
        self.cfg = cfg
        self.config_root = Path(config_root or ".")

    @contextmanager
    def open(self):
        yield self.cfg

    @contextmanager
    def modify(self):
        yield self.cfg


class FakeBorrowServices:
    def __init__(self, mqtt):
        self._mqtt = mqtt

    @contextmanager
    def borrow(self, name):
        assert name == "mqttqueue"
        yield self._mqtt


class FakeVideoServices:
    def __init__(self, videoqueue, payloads):
        self.svcs = {"videoqueue": videoqueue} if videoqueue else {}
        self.refs = {"videoqueue": 0}
        self._payloads = payloads

    def stream(self, name, **kwargs):
        assert name == "videoqueue"
        for payload in self._payloads:
            yield SimpleNamespace(data=payload)


class FakeManagedService:
    def __init__(self, label):
        self.label = label
        self.state = RunState.Stopped
        self.wanted = False
        self.start_calls = 0
        self.stop_calls = 0
        self.await_stopped_calls = 0

    def start(self):
        self.start_calls += 1
        self.state = RunState.Running
        self.wanted = True

    def stop(self):
        self.stop_calls += 1
        self.state = RunState.Stopped
        self.wanted = False

    def await_stopped(self):
        self.await_stopped_calls += 1
        self.state = RunState.Stopped
        return True


class FakeServiceManager:
    def __init__(self, svcs=None, refs=None):
        self.svcs = svcs or {}
        self.refs = refs or {name: 0 for name in self.svcs}

    def __contains__(self, name):
        return name in self.svcs

    def register(self, name, svc):
        self.svcs[name] = svc
        self.refs[name] = 0

    def unregister(self, name):
        del self.svcs[name]
        del self.refs[name]


def _base_config(model="V8111", active_index=0):
    return Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer(model=model)],
        active_printer_index=active_index,
    )


def _install_app_state(**values):
    keys = [
        "config",
        "api_key",
        "login",
        "printer_index",
        "insecure",
        "video_supported",
        "unsupported_device",
    ]
    old_values = {key: app.config.get(key) for key in keys}
    old_svc = app.svc
    for key, value in values.items():
        if key == "svc":
            app.svc = value
        else:
            app.config[key] = value
    return old_values, old_svc


def _restore_app_state(old_values, old_svc):
    app.svc = old_svc
    for key, value in old_values.items():
        app.config[key] = value


def test_read_bed_leveling_grid_parses_response_and_persists_snapshot(tmp_path, monkeypatch):
    manager = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state(config=manager, printer_index=0, insecure=False)

    monkeypatch.setattr("web._log_dir", str(tmp_path))
    monkeypatch.setattr("cli.mqtt.mqtt_open", lambda config, printer_index, insecure: object())
    monkeypatch.setattr(
        "cli.mqtt.mqtt_gcode_dump",
        lambda client, gcode, collect_window=4.0: [
            {"resData": "BL-Grid-0 -0.767 -0.642\nnoise\n"},
            {"resData": "BL-Grid-1 -0.500 -0.250\n"},
        ],
    )

    try:
        data, err = _read_bed_leveling_grid()
    finally:
        _restore_app_state(old_values, old_svc)

    assert err is None
    assert data["rows"] == 2
    assert data["cols"] == 2
    assert data["min"] == -0.767
    assert data["max"] == -0.25
    saved = list((tmp_path / "bed_leveling").glob("*.bed"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["grid"][0] == [-0.767, -0.642]


def test_read_bed_leveling_grid_handles_connect_failure(tmp_path, monkeypatch):
    manager = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state(config=manager, printer_index=0, insecure=False)

    monkeypatch.setattr("cli.mqtt.mqtt_open", lambda config, printer_index, insecure: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        data, err = _read_bed_leveling_grid()
    finally:
        _restore_app_state(old_values, old_svc)

    assert data is None
    assert err[1] == 503
    assert "MQTT connection failed" in err[0]["error"]


def test_read_printer_report_disconnects_client_and_summary_builds_groups(monkeypatch):
    manager = FakeConfigManager(_base_config())
    old_values, old_svc = _install_app_state(config=manager, printer_index=0, insecure=False)
    disconnects = []
    opens = []

    client = SimpleNamespace(_mqtt=SimpleNamespace(disconnect=lambda: disconnects.append(True)))
    monkeypatch.setattr("cli.mqtt.mqtt_open", lambda config, printer_index, insecure: opens.append(True) or client)
    monkeypatch.setattr(
        "web._collect_printer_gcode_output",
        lambda client, gcode, window, drain: {
            "raw_output": "raw",
            "cleaned_output": "M851 X0 Y0 Z-0.12\nM301 P22.2 I1.08 D114.0",
            "chunks": ["raw"],
            "chunk_count": 1,
        },
    )

    try:
        report = _read_printer_report("probe_offset")
    finally:
        _restore_app_state(old_values, old_svc)

    assert report["name"] == "probe_offset"
    assert report["gcode"] == "M851"
    assert disconnects == [True]
    assert opens == [True]

    mqtt = SimpleNamespace(
        get_z_offset_state=lambda: {"available": True, "steps": 25, "mm": 0.25, "source": "cached"},
        refresh_z_offset=lambda timeout=None: {"available": True, "steps": 25, "mm": 0.25, "source": "live"},
    )
    old_values, old_svc = _install_app_state(svc=FakeBorrowServices(mqtt), config=manager)
    monkeypatch.setattr(
        "web._read_printer_report",
        lambda name, printer_index=None, client=None: {
            "name": name,
            "label": name,
            "gcode": {"settings": "M503", "probe_offset": "M851", "babystep": "M290 R"}[name],
            "cleaned_output": {
                "settings": "M420 S1 Z10\nM301 P22.2 I1.08 D114.0",
                "probe_offset": "M851 X0.00 Y0.00 Z-0.12",
                "babystep": "M290 Z0.05",
            }[name],
        },
    )

    try:
        summary = _read_printer_settings_summary()
    finally:
        _restore_app_state(old_values, old_svc)

    assert summary["status"] == "ok"
    assert summary["live_z_offset"]["display"] == "0.25 mm"
    assert any(item["command"] == "M851" for item in summary["highlights"])
    assert any(item["command"] == "M290" for item in summary["groups"]["leveling"])
    # Summary must open exactly one shared connection for all three reports.
    assert opens == [True, True]
    assert disconnects == [True, True]


def test_bed_leveling_last_route_reads_latest_saved_grid(tmp_path, monkeypatch):
    client = app.test_client()
    bed_dir = tmp_path / "bed_leveling"
    bed_dir.mkdir()
    (bed_dir / "20260101_100000.bed").write_text(json.dumps({"grid": [[0.1]], "rows": 1, "cols": 1}))
    (bed_dir / "20260101_110000.bed").write_text(json.dumps({"grid": [[0.2]], "rows": 1, "cols": 1}))

    monkeypatch.setattr("web._log_dir", str(tmp_path))
    old_values, old_svc = _install_app_state(login=True, api_key=None)

    try:
        response = client.get("/api/printer/bed-leveling/last")
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.get_json()["grid"] == [[0.2]]
    assert response.get_json()["saved_at"] == "20260101_110000"


def test_bed_leveling_last_route_scopes_by_printer_index(tmp_path, monkeypatch):
    client = app.test_client()
    bed_dir = tmp_path / "bed_leveling"
    bed_dir.mkdir()
    (bed_dir / "20260101_100000_p0.bed").write_text(json.dumps({"grid": [[0.1]], "printer_index": 0}))
    (bed_dir / "20260101_110000_p1.bed").write_text(json.dumps({"grid": [[0.2]], "printer_index": 1}))

    monkeypatch.setattr("web._log_dir", str(tmp_path))
    manager = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state(login=True, api_key=None, config=manager, printer_index=0)

    try:
        response = client.get("/api/printer/bed-leveling/last?printer_index=0")
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.get_json()["grid"] == [[0.1]]
    assert response.get_json()["saved_at"] == "20260101_100000"


def test_bed_leveling_last_route_falls_back_to_legacy_unsuffixed_files(tmp_path, monkeypatch):
    client = app.test_client()
    bed_dir = tmp_path / "bed_leveling"
    bed_dir.mkdir()
    (bed_dir / "20260101_100000.bed").write_text(json.dumps({"grid": [[0.3]]}))

    monkeypatch.setattr("web._log_dir", str(tmp_path))
    manager = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state(login=True, api_key=None, config=manager, printer_index=0)

    try:
        response = client.get("/api/printer/bed-leveling/last?printer_index=0")
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.get_json()["grid"] == [[0.3]]
    assert response.get_json()["saved_at"] == "20260101_100000"


def test_read_bed_leveling_grid_skips_unparseable_rows(tmp_path, monkeypatch):
    manager = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state(config=manager, printer_index=0, insecure=False)

    monkeypatch.setattr("web._log_dir", str(tmp_path))
    monkeypatch.setattr("cli.mqtt.mqtt_open", lambda config, printer_index, insecure: object())
    monkeypatch.setattr(
        "cli.mqtt.mqtt_gcode_dump",
        lambda client, gcode, collect_window=4.0: [
            {"resData": "BL-Grid-0 -0.767-0.642 -0.500\n"},
            {"resData": "BL-Grid-1 -0.500 -0.250\n"},
        ],
    )

    try:
        data, err = _read_bed_leveling_grid()
    finally:
        _restore_app_state(old_values, old_svc)

    assert err is None
    assert data["grid"] == [[-0.5, -0.25]]
    assert data["rows"] == 1


def test_video_download_requires_auth_and_streams_when_enabled():
    class FakeVideoQueue:
        def __init__(self):
            self.video_enabled = True
            self.state = RunState.Stopped
            self.start_calls = 0
            self.await_ready_calls = 0
            self.viewer_connected_calls = 0
            self.viewer_disconnected_calls = 0

        def start(self):
            self.start_calls += 1
            self.state = RunState.Running

        def await_ready(self):
            self.await_ready_calls += 1

        def viewer_connected(self):
            self.viewer_connected_calls += 1

        def viewer_disconnected(self):
            self.viewer_disconnected_calls += 1

    videoqueue = FakeVideoQueue()
    client = app.test_client()
    old_values, old_svc = _install_app_state(
        api_key=API_KEY,
        login=True,
        video_supported=True,
        svc=FakeVideoServices(videoqueue, [b"abc", b"def"]),
    )

    try:
        unauthorized = client.get("/video")
        authorized = client.get("/video", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.data == b"abcdef"
    assert videoqueue.start_calls == 1
    assert videoqueue.await_ready_calls == 1
    assert videoqueue.viewer_connected_calls == 1
    assert videoqueue.viewer_disconnected_calls == 1


def test_webserver_bootstraps_secret_key_and_skips_services_for_unsupported_device(tmp_path, monkeypatch):
    run_calls = []
    register_calls = []
    resolve_calls = []

    monkeypatch.setattr("web.register_services", lambda flask_app: register_calls.append(flask_app))
    monkeypatch.setattr("web.cli.config.resolve_api_key", lambda config: resolve_calls.append(config) or API_KEY)
    monkeypatch.setattr("web.app.run", lambda host, port: run_calls.append((host, port)))
    monkeypatch.setattr("web.app.context_processor", lambda func: func)
    monkeypatch.setattr("web.os.getenv", lambda key, default=None: default if key != "FLASK_SECRET_KEY" else None)
    monkeypatch.setattr("web.token", lambda length: "stable-secret-key")

    config = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state()
    old_filaments = getattr(app, "filaments", None)

    try:
        webserver(config, printer_index=0, host="127.0.0.1", port=4470)
        first_api_key = app.config["api_key"]
    finally:
        _restore_app_state(old_values, old_svc)
        app.filaments = old_filaments

    secret_file = tmp_path / "flask_secret.key"
    assert secret_file.read_text() == "stable-secret-key"
    assert register_calls == [app]
    assert resolve_calls == [config]
    assert run_calls == [("127.0.0.1", 4470)]
    assert first_api_key == API_KEY

    register_calls.clear()
    run_calls.clear()
    unsupported_root = tmp_path / "unsupported"
    unsupported_root.mkdir()
    unsupported_config = FakeConfigManager(_base_config(model="V8260"), config_root=unsupported_root)
    old_values, old_svc = _install_app_state()
    old_filaments = getattr(app, "filaments", None)

    try:
        webserver(unsupported_config, printer_index=0, host="0.0.0.0", port=9000)
        unsupported_flag = app.config["unsupported_device"]
    finally:
        _restore_app_state(old_values, old_svc)
        app.filaments = old_filaments

    assert register_calls == []
    assert run_calls == [("0.0.0.0", 9000)]
    assert unsupported_flag is True


def test_register_services_replaces_legacy_video_and_pppp_entries_for_multi_printer_setup(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer("SN1", "Printer 1"), _printer("SN2", "Printer 2")],
    )
    manager = FakeConfigManager(cfg)
    legacy_video = FakeManagedService("legacy-video")
    legacy_pppp = FakeManagedService("legacy-pppp")
    fake_manager = FakeServiceManager(
        svcs={
            "videoqueue": legacy_video,
            "pppp": legacy_pppp,
        },
        refs={
            "videoqueue": 0,
            "pppp": 0,
        },
    )
    old_values, old_svc = _install_app_state(config=manager, svc=fake_manager)

    monkeypatch.setattr("web.service.filetransfer.FileTransferService", lambda: FakeManagedService("filetransfer"))
    monkeypatch.setattr("web.service.pppp.PPPPService", lambda printer_index=0: FakeManagedService(f"pppp-{printer_index}"))
    monkeypatch.setattr("web.service.video.VideoQueue", lambda printer_index=0: FakeManagedService(f"video-{printer_index}"))
    monkeypatch.setattr("web.service.mqtt.MqttQueue", lambda printer_index=0: FakeManagedService(f"mqtt-{printer_index}"))

    try:
        register_services(app)
    finally:
        _restore_app_state(old_values, old_svc)

    assert "videoqueue" not in fake_manager.svcs
    assert "pppp" not in fake_manager.svcs
    assert fake_manager.svcs["videoqueue:0"].label == "video-0"
    assert fake_manager.svcs["videoqueue:1"].label == "video-1"
    assert fake_manager.svcs["pppp:0"].label == "pppp-0"
    assert fake_manager.svcs["pppp:1"].label == "pppp-1"
    assert fake_manager.svcs["mqttqueue:0"].start_calls == 1
    assert fake_manager.svcs["mqttqueue:1"].start_calls == 1
