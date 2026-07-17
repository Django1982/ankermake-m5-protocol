import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.model import Account, Config, Printer
from libflagship import resolve_root_dir
from web import _AccessLogNoiseFilter, _ConsoleLogBuffer, _PrinterAlertBuffer
from web import (
    _build_command_group,
    _build_filament_move_gcode,
    _clean_printer_report_output,
    _configure_request_limits,
    _deep_update,
    _env_int,
    _extract_report_commands,
    _filament_service_length,
    _filament_service_temp,
    _format_signed_mm,
    _hotend_nozzle_max_c,
    _probe_printer_storage_files,
    _parse_z_offset_mm,
    _resolve_apprise,
    _safe_same_site_redirect_target,
    _serialize_z_offset_state,
    _set_printer_z_offset,
    _z_offset_mm_to_steps,
    _z_offset_steps_to_mm,
    app,
)
import web as web_module
from web.config import ConfigImportError
from web.util import flash_redirect


def _printer(sn, name, model="V8111", ip_addr=""):
    return Printer(
        id=sn,
        sn=sn,
        name=name,
        model=model,
        create_time=datetime(2024, 1, 1, 12, 0, 0),
        update_time=datetime(2024, 1, 1, 12, 0, 0),
        wifi_mac="aabbccddeeff",
        ip_addr=ip_addr,
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
    def __init__(self, mqtt=None):
        self._mqtt = mqtt if mqtt is not None else SimpleNamespace(is_printing=False)
        self.restart_calls = 0
        self.svcs = {}

    @contextmanager
    def borrow(self, name):
        assert str(name).startswith("mqttqueue")
        yield self._mqtt

    def restart_all(self, await_ready=False):
        self.restart_calls += 1


def test_deep_update_merges_nested_dicts():
    base = {"a": {"b": 1, "c": 2}, "x": 1}
    updates = {"a": {"c": 3, "d": 4}, "y": 2}

    merged = _deep_update(base, updates)

    assert merged == {"a": {"b": 1, "c": 3, "d": 4}, "x": 1, "y": 2}


def test_filament_and_z_offset_helpers():
    assert _filament_service_temp({"nozzle_temp_other_layer": "220"}) == 220
    assert _filament_service_length({"length_mm": "42.345"}, "length_mm") == 42.34
    assert _build_filament_move_gcode(12.5).splitlines()[1] == "G1 E12.5 F240"
    assert _z_offset_steps_to_mm(37) == 0.37
    assert _z_offset_mm_to_steps(0.37) == 37
    assert _format_signed_mm(-0.12) == "-0.12"
    assert _parse_z_offset_mm({"offset": "0.456"}, "offset") == 0.46
    assert _serialize_z_offset_state({"steps": 25, "source": "mqtt"})["display"] == "0.25 mm"


def test_hotend_nozzle_max_defaults_to_standard():
    cfg = Config(
        account=Account(auth_token="token", region="eu", user_id="user-1", email="user@example.com"),
        printers=[_printer("SN1", "Printer")],
    )

    assert _hotend_nozzle_max_c(cfg, printer_index=0) == 260


def test_hotend_nozzle_max_returns_all_metal_when_enabled():
    cfg = Config(
        account=Account(auth_token="token", region="eu", user_id="user-1", email="user@example.com"),
        printers=[_printer("SN1", "Printer")],
    )
    cfg.filament_service["per_printer"]["SN1"] = {"all_metal_hotend": True}

    assert _hotend_nozzle_max_c(cfg, printer_index=0) == 300
    # Unresolvable printer index falls back to the safer standard ceiling.
    assert _hotend_nozzle_max_c(cfg, printer_index=5) == 260


def test_set_printer_z_offset_rejects_out_of_range_targets():
    # Bound check must fire before any MQTT interaction, so a bare
    # namespace without refresh/send methods is enough here.
    mqtt = SimpleNamespace()
    with pytest.raises(ValueError):
        _set_printer_z_offset(mqtt, 50.0)
    with pytest.raises(ValueError):
        _set_printer_z_offset(mqtt, -50.0)


def test_resolve_apprise_drops_progress_max_value():
    cfg = SimpleNamespace(
        notifications={
            "apprise": {
                "enabled": True,
                "progress": {"max_value": 123, "interval_percent": 10},
            }
        }
    )

    apprise = _resolve_apprise(cfg)

    assert apprise["enabled"] is True
    assert "max_value" not in apprise["progress"]
    assert apprise["progress"]["interval_percent"] == 10


def test_clean_report_output_and_extract_commands():
    raw = "\x1b[31mok\r\nM301 P22.2 I1.08 D114.0\r\n+ringbuf stuff\r\nM851 X0.00 Y0.00 Z-0.12\r\n"

    cleaned = _clean_printer_report_output(raw)
    commands = _extract_report_commands(cleaned, "M420 S1 Z10")
    group = _build_command_group(commands, ["M851", "M420", "M301"])

    assert cleaned == "M301 P22.2 I1.08 D114.0\nM851 X0.00 Y0.00 Z-0.12"
    assert commands["M301"] == "M301 P22.2 I1.08 D114.0"
    assert [item["command"] for item in group] == ["M851", "M420", "M301"]


def test_env_int_rejects_invalid_and_too_small_values(caplog):
    assert _env_int("UPLOAD_MAX_FORM_PARTS", 20, env={}) == 20

    with caplog.at_level("WARNING", logger="web"):
        assert _env_int("UPLOAD_MAX_FORM_PARTS", 20, env={"UPLOAD_MAX_FORM_PARTS": "abc"}) == 20
        assert _env_int("UPLOAD_MAX_FORM_PARTS", 20, env={"UPLOAD_MAX_FORM_PARTS": "0"}) == 20

    assert "Ignoring invalid integer value for UPLOAD_MAX_FORM_PARTS" in caplog.text
    assert "Ignoring UPLOAD_MAX_FORM_PARTS='0' because it is smaller than 1" in caplog.text


def test_configure_request_limits_separates_file_and_form_limits():
    flask_app = SimpleNamespace(config={})

    _configure_request_limits(
        flask_app,
        env={
            "UPLOAD_MAX_MB": "4096",
            "UPLOAD_MAX_FORM_MEMORY_KB": "1024",
            "UPLOAD_MAX_FORM_PARTS": "12",
        },
    )

    assert flask_app.config["MAX_CONTENT_LENGTH"] == 4096 * 1024 * 1024
    assert flask_app.config["MAX_FORM_MEMORY_SIZE"] == 1024 * 1024
    assert flask_app.config["MAX_FORM_PARTS"] == 12


def test_resolve_root_dir_supports_bundled_and_source_layouts(tmp_path):
    assert resolve_root_dir(
        frozen=False,
        file_path="/tmp/ankerctl/libflagship/__init__.py",
    ) == Path("/tmp/ankerctl")

    assert resolve_root_dir(
        frozen=True,
        meipass=str(tmp_path / "bundle"),
    ) == (tmp_path / "bundle").resolve()

    assert resolve_root_dir(
        frozen=True,
        meipass=None,
        executable=str(tmp_path / "dist" / "ankerctl"),
    ) == (tmp_path / "dist").resolve()


def test_flash_redirect_requires_path_and_redirects():
    with app.test_request_context("/"):
        response = flash_redirect("/next", "Saved", "success")
        assert response.location.endswith("/next")

    with app.test_request_context("/"):
        try:
            flash_redirect("")
        except ValueError as exc:
            assert "Redirect path is required" in str(exc)
        else:
            raise AssertionError("flash_redirect should reject empty path")


def test_api_health_and_version_routes():
    client = app.test_client()
    old_login = app.config.get("login")
    old_api_key = app.config.get("api_key")
    app.config["login"] = True
    app.config["api_key"] = None

    try:
        health = client.get("/api/health")
        version = client.get("/api/version")

        assert health.status_code == 200
        assert health.get_json() == {"status": "ok"}
        assert version.status_code == 200
        assert version.get_json()["server"] == "1.9.0"
    finally:
        app.config["login"] = old_login
        app.config["api_key"] = old_api_key


def test_config_import_slicer_route_uses_autodetected_cache(monkeypatch, tmp_path):
    client = app.test_client()
    detected = tmp_path / "000005.ldb"
    detected.write_bytes(b"slicer-cache")
    imported = []
    sentinel_config = FakeConfigManager(
        Config(
            account=Account(
                auth_token="secret",
                region="us",
                user_id="user-1",
                email="user@example.com",
                country="US",
            ),
            printers=[_printer("SN1", "Thing 1"), _printer("SN2", "Thing 2")],
        )
    )

    old_config = app.config.get("config")
    app.config["config"] = sentinel_config

    monkeypatch.setattr("web.platform.autodetect_login_path", lambda platform=None: str(detected))
    monkeypatch.setattr(
        "web.config.config_import",
        lambda file, config: imported.append((file.stream.read(), config)),
    )

    try:
        response = client.post("/api/ankerctl/config/import-slicer")
        with client.session_transaction() as sess:
            authenticated = sess.get("authenticated")
            pending_flash = sess.get("post_reload_flash")
    finally:
        app.config["config"] = old_config

    assert response.status_code == 302
    assert imported == [(b"slicer-cache", sentinel_config)]
    assert authenticated is True
    assert pending_flash == {
        "message": "Configuration imported from open eufyMake Studio for user@example.com with 2 printers.",
        "category": "success",
    }


def test_config_import_slicer_route_handles_missing_cache(monkeypatch):
    client = app.test_client()
    monkeypatch.setattr("web.platform.autodetect_login_path", lambda platform=None: None)

    response = client.post("/api/ankerctl/config/import-slicer")

    assert response.status_code == 302
    assert response.location.endswith("/")


def test_config_login_route_returns_inline_error(monkeypatch):
    client = app.test_client()
    old_config = app.config.get("config")
    app.config["config"] = object()

    monkeypatch.setattr(
        "web.config.config_login",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConfigImportError("invalid email or password")),
    )

    try:
        response = client.post(
            "/api/ankerctl/config/login",
            data={
                "login_email": " user@example.com ",
                "login_password": "pw",
                "login_country": " us ",
            },
        )
    finally:
        app.config["config"] = old_config

    assert response.status_code == 200
    assert response.get_json() == {"error": "Login failed: invalid email or password"}


