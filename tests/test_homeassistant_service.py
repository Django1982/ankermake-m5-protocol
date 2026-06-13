import json
from contextlib import contextmanager
from types import SimpleNamespace

import web as web_module
from web.service.homeassistant import HomeAssistantService


class FakeConfigManager:
    def __init__(self, cfg):
        self.cfg = cfg

    @contextmanager
    def open(self):
        yield self.cfg


class FakeMQTTClient:
    def __init__(self):
        self.published = []
        self.subscriptions = []
        self.username_password = None
        self.connected = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.will = None
        self.reconnect_delay = None

    def username_pw_set(self, user, password):
        self.username_password = (user, password)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect(self, host, port, keepalive=60):
        self.connected = (host, port, keepalive)

    def connect_async(self, host, port, keepalive=60):
        self.connected = (host, port, keepalive)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delay = (min_delay, max_delay)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))


def _service_config(enabled=True):
    return SimpleNamespace(
        home_assistant={
            "enabled": enabled,
            "mqtt_host": "mqtt.example",
            "mqtt_port": 1884,
            "mqtt_username": "ha-user",
            "mqtt_password": "ha-pass",
            "discovery_prefix": "ha",
        }
    )


def test_homeassistant_reload_and_state_publish():
    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    client = FakeMQTTClient()
    svc._client = client
    svc._connected = True

    svc.update_state(nozzle_temp=210, print_status="printing")
    svc.update_state(nozzle_temp=210, print_status="printing")

    assert svc.enabled is True
    assert svc._host == "mqtt.example"
    assert svc._port == 1884
    assert len(client.published) == 1
    topic, payload, qos, retain = client.published[0]
    assert topic == "ankerctl/SN123/state"
    assert retain is True
    decoded = json.loads(payload)
    assert decoded["nozzle_temp"] == 210
    assert decoded["print_status"] == "printing"


def test_homeassistant_publish_discovery_emits_expected_entities():
    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    client = FakeMQTTClient()
    svc._client = client
    svc._connected = True

    svc._publish_discovery()

    topics = [topic for topic, *_ in client.published]
    assert "ha/sensor/ankerctl_SN123/print_progress/config" in topics
    assert "ha/sensor/ankerctl_SN123/fan_speed/config" in topics
    assert "ha/binary_sensor/ankerctl_SN123/mqtt_connected/config" in topics
    assert "ha/switch/ankerctl_SN123/light/config" in topics
    assert "ha/camera/ankerctl_SN123/camera/config" in topics
    assert len(client.published) == 16


def test_homeassistant_on_connect_and_light_command(monkeypatch):
    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    client = FakeMQTTClient()
    light_calls = []
    old_svc = web_module.app.svc
    web_module.app.svc = SimpleNamespace(svcs={"videoqueue": SimpleNamespace(api_light_state=lambda state: light_calls.append(state))})

    class FakeThread:
        def __init__(self, target=None, args=None, daemon=None, name=None):
            self.target = target
            self.args = args or ()
            self.daemon = daemon
            self.name = name
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    monkeypatch.setattr("web.service.homeassistant.threading.Thread", FakeThread)

    try:
        svc._client = client
        svc._on_connect(client, None, None, 0)
        svc._handle_light_command("ON")
    finally:
        web_module.app.svc = old_svc

    assert svc._connected is True
    assert ("ankerctl/SN123/light/set", 1) in client.subscriptions
    assert light_calls == [True]
    assert svc._state["light"] is True


def test_homeassistant_start_and_stop(monkeypatch):
    fake_client = FakeMQTTClient()

    class FakePahoModule:
        class CallbackAPIVersion:
            VERSION1 = object()

        @staticmethod
        def Client(*args, **kwargs):
            return fake_client

    monkeypatch.setattr("web.service.homeassistant.paho_mqtt", FakePahoModule)

    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    svc.start()
    svc._connected = True
    svc.stop()

    assert fake_client.username_password == ("ha-user", "ha-pass")
    assert fake_client.connected == ("mqtt.example", 1884, 60)
    assert fake_client.reconnect_delay == (1, 60)
    assert fake_client.loop_started is True
    assert fake_client.disconnected is True
    assert fake_client.loop_stopped is True


