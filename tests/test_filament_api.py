import threading
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from cli.model import Account, Config, Printer
import web as web_module
from web import app
from web.service.filament import FilamentStore


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
        self.config_root = config_root

    @contextmanager
    def open(self):
        yield self.cfg

    @contextmanager
    def modify(self):
        yield self.cfg


class FakeServices:
    def __init__(self, mqtt):
        if isinstance(mqtt, dict):
            self._services = dict(mqtt)
        else:
            self._services = {"mqttqueue": mqtt, "mqttqueue:0": mqtt}
        self.svcs = dict(self._services)

    @contextmanager
    def borrow(self, name):
        if name not in self._services:
            raise KeyError(name)
        yield self._services[name]


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


def _install_state(tmp_path, mqtt):
    old_values = {
        "config": app.config.get("config"),
        "api_key": app.config.get("api_key"),
        "login": app.config.get("login"),
        "printer_index": app.config.get("printer_index"),
        "unsupported_device": app.config.get("unsupported_device"),
    }
    old_svc = app.svc
    old_filaments = getattr(app, "filaments", None)
    old_swap = getattr(app, "filament_swap_state", None)

    app.config["config"] = FakeConfigManager(_base_config(), config_root=tmp_path)
    app.config["api_key"] = API_KEY
    app.config["login"] = True
    app.config["printer_index"] = 0
    app.config["unsupported_device"] = False
    app.svc = FakeServices(mqtt)
    app.filaments = FilamentStore(tmp_path / "filaments.db")
    app.filament_swap_state = {}
    app.filament_swap_threads = {}

    return old_values, old_svc, old_filaments, old_swap


def _restore_state(old_values, old_svc, old_filaments, old_swap):
    app.svc = old_svc
    for key, value in old_values.items():
        app.config[key] = value
    app.filaments = old_filaments
    app.filament_swap_state = old_swap
    app.filament_swap_threads = {}


