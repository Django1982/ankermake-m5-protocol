"""
Test suite for libflagship/httpapi.py - HTTP API Client
Tests authentication, region selection, error handling
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from libflagship.httpapi import (
    AnkerHTTPApi,
    AnkerHTTPAppApiV1,
    AnkerHTTPPassportApiV1,
    AnkerHTTPPassportApiV2,
    AnkerHTTPHubApiV1,
    AnkerHTTPHubApiV2,
    APIError,
    require_auth_token,
    unwrap_api,
    _find_closest_host,
    _measure_host_connect_time,
)


class TestAnkerHTTPApi:
    """Test AnkerHTTPApi base class"""

    def test_init_with_region_eu(self):
        """AnkerHTTPApi initializes with EU region"""
        api = AnkerHTTPApi(region="eu", auth_token="test_token", user_id="123")
        assert api._base == "https://make-app-eu.ankermake.com"
        assert api._auth == "test_token"
        assert api._user_id == "123"
        assert api._verify is True

    def test_init_with_region_us(self):
        """AnkerHTTPApi initializes with US region"""
        api = AnkerHTTPApi(region="us", auth_token="test_token", user_id="123")
        assert api._base == "https://make-app.ankermake.com"

    def test_init_with_custom_base_url(self):
        """AnkerHTTPApi accepts custom base_url"""
        api = AnkerHTTPApi(
            base_url="https://custom.example.com",
            auth_token="test"
        )
        assert api._base == "https://custom.example.com"

    def test_init_with_invalid_region_raises(self):
        """AnkerHTTPApi raises APIError for invalid region without base_url"""
        with pytest.raises(APIError, match="must specify either base_url or region"):
            AnkerHTTPApi(region="invalid", auth_token="test")

    def test_init_without_region_or_base_url_raises(self):
        """AnkerHTTPApi raises APIError when neither region nor base_url provided"""
        with pytest.raises(APIError):
            AnkerHTTPApi(auth_token="test")

    def test_verify_flag(self):
        """AnkerHTTPApi respects verify flag for SSL"""
        api = AnkerHTTPApi(region="eu", auth_token="test", verify=False)
        assert api._verify is False


class TestAPIDecorators:
    """Test decorator functions"""

    def test_require_auth_token_with_token(self):
        """require_auth_token allows call when token present"""

        @require_auth_token
        def dummy_method(self):
            return "success"

        mock_self = Mock()
        mock_self._auth = "valid_token"

        result = dummy_method(mock_self)
        assert result == "success"

    def test_require_auth_token_without_token_raises(self):
        """require_auth_token raises APIError when token missing"""

        @require_auth_token
        def dummy_method(self):
            return "success"

        mock_self = Mock()
        mock_self._auth = None

        with pytest.raises(APIError, match="Missing auth token"):
            dummy_method(mock_self)

    @patch('requests.get')
    def test_unwrap_api_success(self, mock_get):
        """unwrap_api decorator extracts data on success"""

        @unwrap_api
        def dummy_method(self):
            return mock_get.return_value

        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"code": 0, "data": {"result": "value"}}
        mock_get.return_value = mock_response

        mock_self = Mock()
        mock_self.scope = "/test"

        result = dummy_method(mock_self)
        assert result == {"result": "value"}

    @patch('requests.get')
    def test_unwrap_api_redacts_secrets_from_debug_log(self, mock_get, caplog):
        """unwrap_api must not leak auth tokens / device secrets into DEBUG logs"""
        import logging

        @unwrap_api
        def dummy_method(self):
            return mock_get.return_value

        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "code": 0,
            "data": {
                "auth_token": "deadbeefcafebabe0123456789abcdef01234567",
                "dsk_keys": [{"station_sn": "SN1", "dsk_key": "super-secret-dsk"}],
                "user_id": "user-123",
            },
        }
        mock_get.return_value = mock_response

        mock_self = Mock()
        mock_self.scope = "/test"

        with caplog.at_level(logging.DEBUG):
            dummy_method(mock_self)

        assert "deadbeefcafebabe" not in caplog.text
        assert "super-secret-dsk" not in caplog.text
        assert "user-123" in caplog.text  # non-sensitive fields still logged for debugging

    @patch('requests.get')
    def test_unwrap_api_raises_on_non_zero_code(self, mock_get):
        """unwrap_api raises APIError when API returns error code"""

        @unwrap_api
        def dummy_method(self):
            return mock_get.return_value

        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"code": 500, "msg": "Server error"}
        mock_get.return_value = mock_response

        mock_self = Mock()
        mock_self.scope = "/test"

        with pytest.raises(APIError, match="API error"):
            dummy_method(mock_self)

    @patch('requests.get')
    def test_unwrap_api_raises_on_http_error(self, mock_get):
        """unwrap_api raises APIError on HTTP error status"""

        @unwrap_api
        def dummy_method(self):
            return mock_get.return_value

        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 404
        mock_response.reason = "Not Found"
        mock_get.return_value = mock_response

        mock_self = Mock()
        mock_self.scope = "/test"

        with pytest.raises(APIError, match="404 Not Found"):
            dummy_method(mock_self)

    def test_unwrap_api_raises_without_scope(self):
        """unwrap_api raises APIError when scope undefined"""

        @unwrap_api
        def dummy_method(self):
            return Mock()

        mock_self = Mock()
        mock_self.scope = None

        with pytest.raises(APIError, match="scope undefined"):
            dummy_method(mock_self)


class TestAPIRequests:
    """Test HTTP request methods"""

    @patch('requests.get')
    def test_get_request_includes_gtoken(self, mock_get):
        """_get() includes Gtoken header when user_id present"""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"code": 0, "data": {}}
        mock_get.return_value = mock_response

        api = AnkerHTTPApi(region="eu", auth_token="token", user_id="user123")
        api.scope = "/test"
        api._get("/endpoint")

        call_args = mock_get.call_args
        headers = call_args[1]['headers']

        # Gtoken should be MD5 of user_id
        import hashlib
        expected_gtoken = hashlib.md5(b"user123").hexdigest()
        assert headers['Gtoken'] == expected_gtoken

    @patch('requests.post')
    def test_post_request_sends_json_data(self, mock_post):
        """_post() sends JSON payload"""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"code": 0, "data": {}}
        mock_post.return_value = mock_response

        api = AnkerHTTPApi(region="eu", auth_token="token", user_id="123")
        api.scope = "/test"

        payload = {"key": "value"}
        api._post("/endpoint", data=payload)

        call_args = mock_post.call_args
        assert call_args[1]['json'] == payload

    @patch('requests.get')
    def test_request_respects_verify_flag(self, mock_get):
        """HTTP requests respect SSL verify flag"""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"code": 0, "data": {}}
        mock_get.return_value = mock_response

        api = AnkerHTTPApi(region="eu", auth_token="token", verify=False)
        api.scope = "/test"
        api._get("/endpoint")

        call_args = mock_get.call_args
        assert call_args[1]['verify'] is False


class TestRegionSelection:
    """Test region auto-detection"""

    @patch('libflagship.httpapi._find_closest_host')
    def test_guess_region_calls_find_closest_host(self, mock_find):
        """guess_region() delegates to _find_closest_host"""
        mock_find.return_value = "eu"

        result = AnkerHTTPApi.guess_region()

        assert result == "eu"
        mock_find.assert_called_once_with(AnkerHTTPApi.hosts)

    @patch('libflagship.httpapi._measure_host_connect_time')
    def test_find_closest_host_measures_all_hosts(self, mock_measure):
        """_find_closest_host measures latency to all hosts"""
        # EU faster than US
        mock_measure.side_effect = lambda host, port: 0.05 if "eu" in host else 0.15

        hosts = {
            "eu": "make-app-eu.ankermake.com",
            "us": "make-app.ankermake.com",
        }

        result = _find_closest_host(hosts)

        assert result == "eu"
        assert mock_measure.call_count == 2

    @patch('socket.socket')
    def test_measure_host_connect_time_returns_latency(self, mock_socket_class):
        """_measure_host_connect_time returns connection duration"""
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock

        # Simulate successful connection
        result = _measure_host_connect_time("example.com", port=443)

        assert isinstance(result, float)
        mock_sock.connect.assert_called_once()
        mock_sock.close.assert_called_once()

    @patch('socket.socket')
    def test_measure_host_connect_time_timeout_returns_infinity(self, mock_socket_class):
        """_measure_host_connect_time returns infinity on timeout"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = TimeoutError()
        mock_socket_class.return_value = mock_sock

        result = _measure_host_connect_time("example.com", port=443)

        assert result == float('inf')