def test_server_reload_uses_pending_flash_message():
    client = app.test_client()
    old_config = app.config.get("config")
    old_svc = app.svc
    old_login = app.config.get("login")
    old_printer_index = app.config.get("printer_index")
    old_video_supported = app.config.get("video_supported")
    old_unsupported_device = app.config.get("unsupported_device")

    cfg = Config(
        account=Account(
            auth_token="secret",
            region="us",
            user_id="user-1",
            email="user@example.com",
            country="US",
        ),
        printers=[_printer("SN1", "Thing 1")],
    )
    app.config["config"] = FakeConfigManager(cfg)
    app.config["printer_index"] = 0
    app.svc = FakeServices()
    app.svc.svcs = {"placeholder": object()}

    try:
        with client.session_transaction() as sess:
            sess["post_reload_flash"] = {
                "message": "Configuration imported from open eufyMake Studio for user@example.com with 1 printer.",
                "category": "success",
            }

        response = client.get("/api/ankerctl/server/reload")
        with client.session_transaction() as sess:
            flashes = sess.get("_flashes", [])
    finally:
        app.config["config"] = old_config
        app.svc = old_svc
        app.config["login"] = old_login
        app.config["printer_index"] = old_printer_index
        app.config["video_supported"] = old_video_supported
        app.config["unsupported_device"] = old_unsupported_device

    assert response.status_code == 302
    assert flashes == [
        ("success", "Configuration imported from open eufyMake Studio for user@example.com with 1 printer.")
    ]