def test_filament_crud_and_apply_routes(tmp_path):
    sent = []
    mqtt = SimpleNamespace(is_printing=False, send_gcode=lambda gcode: sent.append(gcode), nozzle_temp=220)
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        created = client.post(
            "/api/filaments",
            json={
                "name": "PLA Test",
                "nozzle_temp": 215,
                "nozzle_temp_first_layer": 215,
                "bed_temp": 60,
            },
            headers={"X-Api-Key": API_KEY},
        )
        profile_id = created.get_json()["id"]
        listed = client.get("/api/filaments", headers={"X-Api-Key": API_KEY})
        updated = client.put(
            f"/api/filaments/{profile_id}",
            json={"name": "PLA Test 2", "bed_temp": 65},
            headers={"X-Api-Key": API_KEY},
        )
        duplicated = client.post(
            f"/api/filaments/{profile_id}/duplicate",
            headers={"X-Api-Key": API_KEY},
        )
        applied = client.post(
            f"/api/filaments/{profile_id}/apply",
            headers={"X-Api-Key": API_KEY},
        )
        deleted = client.delete(
            f"/api/filaments/{profile_id}",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert created.status_code == 201
    assert any(item["name"] == "PLA Test" for item in listed.get_json()["filaments"])
    assert updated.status_code == 200
    assert updated.get_json()["name"] == "PLA Test 2"
    assert duplicated.status_code == 201
    assert duplicated.get_json()["name"].startswith("PLA Test 2")
    assert applied.status_code == 200
    assert applied.get_json()["gcode"] == "M104 S215\nM140 S65"
    assert deleted.status_code == 200
    assert sent == ["M104 S215\nM140 S65"]


def test_filament_apply_route_blocks_while_printing(tmp_path):
    sent = []
    mqtt = SimpleNamespace(is_printing=True, send_gcode=lambda gcode: sent.append(gcode), nozzle_temp=220)
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        profile = app.filaments.create({"name": "PLA Busy", "nozzle_temp": 215, "bed_temp": 60})
        applied = client.post(
            f"/api/filaments/{profile['id']}/apply",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert applied.status_code == 409
    assert applied.get_json()["error"] == "Filament service commands are blocked while a print is active"
    assert sent == []


def test_filament_update_route_rejects_blank_name(tmp_path):
    mqtt = SimpleNamespace(is_printing=False, send_gcode=lambda gcode: None, nozzle_temp=220)
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        profile = app.filaments.create({"name": "PLA Keep"})
        updated = client.put(
            f"/api/filaments/{profile['id']}",
            json={"name": "   "},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert updated.status_code == 400
    assert updated.get_json()["error"] == "name is required"


def test_filament_service_preheat_and_move_routes(tmp_path):
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=220,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        profile = app.filaments.create({"name": "PLA", "nozzle_temp": 220})
        app.config["config"].cfg.filament_service["quick_move_length_mm"] = 12.5
        preheat = client.post(
            "/api/filaments/service/preheat",
            json={"profile_id": profile["id"]},
            headers={"X-Api-Key": API_KEY},
        )
        move = client.post(
            "/api/filaments/service/move",
            json={"profile_id": profile["id"], "action": "retract", "length_mm": 25},
            headers={"X-Api-Key": API_KEY},
        )
        move_default = client.post(
            "/api/filaments/service/move",
            json={"profile_id": profile["id"], "action": "extrude"},
            headers={"X-Api-Key": API_KEY},
        )
        invalid = client.post(
            "/api/filaments/service/move",
            json={"profile_id": profile["id"], "action": "spin"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert preheat.status_code == 200
    assert move.status_code == 200
    assert move_default.status_code == 200
    assert invalid.status_code == 400
    assert sent[0] == "M104 S220"
    assert "G1 E-25 F2000" in sent[1]
    assert "G1 E12.5 F600" in sent[2]


def test_filament_swap_routes_follow_manual_guided_flow_by_default(tmp_path):
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=150,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        started = client.post(
            "/api/filaments/service/swap/start",
            headers={"X-Api-Key": API_KEY},
        )
        token = started.get_json()["swap"]["token"]
        confirmed = client.post(
            "/api/filaments/service/swap/confirm",
            json={"token": token},
            headers={"X-Api-Key": API_KEY},
        )
        cancelled = client.post(
            "/api/filaments/service/swap/cancel",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    assert started.get_json()["pending"] is True
    assert started.get_json()["swap"]["mode"] == "manual"
    assert started.get_json()["swap"]["phase"] == "await_manual_swap"
    assert confirmed.status_code == 200
    assert confirmed.get_json()["pending"] is False
    assert cancelled.status_code == 200
    assert cancelled.get_json()["pending"] is False
    assert sent == ["M104 S180"]


def test_filament_swap_state_is_scoped_per_printer(tmp_path):
    sent0 = []
    sent1 = []
    mqtt0 = SimpleNamespace(
        is_printing=False,
        nozzle_temp=150,
        send_gcode=lambda gcode: sent0.append(gcode),
    )
    mqtt1 = SimpleNamespace(
        is_printing=False,
        nozzle_temp=150,
        send_gcode=lambda gcode: sent1.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(
        tmp_path,
        {"mqttqueue:0": mqtt0, "mqttqueue:1": mqtt1},
    )
    app.config["config"].cfg.printers.append(_printer("SN2", "Printer 2"))

    try:
        started0 = client.post(
            "/api/filaments/service/swap/start?printer_index=0",
            headers={"X-Api-Key": API_KEY},
        )
        started1 = client.post(
            "/api/filaments/service/swap/start?printer_index=1",
            headers={"X-Api-Key": API_KEY},
        )
        token0 = (started0.get_json().get("swap") or {}).get("token")
        token1 = (started1.get_json().get("swap") or {}).get("token")
        state0 = client.get(
            "/api/filaments/service/swap?printer_index=0",
            headers={"X-Api-Key": API_KEY},
        )
        state1 = client.get(
            "/api/filaments/service/swap?printer_index=1",
            headers={"X-Api-Key": API_KEY},
        )
        mismatch = client.post(
            "/api/filaments/service/swap/confirm?printer_index=1",
            json={"token": token0},
            headers={"X-Api-Key": API_KEY},
        )
        confirmed1 = client.post(
            "/api/filaments/service/swap/confirm?printer_index=1",
            json={"token": token1},
            headers={"X-Api-Key": API_KEY},
        )
        still0 = client.get(
            "/api/filaments/service/swap?printer_index=0",
            headers={"X-Api-Key": API_KEY},
        )
        confirmed0 = client.post(
            "/api/filaments/service/swap/confirm?printer_index=0",
            json={"token": token0},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started0.status_code == 200
    assert started1.status_code == 200
    assert token0 is not None
    assert token1 is not None
    assert token0 != token1
    assert state0.status_code == 200
    assert state0.get_json()["swap"]["printer_index"] == 0
    assert state1.status_code == 200
    assert state1.get_json()["swap"]["printer_index"] == 1
    assert mismatch.status_code == 409
    assert confirmed1.status_code == 200
    assert confirmed1.get_json()["pending"] is False
    assert still0.status_code == 200
    assert still0.get_json()["pending"] is True
    assert still0.get_json()["swap"]["token"] == token0
    assert confirmed0.status_code == 200
    assert confirmed0.get_json()["pending"] is False
    assert sent0 == ["M104 S180"]
    assert sent1 == ["M104 S180"]


def test_filament_swap_routes_cover_legacy_start_confirm_and_cancel(tmp_path, monkeypatch):
    sent = []
    home_calls = []
    home_waits = []
    motion_waits = []
    z_lift_waits = []
    target_waits = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=250,
        send_gcode=lambda gcode: sent.append(gcode),
        send_home=lambda axis: home_calls.append(axis),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)
    app.config["config"].cfg.filament_service["allow_legacy_swap"] = True
    app.config["config"].cfg.filament_service["swap_prime_length_mm"] = 10
    app.config["config"].cfg.filament_service["swap_unload_length_mm"] = 55
    app.config["config"].cfg.filament_service["swap_load_length_mm"] = 65
    background_calls = []

    def fake_start_background(target, token, printer_index):
        background_calls.append((target, token))
        return SimpleNamespace()

    def fake_motion_wait(length_mm, feedrate_mm_min, should_continue=None):
        motion_waits.append((length_mm, feedrate_mm_min))
        assert should_continue is None or should_continue()

    def fake_z_lift_wait(z_lift_mm, z_feedrate_mm_min=600, should_continue=None):
        z_lift_waits.append(z_lift_mm)
        assert should_continue is None or should_continue()

    def fake_home_wait(should_continue=None, pause_s=None):
        home_waits.append(pause_s)
        assert should_continue is None or should_continue()

    def fake_target_wait(mqtt, target_temp_c, should_continue=None):
        target_waits.append(target_temp_c)
        assert should_continue is None or should_continue()
        return target_temp_c

    monkeypatch.setattr(web_module, "_filament_swap_start_background", fake_start_background)
    monkeypatch.setattr(web_module, "_wait_for_filament_swap_home", fake_home_wait)
    monkeypatch.setattr(web_module, "_wait_for_filament_swap_motion", fake_motion_wait)
    monkeypatch.setattr(web_module, "_wait_for_filament_swap_z_lift", fake_z_lift_wait)
    monkeypatch.setattr(web_module, "_wait_for_filament_service_nozzle_target", fake_target_wait)
    monkeypatch.setattr(web_module, "FILAMENT_SERVICE_SWAP_COOLDOWN_DELAY_S", 0)

    try:
        unload = app.filaments.create({"name": "PLA Black", "nozzle_temp_other_layer": 220, "retract_speed": 40})
        load = app.filaments.create({"name": "PETG White", "nozzle_temp_other_layer": 240, "retract_speed": 12})
        started = client.post(
            "/api/filaments/service/swap/start",
            json={
                "unload_profile_id": unload["id"],
                "load_profile_id": load["id"],
                "home_pause_s": 42,
            },
            headers={"X-Api-Key": API_KEY},
        )
        start_target, token = background_calls.pop()
        start_target(token)
        mismatch = client.post(
            "/api/filaments/service/swap/cancel",
            json={"token": "wrong-token"},
            headers={"X-Api-Key": API_KEY},
        )
        confirmed = client.post(
            "/api/filaments/service/swap/confirm",
            json={"token": token},
            headers={"X-Api-Key": API_KEY},
        )
        confirm_target, confirm_token = background_calls.pop()
        confirm_target(confirm_token)
        cancelled = client.post(
            "/api/filaments/service/swap/cancel",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    assert started.get_json()["pending"] is True
    assert started.get_json()["swap"]["mode"] == "legacy"
    assert mismatch.status_code == 409
    assert confirmed.status_code == 200
    assert confirmed.get_json()["pending"] is True
    assert cancelled.status_code == 200
    assert cancelled.get_json()["pending"] is False
    assert home_calls == ["z"]
    assert home_waits == [42.0]
    assert sent[0] == "M104 S180"
    assert sent[1] == "M104 S220"
    assert sent[2] == "M104 S220"
    assert sent[3] == "G91"
    assert sent[4] == "G1 Z50 F600"
    assert sent[5] == "M400"
    assert sent[6] == "G90"
    assert sent[7] == "M104 S220"
    assert "G1 E10 F240" in sent[8]
    assert "G1 E-55 F2000" in sent[9]
    assert sent[10] == "M104 S240"
    assert "G1 E65 F240" in sent[11]
    assert sent[12] == "M104 S0"
    assert target_waits == [220, 220, 220, 240]
    assert motion_waits == [(10, 240), (55, 2000), (65, 240)]
    assert z_lift_waits == [50.0]


def test_legacy_swap_sends_heat_before_homing_when_nozzle_is_cold(tmp_path, monkeypatch):
    sent = []
    home_calls = []
    home_waits = []
    wait_calls = []
    motion_waits = []
    z_lift_waits = []
    target_waits = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=25,
        send_gcode=lambda gcode: sent.append(gcode),
        send_home=lambda axis: home_calls.append(axis),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)
    app.config["config"].cfg.filament_service["allow_legacy_swap"] = True
    background_calls = []

    def fake_start_background(target, token, printer_index):
        background_calls.append((target, token))
        return SimpleNamespace()

    def fake_wait_for_nozzle(mqtt, target_temp_c, should_continue=None, tolerance_c=5):
        wait_calls.append((target_temp_c, tolerance_c))
        assert should_continue is None or should_continue()
        return target_temp_c

    def fake_motion_wait(length_mm, feedrate_mm_min, should_continue=None):
        motion_waits.append((length_mm, feedrate_mm_min))
        assert should_continue is None or should_continue()

    def fake_z_lift_wait(z_lift_mm, z_feedrate_mm_min=600, should_continue=None):
        z_lift_waits.append(z_lift_mm)
        assert should_continue is None or should_continue()

    def fake_home_wait(should_continue=None, pause_s=None):
        home_waits.append(pause_s)
        assert should_continue is None or should_continue()

    def fake_target_wait(mqtt, target_temp_c, should_continue=None):
        target_waits.append(target_temp_c)
        assert should_continue is None or should_continue()
        return target_temp_c

    monkeypatch.setattr(web_module, "_filament_swap_start_background", fake_start_background)
    monkeypatch.setattr(web_module, "_wait_for_filament_swap_home", fake_home_wait)
    monkeypatch.setattr(web_module, "_wait_for_filament_swap_motion", fake_motion_wait)
    monkeypatch.setattr(web_module, "_wait_for_filament_swap_z_lift", fake_z_lift_wait)
    monkeypatch.setattr(web_module, "_wait_for_filament_service_nozzle", fake_wait_for_nozzle)
    monkeypatch.setattr(web_module, "_wait_for_filament_service_nozzle_target", fake_target_wait)

    try:
        unload = app.filaments.create({"name": "PLA Cold", "nozzle_temp_other_layer": 220})
        load = app.filaments.create({"name": "PETG Cold", "nozzle_temp_other_layer": 240})
        started = client.post(
            "/api/filaments/service/swap/start",
            json={
                "unload_profile_id": unload["id"],
                "load_profile_id": load["id"],
            },
            headers={"X-Api-Key": API_KEY},
        )
        start_target, token = background_calls.pop()
        start_target(token)
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    assert sent[0] == "M104 S180"
    assert sent[1] == "M104 S220"
    assert sent[2] == "M104 S220"
    assert home_calls == ["z"]
    assert home_waits == [70.0]
    assert sent[3] == "G91"
    assert sent[4] == "G1 Z50 F600"
    assert sent[5] == "M400"
    assert sent[6] == "G90"
    assert sent[7] == "M104 S220"
    assert "G1 E10 F240" in sent[8]
    assert "G1 E-40 F2000" in sent[9]
    assert wait_calls == [(180, 0), (220, 5)]
    assert target_waits == [220, 220, 220]
    assert motion_waits == [(10.0, 240), (40.0, 2000)]
    assert z_lift_waits == [50.0]


def test_filament_swap_command_config_overrides_templates(tmp_path):
    mqtt = SimpleNamespace(is_printing=False, nozzle_temp=25, send_gcode=lambda gcode: None)
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)
    command_config = tmp_path / "filament_swap_commands.json"
    command_config.write_text(
        '{"commands":{"set_nozzle_temp":"M109 S{temp_c}","z_lift":"G1 Z{z_lift_mm} F1234"}}',
        encoding="utf-8",
    )

    try:
        nozzle_command = web_module._format_filament_swap_command("set_nozzle_temp", temp_c=220)
        lift_command = web_module._format_filament_swap_command(
            "z_lift",
            z_lift_mm="50",
            z_feedrate=600,
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert nozzle_command == "M109 S220"
    assert lift_command == "G1 Z50 F1234"


def test_legacy_swap_cancel_is_allowed_while_stage_is_running(tmp_path, monkeypatch):
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=25,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)
    app.config["config"].cfg.filament_service["allow_legacy_swap"] = True
    background_calls = []

    def fake_start_background(target, token, printer_index):
        background_calls.append((target, token))
        return SimpleNamespace()

    monkeypatch.setattr(web_module, "_filament_swap_start_background", fake_start_background)

    try:
        unload = app.filaments.create({"name": "PLA Running", "nozzle_temp_other_layer": 220})
        load = app.filaments.create({"name": "PETG Running", "nozzle_temp_other_layer": 240})
        started = client.post(
            "/api/filaments/service/swap/start",
            json={
                "unload_profile_id": unload["id"],
                "load_profile_id": load["id"],
            },
            headers={"X-Api-Key": API_KEY},
        )
        token = started.get_json()["swap"]["token"]
        cancelled = client.post(
            "/api/filaments/service/swap/cancel",
            json={"token": token},
            headers={"X-Api-Key": API_KEY},
        )
        state = client.get(
            "/api/filaments/service/swap",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    assert background_calls
    assert cancelled.status_code == 200
    assert cancelled.get_json()["pending"] is False
    assert state.status_code == 200
    assert state.get_json()["pending"] is False
    assert sent == ["M104 S0"]


def test_filament_service_preheat_clamps_absurd_profile_temp(tmp_path):
    sent = []
    mqtt = SimpleNamespace(is_printing=False, nozzle_temp=25, send_gcode=lambda gcode: sent.append(gcode))
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        # Simulate a legacy database row that predates store-level validation.
        with app.filaments._lock:
            with app.filaments._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO filaments (name, nozzle_temp_other_layer) VALUES (?, ?)",
                    ("Absurd", 999999),
                )
                profile_id = cur.lastrowid
        preheat = client.post(
            "/api/filaments/service/preheat",
            json={"profile_id": profile_id},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert preheat.status_code == 200
    assert preheat.get_json()["target_temp_c"] == 260
    assert sent == ["M104 S260"]


def test_filament_service_temp_clamps_to_printer_hotend_ceiling(tmp_path):
    mqtt = SimpleNamespace(is_printing=False, send_gcode=lambda gcode: None, nozzle_temp=220)
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        profile = {"nozzle_temp_other_layer": 290}
        standard = web_module._filament_service_temp(profile, printer_index=0)
        app.config["config"].cfg.filament_service["per_printer"]["SN1"] = {"all_metal_hotend": True}
        all_metal = web_module._filament_service_temp(profile, printer_index=0)
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert standard == 260
    assert all_metal == 290


def test_filaments_apply_route_uses_per_printer_nozzle_ceiling(tmp_path):
    sent = []
    mqtt = SimpleNamespace(is_printing=False, send_gcode=lambda gcode: sent.append(gcode), nozzle_temp=25)
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        # Simulate a legacy database row that predates store-level validation.
        with app.filaments._lock:
            with app.filaments._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO filaments (name, nozzle_temp_first_layer, bed_temp_first_layer) VALUES (?, ?, ?)",
                    ("High Temp", 290, 130),
                )
                profile_id = cur.lastrowid
        standard = client.post(
            f"/api/filaments/{profile_id}/apply",
            headers={"X-Api-Key": API_KEY},
        )
        app.config["config"].cfg.filament_service["per_printer"]["SN1"] = {"all_metal_hotend": True}
        all_metal = client.post(
            f"/api/filaments/{profile_id}/apply",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert standard.status_code == 200
    assert standard.get_json()["gcode"] == "M104 S260\nM140 S100"
    assert all_metal.status_code == 200
    assert all_metal.get_json()["gcode"] == "M104 S290\nM140 S100"
    assert sent == ["M104 S260\nM140 S100", "M104 S290\nM140 S100"]


def test_filament_swap_start_rejected_while_previous_thread_still_running(tmp_path, monkeypatch):
    sent = []
    mqtt = SimpleNamespace(is_printing=False, nozzle_temp=25, send_gcode=lambda gcode: sent.append(gcode))
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)
    app.config["config"].cfg.filament_service["allow_legacy_swap"] = True
    release = threading.Event()

    def fake_unload_worker(token):
        release.wait(timeout=10)

    monkeypatch.setattr(web_module, "_run_legacy_swap_unload", fake_unload_worker)
    monkeypatch.setattr(web_module, "FILAMENT_SWAP_CANCEL_JOIN_TIMEOUT_S", 0.05)

    try:
        unload = app.filaments.create({"name": "PLA Stale", "nozzle_temp_other_layer": 220})
        load = app.filaments.create({"name": "PETG Stale", "nozzle_temp_other_layer": 240})
        body = {"unload_profile_id": unload["id"], "load_profile_id": load["id"]}
        started = client.post(
            "/api/filaments/service/swap/start",
            json=body,
            headers={"X-Api-Key": API_KEY},
        )
        old_thread = app.filament_swap_threads.get(0)
        cancelled = client.post(
            "/api/filaments/service/swap/cancel",
            headers={"X-Api-Key": API_KEY},
        )
        rejected = client.post(
            "/api/filaments/service/swap/start",
            json=body,
            headers={"X-Api-Key": API_KEY},
        )
        release.set()
        old_thread.join(timeout=5)
        allowed = client.post(
            "/api/filaments/service/swap/start",
            json=body,
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    assert old_thread is not None
    assert cancelled.status_code == 200
    assert rejected.status_code == 409
    assert rejected.get_json()["error"] == "A filament swap is already in progress"
    assert allowed.status_code == 200


def test_legacy_swap_worker_unexpected_error_sets_error_phase_and_cools_nozzle(tmp_path, monkeypatch):
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=25,
        send_gcode=lambda gcode: sent.append(gcode),
        send_home=lambda axis: None,
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)
    app.config["config"].cfg.filament_service["allow_legacy_swap"] = True
    background_calls = []

    def fake_start_background(target, token, printer_index):
        background_calls.append((target, token))
        return SimpleNamespace()

    monkeypatch.setattr(web_module, "_filament_swap_start_background", fake_start_background)

    try:
        unload = app.filaments.create({"name": "PLA Crash", "nozzle_temp_other_layer": 220})
        load = app.filaments.create({"name": "PETG Crash", "nozzle_temp_other_layer": 240})
        started = client.post(
            "/api/filaments/service/swap/start",
            json={"unload_profile_id": unload["id"], "load_profile_id": load["id"]},
            headers={"X-Api-Key": API_KEY},
        )
        target, token = background_calls.pop()

        def boom(mqtt):
            # TypeError: unexpected type not handled by the specific except
            # clauses and not swallowed by borrow_mqtt's candidate loop.
            raise TypeError("mqtt object is malformed")

        monkeypatch.setattr(web_module, "_assert_filament_service_ready", boom)
        target(token)
        state = client.get(
            "/api/filaments/service/swap",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    swap = state.get_json()["swap"]
    assert swap["phase"] == "error"
    assert "mqtt object is malformed" in swap["error"]
    assert "M104 S0" in sent
