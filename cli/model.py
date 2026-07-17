import json
import os
from datetime import datetime
from dataclasses import MISSING, dataclass, field
from platformdirs import PlatformDirs
from libflagship.util import unhex, enhex


DEFAULT_UPLOAD_RATE_MBPS = 10
UPLOAD_RATE_MBPS_CHOICES = (5, 10, 25, 50, 100)
MQTT_CA_CERT_ENVVAR = "ANKERCTL_MQTT_CA_CERT"
HOTEND_STANDARD_NOZZLE_MAX_C = 260
HOTEND_ALL_METAL_NOZZLE_MAX_C = 300
HOTEND_BED_MAX_C = 100


def default_apprise_config():
    return {
        "enabled": False,
        "server_url": "",
        "key": "",
        "tag": "",
        "events": {
            "print_started": True,
            "print_finished": True,
            "print_failed": True,
            "gcode_uploaded": True,
            "print_progress": True,
        },
        "progress": {
            "interval_percent": 25,
            "include_image": False,
            "snapshot_quality": "hd",
            "snapshot_fallback": True,
        },
        "templates": {
            "print_started": "Print started: {filename}",
            "print_finished": "Print finished: {filename} ({duration})",
            "print_failed": "Print failed: {filename} ({reason})",
            "gcode_uploaded": "Upload complete: {filename} ({size})",
            "print_progress": "Progress: {percent}% - {filename}",
        },
    }


def default_notifications_config():
    return {
        "apprise": default_apprise_config(),
    }


def default_timelapse_config():
    return {
        "enabled": os.getenv("TIMELAPSE_ENABLED", "false").lower() in ("true", "1", "yes"),
        "interval": int(os.getenv("TIMELAPSE_INTERVAL_SEC", 30)),
        "max_videos": int(os.getenv("TIMELAPSE_MAX_VIDEOS", 10)),
        "save_persistent": os.getenv("TIMELAPSE_SAVE_PERSISTENT", "true").lower() in ("true", "1", "yes"),
        "output_dir": os.getenv(
            "TIMELAPSE_CAPTURES_DIR",
            os.path.join(str(PlatformDirs("ankerctl").user_config_path), "captures"),
        ),
        "light": os.getenv("TIMELAPSE_LIGHT", None),
        "camera_source": os.getenv("TIMELAPSE_CAMERA_SOURCE", "follow"),
    }


def default_home_assistant_config():
    return {
        "enabled": os.getenv("HA_MQTT_ENABLED", "false").lower() in ("true", "1", "yes"),
        "mqtt_host": os.getenv("HA_MQTT_HOST", "localhost"),
        "mqtt_port": int(os.getenv("HA_MQTT_PORT", 1883)),
        "mqtt_username": os.getenv("HA_MQTT_USER", ""),
        "mqtt_password": os.getenv("HA_MQTT_PASSWORD", ""),
        "discovery_prefix": os.getenv("HA_MQTT_DISCOVERY_PREFIX", "homeassistant"),
        "node_id": "ankermake_m5",
        "ha_base_url": os.getenv("HA_BASE_URL", ""),
        "ha_token": os.getenv("HA_TOKEN", ""),
    }


def default_filament_service_config():
    return {
        "allow_legacy_swap": os.getenv("FILAMENT_ALLOW_LEGACY_SWAP", "false").lower() in ("true", "1", "yes"),
        "manual_swap_preheat_temp_c": int(os.getenv("FILAMENT_MANUAL_SWAP_PREHEAT_TEMP_C", 180)),
        "quick_move_length_mm": float(os.getenv("FILAMENT_QUICK_MOVE_LENGTH_MM", 40)),
        "swap_prime_length_mm": float(os.getenv("FILAMENT_SWAP_PRIME_LENGTH_MM", 10)),
        "swap_unload_length_mm": float(os.getenv("FILAMENT_SWAP_UNLOAD_LENGTH_MM", 40)),
        "swap_load_length_mm": float(os.getenv("FILAMENT_SWAP_LOAD_LENGTH_MM", 120)),
        "swap_home_pause_s": float(os.getenv(
            "FILAMENT_SWAP_HOME_PAUSE_S",
            os.getenv("FILAMENT_SWAP_HOME_SETTLE_S", 70),
        )),
        "per_printer": {},
    }


def default_camera_config():
    return {
        "per_printer": {},
    }


def default_mqtt_ca_cert():
    value = os.getenv(MQTT_CA_CERT_ENVVAR)
    return value or None