def test_console_log_buffer_supports_recent_tail_and_incremental_updates():
    buffer = _ConsoleLogBuffer(max_lines=3)
    for idx in range(1, 6):
        buffer.append(f"line {idx}")

    recent = buffer.snapshot(limit=10)
    incremental = buffer.snapshot(after_id=3, limit=10)
    truncated = buffer.snapshot(after_id=1, limit=10)

    assert [entry["text"] for entry in recent["entries"]] == ["line 3", "line 4", "line 5"]
    assert recent["first_id"] == 3
    assert recent["last_id"] == 5
    assert [entry["text"] for entry in incremental["entries"]] == ["line 4", "line 5"]
    assert incremental["truncated"] is False
    assert truncated["truncated"] is True


def test_printer_alert_buffer_supports_recent_tail_and_incremental_updates():
    buffer = _PrinterAlertBuffer(max_entries=3)
    for idx in range(1, 6):
        buffer.append(
            printer_index=0,
            printer_name="Thing 1",
            alert_type="filament_runout",
            title="Filament runout",
            message=f"alert {idx}",
            cooldown_sec=0,
        )

    recent = buffer.snapshot(limit=10)
    incremental = buffer.snapshot(after_id=3, limit=10)
    truncated = buffer.snapshot(after_id=1, limit=10)

    assert [entry["message"] for entry in recent["entries"]] == ["alert 3", "alert 4", "alert 5"]
    assert recent["first_id"] == 3
    assert recent["last_id"] == 5
    assert [entry["message"] for entry in incremental["entries"]] == ["alert 4", "alert 5"]
    assert incremental["truncated"] is False
    assert truncated["truncated"] is True


