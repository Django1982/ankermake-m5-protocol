import base64
import json
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from cli.config import (
    AnkerConfigManager,
    import_config_from_server,
    merge_config_preferences,
    resolve_api_key,
    update_empty_printer_ips,
    validate_api_key,
)
from cli.model import Account, Config, Printer
from libflagship import logincache


def _sample_printer(sn="SN-1", ip_addr=""):
    return Printer(
        id="printer-1",
        sn=sn,
        name="Printer",
        model="V8111",
        create_time=datetime(2024, 1, 1, 12, 0, 0),
        update_time=datetime(2024, 1, 1, 12, 30, 0),
        wifi_mac="aabbccddeeff",
        ip_addr=ip_addr,
        mqtt_key=b"\x01\x02",
        api_hosts=["api.example"],
        p2p_hosts=["p2p.example"],
        p2p_duid="duid-1",
        p2p_key="secret",
    )


def _sample_config(**overrides):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="printer@example.com",
        ),
        printers=[_sample_printer()],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_validate_api_key_checks_length_and_charset():
    assert validate_api_key("valid_key-123456")[0] is True
    assert validate_api_key("short")[0] is False
    assert validate_api_key("bad key with spaces")[0] is False


def test_resolve_api_key_prefers_environment(monkeypatch):
    monkeypatch.setenv("ANKERCTL_API_KEY", "valid_key-123456")
    config = SimpleNamespace(get_api_key=lambda: "from-config")

    assert resolve_api_key(config) == "valid_key-123456"


def test_resolve_api_key_exits_on_invalid_environment_value(monkeypatch):
    monkeypatch.setenv("ANKERCTL_API_KEY", "bad")
    config = SimpleNamespace(get_api_key=lambda: "from-config")

    with pytest.raises(SystemExit):
        resolve_api_key(config)


def test_merge_config_preferences_preserves_user_settings():
    existing = _sample_config(
        upload_rate_mbps=50,
        notifications={
            "apprise": {
                "enabled": True,
                "server_url": "https://notify.example",
                "events": {
                    "print_started": False,
                },
            }
        },
    )
    new_config = _sample_config(upload_rate_mbps=10, notifications={})

    merged = merge_config_preferences(existing, new_config)

    assert merged.upload_rate_mbps == 50
    assert merged.notifications["apprise"]["enabled"] is True
    assert merged.notifications["apprise"]["server_url"] == "https://notify.example"
    assert merged.notifications["apprise"]["events"]["print_finished"] is True
    assert merged.notifications["apprise"]["events"]["print_started"] is False


def test_update_empty_printer_ips_only_fills_missing_values():
    cfg = _sample_config()
    cfg.printers.append(_sample_printer(sn="SN-2", ip_addr="192.168.1.25"))

    @contextmanager
    def modify():
        yield cfg

    manager = SimpleNamespace(modify=modify)

    update_empty_printer_ips(manager, {"SN-1": "192.168.1.10", "SN-2": "192.168.1.20"})

    assert cfg.printers[0].ip_addr == "192.168.1.10"
    assert cfg.printers[1].ip_addr == "192.168.1.25"


def test_config_manager_round_trips_serialized_config(tmp_path):
    dirs = SimpleNamespace(user_config_path=tmp_path)
    manager = AnkerConfigManager(dirs, classes=(Config, Account, Printer))
    cfg = _sample_config()

    manager.save("default", cfg)
    loaded = manager.load("default", None)

    assert isinstance(loaded, Config)
    assert loaded.account.email == "printer@example.com"
    assert loaded.printers[0].mqtt_key == b"\x01\x02"


def test_config_manager_load_recovers_from_corrupt_json(tmp_path):
    dirs = SimpleNamespace(user_config_path=tmp_path)
    manager = AnkerConfigManager(dirs, classes=(Config, Account, Printer))
    (tmp_path / "default.json").write_text('{"account": {"__type__": "Account", "auth_token"')

    loaded = manager.load("default", "fallback-value")

    assert loaded == "fallback-value"
    remaining = list(tmp_path.iterdir())
    assert not (tmp_path / "default.json").exists()
    assert any(p.name.startswith("default.json.corrupt-") for p in remaining)


def test_config_manager_save_is_atomic_and_leaves_no_tmp_file(tmp_path):
    dirs = SimpleNamespace(user_config_path=tmp_path)
    manager = AnkerConfigManager(dirs, classes=(Config, Account, Printer))
    manager.save("default", _sample_config(upload_rate_mbps=10))

    manager.save("default", _sample_config(upload_rate_mbps=99))

    reloaded = manager.load("default", None)
    assert reloaded.upload_rate_mbps == 99
    assert [p.name for p in tmp_path.iterdir()] == ["default.json"]


def test_logincache_load_parses_eufymake_webview_blob():
    token = "f88b0074484d9fd9dfcda327ef2b4dd28b4511235c0514df"
    token_fragment = base64.b64encode(f"{token}%".encode()).decode().rstrip("=")
    region_fragment = base64.b64encode(b"%22ab_code%22%3A%22US%22").decode().rstrip("=")
    blob = (
        b"noise-before"
        b"vms-userinfo"
        b"\x00"
        + b"2"
        + token_fragment.encode()
        + b"\x00"
        + region_fragment.encode()
        + b"\x00noise-after"
    )

    parsed = logincache.load(blob)

    assert parsed["data"]["auth_token"] == token
    assert parsed["data"]["ab_code"] == "US"


def test_logincache_load_parses_eufymake_webview_userinfo_blob():
    payload = {
        "user_id": "user-123",
        "email": "printer@example.com",
        "auth_token": "a932208fb65ff1fab8296b2a099cc8d2b74e910ff85e7378",
        "ab_code": "US",
    }
    encoded = base64.b64encode(urllib.parse.quote(json.dumps(payload, separators=(",", ":"))).encode()).decode()
    blob = b"prefix userinfo\x01" + encoded.encode() + b"\x00noise-after"

    parsed = logincache.load(blob)

    assert parsed["data"]["auth_token"] == payload["auth_token"]
    assert parsed["data"]["ab_code"] == "US"
    assert parsed["data"]["email"] == "printer@example.com"


def test_import_config_recovers_truncated_webview_auth_token(monkeypatch):
    short_token = "88b0074484d9fd9dfcda327ef2b4dd28b4511235c0514df"
    expected_token = f"f{short_token}"
    cfg = _sample_config()
    load_calls = []
    saved = []

    manager = SimpleNamespace(
        load=lambda name, default=None: None,
        save=lambda name, value: saved.append((name, value)),
    )

    monkeypatch.setattr(
        "cli.config.load_config_from_api",
        lambda auth_token, region, insecure: (
            load_calls.append((auth_token, region, insecure)),
            cfg if auth_token == expected_token else (_ for _ in ()).throw(Exception("bad token"))
        )[1],
    )
    monkeypatch.setattr("cli.config.get_printer_ips", lambda config: {})
    monkeypatch.setattr("cli.config.merge_config_preferences", lambda existing, new_config: new_config)
    monkeypatch.setattr("cli.config.update_empty_printer_ips", lambda config, printer_ips: None)

    import_config_from_server(
        manager,
        {"auth_token": short_token, "ab_code": "US"},
        False,
    )

    assert load_calls[0][0] == short_token
    assert load_calls[-1][0] == expected_token
    assert saved and saved[0][0] == "default"
    assert saved[0][1].account.auth_token == expected_token
