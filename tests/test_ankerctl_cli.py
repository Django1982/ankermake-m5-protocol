import json
from types import SimpleNamespace

from click.testing import CliRunner

import ankerctl
import cli.mqtt
from libflagship.mqtt import MqttMsgType


class FakeConfigManager:
    def __init__(self):
        self.api_keys = []
        self.removed = 0

    def set_api_key(self, key):
        self.api_keys.append(key)

    def remove_api_key(self):
        self.removed += 1


def test_main_configures_logging_and_skips_upgrade_for_http(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    logging_calls = []
    upgrades = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: logging_calls.append((level, log_dir)))
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: upgrades.append("upgrade"))
    monkeypatch.setattr("ankerctl.libflagship.seccode.calc_check_code", lambda duid, mac: "CODE123")

    result = runner.invoke(
        ankerctl.main,
        ["-v", "--printer", "2", "http", "calc-check-code", "DUID", "11:22:33:44:55:66"],
    )

    assert result.exit_code == 0
    assert "check_code: CODE123" in result.output
    assert logging_calls and logging_calls[0][0] == 10
    assert upgrades == []


def test_mqtt_send_blocks_dangerous_commands_without_force(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    opened = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: opened.append(True))

    result = runner.invoke(
        ankerctl.main,
        ["mqtt", "send", "ZZ_MQTT_CMD_RECOVER_FACTORY"],
    )

    assert result.exit_code == 1
    assert opened == []


def test_mqtt_monitor_can_subscribe_to_command_topics(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = SimpleNamespace(subscribed=False, wildcard=None)

    def subscribe_device_topics(wildcard=False):
        fake_client.subscribed = True
        fake_client.wildcard = wildcard
        return ["/device/maker/SN123/command", "/device/maker/SN123/query"]

    def fetchloop():
        yield (
            SimpleNamespace(topic="/device/maker/SN123/command", payload=b"payload"),
            [{"commandType": 1026, "axis": "xy"}],
        )

    fake_client.subscribe_device_topics = subscribe_device_topics
    fake_client.fetchloop = fetchloop

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)

    result = runner.invoke(ankerctl.main, ["mqtt", "monitor", "--command-topics"])

    assert result.exit_code == 0
    assert fake_client.subscribed is True
    assert fake_client.wildcard is False
    assert "[1026] move_zero" in result.output
    assert "{'axis': 'xy'}" in result.output


def test_mqtt_monitor_can_sniff_wildcard_command_topics(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = SimpleNamespace(subscribed=False, wildcard=None)

    def subscribe_device_topics(wildcard=False):
        fake_client.subscribed = True
        fake_client.wildcard = wildcard
        return ["/device/maker/SN123/command", "/device/maker/SN123/query", "/device/maker/SN123/#"]

    def fetchloop():
        yield (
            SimpleNamespace(topic="/device/maker/SN123/command", payload=b"payload"),
            [{"commandType": 1025, "value": 3}],
        )

    fake_client.subscribe_device_topics = subscribe_device_topics
    fake_client.fetchloop = fetchloop

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)

    result = runner.invoke(ankerctl.main, ["mqtt", "monitor", "--sniff-topics"])

    assert result.exit_code == 0
    assert fake_client.subscribed is True
    assert fake_client.wildcard is True
    assert ankerctl.mqtt_topic_direction("/device/maker/SN123/command") == "app->printer"
    assert "[1025] move_direction" in result.output