def test_access_log_noise_filter_suppresses_console_polling_and_static_assets():
    filt = _AccessLogNoiseFilter()

    console_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [08/Apr/2026 17:57:47] "GET /api/console/logs?limit=200&after=108 HTTP/1.1" 200 -',
        args=(), exc_info=None,
    )
    static_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [08/Apr/2026 17:57:47] "GET /static/ankersrv.js HTTP/1.1" 304 -',
        args=(), exc_info=None,
    )
    runtime_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [09/Apr/2026 10:15:00] "GET /api/printer/runtime-state HTTP/1.1" 200 -',
        args=(), exc_info=None,
    )
    swap_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [13/Apr/2026 20:50:30] "GET /api/filaments/service/swap?printer_index=0 HTTP/1.1" 200 -',
        args=(), exc_info=None,
    )
    api_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [08/Apr/2026 17:57:47] "GET /api/health HTTP/1.1" 200 -',
        args=(), exc_info=None,
    )

    assert filt.filter(console_record) is False
    assert filt.filter(static_record) is False
    assert filt.filter(runtime_record) is False
    assert filt.filter(swap_record) is False
    assert filt.filter(api_record) is True


def test_api_console_logs_returns_recent_entries(monkeypatch):
    calls = []

    class FakeConsoleBuffer:
        def snapshot(self, *, limit=200, after_id=None):
            calls.append((limit, after_id))
            return {
                "entries": [{"id": 42, "text": "[*] printer ready"}],
                "first_id": 40,
                "last_id": 42,
                "next_after": 42,
                "truncated": False,
                "max_lines": 2000,
            }

    client = app.test_client()
    old_login = app.config.get("login")
    old_api_key = app.config.get("api_key")
    monkeypatch.setattr(web_module, "_get_console_log_buffer", lambda: FakeConsoleBuffer())
    app.config["login"] = True
    app.config["api_key"] = None

    try:
        response = client.get("/api/console/logs?limit=25&after=10")
    finally:
        app.config["login"] = old_login
        app.config["api_key"] = old_api_key

    assert response.status_code == 200
    assert response.get_json()["entries"] == [{"id": 42, "text": "[*] printer ready"}]
    assert calls == [(25, 10)]


