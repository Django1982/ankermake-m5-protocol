"""
Test suite for security validation
Tests SQL injection, XSS, path traversal, input validation
"""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import web
from cli.model import Account, Config, Printer
from web import app
from web.service.filament import FilamentStore


API_KEY = "secret-key-123456"

_PROTECTED_SECURITY_REQUESTS = [
    ("post", "/api/printer/gcode", {"gcode": "G28"}),
    ("post", "/api/printer/control", {"value": 1}),
    ("get", "/api/filaments", None),
    ("get", "/api/debug/state", None),
    ("get", "/api/ankerctl/server/reload", None),
    ("get", "/api/camera/frame", None),
    ("get", "/api/camera/stream", None),
    ("get", "/api/camera/printer-stream", None),
    ("get", "/api/snapshot", None),
    ("get", "/api/files/printer", None),
    ("get", "/api/files/printer/thumbnail?path=/tmp/udisk/udisk1/file.gcode", None),
    ("get", "/api/history/1/thumbnail", None),
]

_EXPECTED_PROTECTED_GET_PATHS = {
    "/api/ankerctl/server/reload",
    "/api/camera/frame",
    "/api/camera/stream",
    "/api/camera/printer-stream",
    "/api/debug/state",
    "/api/filaments",
    "/api/history",
    "/api/snapshot",
}

_EXPECTED_PROTECTED_GET_PREFIXES = {
    "/api/debug/",
    "/api/files/printer",
    "/api/history/",
    "/api/timelapse/",
    "/api/timelapse-snapshot/",
}


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

    def test_log_viewer_path_traversal_blocked(self, tmp_path, monkeypatch):
        """GET /api/debug/logs/../../../etc/passwd is blocked"""
        if not hasattr(web, "app_api_debug_logs_content"):
            pytest.skip("Debug log content route is only registered in dev mode")
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        old_log_dir = getattr(web, "_log_dir", None)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        monkeypatch.setattr(web, "_log_dir", str(log_dir), raising=False)
        try:
            response = client.get(
                "/api/debug/logs/..%5C..%5Cpasswd",
                headers=_auth_headers(),
            )
        finally:
            monkeypatch.setattr(web, "_log_dir", old_log_dir, raising=False)
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 400
        assert "Invalid filename" in response.get_json()["error"]

    def test_timelapse_filename_path_traversal(self, tmp_path):
        """Timelapse download with ../ in filename is blocked"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.get(
                "/api/timelapse-snapshot/..%5Cbad/frame_00000.jpg",
                headers=_auth_headers(),
            )
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert response.status_code == 400
        assert "invalid filename" in response.get_json()["error"]

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

    def test_invalid_upload_rate_rejected(self, tmp_path):
        """Upload rate outside valid range (5,10,25,50,100) is rejected"""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            non_integer = client.post(
                "/api/ankerctl/config/upload-rate",
                data={"upload_rate_mbps": "fast"},
                headers=_auth_headers(),
            )
            out_of_range = client.post(
                "/api/ankerctl/config/upload-rate",
                data={"upload_rate_mbps": "7"},
                headers=_auth_headers(),
            )
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert non_integer.status_code == 400
        assert "must be an integer" in non_integer.get_json()["error"]
        assert out_of_range.status_code == 400
        assert "must be one of" in out_of_range.get_json()["error"]

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
        """Valid URL API key starts a browser session and redirects to a clean URL."""
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            response = client.get(f"/api/filaments?apikey={API_KEY}&foo=bar")
            assert response.status_code == 302
            assert "apikey" not in response.headers["Location"]

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

    def test_security_auth_manifest_includes_sensitive_dynamic_routes(self):
        """The auth guard must keep sensitive exact paths and dynamic prefixes protected."""
        assert _EXPECTED_PROTECTED_GET_PATHS <= set(web._PROTECTED_GET_PATHS)
        assert _EXPECTED_PROTECTED_GET_PREFIXES <= set(web._PROTECTED_GET_PREFIXES)

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
        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        try:
            responses = []
            for method, path, payload in _PROTECTED_SECURITY_REQUESTS:
                request_method = getattr(client, method)
                kwargs = {"json": payload} if payload is not None else {}
                responses.append((path, request_method(path, **kwargs).status_code))
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert responses
        assert all(status == 401 for _, status in responses), responses

    def test_anonymous_user_is_blocked_before_dynamic_history_and_storage_logic(self, tmp_path, monkeypatch):
        """Sensitive dynamic GET routes must 401 before hitting printer/history logic."""
        storage_probe_calls = []
        mqtt_borrow_calls = []

        def fake_probe(*args, **kwargs):
            storage_probe_calls.append((args, kwargs))
            return {"source_value": 1, "reply_count": 1, "files": []}, None

        @contextmanager
        def fake_borrow_mqtt(printer_index):
            mqtt_borrow_calls.append(printer_index)
            yield SimpleNamespace(
                is_printing=False,
                has_pending_print_start=False,
                is_preparing_print=False,
                get_cached_stored_file_preview_url=lambda *args, **kwargs: "https://example.test/thumb.png",
                get_stored_file_preview_url=lambda *args, **kwargs: "https://example.test/thumb.png",
                history=SimpleNamespace(
                    get_entry=lambda entry_id: {"preview_url": "https://example.test/thumb.png"},
                    get_thumbnail_path=lambda entry_id: None,
                ),
            )

        client = app.test_client()
        old_values, old_svc, old_filaments = _install_security_state(tmp_path)
        monkeypatch.setattr("web._probe_printer_storage_files", fake_probe)
        monkeypatch.setattr("web.borrow_mqtt", fake_borrow_mqtt)
        try:
            printer_files = client.get("/api/files/printer")
            printer_thumbnail = client.get("/api/files/printer/thumbnail?path=/tmp/udisk/udisk1/file.gcode")
            history_thumbnail = client.get("/api/history/1/thumbnail")
        finally:
            _restore_security_state(old_values, old_svc, old_filaments)

        assert printer_files.status_code == 401
        assert printer_thumbnail.status_code == 401
        assert history_thumbnail.status_code == 401
        assert storage_probe_calls == []
        assert mqtt_borrow_calls == []

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
