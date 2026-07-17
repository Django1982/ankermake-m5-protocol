import logging as log
import contextlib
import json
import os
import re
from datetime import datetime

from pathlib import Path
from platformdirs import PlatformDirs

from libflagship.megajank import pppp_decode_initstring
from libflagship.httpapi import (
    AnkerHTTPApi,
    AnkerHTTPAppApiV1,
    AnkerHTTPPassportApiV1,
    AnkerHTTPPassportApiV2,
    APIError,
)
from libflagship.util import unhex
from libflagship import logincache

from .model import (
    Serialize,
    Account,
    Printer,
    Config,
    default_notifications_config,
    merge_dict_defaults,
)


class BaseConfigManager:

    def __init__(self, dirs: PlatformDirs, classes=None):
        self._dirs = dirs
        if classes:
            self._classes = {t.__name__: t for t in classes}
        else:
            self._classes = []
        dirs.user_config_path.mkdir(exist_ok=True, parents=True)
        os.chmod(dirs.user_config_path, 0o700)

    @contextlib.contextmanager
    def _borrow(self, value, write, default=None):
        pr = self.load(value, default)
        yield pr
        if write:
            self.save(value, pr)

    @property
    def config_root(self):
        return self._dirs.user_config_path

    def config_path(self, name):
        return self.config_root / Path(f"{name}.json")

    def _load_json(self, val):
        if "__type__" not in val:
            return val

        typename = val["__type__"]
        if typename not in self._classes:
            return val

        return self._classes[typename].from_dict(val)

    @staticmethod
    def _save_json(val):
        if not isinstance(val, Serialize):
            return val

        data = val.to_dict()
        data["__type__"] = type(val).__name__
        return data

    def load(self, name, default):
        path = self.config_path(name)
        if not path.exists():
            return default

        try:
            with path.open() as f:
                return json.load(f, object_hook=self._load_json)
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            corrupt_path = path.with_name(f"{path.name}.corrupt-{int(datetime.now().timestamp())}")
            log.critical(f"Config file {path} is corrupt ({err}); moving aside to {corrupt_path} and using defaults")
            try:
                path.rename(corrupt_path)
            except OSError as rename_err:
                log.critical(f"Failed to move aside corrupt config file {path}: {rename_err}")
            return default

    def save(self, name, value):
        path = self.config_path(name)
        tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp_path.write_text(json.dumps(value, default=self._save_json, indent=2) + "\n")
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)


class AnkerConfigManager(BaseConfigManager):

    def modify(self):
        return self._borrow("default", write=True)

    def open(self):
        return self._borrow("default", write=False, default=Config(account=None, printers=[]))

    def get_api_key(self):
        """Load the API key from config. Returns None if not set."""
        data = self.load("api_key", None)
        if isinstance(data, dict):
            return data.get("key")
        return None

    def set_api_key(self, key):
        """Save the API key to config."""
        self.save("api_key", {"key": key})

    def remove_api_key(self):
        """Remove the API key from config."""
        path = self.config_path("api_key")
        if path.exists():
            path.unlink()


API_KEY_MIN_LENGTH = 16
API_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
HEX_AUTH_TOKEN_RE = re.compile(r'^[0-9a-f]{47,48}$')


def validate_api_key(key):
    """Validate API key format. Returns (ok, error_message)."""
    if len(key) < API_KEY_MIN_LENGTH:
        return False, f"API key must be at least {API_KEY_MIN_LENGTH} characters (got {len(key)})"
    if not API_KEY_PATTERN.match(key):
        return False, "API key may only contain letters, digits, dashes and underscores [a-zA-Z0-9_-]"
    return True, None


def resolve_api_key(config):
    """Resolve API key: ENV var takes precedence over config file."""
    env_key = os.getenv("ANKERCTL_API_KEY")
    if env_key:
        ok, err = validate_api_key(env_key)
        if not ok:
            log.critical(f"ANKERCTL_API_KEY environment variable is invalid: {err}")
            raise SystemExit(1)
        return env_key
    return config.get_api_key()


def configmgr(profile="default"):
    return AnkerConfigManager(PlatformDirs("ankerctl"), classes=(Config, Account, Printer))