def test_switch_active_printer_locked_by_env_var_returns_403():
    cfg = Config(
        account=Account(auth_token="token", region="eu", user_id="user-1", email="user@example.com"),
        printers=[_printer("SN1", "Printer One"), _printer("SN2", "Printer Two")],
    )
    manager = FakeConfigManager(cfg)
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "login": app.config.get("login"),
        "api_key": app.config.get("api_key"),
    }
    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = True
    app.config["login"] = True
    app.config["api_key"] = "secret-key-123456"

    try:
        resp = client.post(
            "/api/printers/active",
            json={"index": 1},
            headers={"X-Api-Key": "secret-key-123456"},
        )
        assert resp.status_code == 403
        assert app.config["printer_index"] == 0
        assert cfg.active_printer_index != 1
    finally:
        for key, value in old_values.items():
            app.config[key] = value


def test_switch_active_printer_to_unsupported_model_returns_403():
    cfg = Config(
        account=Account(auth_token="token", region="eu", user_id="user-1", email="user@example.com"),
        printers=[_printer("SN1", "Printer One"), _printer("SN2", "UV Printer", model="V8260")],
    )
    manager = FakeConfigManager(cfg)
    services = FakeServices()
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "video_supported": app.config.get("video_supported"),
        "login": app.config.get("login"),
        "unsupported_device": app.config.get("unsupported_device"),
        "api_key": app.config.get("api_key"),
    }
    old_svc = app.svc
    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = False
    app.config["video_supported"] = True
    app.config["login"] = True
    app.config["unsupported_device"] = False
    app.config["api_key"] = "secret-key-123456"
    app.svc = services

    try:
        resp = client.post(
            "/api/printers/active",
            json={"index": 1},
            headers={"X-Api-Key": "secret-key-123456"},
        )
        assert resp.status_code == 403
        assert app.config["printer_index"] == 0
        assert cfg.active_printer_index != 1
    finally:
        app.svc = old_svc
        for key, value in old_values.items():
            app.config[key] = value


def test_unsupported_device_gate_is_scoped_to_requested_printer_index():
    """Regression: /api/printer/* must gate on the printer_index a request
    targets, not just whichever printer is globally "active". A blocked
    device (e.g. a eufyMake E1 mixed into the same account) must not shadow
    requests for a different, fully-supported printer."""
    cfg = Config(
        account=Account(auth_token="token", region="eu", user_id="user-1", email="user@example.com"),
        printers=[_printer("SN1", "UV Printer", model="V8260"), _printer("SN2", "Printer Two")],
    )
    manager = FakeConfigManager(cfg)
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "login": app.config.get("login"),
        "unsupported_device": app.config.get("unsupported_device"),
        "api_key": app.config.get("api_key"),
    }
    app.config["config"] = manager
    app.config["printer_index"] = 0  # active printer is the unsupported one
    app.config["login"] = True
    app.config["unsupported_device"] = True
    app.config["api_key"] = None

    try:
        blocked = client.get("/api/printer/alerts")
        allowed = client.get("/api/printer/alerts?printer_index=1")

        assert blocked.status_code == 503
        assert allowed.status_code == 200
    finally:
        for key, value in old_values.items():
            app.config[key] = value


def test_api_printers_and_switch_active_printer(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[
            _printer("SN1", "Printer One", ip_addr="192.168.1.10"),
            _printer("SN2", "Printer Two", ip_addr="192.168.1.11"),
        ],
    )
    manager = FakeConfigManager(cfg)
    services = FakeServices()
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "video_supported": app.config.get("video_supported"),
        "login": app.config.get("login"),
        "unsupported_device": app.config.get("unsupported_device"),
        "api_key": app.config.get("api_key"),
    }
    old_svc = app.svc

    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = False
    app.config["video_supported"] = True
    app.config["login"] = True
    app.config["unsupported_device"] = False
    app.config["api_key"] = "secret-key-123456"
    app.svc = services

    try:
        printers = client.get("/api/printers", headers={"X-Api-Key": "secret-key-123456"})
        unauthorized = client.post("/api/printers/active", json={"index": 1})
        switched = client.post(
            "/api/printers/active",
            json={"index": 1},
            headers={"X-Api-Key": "secret-key-123456"},
        )

        assert printers.status_code == 200
        assert printers.get_json()["active_index"] == 0
        assert len(printers.get_json()["printers"]) == 2

        assert unauthorized.status_code == 401
        assert "Unauthorized" in unauthorized.get_json()["error"]

        assert switched.status_code == 200
        assert switched.get_json()["printer"]["index"] == 1
        assert app.config["printer_index"] == 1
        assert cfg.active_printer_index == 1
        assert services.restart_calls == 1
    finally:
        app.svc = old_svc
        for key, value in old_values.items():
            app.config[key] = value