def merge_dict_defaults(data, defaults):
    if not isinstance(data, dict):
        return defaults

    merged = {}
    for key, default_value in defaults.items():
        if isinstance(default_value, dict):
            merged[key] = merge_dict_defaults(data.get(key), default_value)
        else:
            merged[key] = data.get(key, default_value)

    for key, value in data.items():
        if key not in merged:
            merged[key] = value

    return merged


class Serialize:

    @classmethod
    def from_dict(cls, data):
        res = {}
        for k, v in cls.__dataclass_fields__.items():
            if k in data:
                value = data[k]
            elif v.default is not MISSING:
                value = v.default
            elif v.default_factory is not MISSING:
                value = v.default_factory()
            else:
                raise KeyError(k)

            if v.type == bytes and isinstance(value, str):
                value = unhex(value)
            elif v.type == datetime and not isinstance(value, datetime):
                value = datetime.fromtimestamp(value)

            res[k] = value
        return cls(**res)

    def to_dict(self):
        res = {}
        for k, v in self.__dataclass_fields__.items():
            res[k] = getattr(self, k)
            if v.type == bytes:
                res[k] = enhex(res[k])
            elif v.type == datetime:
                res[k] = res[k].timestamp()
        return res

    @classmethod
    def from_json(cls, data):
        return cls.from_dict(json.loads(data))

    def to_json(self):
        return json.dumps(self.to_dict())


@dataclass
class Printer(Serialize):
    id: str
    sn: str
    name: str
    model: str
    create_time: datetime
    update_time: datetime
    wifi_mac: str
    ip_addr: str
    mqtt_key: bytes
    api_hosts: list[str]
    p2p_hosts: list[str]
    p2p_duid: str
    p2p_key: str
    p2p_did: str = ""

    @classmethod
    def from_dict(cls, data):
        data = dict(data)

        p2p_duid = data.get("p2p_duid") or data.get("p2p_did", "")
        data["p2p_duid"] = p2p_duid
        data["p2p_did"] = data.get("p2p_did") or p2p_duid

        for key in ("api_hosts", "p2p_hosts"):
            hosts = data.get(key)
            if hosts is None:
                data[key] = []
            elif isinstance(hosts, str):
                data[key] = [hosts] if hosts else []

        return super().from_dict(data)


@dataclass
class Account(Serialize):
    auth_token: str
    region: str
    user_id: str
    email: str
    country: str = ""

    @classmethod
    def from_dict(cls, data):
        if "country" not in data:
            data = {**data, "country": ""}
        return super().from_dict(data)

    @property
    def mqtt_username(self):
        return f"eufy_{self.user_id}"

    @property
    def mqtt_password(self):
        return self.email


@dataclass
class Config(Serialize):
    account: Account
    printers: list[Printer]
    upload_rate_mbps: int = DEFAULT_UPLOAD_RATE_MBPS
    notifications: dict = field(default_factory=default_notifications_config)
    timelapse: dict = field(default_factory=default_timelapse_config)
    home_assistant: dict = field(default_factory=default_home_assistant_config)
    filament_service: dict = field(default_factory=default_filament_service_config)
    camera: dict = field(default_factory=default_camera_config)
    active_printer_index: int = field(default=0)

    @classmethod
    def from_dict(cls, data):
        if "upload_rate_mbps" not in data:
            data = {**data, "upload_rate_mbps": DEFAULT_UPLOAD_RATE_MBPS}

        if "active_printer_index" not in data:
            data = {**data, "active_printer_index": 0}
        else:
            try:
                data = {**data, "active_printer_index": int(data["active_printer_index"])}
            except (ValueError, TypeError):
                data = {**data, "active_printer_index": 0}

        data = {
            **data,
            "notifications": merge_dict_defaults(
                data.get("notifications"),
                default_notifications_config(),
            ),
            "timelapse": merge_dict_defaults(
                data.get("timelapse"),
                default_timelapse_config(),
            ),
            "home_assistant": merge_dict_defaults(
                data.get("home_assistant"),
                default_home_assistant_config(),
            ),
            "filament_service": merge_dict_defaults(
                data.get("filament_service"),
                default_filament_service_config(),
            ),
            "camera": merge_dict_defaults(
                data.get("camera"),
                default_camera_config(),
            ),
        }
        return super().from_dict(data)

    def __bool__(self):
        return bool(self.account)
