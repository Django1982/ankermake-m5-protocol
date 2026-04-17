from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest

from cli.model import Account, Config, Printer
from web import app
from web.camera import CameraCaptureError
from web.service.history import PrintHistory


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


class FakeServices:
    def __init__(self, **services):
        self._services = services
        self.svcs = {name: svc for name, svc in services.items() if svc is not None}

    @contextmanager
    def borrow(self, name):
        yield self._services.get(name)


def _base_config():
    return Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer()],
    )


class FakeHistoryList:
    def __init__(self, label):
        self.label = label

    def get_history(self, limit=50, offset=0):
        return [{
            "id": 1,
            "filename": self.label,
            "thumbnail_available": False,
        }]

    def get_count(self):
        return 1


def _install_app_state(*, mqtt=None, videoqueue=None, filetransfer=None, config=None, login=True, video_supported=True, unsupported=False):
    old_values = {
        "config": app.config.get("config"),
        "api_key": app.config.get("api_key"),
        "login": app.config.get("login"),
        "printer_index": app.config.get("printer_index"),
        "video_supported": app.config.get("video_supported"),
        "unsupported_device": app.config.get("unsupported_device"),
    }
    old_svc = app.svc

    app.config["config"] = FakeConfigManager(config or _base_config())
    app.config["api_key"] = API_KEY
    app.config["login"] = login
    app.config["printer_index"] = 0
    app.config["video_supported"] = video_supported
    app.config["unsupported_device"] = unsupported
    app.svc = FakeServices(mqttqueue=mqtt, videoqueue=videoqueue, filetransfer=filetransfer)

    return old_values, old_svc


def _restore_app_state(old_values, old_svc):
    app.svc = old_svc
    for key, value in old_values.items():
        app.config[key] = value