def test_api_printer_runtime_state_returns_filament_details():
    mqtt = SimpleNamespace(
        get_state=lambda: {
            "print": {
                "print_state": "paused",
                "state": 2,
                "pause_reason": "filament_runout",
                "pause_reason_label": "Filament runout",
            },
            "filament": {
                "state": "not_loaded",
                "label": "Not Loaded",
                "issue": "runout",
                "issue_label": "Filament runout",
                "detail": "Paused: Filament runout. Reload filament to continue.",
                "pause_reason": "filament_runout",
                "pause_reason_label": "Filament runout",
            },
            "timelapse": {
                "enabled": True,
                "capturing": True,
                "recovering": True,
                "recovery_reason": "timelapse has no recent video frame",
                "detail": "Recovering video stream...",
            },
        }
    )
    client = app.test_client()
    old_values = {
        "login": app.config.get("login"),
        "api_key": app.config.get("api_key"),
    }
    old_svc = app.svc

    app.config["login"] = True
    app.config["api_key"] = None
    app.svc = FakeServices(mqtt)

    try:
        response = client.get("/api/printer/runtime-state")
    finally:
        app.svc = old_svc
        for key, value in old_values.items():
            app.config[key] = value

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["print"]["pause_reason_label"] == "Filament runout"
    assert data["filament"]["issue"] == "runout"
    assert data["filament"]["detail"] == "Paused: Filament runout. Reload filament to continue."
    assert data["timelapse"]["recovering"] is True
    assert data["timelapse"]["detail"] == "Recovering video stream..."


def test_api_timelapse_current_start_and_dismiss():
    calls = []
    mqtt = SimpleNamespace(
        start_timelapse_for_current_print=lambda: calls.append("start") or "cube.gcode",
        pause_timelapse_for_current_print=lambda: calls.append("pause") or "cube.gcode",
        resume_timelapse_for_current_print=lambda: calls.append("resume") or "cube.gcode",
        stop_timelapse_for_current_print=lambda: calls.append("stop") or "cube.gcode",
        dismiss_timelapse_start_offer=lambda: calls.append("dismiss"),
        get_state=lambda: {
            "print": {
                "print_state": "printing",
                "state": 1,
            },
            "timelapse": {
                "enabled": True,
                "capturing": "start" in calls and "stop" not in calls,
                "active_capture": "start" in calls and "stop" not in calls,
                "paused": "pause" in calls and "resume" not in calls and "stop" not in calls,
                "pause_reason": "manual" if "pause" in calls and "resume" not in calls and "stop" not in calls else None,
                "manual_paused": "pause" in calls and "resume" not in calls and "stop" not in calls,
                "prompt_start": "dismiss" not in calls and "start" not in calls,
                "prompt_filename": "cube.gcode",
            },
        },
    )
    client = app.test_client()
    old_values = {
        "login": app.config.get("login"),
        "api_key": app.config.get("api_key"),
    }
    old_svc = app.svc

    app.config["login"] = True
    app.config["api_key"] = None
    app.svc = FakeServices(mqtt)

    try:
        start_response = client.post("/api/timelapse/current/start")
        pause_response = client.post("/api/timelapse/current/pause")
        resume_response = client.post("/api/timelapse/current/resume")
        stop_response = client.post("/api/timelapse/current/stop")
        dismiss_response = client.post("/api/timelapse/current/dismiss")
    finally:
        app.svc = old_svc
        for key, value in old_values.items():
            app.config[key] = value

    assert start_response.status_code == 200
    assert pause_response.status_code == 200
    assert resume_response.status_code == 200
    assert stop_response.status_code == 200
    assert dismiss_response.status_code == 200
    assert calls == ["start", "pause", "resume", "stop", "dismiss"]
    assert start_response.get_json()["filename"] == "cube.gcode"
    assert start_response.get_json()["timelapse"]["capturing"] is True
    assert pause_response.get_json()["timelapse"]["paused"] is True
    assert pause_response.get_json()["timelapse"]["pause_reason"] == "manual"
    assert resume_response.get_json()["timelapse"]["paused"] is False
    assert stop_response.get_json()["timelapse"]["capturing"] is False
    assert dismiss_response.get_json()["timelapse"]["prompt_start"] is False


