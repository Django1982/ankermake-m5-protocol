"""Home Assistant MQTT Discovery service.

Connects to an external MQTT broker (typically Home Assistant's) and publishes
MQTT Discovery configuration payloads so the printer appears as a device with
sensors, a camera entity, and a light switch in Home Assistant.

State updates are published whenever the MQTT service forwards new data.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("homeassistant")


import paho.mqtt.client as paho_mqtt


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 1883
_DEFAULT_DISCOVERY_PREFIX = "homeassistant"
_DEFAULT_TOPIC_PREFIX = "ankerctl"
_AVAILABILITY_TIMEOUT = 60  # seconds between availability pings


class HomeAssistantService:
    """Bridges ankerctl printer data to Home Assistant via MQTT Discovery."""

    def __init__(self, config_manager, printer_sn=None, printer_name=None, printer_index=None):
        self._config_manager = config_manager
        self._printer_sn = printer_sn or "ankerctl"
        self._printer_name = printer_name or "AnkerMake M5"
        self._printer_index = 0 if printer_index is None else int(printer_index)
        self._node_id = f"ankerctl_{self._printer_sn}"

        self._client = None
        self._connected = False
        self._lock = threading.Lock()
        self._availability_thread = None
        self._stop_event = threading.Event()
        self._availability_generation = 0

        # Cached state for publishing
        self._state = {
            "print_progress": None,
            "print_status": "idle",
            "nozzle_temp": None,
            "nozzle_temp_target": None,
            "bed_temp": None,
            "bed_temp_target": None,
            "fan_speed": None,
            "print_speed": None,
            "print_layer": None,
            "print_filename": None,
            "time_elapsed": None,
            "time_remaining": None,
            "mqtt_connected": False,
            "pppp_connected": False,
            "light": False,
        }

        # Set defaults
        self._enabled = False
        self._host = _DEFAULT_HOST
        self._port = _DEFAULT_PORT
        self._user = ""
        self._password = ""
        self._discovery_prefix = _DEFAULT_DISCOVERY_PREFIX
        self._topic_prefix = _DEFAULT_TOPIC_PREFIX

        self._ha_mjpeg_entry_id = None
        self._ha_base_url = ""
        self._ha_token = ""

        self.reload_config()

    def reload_config(self, config=None):
        # If no config passed, use stored config_manager
        if config is None:
            config = self._config_manager

        # If config is a ConfigManager (has .open() method), load the actual config
        if config and hasattr(config, 'open'):
            with config.open() as cfg:
                config = cfg

        if not config or not getattr(config, 'home_assistant', None):
            return

        cfg = config.home_assistant
        new_enabled = cfg.get("enabled", False)
        new_host = cfg.get("mqtt_host", _DEFAULT_HOST)
        new_port = int(cfg.get("mqtt_port", _DEFAULT_PORT))
        new_user = cfg.get("mqtt_username", "")
        new_password = cfg.get("mqtt_password", "")
        new_discovery_prefix = cfg.get("discovery_prefix", _DEFAULT_DISCOVERY_PREFIX)
        # topic prefix is not in config model yet? Defaults to ankerctl
        new_topic_prefix = os.getenv("HA_MQTT_TOPIC_PREFIX", _DEFAULT_TOPIC_PREFIX)

        # check if restart needed
        need_restart = False
        if self._client: # Only if currently running
            if (self._host != new_host or 
                self._port != new_port or 
                self._user != new_user or 
                self._password != new_password or
                self._discovery_prefix != new_discovery_prefix or
                self._topic_prefix != new_topic_prefix):
                need_restart = True
            if self._enabled and not new_enabled:
                self.stop() # Just stop
                need_restart = False

        self._enabled = new_enabled
        self._host = new_host
        self._port = new_port
        self._user = new_user
        self._password = new_password
        self._discovery_prefix = new_discovery_prefix
        self._topic_prefix = new_topic_prefix
        # Env vars take precedence over config file values
        self._ha_base_url = (os.getenv("HA_BASE_URL") or cfg.get("ha_base_url", "")).rstrip("/")
        self._ha_token = os.getenv("HA_TOKEN") or cfg.get("ha_token", "")

        if need_restart and self._enabled:
            self.stop()
            self.start()
        elif self._enabled and not self._client:
            self.start()

    @property
    def enabled(self):
        return self._enabled

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Connect to the HA MQTT broker and publish discovery configs.

        Uses connect_async() + loop_start() so a broker that is briefly
        unreachable at startup (for example, when ankerctl and Mosquitto are
        launched together in docker-compose) does not permanently disable HA
        integration. paho's background thread retries with exponential
        backoff.
        """
        if not self._enabled:
            return
        if self._client:
            return

        log.info(f"HA MQTT: connecting to {self._host}:{self._port}")
        if hasattr(paho_mqtt, "CallbackAPIVersion"):
            self._client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION1, client_id=f"ankerctl-{self._printer_sn}", clean_session=True)
        else:
            self._client = paho_mqtt.Client(client_id=f"ankerctl-{self._printer_sn}", clean_session=True)

        if self._user:
            self._client.username_pw_set(self._user, self._password)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Bound reconnect backoff so brief broker outages recover quickly
        # but sustained unreachability doesn't hammer the network.
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

        # Set LWT (Last Will and Testament) so HA marks device offline
        avail_topic = self._availability_topic()
        self._client.will_set(avail_topic, payload="offline", qos=1, retain=True)

        try:
            self._client.connect_async(self._host, self._port, keepalive=60)
        except ValueError as err:
            # Invalid host/port syntax — genuine misconfiguration.
            log.error(f"HA MQTT: invalid broker configuration: {err}")
            self._client = None
            return
        except OSError as err:
            # Broker unreachable right now (ConnectionRefused, NetworkUnreachable, etc.).
            # Log a warning but proceed to loop_start() so paho retries automatically.
            log.warning(f"HA MQTT: initial connect failed ({err}), will retry via background loop")

        self._client.loop_start()

    def stop(self):
        """Disconnect from the HA MQTT broker and mark device offline."""
        self._unregister_ha_mjpeg_camera()

        if not self._client:
            return

        self._stop_event.set()
        if self._availability_thread and self._availability_thread.is_alive():
            self._availability_thread.join(timeout=5)

        try:
            self._publish(self._availability_topic(), "offline", retain=True)
            self._client.disconnect()
            self._client.loop_stop()
        except Exception as err:
            log.warning(f"HA MQTT: disconnect error: {err}")
        finally:
            self._client = None
            self._connected = False

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error(f"HA MQTT: connection refused (rc={rc})")
            return

        log.info("HA MQTT: connected to broker")
        self._connected = True

        # Publish discovery configs
        self._publish_discovery()

        # Mark device online
        self._publish(self._availability_topic(), "online", retain=True)

        # Subscribe to command topics (light switch)
        light_cmd_topic = f"{self._topic_prefix}/{self._printer_sn}/light/set"
        client.subscribe(light_cmd_topic, qos=1)
        log.info(f"HA MQTT: subscribed to {light_cmd_topic}")

        # Start availability heartbeat — stop any existing thread first to avoid leaks.
        # Bump generation so stale threads self-terminate even if they miss the
        # stop event (prevents thread accumulation on rapid reconnects).
        if self._availability_thread and self._availability_thread.is_alive():
            self._stop_event.set()
            self._availability_thread.join(timeout=2)
            self._availability_thread = None
        self._availability_generation += 1
        self._stop_event.clear()
        gen = self._availability_generation
        self._availability_thread = threading.Thread(
            target=self._availability_loop, args=(gen,), daemon=True, name="ha-mqtt-avail"
        )
        self._availability_thread.start()

        # Register MJPEG camera via HA config_entries REST API (best-effort)
        self._register_ha_mjpeg_camera()

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            log.warning(f"HA MQTT: unexpected disconnect (rc={rc}), will auto-reconnect")
        else:
            log.info("HA MQTT: disconnected")

    def _on_message(self, client, userdata, msg):
        """Handle incoming commands from Home Assistant (e.g. light switch)."""
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        log.info(f"HA MQTT: received {topic} = {payload}")

        light_cmd_topic = f"{self._topic_prefix}/{self._printer_sn}/light/set"
        if topic == light_cmd_topic:
            self._handle_light_command(payload)

    def _handle_light_command(self, payload):
        """Forward light on/off command to the printer via the web service."""
        try:
            import web

            turn_on = payload.upper() == "ON"
            if web.set_printer_light_state(turn_on, self._printer_index):
                with self._lock:
                    self._state["light"] = turn_on
                    self._publish_state()
                log.info(f"HA MQTT: light {'ON' if turn_on else 'OFF'}")
            else:
                log.warning("HA MQTT: light control is not available")
        except Exception as err:
            log.warning(f"HA MQTT: light command failed: {err}")

    # ------------------------------------------------------------------
    # State update API (called from mqtt.py)
    # ------------------------------------------------------------------

    def update_state(self, **kwargs):
        """Update cached state and publish to HA.

        Accepts keyword arguments matching state keys:
            print_progress, print_status, nozzle_temp, nozzle_temp_target,
            bed_temp, bed_temp_target, fan_speed, print_speed, print_layer,
            print_filename, time_elapsed, time_remaining,
            mqtt_connected, pppp_connected, light
        """
        if not self._enabled or not self._connected:
            return

        with self._lock:
            changed = False
            for key, value in kwargs.items():
                if key in self._state and self._state[key] != value:
                    self._state[key] = value
                    changed = True

            if changed:
                self._publish_state()

    # ------------------------------------------------------------------
    # MQTT Publishing
    # ------------------------------------------------------------------

    def _publish(self, topic, payload, retain=False, qos=0):
        """Publish a message, handling errors gracefully."""
        if not self._client or not self._connected:
            return
        try:
            self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as err:
            log.warning(f"HA MQTT: publish failed on {topic}: {err}")

    def _availability_topic(self):
        return f"{self._topic_prefix}/{self._printer_sn}/availability"

    def _state_topic(self):
        return f"{self._topic_prefix}/{self._printer_sn}/state"

    def _availability_loop(self, my_generation):
        """Periodically publish availability to keep HA happy."""
        while not self._stop_event.is_set():
            if self._availability_generation != my_generation:
                log.debug("HA MQTT: stale availability thread exiting")
                return
            self._publish(self._availability_topic(), "online", retain=True)
            self._stop_event.wait(_AVAILABILITY_TIMEOUT)

    def _publish_state(self):
        """Publish the full state JSON to the state topic."""
        payload = json.dumps(self._state)
        self._publish(self._state_topic(), payload, retain=True)

    # ------------------------------------------------------------------
    # MQTT Discovery
    # ------------------------------------------------------------------

    def _device_info(self):
        """Return the HA device registry block."""
        return {
            "identifiers": [self._node_id],
            "name": self._printer_name,
            "manufacturer": "AnkerMake",
            "model": "M5",
            "sw_version": "ankerctl",
        }

    def _availability_config(self):
        """Return the availability block for discovery payloads."""
        return [{
            "topic": self._availability_topic(),
            "payload_available": "online",
            "payload_not_available": "offline",
        }]

    def _publish_discovery(self):
        """Publish all MQTT Discovery config payloads."""
        state_topic = self._state_topic()
        device = self._device_info()
        availability = self._availability_config()

        sensors = [
            {
                "id": "print_progress",
                "name": "Print Progress",
                "unit": "%",
                "icon": "mdi:printer-3d",
                "value_template": "{{ value_json.print_progress | default(0) }}",
            },
            {
                "id": "print_status",
                "name": "Print Status",
                "icon": "mdi:printer-3d-nozzle",
                "value_template": "{{ value_json.print_status | default('idle') }}",
            },
            {
                "id": "nozzle_temp",
                "name": "Nozzle Temperature",
                "unit": "\u00b0C",
                "device_class": "temperature",
                "value_template": "{{ value_json.nozzle_temp | default(0) }}",
            },
            {
                "id": "nozzle_temp_target",
                "name": "Nozzle Target",
                "unit": "\u00b0C",
                "device_class": "temperature",
                "value_template": "{{ value_json.nozzle_temp_target | default(0) }}",
            },
            {
                "id": "bed_temp",
                "name": "Bed Temperature",
                "unit": "\u00b0C",
                "device_class": "temperature",
                "value_template": "{{ value_json.bed_temp | default(0) }}",
            },
            {
                "id": "bed_temp_target",
                "name": "Bed Target",
                "unit": "\u00b0C",
                "device_class": "temperature",
                "value_template": "{{ value_json.bed_temp_target | default(0) }}",
            },
            {
                "id": "fan_speed",
                "name": "Part Cooling Fan",
                "unit": "%",
                "icon": "mdi:fan",
                "value_template": "{{ value_json.fan_speed | default(0) }}",
            },
            {
                "id": "print_speed",
                "name": "Print Speed",
                "unit": "mm/s",
                "icon": "mdi:speedometer",
                "value_template": "{{ value_json.print_speed | default(0) }}",
            },
            {
                "id": "print_layer",
                "name": "Print Layer",
                "icon": "mdi:layers",
                "value_template": "{{ value_json.print_layer | default('') }}",
            },
            {
                "id": "print_filename",
                "name": "Print Filename",
                "icon": "mdi:file",
                "value_template": "{{ value_json.print_filename | default('') }}",
            },
            {
                "id": "time_elapsed",
                "name": "Time Elapsed",
                "unit": "s",
                "device_class": "duration",
                "value_template": "{{ value_json.time_elapsed | default(0) }}",
            },
            {
                "id": "time_remaining",
                "name": "Time Remaining",
                "unit": "s",
                "device_class": "duration",
                "value_template": "{{ value_json.time_remaining | default(0) }}",
            },
        ]

        for sensor in sensors:
            config = {
                "name": sensor["name"],
                "unique_id": f"{self._node_id}_{sensor['id']}",
                "object_id": f"{self._node_id}_{sensor['id']}",
                "state_topic": state_topic,
                "value_template": sensor["value_template"],
                "device": device,
                "availability": availability,
            }
            if "unit" in sensor:
                config["unit_of_measurement"] = sensor["unit"]
            if "device_class" in sensor:
                config["device_class"] = sensor["device_class"]
            if "icon" in sensor:
                config["icon"] = sensor["icon"]

            topic = f"{self._discovery_prefix}/sensor/{self._node_id}/{sensor['id']}/config"
            self._publish(topic, json.dumps(config), retain=True)

        # Binary sensors: mqtt_connected, pppp_connected
        binary_sensors = [
            {
                "id": "mqtt_connected",
                "name": "MQTT Connected",
                "device_class": "connectivity",
                "value_template": "{{ 'ON' if value_json.mqtt_connected else 'OFF' }}",
            },
            {
                "id": "pppp_connected",
                "name": "PPPP Connected",
                "device_class": "connectivity",
                "value_template": "{{ 'ON' if value_json.pppp_connected else 'OFF' }}",
            },
        ]

        for bs in binary_sensors:
            config = {
                "name": bs["name"],
                "unique_id": f"{self._node_id}_{bs['id']}",
                "object_id": f"{self._node_id}_{bs['id']}",
                "state_topic": state_topic,
                "value_template": bs["value_template"],
                "device_class": bs["device_class"],
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device,
                "availability": availability,
            }
            topic = f"{self._discovery_prefix}/binary_sensor/{self._node_id}/{bs['id']}/config"
            self._publish(topic, json.dumps(config), retain=True)

        # Light switch
        light_config = {
            "name": "Printer Light",
            "unique_id": f"{self._node_id}_light",
            "object_id": f"{self._node_id}_light",
            "state_topic": state_topic,
            "command_topic": f"{self._topic_prefix}/{self._printer_sn}/light/set",
            "value_template": "{{ 'ON' if value_json.light else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:lightbulb",
            "device": device,
            "availability": availability,
        }
        topic = f"{self._discovery_prefix}/switch/{self._node_id}/light/config"
        self._publish(topic, json.dumps(light_config), retain=True)

        # Camera entity — use url_template pointing at /api/snapshot for HA MQTT camera
        flask_host = os.getenv("FLASK_HOST") or "127.0.0.1"
        if flask_host in ("0.0.0.0", "::"):
            flask_host = "127.0.0.1"
        flask_port = os.getenv("FLASK_PORT") or "4470"
        snapshot_url = f"http://{flask_host}:{flask_port}/api/snapshot"
        stream_url = f"http://{flask_host}:{flask_port}/api/camera/stream"
        camera_config = {
            "name": "Camera",
            "unique_id": f"{self._node_id}_camera",
            "object_id": f"{self._node_id}_camera",
            "topic": f"{self._topic_prefix}/{self._printer_sn}/camera",
            "url_template": snapshot_url,
            "device": device,
            "availability": availability,
            "icon": "mdi:camera",
        }
        topic = f"{self._discovery_prefix}/camera/{self._node_id}/camera/config"
        self._publish(topic, json.dumps(camera_config), retain=True)

        log.info(f"HA camera stream available at: {stream_url}")
        log.info(f"HA camera snapshot available at: {snapshot_url}")
        log.info(f"HA MQTT: published discovery configs ({len(sensors)} sensors, "
                 f"{len(binary_sensors)} binary sensors, 1 switch, 1 camera)")

    # ------------------------------------------------------------------
    # HA REST API — MJPEG IP Camera config entry
    # ------------------------------------------------------------------

    def _ha_web_urls(self):
        """Return (mjpeg_url, snapshot_url) for this ankerctl instance.

        Uses the same host/port resolution as _publish_discovery().
        Appends printer_index and apikey query params when available.
        """
        flask_host = os.getenv("FLASK_HOST") or "127.0.0.1"
        if flask_host in ("0.0.0.0", "::"):
            flask_host = "127.0.0.1"
        flask_port = os.getenv("FLASK_PORT") or "4470"

        params = f"printer_index={self._printer_index}"
        api_key = os.getenv("ANKERCTL_API_KEY", "")
        if api_key:
            params += f"&apikey={api_key}"

        mjpeg_url = f"http://{flask_host}:{flask_port}/api/camera/stream?{params}"
        snapshot_url = f"http://{flask_host}:{flask_port}/api/snapshot?{params}"
        return mjpeg_url, snapshot_url

    def _ha_rest_request(self, method, path, body=None, ha_base_url=None, ha_token=None):
        """Execute a HA REST API request via urllib.request.

        Returns the parsed JSON response body on success, or None on error.
        Logs HTTP and network errors at DEBUG level (all are non-fatal).
        """
        url = f"{ha_base_url}{path}"
        headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            log.debug(f"HA REST: {method} {path} → HTTP {err.code}: {err.reason}")
        except urllib.error.URLError as err:
            log.debug(f"HA REST: {method} {path} → {err.reason}")
        except Exception as err:
            log.debug(f"HA REST: {method} {path} → {err}")
        return None

    def _register_ha_mjpeg_camera(self):
        """Register an MJPEG IP Camera config entry in Home Assistant.

        Reads HA_BASE_URL and HA_TOKEN from environment on each call so that
        config changes take effect on reconnect without a service restart.
        Returns silently if either env var is missing or on any error.
        """
        ha_base_url = self._ha_base_url
        ha_token = self._ha_token
        if not ha_base_url or not ha_token:
            return

        mjpeg_url, snapshot_url = self._ha_web_urls()
        camera_name = f"AnkerMake {self._printer_sn}"

        # Idempotency check: look for an existing ankerctl mjpeg entry
        entries = self._ha_rest_request(
            "GET", "/api/config/config_entries",
            ha_base_url=ha_base_url, ha_token=ha_token,
        )
        if entries is None:
            log.debug("HA REST: could not fetch config entries — skipping MJPEG registration")
            return
        if isinstance(entries, list):
            for entry in entries:
                if entry.get("domain") == "mjpeg" and "ankerctl" in entry.get("title", ""):
                    self._ha_mjpeg_entry_id = entry.get("entry_id")
                    log.debug(f"HA REST: existing MJPEG camera entry found ({self._ha_mjpeg_entry_id}), skipping")
                    return

        # Start config flow
        flow_resp = self._ha_rest_request(
            "POST", "/api/config/config_entries/flow",
            body={"handler": "mjpeg", "show_advanced_options": False},
            ha_base_url=ha_base_url, ha_token=ha_token,
        )
        if not flow_resp or "flow_id" not in flow_resp:
            log.warning("HA REST: failed to start MJPEG config flow")
            return

        flow_id = flow_resp["flow_id"]

        # Complete the config flow with camera details
        result = self._ha_rest_request(
            "POST", f"/api/config/config_entries/flow/{flow_id}",
            body={
                "mjpeg_url": mjpeg_url,
                "still_image_url": snapshot_url,
                "name": camera_name,
                "verify_ssl": False,
            },
            ha_base_url=ha_base_url, ha_token=ha_token,
        )
        if not result:
            log.warning("HA REST: failed to complete MJPEG config flow")
            return

        if result.get("type") == "create_entry":
            self._ha_mjpeg_entry_id = result.get("entry_id")
            log.info(f"HA: registered MJPEG camera {self._ha_mjpeg_entry_id}")
        else:
            log.warning(f"HA REST: unexpected flow result type: {result.get('type')}")

    def _unregister_ha_mjpeg_camera(self):
        """Delete the MJPEG IP Camera config entry from Home Assistant on stop."""
        if not self._ha_mjpeg_entry_id:
            return

        ha_base_url = self._ha_base_url
        ha_token = self._ha_token
        if not ha_base_url or not ha_token:
            self._ha_mjpeg_entry_id = None
            return

        entry_id = self._ha_mjpeg_entry_id
        result = self._ha_rest_request(
            "DELETE", f"/api/config/config_entries/{entry_id}",
            ha_base_url=ha_base_url, ha_token=ha_token,
        )
        if result is not None:
            log.info(f"HA: unregistered MJPEG camera {entry_id}")
        else:
            log.debug(f"HA: could not unregister MJPEG camera {entry_id} (may already be gone)")

        self._ha_mjpeg_entry_id = None
