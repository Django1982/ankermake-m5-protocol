from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from cli.model import Account, Config, Printer
import web as web_module
from web import app


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
        self.config_root = config_root

    @contextmanager
    def open(self):
        yield self.cfg

    @contextmanager
    def modify(self):
        yield self.cfg


@contextmanager
def _borrow_value(value):
    yield value


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


def _install_app_state(cfg=None, mqtt=None, config_root=None):
    cfg = cfg or _base_config()
    manager = FakeConfigManager(cfg, config_root=config_root)
    mqtt = mqtt if mqtt is not None else SimpleNamespace(
        timelapse=SimpleNamespace(reload_config=lambda config: None),
        ha=SimpleNamespace(reload_config=lambda config: None),
    )

    old = {
        "config": app.config.get("config"),
        "api_key": app.config.get("api_key"),
        "login": app.config.get("login"),
        "printer_index": app.config.get("printer_index"),
    }
    old_svc = app.svc

    app.config["config"] = manager
    app.config["api_key"] = "secret-key-123456"
    app.config["login"] = True
    app.config["printer_index"] = 0
    app.svc = SimpleNamespace(borrow=lambda name: _borrow_value(mqtt))

    return old, old_svc, cfg, mqtt


def _restore_app_state(old, old_svc):
    app.svc = old_svc
    for key, value in old.items():
        app.config[key] = value