def test_api_printer_alerts_returns_recent_entries(monkeypatch):
    calls = []

    class FakeAlertBuffer:
        def snapshot(self, *, limit=20, after_id=None):
            calls.append((limit, after_id))
            return {
                "entries": [{
                    "id": 9,
                    "printer_index": 0,
                    "printer_name": "Thing 1",
                    "type": "filament_runout",
                    "title": "Filament runout",
                    "message": "Filament runout or break detected.",
                    "level": "warning",
                }],
                "first_id": 9,
                "last_id": 9,
                "next_after": 9,
                "truncated": False,
                "max_entries": 100,
            }

    client = app.test_client()
    old_values = {
        "login": app.config.get("login"),
        "api_key": app.config.get("api_key"),
    }
    app.config["login"] = True
    app.config["api_key"] = None
    monkeypatch.setattr(web_module, "_get_printer_alert_buffer", lambda: FakeAlertBuffer())

    try:
        response = client.get("/api/printer/alerts?limit=15&after=3")
    finally:
        for key, value in old_values.items():
            app.config[key] = value

    assert response.status_code == 200
    assert response.get_json()["entries"][0]["title"] == "Filament runout"
    assert calls == [(15, 3)]


def test_root_shows_ffmpeg_warning_only_for_camera_capable_devices(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer("SN1", "Printer One", model="V8111")],
    )
    manager = FakeConfigManager(cfg)
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "video_supported": app.config.get("video_supported"),
        "login": app.config.get("login"),
        "unsupported_device": app.config.get("unsupported_device"),
        "api_key": app.config.get("api_key"),
    }

    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = False
    app.config["video_supported"] = True
    app.config["login"] = True
    app.config["unsupported_device"] = False
    app.config["api_key"] = None

    try:
        monkeypatch.setattr("web._ffmpeg_available", lambda: False)
        camera = client.get("/")
        app.config["video_supported"] = False
        no_camera = client.get("/")
    finally:
        for key, value in old_values.items():
            app.config[key] = value

    assert camera.status_code == 200
    assert "Camera features need `ffmpeg`" in camera.get_data(as_text=True)
    assert no_camera.status_code == 200
    assert "Camera features need `ffmpeg`" not in no_camera.get_data(as_text=True)


def test_root_handles_config_with_no_printers(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[],
    )
    manager = FakeConfigManager(cfg)
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "video_supported": app.config.get("video_supported"),
        "login": app.config.get("login"),
        "unsupported_device": app.config.get("unsupported_device"),
        "api_key": app.config.get("api_key"),
    }

    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = False
    app.config["video_supported"] = True
    app.config["login"] = True
    app.config["unsupported_device"] = False
    app.config["api_key"] = None

    try:
        monkeypatch.setattr("web._ffmpeg_available", lambda: False)
        response = client.get("/")
    finally:
        for key, value in old_values.items():
            app.config[key] = value

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "No printers are configured for this account" in html
    assert 'id="setup"' in html
    assert "Camera features need `ffmpeg`" not in html


