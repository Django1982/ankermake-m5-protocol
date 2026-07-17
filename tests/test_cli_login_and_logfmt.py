import logging
from pathlib import Path
from types import SimpleNamespace

import click
import os
from click.testing import CliRunner

import ankerctl
from cli.logfmt import ColorFormatter, ExitOnExceptionHandler
from libflagship.httpapi import APIError


class FakeConfigManager:
    def __init__(self, cfg=None):
        self._cfg = cfg

    def open(self):
        class _Ctx:
            def __enter__(inner_self):
                return self._cfg

            def __exit__(inner_self, exc_type, exc, tb):
                return False

        return _Ctx()


def test_config_decode_and_import_cli(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_config = FakeConfigManager(SimpleNamespace(account=None))
    imported = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.libflagship.logincache.load", lambda raw: {"data": {"auth_token": "abc", "email": "user@example.com"}})
    monkeypatch.setattr(
        "ankerctl.cli.config.import_config_from_server",
        lambda config, cache, insecure: imported.append((config, cache, insecure)),
    )

    login_file = tmp_path / "login.json"
    login_file.write_text('{"data": {"auth_token": "abc"}}')

    decode = runner.invoke(ankerctl.main, ["config", "decode", str(login_file)])
    imported_cli = runner.invoke(ankerctl.main, ["config", "import", str(login_file)])

    assert decode.exit_code == 0
    assert '"auth_token": "abc...<REDACTED>"' in decode.output
    assert imported_cli.exit_code == 0
    assert imported == [(fake_config, {"auth_token": "abc", "email": "user@example.com"}, False)]


def test_find_login_file_detects_darwin_and_windows_locations(tmp_path, monkeypatch):
    darwin_file = tmp_path / "login.json"
    darwin_file.write_bytes(b"darwin")
    monkeypatch.setattr("ankerctl.platform.system", lambda: "Darwin")
    monkeypatch.setattr("ankerctl.path.expanduser", lambda value: str(darwin_file))

    with ankerctl._find_login_file() as fh:
        assert fh.read() == b"darwin"

    windows_dir = tmp_path / "leveldb"
    windows_dir.mkdir()
    windows_file = windows_dir / "000005.ldb"
    windows_file.write_bytes(b"prefix vms-userinfo windows")
    monkeypatch.setattr("ankerctl.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "ankerctl.path.expandvars",
        lambda value: str(windows_dir) if "Local Storage\\leveldb" in value else str(tmp_path / "missing"),
    )
    monkeypatch.setattr("ankerctl.path.isdir", lambda value: Path(value) == windows_dir)
    monkeypatch.setattr("ankerctl.path.isfile", lambda value: Path(value) == windows_file)
    monkeypatch.setattr(os, "listdir", lambda value: ["000005.ldb"] if Path(value) == windows_dir else [])

    with ankerctl._find_login_file() as fh:
        assert b"windows" in fh.read()


def test_find_login_file_prefers_newer_userinfo_cache(tmp_path, monkeypatch):
    windows_dir = tmp_path / "leveldb"
    windows_dir.mkdir()
    older = windows_dir / "000005.ldb"
    older.write_bytes(b"prefix vms-userinfo windows")
    newer = windows_dir / "000123.ldb"
    newer.write_bytes(b"prefix userinfo windows")

    monkeypatch.setattr("ankerctl.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "ankerctl.path.expandvars",
        lambda value: str(windows_dir) if "Local Storage\\leveldb" in value else str(tmp_path / "missing"),
    )
    monkeypatch.setattr("ankerctl.path.isdir", lambda value: Path(value) == windows_dir)
    monkeypatch.setattr("ankerctl.path.isfile", lambda value: Path(value) in {older, newer})
    monkeypatch.setattr(os, "listdir", lambda value: ["000005.ldb", "000123.ldb"] if Path(value) == windows_dir else [])

    with ankerctl._find_login_file() as fh:
        assert b"userinfo" in fh.read()


def test_config_login_retries_with_captcha_and_imports(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager(SimpleNamespace(account=None))
    fetch_calls = []
    imported = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.webbrowser.open", lambda url, new=0: True)

    def fake_fetch(email, password, region, insecure, captcha_id=None, captcha_answer=None):
        fetch_calls.append((email, password, region, captcha_id, captcha_answer))
        if len(fetch_calls) == 1:
            raise APIError(
                "captcha required",
                json={"data": {"captcha_id": "cap-1", "item": "https://captcha.example/img"}},
            )
        return {"auth_token": "abc", "ab_code": "DE"}

    monkeypatch.setattr("ankerctl.cli.config.fetch_config_by_login", fake_fetch)
    monkeypatch.setattr(
        "ankerctl.cli.config.import_config_from_server",
        lambda config, login, insecure: imported.append((config, login, insecure)),
    )

    result = runner.invoke(
        ankerctl.main,
        ["config", "login", "DE", "user@example.com", "pw"],
        input="captcha-answer\n",
    )

    assert result.exit_code == 0
    assert len(fetch_calls) == 2
    assert fetch_calls[1][3:] == ("cap-1", "captcha-answer")
    assert imported == [(fake_config, {"auth_token": "abc", "ab_code": "DE"}, False)]


def test_color_formatter_and_exit_handler_behaviour(monkeypatch):
    formatter = ColorFormatter("%(message)s")
    record = logging.LogRecord("test", logging.WARNING, __file__, 1, "careful", (), None)
    formatted = formatter.format(record)

    calls = []
    monkeypatch.setattr(logging.StreamHandler, "emit", lambda self, record: calls.append(record.levelno))
    handler = ExitOnExceptionHandler()

    handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "ok", (), None))
    assert calls == [logging.INFO]

    try:
        handler.emit(logging.LogRecord("test", logging.CRITICAL, __file__, 1, "boom", (), None))
    except SystemExit as exc:
        assert exc.code == 127
    else:
        raise AssertionError("critical log should exit")

    assert click.unstyle(formatted) == "[W] careful"


def test_setup_logging_creates_log_files_and_startup_entries(monkeypatch, tmp_path):
    from cli import logfmt

    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    root.handlers = []

    def fake_basic_config(*, handlers=None, level=None):
        root.handlers = list(handlers or [])
        root.setLevel(level)

    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)

    try:
        logger = logfmt.setup_logging(level=logging.INFO, log_dir=str(tmp_path))
        logger.info("root-message")
        logging.getLogger("mqtt").info("mqtt-message")
    finally:
        for handler in root.handlers:
            try:
                handler.close()
            except Exception:
                pass
        root.handlers = old_handlers
        root.setLevel(old_level)

    assert (tmp_path / "ankerctl.log").exists()
    assert (tmp_path / "mqtt.log").exists()
    assert "log initialized" in (tmp_path / "ankerctl.log").read_text()
    assert "mqtt-message" in (tmp_path / "mqtt.log").read_text()
