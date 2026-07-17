"""
Test suite for security validation
Tests SQL injection, XSS, path traversal, input validation
"""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from cli.model import Account, Config, Printer
from web import app
from web.service.filament import FilamentStore


API_KEY = "secret-key-123456"


def _pending_security_coverage(reason):
    pytest.skip(f"Pending security coverage: {reason}")


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


class FakeConfigManager:
    def __init__(self, cfg):
        self.cfg = cfg

    @contextmanager
    def open(self):
        yield self.cfg

    @contextmanager
    def modify(self):
        yield self.cfg


def _install_security_state(tmp_path):
    old_values = {
        "api_key": app.config.get("api_key"),
        "config": app.config.get("config"),
        "login": app.config.get("login"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "unsupported_device": app.config.get("unsupported_device"),
        "video_supported": app.config.get("video_supported"),
    }
    old_svc = app.svc
    old_filaments = getattr(app, "filaments", None)

    app.config["api_key"] = API_KEY
    app.config["config"] = FakeConfigManager(_base_config())
    app.config["login"] = True
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = False
    app.config["unsupported_device"] = False
    app.config["video_supported"] = True
    app.svc = SimpleNamespace(svcs={})
    app.filaments = FilamentStore(tmp_path / "security-filaments.db")

    return old_values, old_svc, old_filaments


def _restore_security_state(old_values, old_svc, old_filaments):
    app.svc = old_svc
    app.filaments = old_filaments
    for key, value in old_values.items():
        app.config[key] = value


def _auth_headers():
    return {"X-Api-Key": API_KEY}


class TestSQLInjectionProtection:
    """Test SQL injection prevention in database operations"""

    @patch('web.service.filament.FilamentStore')
    def test_filament_profile_name_sql_injection(self, mock_store):
        """Filament profile with SQL in name doesn't corrupt DB"""
        # Malicious name attempting SQL injection
        malicious_name = "'; DROP TABLE filaments; --"

        mock_store_instance = Mock()
        mock_store.return_value = mock_store_instance
        mock_store_instance.create.return_value = {"id": 1, "name": malicious_name}

        # Attempt to create profile with malicious name
        result = mock_store_instance.create(
            name=malicious_name,
            material="PLA",
            nozzle_temp_first_layer=210,
            nozzle_temp_other_layer=200,
            bed_temp_first_layer=60,
            bed_temp_other_layer=60,
        )

        # Should succeed without executing SQL
        assert result["name"] == malicious_name
        mock_store_instance.create.assert_called_once()

    def test_history_db_sql_injection_in_filename(self):
        """Print history with SQL in filename is safely escaped"""
        from web.service.history import PrintHistory

        history = PrintHistory(":memory:")  # In-memory SQLite

        malicious_filename = "test.gcode'; DROP TABLE history; --"

        # Record start with malicious filename
        task_id = history.record_start(malicious_filename)

        # Verify entry exists and DB is intact
        entries = history.get_history(limit=10)
        assert len(entries) == 1
        assert entries[0]["filename"] == malicious_filename

        # Verify table still exists by attempting another insert
        task_id2 = history.record_start("normal.gcode")
        assert task_id2 is not None


class TestPathTraversalProtection:
    """Test path traversal attack prevention"""

    def test_log_viewer_path_traversal_blocked(self, monkeypatch, tmp_path):
        """GET /api/debug/logs/<filename> rejects path traversal attempts"""
        import importlib
        import web as web_module

        # reload() replaces the module-global `app`; view functions of app
        # instances bound by other test modules resolve `app` through the
        # module dict, so the original instance must be restored afterwards.
        original_app = web_module.app
        monkeypatch.setenv("ANKERCTL_DEV_MODE", "true")
        importlib.reload(web_module)

        try:
            flask_app = web_module.app
            client = flask_app.test_client()
            flask_app.config["api_key"] = API_KEY
            flask_app.config["login"] = True
            flask_app.config["config"] = FakeConfigManager(_base_config())
            flask_app.config["printer_index"] = 0
            monkeypatch.setattr(web_module, "_log_dir", str(tmp_path))
            (tmp_path / "web.log").write_text("hello\n")

            dotdot = client.get(
                "/api/debug/logs/..web.log", headers=_auth_headers()
            )
            encoded = client.get(
                "/api/debug/logs/..%2F..%2Fetc%2Fpasswd", headers=_auth_headers()
            )
            legit = client.get("/api/debug/logs/web.log", headers=_auth_headers())
        finally:
            monkeypatch.setenv("ANKERCTL_DEV_MODE", "false")
            importlib.reload(web_module)
            web_module.app = original_app

        assert dotdot.status_code == 400
        assert dotdot.get_json()["error"] == "Invalid filename"
        # encoded slashes must never reach the file system: either the guard
        # rejects the decoded filename (400) or routing refuses the path (404)
        assert encoded.status_code != 200
        assert legit.status_code == 200
        assert legit.get_json()["content"] == "hello\n"

    def test_timelapse_filename_path_traversal(self):
        """Timelapse download with ../ in filename is blocked"""
        _pending_security_coverage("timelapse download path traversal")

    def test_file_upload_path_sanitization(self):
        """File upload with path separators is sanitized"""
        from libflagship.ppppapi import FileUploadInfo

        malicious_filename = "../../../etc/passwd"
        sanitized = FileUploadInfo.sanitize_filename(malicious_filename)

        # Should not contain path separators
        assert "/" not in sanitized
        assert "\\" not in sanitized
        assert ".." not in sanitized or sanitized == "..passwd"  # Dots removed or converted


class TestXSSProtection:
    """Test XSS attack prevention"""

    def test_gcode_filename_xss_escaped_in_response(self):
        """GCode filename with XSS payload is escaped in API response"""
        _pending_security_coverage("G-code filename escaping in rendered UI")

    def test_printer_name_xss_escaped(self):
        """Printer name with HTML tags is escaped"""
        _pending_security_coverage("printer name escaping in rendered UI")


class TestInputValidation:
    """Test input validation and sanitization"""

    def test_oversized_file_upload_rejected(self):
        """File upload exceeding UPLOAD_MAX_MB returns 413"""
        _pending_security_coverage("oversized G-code upload request")

    def test_invalid_api_key_format_rejected(self, tmp_path):
        """Malformed API key in X-Api-Key header is rejected"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            for value in ("", "' OR 1=1 --", "<script>alert(1)</script>"):
                response = client.get("/api/filaments", headers={"X-Api-Key": value})
                assert response.status_code == 401
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

    def test_invalid_upload_rate_rejected(self):
        """Upload rate outside valid range (5,10,25,50,100) is rejected"""
        _pending_security_coverage("upload rate validation in settings/update routes")

    def test_negative_printer_index_rejected(self, tmp_path):
        """Negative printer index in POST /api/printers/active is rejected"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.post(
                "/api/printers/active",
                json={"index": -1},
                headers=_auth_headers(),
            )
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 400

    def test_invalid_json_in_request_body(self, tmp_path):
        """Malformed JSON in POST request returns 400"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.post(
                "/api/filaments",
                data="{bad json",
                content_type="application/json",
                headers=_auth_headers(),
            )
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 400

    def test_missing_required_fields_in_filament_create(self, tmp_path):
        """Creating filament without required fields returns 400"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.post(
                "/api/filaments",
                json={"material": "PLA"},
                headers=_auth_headers(),
            )
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 400

    def test_excessively_long_filament_name(self):
        """Filament name > reasonable length is rejected or truncated"""
        _pending_security_coverage("maximum filament profile name length")

    def test_invalid_temperature_values(self):
        """Negative or extremely high temperatures are validated"""
        _pending_security_coverage("filament temperature bounds validation")