def load_config_from_api(auth_token, region, insecure):
    log.info("Initializing API..")
    ppapi = AnkerHTTPPassportApiV1(auth_token=auth_token, region=region, verify=not insecure)

    # request profile and printer list
    log.info("Requesting profile data..")
    profile = ppapi.profile()
    appapi = AnkerHTTPAppApiV1(auth_token=auth_token, user_id=profile["user_id"], region=region, verify=not insecure)

    # create config object
    config = Config(account=Account(
        auth_token=auth_token,
        region=region,
        user_id=profile['user_id'],
        email=profile["email"],
        country=profile.get("country", {}).get("code", ""),
    ), printers=[])

    log.info("Requesting printer list..")
    printers = appapi.query_fdm_list() or []

    log.info("Requesting pppp keys..")
    sns = [pr["station_sn"] for pr in printers]
    dsk_data = appapi.equipment_get_dsk_keys(station_sns=sns) or {}
    dsks = {dsk["station_sn"]: dsk for dsk in dsk_data.get("dsk_keys") or []}

    # populate config object with printer list
    # Sort the list of printers by printer.id
    printers.sort(key=lambda p: p["station_id"])
    for pr in printers:
        station_sn = pr["station_sn"]
        config.printers.append(Printer(
            id=pr["station_id"],
            sn=station_sn,
            name=pr["station_name"],
            model=pr["station_model"],
            create_time=datetime.fromtimestamp(pr["create_time"]),
            update_time=datetime.fromtimestamp(pr["update_time"]),
            mqtt_key=unhex(pr["secret_key"]),
            wifi_mac=pr["wifi_mac"],
            ip_addr=pr["ip_addr"],
            api_hosts=pppp_decode_initstring(pr["app_conn"]),
            p2p_hosts=pppp_decode_initstring(pr["p2p_conn"]),
            p2p_duid=pr["p2p_did"],
            p2p_key=dsks[pr["station_sn"]]["dsk_key"],
        ))
        log.info(f"Adding printer [{station_sn}]")

    return config


def fetch_config_by_login(email, password, region, insecure, captcha_id=None, captcha_answer=None):
    log.info("Initializing API..")
    if not region:
        region = AnkerHTTPApi.guess_region()
        log.info(f"Using region '{region.upper()}'")
    ppapi = AnkerHTTPPassportApiV2(region=region, verify=not insecure)

    log.info("Logging in..")
    login = ppapi.login(email, password, captcha_id=captcha_id, captcha_answer=captcha_answer)
    return login


def import_config_from_server(config, login_data, insecure):
    # extract auth token
    auth_token = login_data["auth_token"]

    # extract account region
    region = logincache.guess_region(login_data["ab_code"])

    candidate_tokens = [auth_token]
    if isinstance(auth_token, str) and len(auth_token) == 47 and HEX_AUTH_TOKEN_RE.match(auth_token):
        log.info("Detected a truncated eufyMake Studio session token; validating candidate prefixes..")
        candidate_tokens.extend(f"{prefix}{auth_token}" for prefix in "0123456789abcdef")

    cfg = None
    last_error = None
    recovered_token = None
    for candidate_token in candidate_tokens:
        try:
            cfg = load_config_from_api(candidate_token, region, insecure)
            recovered_token = candidate_token
            break
        except APIError as err:
            last_error = err
        except Exception as err:
            last_error = err
            if len(candidate_tokens) == 1:
                log.critical(f"Config import failed: {err}")
                raise

    if cfg is None:
        if isinstance(last_error, APIError):
            log.critical(f"Config import failed: {last_error} "
                         "(auth token might be expired: try 'ankerctl config login' to refresh)")
        elif last_error is not None:
            log.critical(f"Config import failed: {last_error}")
        raise last_error

    if recovered_token and recovered_token != auth_token:
        log.info("Recovered full auth token from eufyMake Studio session cache.")
        auth_token = recovered_token
        cfg.account.auth_token = recovered_token

    # keep any user preferences and printer IPs
    existing = config.load("default", None)
    printer_ips = get_printer_ips(config)
    cfg = merge_config_preferences(existing, cfg)

    config.save("default", cfg)
    update_empty_printer_ips(config, printer_ips)


def get_printer_ips(config):
    try:
        with config.open() as cfg:
            printer_ips = dict([[p.sn, p.ip_addr] for p in cfg.printers if p.ip_addr])
    except KeyError:
        printer_ips = {}

    return printer_ips


def update_empty_printer_ips(config, printer_ips):
    with config.modify() as cfg:
        for printer in cfg.printers:
            if not printer.ip_addr and printer.sn in printer_ips:
                log.debug(f"Updating IP address of printer [{printer.sn}] to {printer_ips[printer.sn]}")
                printer.ip_addr = printer_ips[printer.sn]


def merge_config_preferences(existing, new_config):
    if new_config is None:
        return new_config

    if existing is not None and hasattr(existing, "upload_rate_mbps"):
        new_config.upload_rate_mbps = existing.upload_rate_mbps

    if existing is not None and hasattr(existing, "notifications"):
        new_config.notifications = merge_dict_defaults(
            existing.notifications,
            default_notifications_config(),
        )
    else:
        new_config.notifications = merge_dict_defaults(
            getattr(new_config, "notifications", None),
            default_notifications_config(),
        )

    return new_config


def attempt_config_upgrade(config, profile, insecure):
    path = config.config_path("default")
    with path.open() as f:
        data = json.load(f)
    cfg = load_config_from_api(
        data["account"]["auth_token"],
        data["account"]["region"],
        insecure
    )

    # save config to json file named `ankerctl/default.json`
    existing = config.load("default", None)
    cfg = merge_config_preferences(existing, cfg)
    config.save("default", cfg)
    log.info("Finished import")