def test_upload_rate_endpoint_updates_config():
    client = app.test_client()
    old, old_svc, cfg, _mqtt = _install_app_state()

    try:
        missing = client.post("/api/ankerctl/config/upload-rate")
        invalid = client.post(
            "/api/ankerctl/config/upload-rate",
            data={"upload_rate_mbps": "7"},
            headers={"X-Api-Key": "secret-key-123456"},
        )
        updated = client.post(
            "/api/ankerctl/config/upload-rate",
            data={"upload_rate_mbps": "25"},
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    assert missing.status_code == 401
    assert invalid.status_code == 400
    assert updated.status_code == 200
    assert updated.get_json()["upload_rate_mbps"] == 25
    assert cfg.upload_rate_mbps == 25


def test_notifications_settings_get_update_and_test(monkeypatch):
    client = app.test_client()
    old, old_svc, cfg, _mqtt = _install_app_state()

    class FakeNotifier:
        def __init__(self, config, settings=None):
            self.settings = settings

        def build_attachments(self):
            return ["preview.jpg"], ["cleanup.jpg"]

        def cleanup_attachments(self, paths):
            cleaned.extend(paths)

    class FakeClient:
        def __init__(self, config):
            created_configs.append(config)

        def _post(self, title, body, attachments=None):
            posts.append((title, body, attachments))
            return True, "sent"

    posts = []
    cleaned = []
    created_configs = []
    monkeypatch.setattr("web.notifications.AppriseNotifier", FakeNotifier)
    monkeypatch.setattr("web.AppriseClient", FakeClient)

    try:
        unauthorized = client.get("/api/notifications/settings")
        got = client.get("/api/notifications/settings", headers={"X-Api-Key": "secret-key-123456"})
        updated = client.post(
            "/api/notifications/settings",
            json={"apprise": {"enabled": True, "server_url": "https://notify.example", "events": {"print_started": False}}},
            headers={"X-Api-Key": "secret-key-123456"},
        )
        tested = client.post(
            "/api/notifications/test",
            json={"apprise": {"enabled": True, "server_url": "https://notify.example", "key": "abc1234567890123"}},
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    assert unauthorized.status_code == 401
    assert got.status_code == 200
    assert "apprise" in got.get_json()
    assert updated.status_code == 200
    assert cfg.notifications["apprise"]["enabled"] is True
    assert cfg.notifications["apprise"]["events"]["print_started"] is False
    assert tested.status_code == 200
    assert posts and posts[0][2] == ["preview.jpg"]
    assert cleaned == ["cleanup.jpg"]
    assert created_configs and created_configs[0]["server_url"] == "https://notify.example"


def test_timelapse_and_mqtt_settings_endpoints_reload_services():
    client = app.test_client()
    reload_calls = []
    mqtt = SimpleNamespace(
        timelapse=SimpleNamespace(reload_config=lambda config: reload_calls.append(("timelapse", config))),
        ha=SimpleNamespace(reload_config=lambda config: reload_calls.append(("ha", config))),
    )
    old, old_svc, cfg, _mqtt = _install_app_state(mqtt=mqtt)

    try:
        tl_get = client.get("/api/settings/timelapse", headers={"X-Api-Key": "secret-key-123456"})
        tl_update = client.post(
            "/api/settings/timelapse",
            json={"timelapse": {"enabled": True, "interval": 15}},
            headers={"X-Api-Key": "secret-key-123456"},
        )
        mqtt_get = client.get("/api/settings/mqtt", headers={"X-Api-Key": "secret-key-123456"})
        mqtt_update = client.post(
            "/api/settings/mqtt",
            json={"home_assistant": {"enabled": True, "mqtt_host": "ha.local", "mqtt_port": 1884}},
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    assert tl_get.status_code == 200
    assert tl_update.status_code == 200
    assert cfg.timelapse["per_printer"]["SN1"]["enabled"] is True
    assert cfg.timelapse["per_printer"]["SN1"]["interval"] == 15
    assert mqtt_get.status_code == 200
    assert mqtt_update.status_code == 200
    assert cfg.home_assistant["enabled"] is True
    assert cfg.home_assistant["mqtt_host"] == "ha.local"
    assert len(reload_calls) == 2
    assert reload_calls[0][0] == "timelapse"
    assert reload_calls[1][0] == "ha"
    assert hasattr(reload_calls[0][1], "open")
    assert hasattr(reload_calls[1][1], "modify")


def test_camera_settings_endpoints_persist_per_printer():
    client = app.test_client()
    cfg = _base_config()
    cfg.printers.append(_printer(sn="SN2", name="Printer 2"))
    old, old_svc, cfg, _mqtt = _install_app_state(cfg=cfg)

    try:
        got = client.get("/api/settings/camera", headers={"X-Api-Key": "secret-key-123456"})
        updated = client.post(
            "/api/settings/camera",
            json={
                "camera": {
                    "source": "external",
                    "integration": {
                        "enabled": True,
                    },
                    "external": {
                        "name": "Workbench Cam",
                        "snapshot_url": "http://cam.local/snapshot.jpg",
                        "refresh_sec": 4,
                    },
                }
            },
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    assert got.status_code == 200
    assert got.get_json()["camera"]["source"] == "printer"
    assert got.get_json()["camera"]["integration"]["enabled"] is False
    assert "endpoint_url_with_api_key" not in got.get_json()["camera"]["integration"]
    assert got.get_json()["camera"]["integration"]["api_key_required"] is True
    assert "apikey=" not in got.get_json()["camera"]["integration"]["endpoint_url"]
    assert updated.status_code == 200
    camera = updated.get_json()["camera"]
    assert camera["source"] == "external"
    assert camera["effective_source"] == "external"
    assert camera["integration"]["enabled"] is True
    assert "endpoint_url_with_api_key" not in camera["integration"]
    assert camera["integration"]["api_key_required"] is True
    assert "apikey=" not in camera["integration"]["endpoint_url"]
    assert camera["external"]["name"] == "Workbench Cam"
    assert camera["external"]["snapshot_url"] == "http://cam.local/snapshot.jpg"
    assert cfg.camera["per_printer"]["SN1"]["source"] == "external"
    assert cfg.camera["per_printer"]["SN1"]["integration"]["enabled"] is True
    assert "SN2" not in cfg.camera["per_printer"]


def test_camera_settings_endpoints_honor_requested_printer_index():
    client = app.test_client()
    cfg = _base_config()
    cfg.printers.append(_printer(sn="SN2", name="Printer 2"))
    old, old_svc, cfg, _mqtt = _install_app_state(cfg=cfg)

    try:
        updated = client.post(
            "/api/settings/camera?printer_index=1",
            json={
                "camera": {
                    "source": "external",
                    "integration": {
                        "enabled": True,
                    },
                    "external": {
                        "name": "Thing 2 Cam",
                        "snapshot_url": "http://thing2.local/snapshot.jpg",
                        "refresh_sec": 2,
                    },
                }
            },
            headers={"X-Api-Key": "secret-key-123456"},
        )
        first_printer = client.get(
            "/api/settings/camera?printer_index=0",
            headers={"X-Api-Key": "secret-key-123456"},
        )
        second_printer = client.get(
            "/api/settings/camera?printer_index=1",
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    assert updated.status_code == 200
    assert first_printer.status_code == 200
    assert second_printer.status_code == 200
    assert first_printer.get_json()["camera"]["source"] == "printer"
    assert first_printer.get_json()["camera"]["integration"]["enabled"] is False
    assert "endpoint_url_with_api_key" not in first_printer.get_json()["camera"]["integration"]
    assert first_printer.get_json()["camera"]["integration"]["api_key_required"] is True
    assert second_printer.get_json()["camera"]["source"] == "external"
    assert second_printer.get_json()["camera"]["integration"]["enabled"] is True
    assert "endpoint_url_with_api_key" not in second_printer.get_json()["camera"]["integration"]
    assert second_printer.get_json()["camera"]["integration"]["api_key_required"] is True
    assert "apikey=" not in second_printer.get_json()["camera"]["integration"]["endpoint_url"]
    assert second_printer.get_json()["camera"]["external"]["name"] == "Thing 2 Cam"
    assert "SN1" not in cfg.camera["per_printer"]
    assert cfg.camera["per_printer"]["SN2"]["source"] == "external"
    assert cfg.camera["per_printer"]["SN2"]["integration"]["enabled"] is True


def test_timelapse_settings_endpoints_persist_per_printer():
    client = app.test_client()
    cfg = _base_config()
    cfg.printers.append(_printer(sn="SN2", name="Printer 2"))
    old, old_svc, cfg, _mqtt = _install_app_state(cfg=cfg)

    try:
        got = client.get("/api/settings/timelapse", headers={"X-Api-Key": "secret-key-123456"})
        updated = client.post(
            "/api/settings/timelapse",
            json={"timelapse": {"enabled": True, "interval": 12, "light": "snapshot", "camera_source": "external"}},
            headers={"X-Api-Key": "secret-key-123456"},
        )
        app.config["printer_index"] = 1
        other_printer = client.get("/api/settings/timelapse", headers={"X-Api-Key": "secret-key-123456"})
    finally:
        _restore_app_state(old, old_svc)

    assert got.status_code == 200
    assert updated.status_code == 200
    assert updated.get_json()["timelapse"]["enabled"] is True
    assert updated.get_json()["timelapse"]["interval"] == 12
    assert updated.get_json()["timelapse"]["light"] == "snapshot"
    assert updated.get_json()["timelapse"]["camera_source"] == "external"
    assert cfg.timelapse["per_printer"]["SN1"]["enabled"] is True
    assert cfg.timelapse["per_printer"]["SN1"]["interval"] == 12
    assert cfg.timelapse["per_printer"]["SN1"]["light"] == "snapshot"
    assert cfg.timelapse["per_printer"]["SN1"]["camera_source"] == "external"
    assert "SN2" not in cfg.timelapse["per_printer"]
    assert other_printer.status_code == 200
    assert other_printer.get_json()["timelapse"]["enabled"] is False
    assert other_printer.get_json()["timelapse"]["interval"] == 30
    assert other_printer.get_json()["timelapse"]["camera_source"] == "follow"


def test_launcher_bat_download_uses_requested_install_dir():
    client = app.test_client()
    old, old_svc, _cfg, _mqtt = _install_app_state()

    try:
        response = client.post(
            "/api/settings/launcher-bat",
            json={"install_dir": r"C:\Users\TestUser\Documents\GitHub\ankermake-m5-protocol"},
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'attachment; filename="ankerctl-launcher.bat"' in response.headers["Content-Disposition"]
    assert r'set "ANKERCTL_DIR=C:\Users\TestUser\Documents\GitHub\ankermake-m5-protocol"' in body
    assert r"py .\ankerctl.py webserver run" in body
    assert r"python .\ankerctl.py webserver run" in body


def test_filament_service_settings_endpoints_persist_manual_and_legacy_modes():
    client = app.test_client()
    old, old_svc, cfg, _mqtt = _install_app_state()

    try:
        unauthorized = client.get("/api/settings/filament-service")
        got = client.get("/api/settings/filament-service", headers={"X-Api-Key": "secret-key-123456"})
        updated = client.post(
            "/api/settings/filament-service",
            json={
                "filament_service": {
                    "allow_legacy_swap": True,
                    "manual_swap_preheat_temp_c": 149,
                    "quick_move_length_mm": 12.5,
                    "swap_prime_length_mm": 10,
                    "swap_unload_length_mm": 55,
                    "swap_load_length_mm": 65,
                    "swap_home_pause_s": 42,
                }
            },
            headers={"X-Api-Key": "secret-key-123456"},
        )
        clamped = client.post(
            "/api/settings/filament-service",
            json={"filament_service": {"manual_swap_preheat_temp_c": 999}},
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    assert unauthorized.status_code == 401
    assert got.status_code == 200
    assert got.get_json()["filament_service"]["allow_legacy_swap"] is False
    assert got.get_json()["filament_service"]["quick_move_length_mm"] == 40
    assert got.get_json()["filament_service"]["swap_prime_length_mm"] == 10
    assert got.get_json()["filament_service"]["swap_unload_length_mm"] == 40
    assert got.get_json()["filament_service"]["swap_load_length_mm"] == 120
    assert got.get_json()["filament_service"]["swap_home_pause_s"] == 70
    assert updated.status_code == 200
    assert updated.get_json()["filament_service"]["allow_legacy_swap"] is True
    assert updated.get_json()["filament_service"]["manual_swap_preheat_temp_c"] == 149
    assert updated.get_json()["filament_service"]["quick_move_length_mm"] == 12.5
    assert updated.get_json()["filament_service"]["swap_prime_length_mm"] == 10
    assert updated.get_json()["filament_service"]["swap_unload_length_mm"] == 55
    assert updated.get_json()["filament_service"]["swap_load_length_mm"] == 65
    assert updated.get_json()["filament_service"]["swap_home_pause_s"] == 42
    assert clamped.status_code == 200
    assert cfg.filament_service["allow_legacy_swap"] is True
    assert cfg.filament_service["quick_move_length_mm"] == 12.5
    assert cfg.filament_service["swap_prime_length_mm"] == 10
    assert cfg.filament_service["swap_unload_length_mm"] == 55
    assert cfg.filament_service["swap_load_length_mm"] == 65
    assert cfg.filament_service["swap_home_pause_s"] == 42
    assert cfg.filament_service["manual_swap_preheat_temp_c"] == 300
    assert clamped.get_json()["filament_service"]["manual_swap_preheat_temp_c"] == 300


def test_filament_service_advanced_command_config_creates_and_opens(tmp_path, monkeypatch):
    client = app.test_client()
    opened_paths = []
    monkeypatch.setattr(
        web_module,
        "_open_file_with_default_app",
        lambda path: opened_paths.append(path) or True,
    )
    old, old_svc, _cfg, _mqtt = _install_app_state(config_root=tmp_path)

    try:
        unauthorized = client.get("/api/settings/filament-service/advanced")
        got = client.get(
            "/api/settings/filament-service/advanced",
            headers={"X-Api-Key": "secret-key-123456"},
        )
        opened = client.post(
            "/api/settings/filament-service/advanced/open",
            headers={"X-Api-Key": "secret-key-123456"},
        )
    finally:
        _restore_app_state(old, old_svc)

    command_path = tmp_path / "filament_swap_commands.json"
    assert unauthorized.status_code == 401
    assert got.status_code == 200
    assert got.get_json()["created"] is True
    assert got.get_json()["path"] == str(command_path)
    assert "z_lift" in got.get_json()["config"]["commands"]
    assert got.get_json()["config"]["settings"]["swap_unload_length_mm"] == 40.0
    assert got.get_json()["config"]["settings"]["swap_load_length_mm"] == 120.0
    assert command_path.exists()
    assert opened.status_code == 200
    assert opened.get_json()["path"] == str(command_path)
    assert opened_paths == [command_path]