class TestAPIError:
    """Test APIError exception"""

    def test_api_error_basic(self):
        """APIError can be raised with message"""
        error = APIError("Test error")
        assert str(error) == "Test error"
        assert error.json is None

    def test_api_error_with_json(self):
        """APIError stores JSON payload"""
        json_data = {"code": 500, "msg": "Server error"}
        error = APIError("API failed", json=json_data)

        assert error.json == json_data
        assert "{'code': 500" in str(error)


class TestAPISubclasses:
    """Test API subclass scope definitions"""

    def test_app_api_v1_scope(self):
        """AnkerHTTPAppApiV1 has correct scope"""
        assert AnkerHTTPAppApiV1.scope == "/v1/app"

    def test_passport_api_v1_scope(self):
        """AnkerHTTPPassportApiV1 has correct scope"""
        assert AnkerHTTPPassportApiV1.scope == "/v1/passport"

    def test_passport_api_v2_scope(self):
        """AnkerHTTPPassportApiV2 has correct scope"""
        assert AnkerHTTPPassportApiV2.scope == "/v2/passport"

    def test_hub_api_v1_scope(self):
        """AnkerHTTPHubApiV1 has correct scope"""
        assert AnkerHTTPHubApiV1.scope == "/v1/hub"

    def test_hub_api_v2_scope(self):
        """AnkerHTTPHubApiV2 has correct scope"""
        assert AnkerHTTPHubApiV2.scope == "/v2/hub"