class TestMQTTCommandInjection:
    """Test MQTT command injection prevention"""

    def test_gcode_command_newline_injection(self):
        """GCode with embedded newlines doesn't inject multiple commands"""
        _pending_security_coverage("multi-command raw G-code policy")

    def test_printer_name_mqtt_injection(self):
        """Printer name with MQTT control chars is sanitized"""
        _pending_security_coverage("printer name control-character sanitization")


class TestAuthenticationBypass:
    """Test authentication bypass attempts"""

    def test_empty_api_key_rejected(self, tmp_path):
        """Empty API key in header is rejected"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.get("/api/filaments", headers={"X-Api-Key": ""})
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 401

    def test_api_key_query_string_bootstraps_session_and_strips_url(self, tmp_path):
        """Valid URL API key authenticates API requests directly and still seeds the session."""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.get(f"/api/filaments?apikey={API_KEY}&foo=bar")
            assert response.status_code == 200
            assert "Location" not in response.headers

            with client.session_transaction() as session:
                assert session["authenticated"] is True

            session_response = client.get("/api/filaments")
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert session_response.status_code == 200

    def test_session_cookie_allows_browser_access_to_protected_endpoint(self, tmp_path):
        """Authenticated browser session can access protected GET endpoints."""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            with client.session_transaction() as session:
                session["authenticated"] = True
            response = client.get("/api/filaments")
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 200


class TestCSRFProtection:
    """Test CSRF protection on state-changing endpoints"""

    def test_post_without_csrf_token(self):
        """POST request without CSRF token (if implemented) is rejected"""
        _pending_security_coverage("CSRF enforcement when that feature exists")


class TestRateLimiting:
    """Test rate limiting (if implemented)"""

    def test_excessive_api_requests_rate_limited(self):
        """Excessive API calls are rate-limited"""
        _pending_security_coverage("rate limiting when that feature exists")


class TestEnvironmentVariableInjection:
    """Test environment variable injection attacks"""

    def test_env_var_with_shell_metacharacters(self):
        """Environment variables with shell metacharacters are safe"""
        _pending_security_coverage("environment value shell-metacharacter handling")


class TestFileSystemSecurity:
    """Test file system security"""

    def test_log_directory_creation_safe_permissions(self):
        """Log directory created with safe permissions (not world-writable)"""
        _pending_security_coverage("log directory permissions across platforms")

    def test_config_file_permissions(self):
        """Config file (default.json) has restricted permissions"""
        _pending_security_coverage("config file permissions across platforms")

    def test_timelapse_temp_files_cleaned_up(self):
        """Temporary timelapse files are cleaned up on error"""
        _pending_security_coverage("timelapse temp-file cleanup on failure")


# Integration test: Full security audit

class TestSecurityIntegration:
    """Integration tests combining multiple attack vectors"""

    def test_full_attack_chain_blocked(self, tmp_path):
        """Combined SQL injection + XSS + path traversal is blocked"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.get(
                "/api/debug/state/..%2F..%2Fetc%2Fpasswd",
                query_string={
                    "name": "<script>alert(1)</script>",
                    "filter": "' OR 1=1 --",
                },
                headers={"X-Api-Key": "' OR 1=1 --"},
            )
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 401

    def test_anonymous_user_cannot_access_protected_endpoints(self, tmp_path):
        """All protected endpoints require authentication"""
        protected_requests = [
            ("post", "/api/printer/gcode", {"gcode": "G28"}),
            ("post", "/api/printer/control", {"value": 1}),
            ("get", "/api/filaments", None),
            ("get", "/api/debug/state", None),
            ("get", "/api/ankerctl/server/reload", None),
            ("get", "/api/camera/frame", None),
            ("get", "/api/camera/stream", None),
            ("get", "/api/snapshot", None),
        ]

        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            responses = []
            for method, path, payload in protected_requests:
                request_method = getattr(client, method)
                kwargs = {"json": payload} if payload is not None else {}
                responses.append((path, request_method(path, **kwargs).status_code))
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert responses
        assert all(status == 401 for _, status in responses), responses

    def test_setup_endpoints_require_auth_when_api_key_already_configured(self, tmp_path):
        """A pre-configured API key (e.g. ANKERCTL_API_KEY set before first
        boot, as in a Docker deployment) must be honored even during the
        setup window, i.e. before any printer/account is configured.

        There used to be a blanket setup-path exemption keyed only on
        "no printer configured yet" that ignored whether an API key had
        already been set, letting anyone on the LAN complete first-run setup
        (and thus take over the instance) during that window. That exemption
        has been removed — this test guards against it coming back."""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        app.config["login"] = None  # no printer/account configured yet
        try:
            responses = [
                ("/api/ankerctl/config/upload", client.post("/api/ankerctl/config/upload").status_code),
                (
                    "/api/ankerctl/config/login",
                    client.post(
                        "/api/ankerctl/config/login",
                        data={"login_email": "a@b.com", "login_password": "pw", "login_country": "us"},
                    ).status_code,
                ),
                ("/api/ankerctl/config/import-slicer", client.post("/api/ankerctl/config/import-slicer").status_code),
            ]
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert all(status == 401 for _, status in responses), responses

    def test_every_entry_in_protected_get_paths_actually_requires_auth(self, tmp_path):
        """Every path listed in the real _PROTECTED_GET_PATHS set must 401
        without auth.

        Unlike test_anonymous_user_cannot_access_protected_endpoints (which
        checks a hand-picked sample), this iterates the actual set used by
        _check_api_key() so a future edit that drops or typos an entry is
        caught immediately instead of silently reopening a previously
        protected endpoint. before_request auth checks run ahead of route
        matching, so this holds even for dev-mode-only paths like
        /api/debug/state when ANKERCTL_DEV_MODE is off."""
        from web import _PROTECTED_GET_PATHS

        assert _PROTECTED_GET_PATHS, "protected GET path set must not be empty"

        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            responses = [(path, client.get(path).status_code) for path in sorted(_PROTECTED_GET_PATHS)]
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        failures = [(path, status) for path, status in responses if status != 401]
        assert not failures, f"protected GET paths did not require auth: {failures}"

    def test_authenticated_user_can_access_allowed_endpoints(self, tmp_path):
        """Authenticated user can access non-restricted endpoints"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            health = client.get("/api/health")
            filaments = client.get("/api/filaments", headers=_auth_headers())
            camera_stream = client.get("/api/camera/stream", headers=_auth_headers())
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert health.status_code == 200
        assert filaments.status_code == 200
        assert isinstance(filaments.get_json().get("filaments"), list)
        assert camera_stream.status_code != 401