def test_mqtt_file_list_probe_defaults_to_onboard_and_collects_replies(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = object()
    calls = []

    def mqtt_collect_command(client, msg, timeout, collect_window):
        calls.append((client, msg, timeout, collect_window))
        return [{"commandType": msg["commandType"], "value": msg["value"], "files": ["local.gcode"]}]

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_collect_command", mqtt_collect_command)

    result = runner.invoke(ankerctl.main, ["mqtt", "file-list-probe"])

    assert result.exit_code == 0
    assert calls == [
        (
            fake_client,
            {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_FILE_LIST_REQUEST.value,
                "value": 1,
            },
            10.0,
            3.0,
        )
    ]
    assert "Probing file list with value=1 (printer/onboard)" in result.output
    assert '"reply_count": 1' in result.output
    assert "local.gcode" in result.output


def test_mqtt_file_list_probe_uses_usb_default_value_and_allows_override(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = object()
    calls = []

    def mqtt_collect_command(client, msg, timeout, collect_window):
        calls.append((client, msg, timeout, collect_window))
        return [{"commandType": msg["commandType"], "value": msg["value"], "files": ["usb.gcode"]}]

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_collect_command", mqtt_collect_command)

    usb_result = runner.invoke(ankerctl.main, ["mqtt", "file-list-probe", "--source", "usb"])
    override_result = runner.invoke(
        ankerctl.main,
        ["mqtt", "file-list-probe", "--source", "usb", "--value", "2", "--timeout", "5", "--window", "1.5"],
    )

    assert usb_result.exit_code == 0
    assert override_result.exit_code == 0
    assert calls == [
        (
            fake_client,
            {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_FILE_LIST_REQUEST.value,
                "value": 0,
            },
            10.0,
            3.0,
        ),
        (
            fake_client,
            {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_FILE_LIST_REQUEST.value,
                "value": 2,
            },
            5.0,
            1.5,
        ),
    ]
    assert "value=0 (usb/thumb drive candidate)" in usb_result.output
    assert "value=2 (usb/thumb drive candidate)" in override_result.output


def test_parse_file_list_replies_filters_mismatched_storage_paths():
    result = cli.mqtt.parse_file_list_replies(
        [{
            "commandType": 1009,
            "fileLists": json.dumps([
                {"name": "cube.gcode", "path": "/usr/data/local/model/cube.gcode", "timestamp": 123},
                {"name": "usb.gcode", "path": "/tmp/udisk/udisk1/usb.gcode", "timestamp": 456},
            ]),
        }],
        requested_source="onboard",
    )

    assert result["reply_count"] == 1
    assert result["files"] == [
        {
            "name": "cube.gcode",
            "path": "/usr/data/local/model/cube.gcode",
            "timestamp": 123,
            "source": "onboard",
        }
    ]


def test_http_calc_sec_code_and_webserver_run_dispatch(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    webserver_calls = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.libflagship.seccode.create_check_code_v1", lambda duid, mac: (123, "SEC456"))
    monkeypatch.setattr("web.webserver", lambda config, printer_index, host, port, insecure, **kwargs: webserver_calls.append((config, printer_index, host, port, insecure, kwargs)))

    sec_result = runner.invoke(
        ankerctl.main,
        ["http", "calc-sec-code", "DUID", "11:22:33:44:55:66"],
    )
    run_result = runner.invoke(
        ankerctl.main,
        ["--insecure", "--pppp-dump", "trace.log", "--printer", "1", "webserver", "run", "--host", "0.0.0.0", "--port", "7788"],
    )

    assert sec_result.exit_code == 0
    assert "sec_ts:   123" in sec_result.output
    assert "sec_code: SEC456" in sec_result.output
    assert run_result.exit_code == 0
    assert webserver_calls == [
        (fake_config, 1, "0.0.0.0", 7788, True, {"pppp_dump": "trace.log"})
    ]


def test_webserver_run_forwards_mqtt_ca_cert_when_configured(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    webserver_calls = []
    ca_cert = tmp_path / "ca-cert.pem"
    ca_cert.write_text("test-ca")

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr(
        "web.webserver",
        lambda config, printer_index, host, port, insecure, **kwargs: webserver_calls.append(
            (config, printer_index, host, port, insecure, kwargs)
        ),
    )

    result = runner.invoke(
        ankerctl.main,
        [
            "--mqtt-ca-cert",
            str(ca_cert),
            "--pppp-dump",
            "trace.log",
            "webserver",
            "run",
        ],
    )

    assert result.exit_code == 0
    assert webserver_calls == [
        (
            fake_config,
            0,
            "127.0.0.1",
            4470,
            False,
            {"pppp_dump": "trace.log", "mqtt_ca_cert": str(ca_cert)},
        )
    ]


def test_config_password_commands_validate_generate_and_remove(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.cli.config.validate_api_key", lambda key: (False, "bad key"))
    monkeypatch.setattr("ankerctl.secrets.token_hex", lambda size: "RANDOMKEY1234567890")

    invalid = runner.invoke(ankerctl.main, ["config", "set-password", "bad"])
    generated = runner.invoke(ankerctl.main, ["config", "set-password"])
    removed = runner.invoke(ankerctl.main, ["config", "remove-password"])

    assert invalid.exit_code == 1
    assert generated.exit_code == 0
    assert removed.exit_code == 0
    assert fake_config.api_keys == ["RANDOMKEY1234567890"]
    assert fake_config.removed == 1


class FakeConfigContext:
    """Minimal config manager for testing mqtt_open/pppp_open bounds checks."""
    def __init__(self, printers):
        self._printers = printers

    def open(self):
        return self

    def __enter__(self):
        return SimpleNamespace(
            printers=self._printers,
            account=SimpleNamespace(region="eu", auth_token="fake", user_id="fake"),
        )

    def __exit__(self, *args):
        pass


def test_mqtt_open_raises_on_invalid_printer_index():
    import cli.mqtt
    config = FakeConfigContext(printers=[])
    import pytest
    with pytest.raises(ValueError, match="out of range"):
        cli.mqtt.mqtt_open(config, printer_index=0, insecure=False)


def test_pppp_open_raises_on_invalid_printer_index():
    import cli.pppp
    config = FakeConfigContext(printers=[])
    import pytest
    with pytest.raises(ValueError, match="out of range"):
        cli.pppp.pppp_open(config, printer_index=0)


def test_mqtt_open_raises_on_negative_printer_index():
    """Negative indices must not silently select the last printer via
    Python's negative indexing."""
    import cli.mqtt
    config = FakeConfigContext(printers=[SimpleNamespace(name="p0"), SimpleNamespace(name="p1")])
    import pytest
    with pytest.raises(ValueError, match="out of range"):
        cli.mqtt.mqtt_open(config, printer_index=-1, insecure=False)


def test_pppp_open_raises_on_negative_printer_index():
    import cli.pppp
    config = FakeConfigContext(printers=[SimpleNamespace(name="p0"), SimpleNamespace(name="p1")])
    import pytest
    with pytest.raises(ValueError, match="out of range"):
        cli.pppp.pppp_open(config, printer_index=-1)


def test_printer_option_rejects_negative_values():
    """click IntRange(min=0) rejects --printer -1 at argument parse time."""
    runner = CliRunner()
    result = runner.invoke(ankerctl.main, ["--printer", "-1", "config", "show"])
    assert result.exit_code != 0
    assert "is not in the range" in result.output or "Invalid value" in result.output


def test_pppp_print_file_stops_session_on_invalid_upload_rate(monkeypatch, tmp_path):
    """Regression: api.stop() must run even when --upload-rate-mbps
    validation fails, so the PPPP session isn't leaked."""
    runner = CliRunner()
    fake_config = FakeConfigContext(printers=[SimpleNamespace(name="p0")])
    opens = []
    stops = []

    class FakeApi:
        def stop(self):
            stops.append(True)

    def fake_pppp_open(config, printer_index, dumpfile=None):
        opens.append(True)
        return FakeApi()

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.pppp.pppp_open", fake_pppp_open)

    gcode = tmp_path / "job.gcode"
    gcode.write_bytes(b"G28\n")

    result = runner.invoke(
        ankerctl.main,
        ["pppp", "print-file", str(gcode), "--upload-rate-mbps", "7"],
    )

    assert result.exit_code != 0
    assert len(stops) == len(opens)


def test_pppp_capture_video_stops_session_after_capture(monkeypatch, tmp_path):
    """Regression: capture-video must call api.stop() after capture completes."""
    runner = CliRunner()
    fake_config = FakeConfigContext(printers=[SimpleNamespace(name="p0")])
    sends = []
    stops = []

    timeouts = []

    class FakeApi:
        def send_xzyh(self, data, cmd=None, timeout=None):
            sends.append(json.loads(data))
            timeouts.append(timeout)

        def recv_xzyh(self, chan=None):
            return SimpleNamespace(data=b"x" * 2048)

        def stop(self):
            stops.append(True)

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.pppp.pppp_open", lambda *args, **kwargs: FakeApi())

    out = tmp_path / "out.h264"
    result = runner.invoke(
        ankerctl.main,
        ["pppp", "capture-video", "--max-size", "1kb", str(out)],
    )

    assert result.exit_code == 0
    assert stops == [True]
    assert len(sends) == 2  # START_LIVE + CLOSE_LIVE
    # Regression: both control-command sends must pass an explicit timeout,
    # otherwise a silently-dropped ACK blocks Channel.write() forever (same
    # bug class as the file-upload handshake hang).
    assert all(t is not None for t in timeouts)
    assert out.read_bytes() == b"x" * 2048


def test_mqtt_rename_printer_rejects_empty_name(monkeypatch):
    """Regression: an empty printer name must be rejected before connecting."""
    runner = CliRunner()
    fake_config = FakeConfigManager()
    opened = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: opened.append(True))

    result = runner.invoke(ankerctl.main, ["mqtt", "rename-printer", ""])

    assert result.exit_code == 1
    assert opened == []


def test_config_decode_redacts_auth_token_by_default(monkeypatch, tmp_path):
    """Regression: full auth_token/user_id must not appear in default output."""
    runner = CliRunner()
    fake_config = FakeConfigManager()
    cache = {
        "auth_token": "SECRETTOKEN1234567890",
        "user_id": "USERID9876543210",
        "email": "user@example.com",
    }

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.libflagship.logincache.load", lambda data: {"data": dict(cache)})

    login = tmp_path / "login.json"
    login.write_bytes(b"irrelevant")

    redacted = runner.invoke(ankerctl.main, ["config", "decode", str(login)])
    full = runner.invoke(ankerctl.main, ["config", "decode", "--show-secrets", str(login)])

    assert redacted.exit_code == 0
    assert "SECRETTOKEN1234567890" not in redacted.output
    assert "USERID9876543210" not in redacted.output
    assert "SECRETTOKE...<REDACTED>" in redacted.output
    assert "USERID9876...<REDACTED>" in redacted.output
    assert "user@example.com" in redacted.output

    assert full.exit_code == 0
    assert "SECRETTOKEN1234567890" in full.output


def test_config_login_warns_when_password_passed_positionally(monkeypatch, caplog):
    """Regression: a warning must be logged when password is passed positionally."""
    import logging

    runner = CliRunner()
    fake_config = FakeConfigContext(printers=[])
    imported = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr(
        "ankerctl.cli.config.fetch_config_by_login",
        lambda email, password, region, insecure, captcha_id=None, captcha_answer=None: {"auth_token": "abc"},
    )
    monkeypatch.setattr(
        "ankerctl.cli.config.import_config_from_server",
        lambda config, login, insecure: imported.append(login),
    )

    with caplog.at_level(logging.WARNING, logger="main"):
        result = runner.invoke(
            ankerctl.main,
            ["config", "login", "DE", "user@example.com", "somepassword"],
        )

    assert result.exit_code == 0
    assert imported == [{"auth_token": "abc"}]
    assert any("shell history" in record.message for record in caplog.records)