def test_printer_gcode_route_normalizes_safe_commands_and_blocks_motion_while_printing():
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        normal = client.post(
            "/api/printer/gcode",
            json={"gcode": "G28 ; home\n\nM104 S200\n"},
            headers={"X-Api-Key": API_KEY},
        )
        mqtt.is_printing = True
        blocked = client.post(
            "/api/printer/gcode",
            json={"gcode": "G1 X10 Y10"},
            headers={"X-Api-Key": API_KEY},
        )
        safe = client.post(
            "/api/printer/gcode",
            json={"gcode": "M117 Printing"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert normal.status_code == 200
    assert safe.status_code == 200
    assert blocked.status_code == 409
    assert sent == ["G28\nM104 S200", "M117 Printing"]


def test_printer_control_and_autolevel_routes_validate_and_dispatch():
    control_calls = []
    autolevel_calls = []
    home_calls = []
    mqtt = SimpleNamespace(
        is_printing=False,
        send_print_control=lambda value: control_calls.append(value),
        send_auto_leveling=lambda: autolevel_calls.append(True),
        send_home=lambda axis: home_calls.append(axis),
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        invalid = client.post(
            "/api/printer/control",
            json={"value": "abc"},
            headers={"X-Api-Key": API_KEY},
        )
        control = client.post(
            "/api/printer/control",
            json={"value": 2},
            headers={"X-Api-Key": API_KEY},
        )
        allowed = client.post(
            "/api/printer/autolevel",
            headers={"X-Api-Key": API_KEY},
        )
        home = client.post(
            "/api/printer/home",
            json={"axis": "xy"},
            headers={"X-Api-Key": API_KEY},
        )
        bad_home = client.post(
            "/api/printer/home",
            json={"axis": "bad"},
            headers={"X-Api-Key": API_KEY},
        )
        mqtt.is_printing = True
        blocked = client.post(
            "/api/printer/autolevel",
            headers={"X-Api-Key": API_KEY},
        )
        blocked_home = client.post(
            "/api/printer/home",
            json={"axis": "z"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert invalid.status_code == 400
    assert control.status_code == 200
    assert allowed.status_code == 200
    assert home.status_code == 200
    assert bad_home.status_code == 400
    assert blocked.status_code == 409
    assert blocked_home.status_code == 409
    assert control_calls == [2]
    assert autolevel_calls == [True]
    assert home_calls == ["xy"]


def test_printer_storage_file_list_route_returns_files_and_validates_input(monkeypatch):
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=SimpleNamespace())
    calls = []

    def fake_probe(source="onboard", source_value=None, timeout=5.0, collect_window=1.0, printer_index=None):
        calls.append((source, source_value, timeout, collect_window, printer_index))
        files = [{
            "name": "cube.gcode" if source == "onboard" else "usb-part.gcode",
            "path": "/usr/data/local/model/cube.gcode" if source == "onboard" else "/tmp/udisk/udisk1/usb-part.gcode",
            "timestamp": 123,
            "source": source,
        }]
        return ({
            "source_value": 1 if source == "onboard" else 0,
            "reply_count": 1,
            "files": files,
            "replies": [{"commandType": 1009, "reply": 0}],
        }, None)

    monkeypatch.setattr("web._probe_printer_storage_files", fake_probe)

    try:
        onboard = client.get("/api/files/printer?source=onboard", headers={"X-Api-Key": API_KEY})
        usb = client.get("/api/files/printer?source=usb", headers={"X-Api-Key": API_KEY})
        bad_value = client.get("/api/files/printer?value=abc", headers={"X-Api-Key": API_KEY})
        bad_source = client.get("/api/files/printer?source=cloud", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert onboard.status_code == 200
    assert onboard.get_json()["source"] == "onboard"
    assert onboard.get_json()["files"][0]["path"] == "/usr/data/local/model/cube.gcode"
    assert "/api/files/printer/thumbnail" in onboard.get_json()["files"][0]["thumbnail_url"]
    assert usb.status_code == 200
    assert usb.get_json()["source"] == "usb"
    assert usb.get_json()["files"][0]["path"] == "/tmp/udisk/udisk1/usb-part.gcode"
    assert bad_value.status_code == 400
    assert bad_source.status_code == 400
    assert calls == [
        ("onboard", None, 5.0, 1.0, 0),
        ("usb", None, 5.0, 1.0, 0),
    ]


def test_printer_storage_thumbnail_route_fetches_preview_url(monkeypatch):
    mqtt = SimpleNamespace(
        is_printing=False,
        has_pending_print_start=False,
        is_preparing_print=False,
        get_cached_stored_file_preview_url=lambda path: None,
        get_stored_file_preview_url=lambda path, allow_probe=True: "https://example.test/storage-thumb.png",
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)
    captured = []
    monkeypatch.setattr(
        "web._fetch_remote_image",
        lambda url, timeout=10.0: (captured.append(url) or (b"png-bytes", "image/png")),
    )

    try:
        response = client.get(
            "/api/files/printer/thumbnail?source=usb&path=/tmp/udisk/udisk1/file.gcode",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.data == b"png-bytes"
    assert response.mimetype == "image/png"
    assert captured == ["https://example.test/storage-thumb.png"]


def test_printer_storage_file_list_route_surfaces_probe_errors(monkeypatch):
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=SimpleNamespace())
    monkeypatch.setattr(
        "web._probe_printer_storage_files",
        lambda source="onboard", source_value=None, timeout=5.0, collect_window=1.0, printer_index=None: (
            None,
            ({"error": "No response from printer for storage source 'usb'"}, 504),
        ),
    )

    try:
        response = client.get("/api/files/printer?source=usb", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 504
    assert "No response from printer" in response.get_json()["error"]


def test_printer_storage_print_route_validates_dispatches_and_blocks_when_busy():
    start_calls = []
    mqtt = SimpleNamespace(
        is_printing=False,
        has_pending_print_start=False,
        is_preparing_print=False,
        start_stored_file=lambda path: (start_calls.append(path) or True),
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        usb = client.post(
            "/api/files/printer/print",
            json={"source": "usb", "path": "/tmp/udisk/udisk1/Top parts 5h_PETG_M5 grey.gcode"},
            headers={"X-Api-Key": API_KEY},
        )
        onboard = client.post(
            "/api/files/printer/print",
            json={"source": "onboard", "path": "/usr/data/local/model/AnkerMake Model/Autodesk_Kickstarter_Geometry.gcode"},
            headers={"X-Api-Key": API_KEY},
        )
        bad_source = client.post(
            "/api/files/printer/print",
            json={"source": "cloud", "path": "/tmp/udisk/udisk1/file.gcode"},
            headers={"X-Api-Key": API_KEY},
        )
        mismatched = client.post(
            "/api/files/printer/print",
            json={"source": "usb", "path": "/usr/data/local/model/model.gcode"},
            headers={"X-Api-Key": API_KEY},
        )
        mqtt.is_printing = True
        busy = client.post(
            "/api/files/printer/print",
            json={"source": "usb", "path": "/tmp/udisk/udisk1/another.gcode"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert usb.status_code == 200
    assert usb.get_json()["source"] == "usb"
    assert onboard.status_code == 200
    assert onboard.get_json()["source"] == "onboard"
    assert bad_source.status_code == 400
    assert mismatched.status_code == 400
    assert busy.status_code == 409
    assert start_calls == [
        "/tmp/udisk/udisk1/Top parts 5h_PETG_M5 grey.gcode",
        "/usr/data/local/model/AnkerMake Model/Autodesk_Kickstarter_Geometry.gcode",
    ]


def test_printer_storage_print_route_returns_error_when_printer_never_confirms_start():
    mqtt = SimpleNamespace(
        is_printing=False,
        has_pending_print_start=False,
        is_preparing_print=False,
        start_stored_file=lambda path: False,
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        response = client.post(
            "/api/files/printer/print",
            json={"source": "usb", "path": "/tmp/udisk/udisk1/Top parts 5h_PETG_M5 grey.gcode"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 504
    assert "did not confirm the job start" in response.get_json()["error"]


def test_printer_z_offset_routes_refresh_set_and_nudge():
    send_calls = []
    state = {"available": True, "steps": 12, "mm": 0.12, "source": "cached", "seq": 1}

    def refresh_z_offset(timeout=None):
        return dict(state)

    def wait_for_target(target_steps, after_seq=None, timeout=None):
        state["steps"] = target_steps
        state["mm"] = round(target_steps / 100.0, 2)
        state["seq"] = after_seq + 1
        state["source"] = "confirmed"
        return dict(state)

    mqtt = SimpleNamespace(
        refresh_z_offset=refresh_z_offset,
        wait_for_z_offset_target=wait_for_target,
        send_gcode=lambda gcode: send_calls.append(gcode),
        get_z_offset_state=lambda: {"available": False, "steps": None, "mm": None, "source": "cached"},
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        get_resp = client.get("/api/printer/z-offset", headers={"X-Api-Key": API_KEY})
        refresh_resp = client.post(
            "/api/printer/z-offset/refresh",
            headers={"X-Api-Key": API_KEY},
        )
        set_resp = client.post(
            "/api/printer/z-offset",
            json={"target_mm": 0.15},
            headers={"X-Api-Key": API_KEY},
        )
        nudge_resp = client.post(
            "/api/printer/z-offset/nudge",
            json={"delta_mm": -0.02},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert get_resp.status_code == 200
    assert get_resp.get_json()["z_offset"]["display"] == "0.12 mm"
    assert refresh_resp.status_code == 200
    assert set_resp.status_code == 200
    assert set_resp.get_json()["delta"]["display"] == "+0.03 mm"
    assert nudge_resp.status_code == 200
    assert nudge_resp.get_json()["nudge"]["display"] == "-0.02 mm"
    assert send_calls == ["M290 Z+0.03", "M500", "M290 Z-0.02", "M500"]


def test_history_routes_require_auth_and_clear_entries():
    calls = []
    history = SimpleNamespace(
        get_history=lambda limit, offset: calls.append(("get", limit, offset)) or [{"filename": "cube.gcode"}],
        get_count=lambda: 3,
        clear=lambda: calls.append(("clear",)),
    )
    mqtt = SimpleNamespace(history=history)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        unauthorized = client.get("/api/history")
        authorized = client.get("/api/history?limit=999&offset=-5", headers={"X-Api-Key": API_KEY})
        cleared = client.delete("/api/history", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.get_json()["total"] == 3
    assert authorized.get_json()["entries"][0]["thumbnail_url"] is None
    assert cleared.status_code == 200
    assert calls == [("get", 500, 0), ("clear",)]


def test_history_route_uses_requested_or_active_indexed_mqtt_service():
    cfg = _base_config()
    cfg.printers = [
        _printer(sn="SN0", name="Thing 1"),
        _printer(sn="SN1", name="Thing 2"),
    ]
    legacy = SimpleNamespace(history=FakeHistoryList("legacy.gcode"))
    printer0 = SimpleNamespace(history=FakeHistoryList("thing1.gcode"))
    printer1 = SimpleNamespace(history=FakeHistoryList("thing2.gcode"))
    services = FakeServices()
    services._services = {
        "mqttqueue": legacy,
        "mqttqueue:0": printer0,
        "mqttqueue:1": printer1,
    }
    services.svcs = dict(services._services)

    client = app.test_client()
    old_values, old_svc = _install_app_state(config=cfg)
    app.config["printer_index"] = 1
    app.svc = services

    try:
        active = client.get("/api/history", headers={"X-Api-Key": API_KEY})
        requested = client.get("/api/history?printer_index=0", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert active.status_code == 200
    assert active.get_json()["entries"][0]["filename"] == "thing2.gcode"
    assert requested.status_code == 200
    assert requested.get_json()["entries"][0]["filename"] == "thing1.gcode"


def test_history_route_supports_all_printers_admin_view(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    history0.record_start("thing1.gcode", task_id="thing1-task")
    history0.record_finish(task_id="thing1-task", progress=100)
    history1.record_start("thing2.gcode", task_id="thing2-task")
    history1.record_finish(task_id="thing2-task", progress=100)

    cfg = _base_config()
    cfg.printers = [
        _printer(sn="SN0", name="Thing 1"),
        _printer(sn="SN1", name="Thing 2"),
    ]
    services = FakeServices()
    services._services = {
        "mqttqueue": SimpleNamespace(history=history0),
        "mqttqueue:0": SimpleNamespace(history=history0),
        "mqttqueue:1": SimpleNamespace(history=history1),
    }
    services.svcs = dict(services._services)

    client = app.test_client()
    old_values, old_svc = _install_app_state(config=cfg)
    app.config["config"].config_root = tmp_path
    app.config["printer_index"] = 0
    app.svc = services

    try:
        response = client.get("/api/history?all_printers=1", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 2
    by_filename = {entry["filename"]: entry for entry in payload["entries"]}
    assert by_filename["thing1.gcode"]["printer_name"] == "Thing 1"
    assert by_filename["thing2.gcode"]["printer_name"] == "Thing 2"


def test_history_delete_selected_route_supports_all_printers_admin_view(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    row0_id = history0.record_start("thing1.gcode", task_id="thing1-task")
    history0.record_finish(task_id="thing1-task", progress=100)
    row1_id = history1.record_start("thing2.gcode", task_id="thing2-task")
    history1.record_finish(task_id="thing2-task", progress=100)

    cfg = _base_config()
    cfg.printers = [
        _printer(sn="SN0", name="Thing 1"),
        _printer(sn="SN1", name="Thing 2"),
    ]
    services = FakeServices()
    services._services = {
        "mqttqueue": SimpleNamespace(history=history0),
        "mqttqueue:0": SimpleNamespace(history=history0),
        "mqttqueue:1": SimpleNamespace(history=history1),
    }
    services.svcs = dict(services._services)

    client = app.test_client()
    old_values, old_svc = _install_app_state(config=cfg)
    app.config["config"].config_root = tmp_path
    app.config["printer_index"] = 0
    app.svc = services

    try:
        response = client.post(
            "/api/history/delete?all_printers=1",
            json={"ids": [row1_id]},
            headers={"X-Api-Key": API_KEY},
        )
        listed = client.get("/api/history?all_printers=1", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.get_json()["deleted"] == 1
    assert history0.get_entry(row0_id) is not None
    assert history1.get_entry(row1_id) is None
    assert [entry["filename"] for entry in listed.get_json()["entries"]] == ["thing1.gcode"]


def test_history_reprint_route_uses_entry_printer_in_all_printers_mode(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    archive = history1.archive_upload("thing2.gcode", b"G28\nM104 S210\n")
    row1_id = history1.record_start(
        "thing2.gcode",
        task_id="thing2-task",
        archive_relpath=archive["archive_relpath"],
        archive_size=archive["archive_size"],
    )
    history1.record_finish(task_id="thing2-task", progress=100)

    cfg = _base_config()
    cfg.printers = [
        _printer(sn="SN0", name="Thing 1"),
        _printer(sn="SN1", name="Thing 2"),
    ]
    sent = []
    filetransfer = SimpleNamespace(
        send_bytes=lambda archive_bytes, filename, user_name, rate_limit_mbps=None, start_print=True, printer_index=None, archive_info=None: sent.append({
            "filename": filename,
            "printer_index": printer_index,
            "archive_info": archive_info,
            "size": len(archive_bytes),
        })
    )
    services = FakeServices()
    services._services = {
        "mqttqueue": SimpleNamespace(history=history0, is_printing=False, has_pending_print_start=False, is_preparing_print=False),
        "mqttqueue:0": SimpleNamespace(history=history0, is_printing=False, has_pending_print_start=False, is_preparing_print=False),
        "mqttqueue:1": SimpleNamespace(history=history1, is_printing=False, has_pending_print_start=False, is_preparing_print=False),
        "filetransfer": filetransfer,
    }
    services.svcs = dict(services._services)

    client = app.test_client()
    old_values, old_svc = _install_app_state(config=cfg)
    app.config["config"].config_root = tmp_path
    app.config["printer_index"] = 0
    app.svc = services

    try:
        response = client.post(
            f"/api/history/{row1_id}/reprint?all_printers=1",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert sent == [{
        "filename": "thing2.gcode",
        "printer_index": 1,
        "archive_info": {
            "archive_relpath": archive["archive_relpath"],
            "archive_size": archive["archive_size"],
        },
        "size": archive["archive_size"],
    }]


def test_history_delete_selected_route_deletes_finished_entries(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")
    first_id = history.record_start("one.gcode")
    second_id = history.record_start("two.gcode")
    history.record_finish(filename="two.gcode")
    history.record_finish(filename="one.gcode")
    mqtt = SimpleNamespace(history=history)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        response = client.post(
            "/api/history/delete",
            json={"ids": [first_id]},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.get_json()["deleted"] == 1
    assert history.get_entry(first_id) is None
    assert history.get_entry(second_id) is not None


def test_history_delete_selected_route_rejects_active_entries(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")
    entry_id = history.record_start("active.gcode")
    mqtt = SimpleNamespace(history=history)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        response = client.post(
            "/api/history/delete",
            json={"ids": [entry_id]},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 409
    assert "in-progress" in response.get_json()["error"]


def test_history_delete_selected_route_respects_requested_printer_index(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    row0_id = history0.record_start("thing1.gcode", task_id="thing1-task")
    history0.record_finish(task_id="thing1-task", progress=100)
    row1_id = history1.record_start("thing2.gcode", task_id="thing2-task")
    history1.record_finish(task_id="thing2-task", progress=100)

    cfg = _base_config()
    cfg.printers = [
        _printer(sn="SN0", name="Thing 1"),
        _printer(sn="SN1", name="Thing 2"),
    ]
    services = FakeServices()
    services._services = {
        "mqttqueue": SimpleNamespace(history=history0),
        "mqttqueue:0": SimpleNamespace(history=history0),
        "mqttqueue:1": SimpleNamespace(history=history1),
    }
    services.svcs = dict(services._services)

    client = app.test_client()
    old_values, old_svc = _install_app_state(config=cfg)
    app.config["printer_index"] = 0
    app.svc = services

    try:
        response = client.post(
            "/api/history/delete?printer_index=0",
            json={"ids": [row1_id]},
            headers={"X-Api-Key": API_KEY},
        )
        listed0 = client.get("/api/history?printer_index=0", headers={"X-Api-Key": API_KEY})
        listed1 = client.get("/api/history?printer_index=1", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.get_json()["deleted"] == 0
    assert history0.get_entry(row0_id) is not None
    assert history1.get_entry(row1_id) is not None
    assert listed0.get_json()["entries"][0]["filename"] == "thing1.gcode"
    assert listed1.get_json()["entries"][0]["filename"] == "thing2.gcode"


def test_history_thumbnail_route_serves_local_archive_thumbnail(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")
    archive_info = history.archive_upload(
        "cube.gcode",
        (
            b"; thumbnail begin 32x32 10\n"
            b"; iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5ZQAAAAASUVORK5CYII=\n"
            b"; thumbnail end\n"
            b"G28\n"
        ),
    )
    entry_id = history.record_start(
        "cube.gcode",
        archive_relpath=archive_info["archive_relpath"],
        archive_size=archive_info["archive_size"],
    )
    history.record_finish(filename="cube.gcode")
    mqtt = SimpleNamespace(history=history)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        history_resp = client.get("/api/history", headers={"X-Api-Key": API_KEY})
        thumb_resp = client.get(f"/api/history/{entry_id}/thumbnail", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert history_resp.status_code == 200
    thumbnail_url = history_resp.get_json()["entries"][0]["thumbnail_url"]
    assert thumbnail_url.startswith(f"/api/history/{entry_id}/thumbnail")
    assert "printer_index=0" in thumbnail_url
    assert thumb_resp.status_code == 200
    assert thumb_resp.mimetype == "image/png"
    assert thumb_resp.data.startswith(b"\x89PNG")


def test_history_thumbnail_route_proxies_preview_url_when_no_local_thumbnail(monkeypatch, tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")
    entry_id = history.record_start(
        "usb-file.gcode",
        preview_url="https://example.test/history-thumb.png",
    )
    history.record_finish(filename="usb-file.gcode")
    mqtt = SimpleNamespace(history=history)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)
    captured = []
    monkeypatch.setattr(
        "web._fetch_remote_image",
        lambda url, timeout=10.0: (captured.append(url) or (b"history-thumb", "image/jpeg")),
    )

    try:
        response = client.get(f"/api/history/{entry_id}/thumbnail", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.data == b"history-thumb"
    assert response.mimetype == "image/jpeg"
    assert captured == ["https://example.test/history-thumb.png"]


def test_history_reprint_route_dispatches_archived_upload_and_validates_busy(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")
    archive_info = history.archive_upload("cube.gcode", b"G28\nM104 S200\n")
    entry_id = history.record_start(
        "cube.gcode",
        archive_relpath=archive_info["archive_relpath"],
        archive_size=archive_info["archive_size"],
    )
    history.record_finish(filename="cube.gcode")

    upload_calls = []
    mqtt = SimpleNamespace(
        is_printing=False,
        has_pending_print_start=False,
        is_preparing_print=False,
        history=history,
    )
    filetransfer = SimpleNamespace(
        send_bytes=lambda *args, **kwargs: upload_calls.append((args, kwargs)),
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt, filetransfer=filetransfer)

    try:
        ok = client.post(f"/api/history/{entry_id}/reprint", headers={"X-Api-Key": API_KEY})
        missing = client.post("/api/history/99999/reprint", headers={"X-Api-Key": API_KEY})
        mqtt.is_printing = True
        busy = client.post(f"/api/history/{entry_id}/reprint", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert ok.status_code == 200
    assert ok.get_json()["name"] == "cube.gcode"
    assert missing.status_code == 404
    assert busy.status_code == 409
    assert len(upload_calls) == 1
    args, kwargs = upload_calls[0]
    assert args[1] == "cube.gcode"
    assert kwargs["start_print"] is True
    assert kwargs["archive_info"]["archive_relpath"] == archive_info["archive_relpath"]


def test_history_reprint_route_requires_archived_file(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")
    entry_id = history.record_start("usb-file.gcode")
    history.record_finish(filename="usb-file.gcode")
    mqtt = SimpleNamespace(
        is_printing=False,
        has_pending_print_start=False,
        is_preparing_print=False,
        history=history,
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt, filetransfer=SimpleNamespace(send_bytes=lambda *args, **kwargs: None))

    try:
        response = client.post(f"/api/history/{entry_id}/reprint", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 404
    assert "No archived GCode" in response.get_json()["error"]


def test_fetch_remote_image_rejects_untrusted_preview_host(monkeypatch):
    import web

    cfg = _base_config()
    cfg.printers[0].api_hosts = ["make-app.ankermake.com"]
    old_values, old_svc = _install_app_state(config=cfg)
    opener_calls = []
    monkeypatch.setattr("urllib.request.build_opener", lambda *args, **kwargs: opener_calls.append((args, kwargs)))

    try:
        with pytest.raises(ValueError, match="host is not allowed"):
            web._fetch_remote_image("https://evil.example/preview.png")
    finally:
        _restore_app_state(old_values, old_svc)

    assert opener_calls == []


def test_fetch_remote_image_rejects_oversized_remote_payload(monkeypatch):
    from email.message import Message
    import web

    cfg = _base_config()
    cfg.printers[0].api_hosts = ["example.test"]
    old_values, old_svc = _install_app_state(config=cfg)

    class FakeRemote:
        def __init__(self):
            self.headers = Message()
            self.headers["Content-Type"] = "image/png"
            self.headers["Content-Length"] = "6"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size=-1):
            return b""

        def geturl(self):
            return "https://example.test/preview.png"

    class FakeOpener:
        def open(self, request_obj, timeout=10.0):
            return FakeRemote()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args, **kwargs: FakeOpener())
    monkeypatch.setattr("web._PREVIEW_IMAGE_MAX_BYTES", 5)

    try:
        with pytest.raises(RuntimeError, match="exceeded"):
            web._fetch_remote_image("https://example.test/preview.png")
    finally:
        _restore_app_state(old_values, old_svc)


def test_timelapse_routes_list_download_delete_and_reject_traversal(tmp_path):
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    video_path = capture_dir / "cube.mp4"
    video_path.write_bytes(b"fake-mp4")
    snapshot_dir = capture_dir / "snapshots" / "cube_capture"
    snapshot_dir.mkdir(parents=True)
    snapshot_path = snapshot_dir / "frame_00000.jpg"
    snapshot_path.write_bytes(b"fake-jpg")

    timelapse = SimpleNamespace(
        enabled=True,
        _captures_dir=str(capture_dir),
        list_videos=lambda: [{"filename": "cube.mp4", "size": 8}],
        list_snapshots=lambda: [{
            "id": "cube_capture",
            "label": "cube.gcode",
            "frame_count": 1,
            "allow_delete": True,
            "frames": [{"filename": "frame_00000.jpg", "size_bytes": 8}],
        }],
        get_video_path=lambda filename: str(video_path) if filename == "cube.mp4" else None,
        delete_video=lambda filename: filename == "cube.mp4",
        get_snapshot_path=lambda collection_id, filename: (
            str(snapshot_path)
            if collection_id == "cube_capture" and filename == "frame_00000.jpg"
            else None
        ),
        delete_snapshot=lambda collection_id, filename: (
            collection_id == "cube_capture" and filename == "frame_00000.jpg"
        ),
        delete_snapshot_collection=lambda collection_id: collection_id == "cube_capture",
    )
    mqtt = SimpleNamespace(timelapse=timelapse)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        listed = client.get("/api/timelapses", headers={"X-Api-Key": API_KEY})
        listed_snapshots = client.get("/api/timelapse-snapshots", headers={"X-Api-Key": API_KEY})
        invalid = client.get("/api/timelapse/..\\\\passwd.mp4", headers={"X-Api-Key": API_KEY})
        downloaded = client.get("/api/timelapse/cube.mp4", headers={"X-Api-Key": API_KEY})
        deleted = client.delete("/api/timelapse/cube.mp4", headers={"X-Api-Key": API_KEY})
        invalid_snapshot = client.get("/api/timelapse-snapshot/..\\\\bad/frame_00000.jpg", headers={"X-Api-Key": API_KEY})
        snapshot_downloaded = client.get("/api/timelapse-snapshot/cube_capture/frame_00000.jpg", headers={"X-Api-Key": API_KEY})
        snapshot_deleted = client.delete("/api/timelapse-snapshot/cube_capture/frame_00000.jpg", headers={"X-Api-Key": API_KEY})
        collection_deleted = client.delete("/api/timelapse-snapshot/cube_capture", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert listed.status_code == 200
    assert listed.get_json()["enabled"] is True
    assert listed_snapshots.status_code == 200
    assert listed_snapshots.get_json()["collections"][0]["id"] == "cube_capture"
    assert invalid.status_code == 400
    assert downloaded.status_code == 200
    assert downloaded.data == b"fake-mp4"
    assert deleted.status_code == 200
    assert invalid_snapshot.status_code == 400
    assert snapshot_downloaded.status_code == 200
    assert snapshot_downloaded.data == b"fake-jpg"
    assert snapshot_deleted.status_code == 200
    assert collection_deleted.status_code == 200


def test_timelapse_media_routes_honor_requested_printer_index(tmp_path):
    cfg = _base_config()
    cfg.printers.append(_printer("SN2", "Printer 2"))

    zero_dir = tmp_path / "captures-zero"
    zero_dir.mkdir()
    zero_video = zero_dir / "zero.mp4"
    zero_video.write_bytes(b"zero-mp4")
    zero_snapshot_dir = zero_dir / "snapshots" / "zero_capture"
    zero_snapshot_dir.mkdir(parents=True)
    zero_snapshot = zero_snapshot_dir / "frame_00000.jpg"
    zero_snapshot.write_bytes(b"zero-jpg")

    one_dir = tmp_path / "captures-one"
    one_dir.mkdir()
    one_video = one_dir / "one.mp4"
    one_video.write_bytes(b"one-mp4")
    one_snapshot_dir = one_dir / "snapshots" / "one_capture"
    one_snapshot_dir.mkdir(parents=True)
    one_snapshot = one_snapshot_dir / "frame_00000.jpg"
    one_snapshot.write_bytes(b"one-jpg")

    timelapse_zero = SimpleNamespace(
        enabled=False,
        _captures_dir=str(zero_dir),
        list_videos=lambda: [{"filename": "zero.mp4", "size_bytes": 8}],
        list_snapshots=lambda: [{
            "id": "zero_capture",
            "label": "zero.gcode",
            "frame_count": 1,
            "allow_delete": True,
            "frames": [{"filename": "frame_00000.jpg", "size_bytes": 8}],
        }],
        get_video_path=lambda filename: str(zero_video) if filename == "zero.mp4" else None,
        delete_video=lambda filename: filename == "zero.mp4",
        get_snapshot_path=lambda collection_id, filename: (
            str(zero_snapshot)
            if collection_id == "zero_capture" and filename == "frame_00000.jpg"
            else None
        ),
        delete_snapshot=lambda collection_id, filename: (
            collection_id == "zero_capture" and filename == "frame_00000.jpg"
        ),
    )
    timelapse_one = SimpleNamespace(
        enabled=True,
        _captures_dir=str(one_dir),
        list_videos=lambda: [{"filename": "one.mp4", "size_bytes": 7}],
        list_snapshots=lambda: [{
            "id": "one_capture",
            "label": "one.gcode",
            "frame_count": 1,
            "allow_delete": True,
            "frames": [{"filename": "frame_00000.jpg", "size_bytes": 7}],
        }],
        get_video_path=lambda filename: str(one_video) if filename == "one.mp4" else None,
        delete_video=lambda filename: filename == "one.mp4",
        get_snapshot_path=lambda collection_id, filename: (
            str(one_snapshot)
            if collection_id == "one_capture" and filename == "frame_00000.jpg"
            else None
        ),
        delete_snapshot=lambda collection_id, filename: (
            collection_id == "one_capture" and filename == "frame_00000.jpg"
        ),
    )

    mqtt_zero = SimpleNamespace(
        timelapse=timelapse_zero,
        get_state=lambda: {"printer_marker": "zero"},
    )
    mqtt_one = SimpleNamespace(
        timelapse=timelapse_one,
        get_state=lambda: {"printer_marker": "one"},
    )

    client = app.test_client()
    old_values, old_svc = _install_app_state(config=cfg)
    app.svc = FakeServices(**{"mqttqueue:0": mqtt_zero, "mqttqueue:1": mqtt_one})

    try:
        listed = client.get("/api/timelapses?printer_index=1", headers={"X-Api-Key": API_KEY})
        listed_snapshots = client.get("/api/timelapse-snapshots?printer_index=1", headers={"X-Api-Key": API_KEY})
        runtime = client.get("/api/printer/runtime-state?printer_index=1", headers={"X-Api-Key": API_KEY})
        downloaded = client.get("/api/timelapse/one.mp4?printer_index=1", headers={"X-Api-Key": API_KEY})
        deleted = client.delete("/api/timelapse/one.mp4?printer_index=1", headers={"X-Api-Key": API_KEY})
        snapshot_downloaded = client.get(
            "/api/timelapse-snapshot/one_capture/frame_00000.jpg?printer_index=1",
            headers={"X-Api-Key": API_KEY},
        )
        snapshot_deleted = client.delete(
            "/api/timelapse-snapshot/one_capture/frame_00000.jpg?printer_index=1",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert listed.status_code == 200
    assert listed.get_json()["enabled"] is True
    assert listed.get_json()["videos"] == [{"filename": "one.mp4", "size_bytes": 7}]
    assert listed_snapshots.status_code == 200
    assert listed_snapshots.get_json()["collections"][0]["id"] == "one_capture"
    assert runtime.status_code == 200
    assert runtime.get_json()["printer_marker"] == "one"
    assert downloaded.status_code == 200
    assert downloaded.data == b"one-mp4"
    assert deleted.status_code == 200
    assert snapshot_downloaded.status_code == 200
    assert snapshot_downloaded.data == b"one-jpg"
    assert snapshot_deleted.status_code == 200


def test_timelapse_current_routes_honor_requested_printer_index():
    cfg = _base_config()
    cfg.printers.append(_printer("SN2", "Printer 2"))

    zero_calls = []
    one_calls = []

    mqtt_zero = SimpleNamespace(
        start_timelapse_for_current_print=lambda: zero_calls.append("start") or "zero.gcode",
        pause_timelapse_for_current_print=lambda: zero_calls.append("pause") or "zero.gcode",
        resume_timelapse_for_current_print=lambda: zero_calls.append("resume") or "zero.gcode",
        stop_timelapse_for_current_print=lambda: zero_calls.append("stop") or "zero.gcode",
        dismiss_timelapse_start_offer=lambda: zero_calls.append("dismiss"),
        get_state=lambda: {"printer_marker": "zero"},
    )
    mqtt_one = SimpleNamespace(
        start_timelapse_for_current_print=lambda: one_calls.append("start") or "one.gcode",
        pause_timelapse_for_current_print=lambda: one_calls.append("pause") or "one.gcode",
        resume_timelapse_for_current_print=lambda: one_calls.append("resume") or "one.gcode",
        stop_timelapse_for_current_print=lambda: one_calls.append("stop") or "one.gcode",
        dismiss_timelapse_start_offer=lambda: one_calls.append("dismiss"),
        get_state=lambda: {"printer_marker": "one"},
    )

    client = app.test_client()
    old_values, old_svc = _install_app_state(config=cfg)
    app.svc = FakeServices(**{"mqttqueue:0": mqtt_zero, "mqttqueue:1": mqtt_one})

    try:
        start_response = client.post("/api/timelapse/current/start?printer_index=1", headers={"X-Api-Key": API_KEY})
        pause_response = client.post("/api/timelapse/current/pause?printer_index=1", headers={"X-Api-Key": API_KEY})
        resume_response = client.post("/api/timelapse/current/resume?printer_index=1", headers={"X-Api-Key": API_KEY})
        stop_response = client.post("/api/timelapse/current/stop?printer_index=1", headers={"X-Api-Key": API_KEY})
        dismiss_response = client.post("/api/timelapse/current/dismiss?printer_index=1", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert start_response.status_code == 200
    assert pause_response.status_code == 200
    assert resume_response.status_code == 200
    assert stop_response.status_code == 200
    assert dismiss_response.status_code == 200
    assert zero_calls == []
    assert one_calls == ["start", "pause", "resume", "stop", "dismiss"]
    assert start_response.get_json()["filename"] == "one.gcode"
    assert start_response.get_json()["printer_marker"] == "one"


def test_snapshot_route_reports_expected_error_paths(monkeypatch):
    client = app.test_client()
    old_values, old_svc = _install_app_state(video_supported=False, videoqueue=None)

    try:
        not_supported = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._ffmpeg_path", lambda: None)
    old_values, old_svc = _install_app_state(video_supported=True, videoqueue=object())
    try:
        no_ffmpeg = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    old_values, old_svc = _install_app_state(video_supported=True, videoqueue=None)
    try:
        no_service = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    old_values, old_svc = _install_app_state(
        video_supported=True,
        videoqueue=SimpleNamespace(video_enabled=False),
    )
    try:
        video_disabled = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._video_has_recent_frame", lambda vq: False)
    old_values, old_svc = _install_app_state(
        video_supported=True,
        videoqueue=SimpleNamespace(video_enabled=True, last_frame_at=None),
    )
    try:
        no_frames = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._video_has_recent_frame", lambda vq: True)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))
        ),
    )
    old_values, old_svc = _install_app_state(
        video_supported=True,
        videoqueue=SimpleNamespace(video_enabled=True, last_frame_at=123.0),
    )
    try:
        timeout = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert not_supported.status_code == 400
    assert no_ffmpeg.status_code == 500
    assert no_service.status_code == 503
    assert video_disabled.status_code == 409
    assert "Enable printer video" in video_disabled.get_json()["error"]
    assert no_frames.status_code == 409
    assert "no live camera frames" in no_frames.get_json()["error"]
    assert timeout.status_code == 504
    assert "timed out" in timeout.get_json()["error"]
    assert "Command" not in timeout.get_json()["error"]


def test_snapshot_and_camera_frame_routes_support_external_camera(monkeypatch):
    cfg = _base_config()
    cfg.camera = {
        "per_printer": {
            "SN1": {
                "source": "external",
                "external": {
                    "name": "Workbench Cam",
                    "snapshot_url": "http://cam.local/snapshot.jpg",
                    "stream_url": "",
                    "refresh_sec": 2,
                },
            }
        }
    }
    client = app.test_client()
    old_values, old_svc = _install_app_state(
        config=cfg,
        video_supported=False,
        videoqueue=None,
    )

    captures = []

    def fake_capture(camera_settings, ffmpeg_path, output_path, **kwargs):
        captures.append({
            "camera_settings": camera_settings,
            "ffmpeg_path": ffmpeg_path,
            **kwargs,
        })
        Path(output_path).write_bytes(b"jpeg")

    monkeypatch.setattr("web._ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("web.camera.capture_camera_snapshot_to_file", fake_capture)

    try:
        snapshot = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
        frame = client.get("/api/camera/frame", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert snapshot.status_code == 200
    assert frame.status_code == 200
    assert snapshot.mimetype == "image/jpeg"
    assert frame.mimetype == "image/jpeg"
    assert len(captures) == 2
    assert all(call["camera_settings"]["effective_source"] == "external" for call in captures)
    assert all(call["ffmpeg_path"] == "/usr/bin/ffmpeg" for call in captures)


def test_external_camera_stream_requires_auth_when_api_key_enabled():
    client = app.test_client()
    old_values, old_svc = _install_app_state(
        video_supported=False,
        videoqueue=None,
    )

    try:
        unauthorized = client.get("/api/camera/stream")
        authorized = client.get("/api/camera/stream", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert unauthorized.status_code == 401
    assert authorized.status_code == 400
    assert authorized.get_json()["error"] == "External camera is not the active camera source."


def test_camera_frame_route_reports_external_capture_errors_as_bad_gateway(monkeypatch):
    cfg = _base_config()
    cfg.camera = {
        "per_printer": {
            "SN1": {
                "source": "external",
                "external": {
                    "name": "Workbench Cam",
                    "snapshot_url": "",
                    "stream_url": "rtsp://cam.local/live",
                    "refresh_sec": 2,
                },
            }
        }
    }
    client = app.test_client()
    old_values, old_svc = _install_app_state(
        config=cfg,
        video_supported=False,
        videoqueue=None,
    )

    monkeypatch.setattr("web._ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "web.camera.capture_camera_snapshot_to_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(CameraCaptureError("RTSP preview failed")),
    )

    try:
        frame = client.get("/api/camera/frame", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert frame.status_code == 502
    assert frame.get_json()["error"] == "RTSP preview failed"


def test_snapshot_route_saves_manual_snapshot_into_timelapse_gallery(monkeypatch):
    cfg = _base_config()
    cfg.camera = {
        "per_printer": {
            "SN1": {
                "source": "external",
                "external": {
                    "name": "Workbench Cam",
                    "snapshot_url": "http://cam.local/snapshot.jpg",
                    "stream_url": "",
                    "refresh_sec": 2,
                },
            }
        }
    }

    saved = []
    timelapse = SimpleNamespace(
        save_manual_snapshot=lambda path, camera_settings=None, taken_at=None: saved.append({
            "path": path,
            "camera_settings": camera_settings,
            "taken_at": taken_at,
        })
    )
    mqtt = SimpleNamespace(timelapse=timelapse)
    client = app.test_client()
    old_values, old_svc = _install_app_state(
        config=cfg,
        mqtt=mqtt,
        video_supported=False,
        videoqueue=None,
    )

    monkeypatch.setattr("web._ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "web.camera.capture_camera_snapshot_to_file",
        lambda camera_settings, ffmpeg_path, output_path, **kwargs: Path(output_path).write_bytes(b"jpeg"),
    )

    try:
        response = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert len(saved) == 1
    assert saved[0]["camera_settings"]["effective_source"] == "external"
    assert saved[0]["camera_settings"]["external"]["name"] == "Workbench Cam"
    assert isinstance(saved[0]["taken_at"], datetime)


def test_unsupported_device_guard_blocks_printer_control_routes():
    mqtt = SimpleNamespace(send_print_control=lambda value: None)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt, unsupported=True)

    try:
        response = client.post(
            "/api/printer/control",
            json={"value": 1},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 503
    assert "not supported" in response.get_json()["error"]