def test_probe_printer_storage_files_uses_one_shot_mqtt_probe(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer("SN1", "Printer One", ip_addr="192.168.1.10")],
    )
    manager = FakeConfigManager(cfg)
    disconnects = []
    fake_client = SimpleNamespace(_mqtt=SimpleNamespace(disconnect=lambda: disconnects.append(True)))

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "insecure": app.config.get("insecure"),
    }
    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["insecure"] = False

    monkeypatch.setattr("web.cli.mqtt.mqtt_open", lambda config, printer_index, insecure: fake_client)
    monkeypatch.setattr(
        "web.cli.mqtt.mqtt_file_list_probe",
        lambda client, source, source_value, timeout, collect_window: {
            "request": {"commandType": 1009, "value": 1},
            "source_value": 1,
            "reply_count": 1,
            "replies": [{"commandType": 1009, "reply": 0}],
            "files": [{"name": "cube.gcode", "path": "/usr/data/local/model/cube.gcode", "timestamp": 123, "source": "onboard"}],
        },
    )

    try:
        result, error = _probe_printer_storage_files(source="onboard")
    finally:
        for key, value in old_values.items():
            app.config[key] = value

    assert error is None
    assert result["source_value"] == 1
    assert result["files"][0]["path"] == "/usr/data/local/model/cube.gcode"
    assert disconnects == [True]


def test_printer_control_guard_without_login():
    client = app.test_client()
    old_login = app.config.get("login")
    old_api_key = app.config.get("api_key")
    app.config["login"] = False
    app.config["api_key"] = None
    try:
        response = client.get("/api/printer/bed-leveling")
        assert response.status_code == 503
        assert "No printer configured" in response.get_json()["error"]
    finally:
        app.config["login"] = old_login
        app.config["api_key"] = old_api_key


def test_safe_same_site_redirect_target_rejects_external_style_paths():
    with app.test_request_context("/"):
        assert _safe_same_site_redirect_target("//evil.example/path", {"x": ["1"]}) == "/"
        assert _safe_same_site_redirect_target("https:/evil.example", None) == "/"


def test_apikey_url_param_redirects_and_sets_session():
    """?apikey= should authenticate API requests directly and bootstrap the session."""
    client = app.test_client()
    old_api_key = app.config.get("api_key")
    app.config["api_key"] = "test-secret-key"
    try:
        resp = client.get("/api/health?apikey=test-secret-key&foo=bar")
        assert resp.status_code == 200, f"Expected direct API access, got {resp.status_code}"
        assert "Location" not in resp.headers

        with client.session_transaction() as sess:
            assert sess.get("authenticated"), \
                "Session should be authenticated after ?apikey= API access"
    finally:
        app.config["api_key"] = old_api_key


def test_mqtt_reconnect_endpoint_calls_reset_backoff():
    """POST /api/mqtt/reconnect must call reset_reconnect_backoff() and return 200."""
    from types import SimpleNamespace
    client = app.test_client()
    calls = []
    fake_mqtt = SimpleNamespace(
        _silent_reconnect_count=5,
        reset_reconnect_backoff=lambda: calls.append("reset"),
        wake=lambda: None,
    )
    old_svc = app.svc
    old_login = app.config.get("login")
    old_api_key = app.config.get("api_key")
    old_printer_index = app.config.get("printer_index")
    app.config["login"] = True
    app.config["api_key"] = None
    app.config["printer_index"] = 0
    # Use both naming conventions to cover legacy and index-based lookup
    app.svc = FakeServices()
    app.svc.svcs["mqttqueue"] = fake_mqtt
    try:
        resp = client.post("/api/mqtt/reconnect")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.data}"
        assert resp.get_json()["status"] == "ok"
        assert calls == ["reset"]
    finally:
        app.svc = old_svc
        app.config["login"] = old_login
        app.config["api_key"] = old_api_key
        app.config["printer_index"] = old_printer_index
