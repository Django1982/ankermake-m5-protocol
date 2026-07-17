import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

log = logging.getLogger(__name__)

import web
import web.camera

from libflagship.notifications import AppriseClient

from web import app
from web.lib.service import RunState, ServiceStoppedError

from cli.model import (
    default_notifications_config,
    default_apprise_config,
    merge_dict_defaults,
)

_SNAPSHOT_SIZES = {
    "hd": (1280, 720),
    "fhd": (1920, 1080),
}
_DEFAULT_SNAPSHOT_QUALITY = "hd"
_SNAPSHOT_TIMEOUT = 6
_FRAME_WAIT_TIMEOUT = 2.5
_FRAME_MAX_AGE = 1.5
_SNAPSHOT_KEEPALIVE = 15

_BOOL_TRUE = {"1", "true", "yes", "on", "y", "t"}
_BOOL_FALSE = {"0", "false", "no", "off", "n", "f"}


def _parse_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    return None


def format_duration(seconds):
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        seconds = 0
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_bytes(num_bytes):
    if num_bytes is None:
        return ""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    precision = 0 if size >= 10 or idx == 0 else 1
    return f"{size:.{precision}f} {units[idx]}"


class AppriseNotifier:
    def __init__(self, config_manager, reload_interval=5.0, settings=None, printer_index=None):
        self._config_manager = config_manager
        self._reload_interval = reload_interval
        self._explicit_settings = settings
        self._printer_index = 0 if printer_index is None else int(printer_index)
        self._last_load = 0.0
        self._client = None
        self._settings = None
        self._snapshot_lock = threading.Lock()
        self._snapshot_timer_lock = threading.Lock()
        self._snapshot_timer = None
        self._snapshot_hold_until = 0.0
        self._snapshot_enabled_by_notifier = False
        self._load_lock = threading.Lock()

    def _load(self):
        if self._explicit_settings:
            self._settings = self._explicit_settings
            return

        now = time.monotonic()
        if self._client and (now - self._last_load) < self._reload_interval:
            return

        with self._load_lock:
            # Re-check after acquiring lock (another thread may have loaded)
            now = time.monotonic()
            if self._client and (now - self._last_load) < self._reload_interval:
                return

            try:
                with self._config_manager.open() as cfg:
                    if not cfg:
                        self._client = None
                        self._settings = None
                        self._last_load = now
                        return
                    notifications = merge_dict_defaults(
                        getattr(cfg, "notifications", None),
                        default_notifications_config(),
                    )
                    apprise_config = merge_dict_defaults(
                        notifications.get("apprise"),
                        default_apprise_config(),
                    )
            except Exception as err:
                log.warning(f"Failed to load apprise config: {err}")
                self._client = None
                self._settings = None
                self._last_load = now
                return

            self._client = AppriseClient(apprise_config)
            self._settings = self._client.settings
            self._last_load = now

    def client(self):
        self._load()
        return self._client

    def settings(self):
        self._load()
        return self._settings or {}

    def progress_interval(self, default=25):
        progress = self.settings().get("progress", {})
        interval = None
        if isinstance(progress, dict):
            interval = progress.get("interval_percent")
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = default
        if interval < 1:
            interval = 1
        if interval > 100:
            interval = 100
        return interval

    def progress_max(self):
        progress = self.settings().get("progress", {})
        max_value = None
        if isinstance(progress, dict):
            max_value = progress.get("max_value")
        try:
            max_value = int(max_value)
        except (TypeError, ValueError):
            return None
        if max_value <= 0:
            return None
        return max_value

    def include_image(self):
        progress = self.settings().get("progress", {})
        if isinstance(progress, dict):
            return bool(progress.get("include_image"))
        return False

    def snapshot_quality(self, default=_DEFAULT_SNAPSHOT_QUALITY):
        progress = self.settings().get("progress", {})
        quality = None
        if isinstance(progress, dict):
            quality = progress.get("snapshot_quality")
        if not isinstance(quality, str):
            return default
        quality = quality.strip().lower()
        if quality not in _SNAPSHOT_SIZES:
            return default
        return quality

    def snapshot_fallback(self, default=True):
        progress = self.settings().get("progress", {})
        value = None
        if isinstance(progress, dict):
            value = progress.get("snapshot_fallback")
        parsed = _parse_bool(value)
        if parsed is None:
            return default
        return parsed

    def snapshot_light(self, default=False):
        progress = self.settings().get("progress", {})
        value = None
        if isinstance(progress, dict):
            value = progress.get("snapshot_light")
        parsed = _parse_bool(value)
        if parsed is None:
            return default
        return parsed

    def build_attachments(self, preview_url=None):
        if not self.include_image():
            return None, []
        snapshot = self._capture_live_snapshot()
        if snapshot:
            return [snapshot], [snapshot]
        if preview_url and self.snapshot_fallback():
            return [preview_url], []
        return None, []

    def cleanup_attachments(self, paths):
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass

    def send(self, event, payload=None, attachments=None):
        client = self.client()
        if not client or not client.is_enabled():
            return False, "Apprise disabled"
        if not client.is_event_enabled(event):
            return False, "Event disabled"

        ok, message = client.send(event, payload=payload, attachments=attachments)
        if not ok:
            log.warning(f"Apprise notify failed: {message}")
        return ok, message

    def is_event_enabled(self, event):
        client = self.client()
        if not client or not client.is_enabled():
            return False
        return client.is_event_enabled(event)

    def _await_video_frame(self, vq, timeout=_FRAME_WAIT_TIMEOUT, max_age=_FRAME_MAX_AGE):
        if not hasattr(vq, "last_frame_at"):
            return True
        now = time.monotonic()
        last_frame = getattr(vq, "last_frame_at", None)
        if last_frame and (now - last_frame) <= max_age:
            return True
        deadline = now + timeout
        while time.monotonic() < deadline:
            last_frame = getattr(vq, "last_frame_at", None)
            if last_frame and (time.monotonic() - last_frame) <= max_age:
                return True
            time.sleep(0.1)
        return False

    def _schedule_snapshot_disable(self, was_enabled, keepalive=_SNAPSHOT_KEEPALIVE):
        if keepalive <= 0:
            return
        with self._snapshot_timer_lock:
            if not was_enabled:
                self._snapshot_enabled_by_notifier = True
            if not self._snapshot_enabled_by_notifier:
                return
            now = time.monotonic()
            hold_until = now + keepalive
            if hold_until > self._snapshot_hold_until:
                self._snapshot_hold_until = hold_until
            if self._snapshot_timer:
                self._snapshot_timer.cancel()
            delay = max(0.1, self._snapshot_hold_until - now)
            timer = threading.Timer(delay, self._snapshot_disable)
            timer.daemon = True
            self._snapshot_timer = timer
            timer.start()

    def _snapshot_disable(self):
        with self._snapshot_timer_lock:
            now = time.monotonic()
            if now < self._snapshot_hold_until:
                delay = max(0.1, self._snapshot_hold_until - now)
                timer = threading.Timer(delay, self._snapshot_disable)
                timer.daemon = True
                self._snapshot_timer = timer
                timer.start()
                return
            self._snapshot_timer = None
            enabled_by_notifier = self._snapshot_enabled_by_notifier

        if not enabled_by_notifier:
            return

        vq = web.get_video_service(self._printer_index)
        if not vq:
            with self._snapshot_timer_lock:
                self._snapshot_enabled_by_notifier = False
            return

        refs = getattr(app.svc, "refs", {}).get(
            web.resolve_video_service_name(self._printer_index),
            0,
        )
        if refs:
            with self._snapshot_timer_lock:
                self._snapshot_hold_until = time.monotonic() + _SNAPSHOT_KEEPALIVE
                delay = max(0.1, self._snapshot_hold_until - time.monotonic())
                timer = threading.Timer(delay, self._snapshot_disable)
                timer.daemon = True
                self._snapshot_timer = timer
                timer.start()
            return

        vq.set_video_enabled(False)
        with self._snapshot_timer_lock:
            self._snapshot_enabled_by_notifier = False

    def _capture_live_snapshot(self):
        camera_settings = web._resolve_camera_settings(printer_index=self._printer_index)
        if not camera_settings.get("effective_source"):
            return None

        ffmpeg_path = web._ffmpeg_path()
        if not ffmpeg_path:
            log.warning("Apprise snapshot skipped: ffmpeg not available")
            return None

        using_printer_camera = camera_settings.get("effective_source") == web.camera.CAMERA_SOURCE_PRINTER
        vq = web.get_video_service(self._printer_index) if using_printer_camera else None
        if using_printer_camera and not vq:
            return None

        quality = self.snapshot_quality()
        width, height = _SNAPSHOT_SIZES.get(quality, _SNAPSHOT_SIZES[_DEFAULT_SNAPSHOT_QUALITY])

        temp_path = None

        with self._snapshot_lock:
            use_light = using_printer_camera and self.snapshot_light()
            original_light_state = getattr(vq, "saved_light_state", None) if vq else None
            light_changed = False

            try:
                if use_light and original_light_state is not True:
                    log.info("Apprise snapshot: Turning on light")
                    vq.api_light_state(True)
                    light_changed = True
                    # Give it a moment to actually turn on and for the camera to adjust exposure
                    time.sleep(1.5)

                if using_printer_camera:
                    was_enabled = vq.video_enabled
                    if not was_enabled:
                        vq.set_video_enabled(True)
                    self._schedule_snapshot_disable(was_enabled)
                    if not vq.wanted:
                        vq.start()
                    if vq.state != RunState.Running:
                        vq.await_ready()
                    self._await_video_frame(vq)

                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                temp_path = temp_file.name
                temp_file.close()

                host, port = web._local_web_host_port()
                web.camera.capture_camera_snapshot_to_file(
                    camera_settings,
                    ffmpeg_path,
                    temp_path,
                    host=host,
                    port=port,
                    api_key=app.config.get("api_key"),
                    timeout=_SNAPSHOT_TIMEOUT,
                    scale=(width, height),
                )
                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    return temp_path
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                return None
            except (
                OSError,
                subprocess.SubprocessError,
                ServiceStoppedError,
                subprocess.TimeoutExpired,
                web.camera.CameraCaptureError,
            ) as err:
                log.warning(f"Apprise snapshot failed: {err}")
                if temp_path:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                return None
            finally:
                if light_changed:
                    log.info("Apprise snapshot: Restoring light state")
                    restore_state = original_light_state if original_light_state is not None else False
                    try:
                        vq.api_light_state(restore_state)
                    except Exception as err:
                        log.warning(f"Apprise snapshot: failed to restore light state: {err}")