# Edge case tests

class TestEdgeCases:
    """Test edge cases and error conditions"""

    @patch('requests.get')
    def test_network_timeout(self, mock_get):
        """API handles network timeouts gracefully"""
        import requests
        mock_get.side_effect = requests.exceptions.Timeout()

        api = AnkerHTTPApi(region="eu", auth_token="token")
        api.scope = "/test"

        with pytest.raises(requests.exceptions.Timeout):
            api._get("/endpoint")

    @patch('requests.get')
    def test_connection_error(self, mock_get):
        """API handles connection errors"""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError()

        api = AnkerHTTPApi(region="eu", auth_token="token")
        api.scope = "/test"

        with pytest.raises(requests.exceptions.ConnectionError):
            api._get("/endpoint")

    @patch('requests.get')
    def test_invalid_json_response(self, mock_get):
        """API handles malformed JSON gracefully"""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.side_effect = ValueError("Invalid JSON")
        mock_get.return_value = mock_response

        api = AnkerHTTPApi(region="eu", auth_token="token")
        api.scope = "/test"

        with pytest.raises(ValueError):
            api._get("/endpoint")

    @patch('requests.get')
    def test_missing_data_field_in_response(self, mock_get):
        """API handles missing 'data' field in response"""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"code": 0}  # No 'data' field
        mock_get.return_value = mock_response

        api = AnkerHTTPApi(region="eu", auth_token="token")
        api.scope = "/test"

        result = api._get("/endpoint")
        assert result is None  # .get("data") returns None

    def test_empty_user_id(self):
        """API handles empty user_id"""
        api = AnkerHTTPApi(region="eu", auth_token="token", user_id="")
        assert api._user_id == ""

    @patch('requests.get')
    def test_custom_headers_merged(self, mock_get):
        """Custom headers are merged with default headers"""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"code": 0, "data": {}}
        mock_get.return_value = mock_response

        api = AnkerHTTPApi(region="eu", auth_token="token", user_id="123")
        api.scope = "/test"

        custom_headers = {"X-Custom": "value"}
        api._get("/endpoint", headers=custom_headers)

        call_args = mock_get.call_args
        headers = call_args[1]['headers']

        assert 'X-Custom' in headers
        assert 'Gtoken' in headers
        assert headers['X-Custom'] == "value"