def test_homeassistant_start_survives_unreachable_broker(monkeypatch):
    """connect_async stores params without doing I/O, so a broker that is
    unreachable at startup should still leave the client active — paho's
    background thread will retry."""
    fake_client = FakeMQTTClient()

    class FakePahoModule:
        class CallbackAPIVersion:
            VERSION1 = object()

        @staticmethod
        def Client(*args, **kwargs):
            return fake_client

    monkeypatch.setattr("web.service.homeassistant.paho_mqtt", FakePahoModule)

    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    svc.start()

    # Broker is unreachable, so we are not connected yet — but the client
    # is alive and loop_start has been called, so paho will keep retrying.
    assert svc._client is fake_client
    assert svc._connected is False
    assert fake_client.loop_started is True
    assert fake_client.reconnect_delay == (1, 60)


def test_homeassistant_start_handles_invalid_broker_config(monkeypatch):
    """connect_async raises ValueError for bad host/port — that's a
    genuine misconfiguration, not transient unreachability, so we give up
    cleanly rather than leaving paho's thread running."""
    fake_client = FakeMQTTClient()

    def bad_connect_async(host, port, keepalive=60):
        raise ValueError(f"invalid port: {port}")
    fake_client.connect_async = bad_connect_async

    class FakePahoModule:
        class CallbackAPIVersion:
            VERSION1 = object()

        @staticmethod
        def Client(*args, **kwargs):
            return fake_client

    monkeypatch.setattr("web.service.homeassistant.paho_mqtt", FakePahoModule)

    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    svc.start()

    assert svc._client is None
    # loop_start must NOT have been called when connect_async failed
    assert fake_client.loop_started is False


def test_homeassistant_reload_restarts_on_config_change(monkeypatch):
    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    svc._client = FakeMQTTClient()
    calls = []
    monkeypatch.setattr(svc, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(svc, "start", lambda: calls.append("start"))

    changed = SimpleNamespace(
        home_assistant={
            "enabled": True,
            "mqtt_host": "other.example",
            "mqtt_port": 1884,
            "mqtt_username": "ha-user",
            "mqtt_password": "ha-pass",
            "discovery_prefix": "ha",
        }
    )

    svc.reload_config(changed)

    assert calls == ["stop", "start"]
    assert svc._host == "other.example"


def test_homeassistant_disconnect_and_publish_failures_are_non_fatal():
    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    svc._client = SimpleNamespace(publish=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    svc._connected = True

    svc._publish("topic/test", "payload", retain=True)
    svc._on_disconnect(None, None, 1)

    assert svc._connected is False


def test_rapid_reconnect_no_thread_leak():
    """Rapid _on_connect calls should not accumulate availability threads.
    The generation counter ensures stale threads self-terminate."""
    import threading
    import time

    svc = HomeAssistantService(FakeConfigManager(_service_config()), printer_sn="SN123", printer_name="Printer")
    svc._enabled = True
    svc._connected = True
    svc._client = SimpleNamespace(
        subscribe=lambda *a, **kw: None,
        publish=lambda *a, **kw: None,
    )
    svc._publish_discovery = lambda: None

    for _ in range(5):
        svc._on_connect(svc._client, None, None, 0)

    assert svc._availability_generation == 5
    time.sleep(0.2)
    alive = [t for t in threading.enumerate() if t.name == "ha-mqtt-avail"]
    assert len(alive) <= 1, f"Expected <=1 availability threads, found {len(alive)}"

    svc._stop_event.set()
    if svc._availability_thread:
        svc._availability_thread.join(timeout=2)
