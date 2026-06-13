import json
import logging
import os
import queue
import re
import threading
import time
from enum import Enum

from ..lib.service import Service, ServiceRestartSignal
from .. import app

from libflagship.util import enhex
from libflagship.mqtt import MqttMsgType
from libflagship.notifications.events import (
    EVENT_PRINT_STARTED,
    EVENT_PRINT_FINISHED,
    EVENT_PRINT_FAILED,
    EVENT_PRINT_PROGRESS,
)

log = logging.getLogger("mqtt")


class PrintState(Enum):
    """Printer state as seen by the MQTT state machine.

    IDLE ──────────────────────────────────────────────────────┐
      │ mark_pending_print_start()    │ ct=1000 val=1          │
      v                               │ ct=1001 progress>0     │
    PREPARING                         │ (_transition_to_active) │
      │ ct=1044 + pending             v                        │
      v                          PRINTING ─────────────────────┘
    PRE_PRINT ──────────────────────^  │ │      (_reset)
      │ ct=1000 val=1 (upgrade)        │ v
      └─────────────────────────>      │ PAUSED ──ct=1000 val=3──>PRINTING
                                       v   │
                                    FAILED  │ ct=1000 val=0+stop
                                       │   v
                                       └─>FAILED──────────────────┘
                                                   (_reset)
    """
    IDLE = "idle"
    PREPARING = "preparing"
    PRE_PRINT = "pre_print"
    PRINTING = "printing"
    PAUSED = "paused"
    FAILED = "failed"


MQTT_PRINT_STATE_LABELS = {
    0: "idle",
    1: "printing",
    2: "paused",
    3: "resume_ack",
    8: "preparing_or_aborted",
}

FILAMENT_STATE_LABELS = {
    "unknown": "Unknown",
    "loaded": "Loaded",
    "changing": "Changing",
    "not_loaded": "Not Loaded",
}

FILAMENT_ISSUE_LABELS = {
    "runout": "Filament runout",
}

PAUSE_REASON_LABELS = {
    "filament_runout": "Filament runout",
}

FILAMENT_RUNOUT_ERROR_CODE = "0xFF01030001"
FILAMENT_RUNOUT_CONFIRM_WINDOW_SEC = 8.0
TIMELAPSE_START_PROMPT_BOOT_WINDOW_SEC = 20.0
STOP_CONFIRMATION_COMMAND_TYPES = {
    1057,  # Observed firmware stop-complete reply on stored-file jobs
}

G28_DEDUPE_WINDOW_SEC = 10.0
MQTT_NO_RESPONSE_TIMEOUT_SEC = 90.0
STORED_FILE_SELECTION_TIMEOUT_SEC = 2.0
STORED_FILE_START_CONFIRM_TIMEOUT_SEC = 12.0
STORED_FILE_ONBOARD_START_CONFIRM_TIMEOUT_SEC = 20.0
STORED_FILE_LIST_PAGE_SIZE = 47
RECENT_COMPLETION_GUARD_SEC = 120.0
STORED_FILE_SOURCE_ROOTS = (
    "/tmp/udisk/",
    "/usr/data/local/model/",
)
STORED_FILE_TMPMODEL_ROOT = "/usr/data/local/tmpmodel/"
HOME_MOVE_ZERO_VALUE_BY_AXIS = {
    "xy": 0,
    "z": 2,
}


import cli.mqtt
import cli.model
from ..notifications import AppriseNotifier, format_duration
from .history import PrintHistory
from .timelapse import TimelapseService
from .homeassistant import HomeAssistantService


class MqttQueue(Service):
    def __init__(self, printer_index):
        self.printer_index = printer_index
        super().__init__()
        self.persistent = True
        self._state_lock = threading.RLock()
        self._print_state_event = threading.Event()

    @property
    def name(self):
        return f"MqttQueue[{self.printer_index}]"

    def worker_init(self):
        self._notifier = AppriseNotifier(app.config["config"], printer_index=self.printer_index)
        self._notification_queue = queue.Queue()
        self._notification_thread = threading.Thread(
            target=self._notification_worker,
            daemon=True,
            name=f"{self.name}-notify",
        )
        self._notification_thread.start()
        config_root = str(app.config["config"].config_root)
        self._history = PrintHistory(
            db_path=f"{config_root}/history.db",
            printer_index=self.printer_index,
        )
        try:
            self._timelapse = TimelapseService(app.config["config"], printer_index=self.printer_index)
        except Exception as err:
            log.error(f"TimelapseService init failed, timelapse disabled: {err}", exc_info=True)
            self._timelapse = None

        # Home Assistant MQTT Discovery
        printer_sn = None
        printer_name = None
        self._control_username = None
        self._control_user_id = None
        with app.config["config"].open() as cfg:
            if cfg and getattr(cfg, "account", None):
                self._control_username = getattr(cfg.account, "email", None)
                self._control_user_id = getattr(cfg.account, "user_id", None)
            if cfg and cfg.printers:
                printer = cfg.printers[self.printer_index]
                printer_sn = getattr(printer, "sn", None)
                printer_name = getattr(printer, "name", None) or "AnkerMake M5"
        self._ha = HomeAssistantService(
            app.config["config"],
            printer_sn=printer_sn,
            printer_name=printer_name,
            printer_index=self.printer_index,
        )
        # HomeAssistantService.__init__ calls reload_config() which calls start()
        # when enabled, so no explicit start() call is needed here.
        self._printer_name = printer_name or "AnkerMake M5"
        self._printer_sn = printer_sn

        self._reset_print_state()
        self._gcode_layer_count = None  # Override from GCode header, survives print resets
        self._last_message_time = 0.0
        self._silent_reconnect_count = 0  # Persists across reconnects; only reset on real message
        self._nozzle_temp = None
        self._nozzle_temp_target = None
        self._nozzle_temp_updated_at = 0.0
        self._bed_temp = None
        self._bed_temp_target = None
        self._fan_speed = None
        self._z_offset_steps = None
        self._z_offset_updated_at = 0.0
        self._z_offset_seq = 0
        self._z_offset_cond = threading.Condition()
        self._filament_state = "unknown"
        self._filament_change_value = None
        self._filament_change_progress = None
        self._filament_change_step_len = None
        self._filament_issue = None
        self._filament_issue_code = None
        self._filament_vendor = None
        self._filament_type = None
        self._filament_metadata_source = None
        self._pause_reason = None
        self._filament_runout_pending = False
        self._filament_runout_pending_at = 0.0
        self._stored_file_selection_cond = threading.Condition(self._state_lock)
        self._stored_file_preview_request_lock = threading.Lock()
        self._stored_file_preview_cache = {}
        self._stored_file_preview_seq = 0
        self._last_selected_storage_file_path = None
        self._last_selected_storage_file_name = None
        self._pending_stored_file_path = None
        self._preview_file_path = None
        self._last_g28_command = None
        self._last_g28_command_at = 0.0
        self._recent_completion_filename = None
        self._recent_completion_task_id = None
        self._recent_completion_at = 0.0

    def set_gcode_layer_count(self, count: int):
        """Store the layer count extracted from a GCode header for UI display."""
        self._gcode_layer_count = count

    def set_gcode_filament_info(self, vendor=None, type=None):
        """Store slicer filament metadata for the next print."""
        self._filament_vendor = self._clean_filament_metadata(vendor)
        self._filament_type = self._clean_filament_metadata(type)
        self._filament_metadata_source = "gcode" if self._filament_vendor or self._filament_type else None

    def reset_reconnect_backoff(self):
        """Cancel any reconnect back-off and attempt connection immediately.

        Called when a new WebSocket client connects or the user clicks Reconnect.
        Only effective when the service is in a Starting/holdoff state.
        """
        self._silent_reconnect_count = 0
        self.wake()

    def worker_start(self):
        mqtt_ca_cert = app.config.get("mqtt_ca_cert") or cli.model.default_mqtt_ca_cert()
        self.client = cli.mqtt.mqtt_open(
            app.config["config"],
            self.printer_index,
            app.config["insecure"],
            mqtt_ca_cert,
        )
        self._reset_print_state()
        self._ha.start()
        self._ha.update_state(mqtt_connected=True)
        self._last_query = 0
        self._connection_started_at = time.time()
        self._timelapse_start_prompt_window_until = (
            time.monotonic() + TIMELAPSE_START_PROMPT_BOOT_WINDOW_SEC
        )

    def _reset_print_state(self):
        self._state = PrintState.IDLE
        self._last_state_value = 0
        self._print_started_at = None
        self._last_progress = None
        self._last_progress_bucket = None
        self._last_interval = None
        self._last_filename = None
        self._last_task_id = None
        self._failure_sent = False
        self._preview_url = None
        self._preview_file_path = None
        self._pending_history_start = False
        self._stop_requested = False
        self._pending_archive_info = None
        self._pending_stored_file_path = None
        self._last_selected_storage_file_path = None
        self._last_selected_storage_file_name = None
        self._last_print_schedule_filename = None
        self._last_print_schedule_seen_at = 0.0
        self._pending_prepare_state_logged = False
        self._filament_vendor = None
        self._filament_type = None
        self._filament_metadata_source = None
        self._clear_timelapse_start_offer()
        self._pause_reason = None
        self._filament_runout_pending = False
        self._filament_runout_pending_at = 0.0
        if hasattr(self, "_timelapse"):
            self._sync_timelapse_capture_pause()
        # Preserve debug setting across resets if possible, but init here if missing
        if not hasattr(self, "_debug_log_payloads"):
             self._debug_log_payloads = False
        if not hasattr(self, "_print_state_event"):
            self._print_state_event = threading.Event()

    def _record_failure(self, payload, progress, reason):
        """Record a print failure: history, timelapse, HA state, and notification event."""
        self._remember_recent_completion()
        self._history.record_fail(filename=self._last_filename, reason=reason, task_id=self._last_task_id)
        if self._timelapse:
            self._timelapse.fail_capture()
        self._ha.update_state(print_status="failed")
        self._send_event(
            EVENT_PRINT_FAILED,
            self._build_payload(payload, progress, failure_reason=reason),
        )
        self._failure_sent = True
        self._state = PrintState.FAILED

    def _sync_timelapse_capture_pause(self):
        set_capture_paused = getattr(self._timelapse, "set_capture_paused", None)
        if not callable(set_capture_paused):
            return

        reason = None
        if (
            getattr(self, "_filament_issue", None) == "runout"
            or getattr(self, "_pause_reason", None) == "filament_runout"
        ):
            reason = "filament_runout"
        elif getattr(self, "_filament_state", None) == "changing":
            reason = "filament_change"

        set_capture_paused(reason is not None, reason=reason)

    def _clear_timelapse_start_offer(self):
        self._timelapse_start_prompt_pending = False
        self._timelapse_start_prompt_filename = None

    def _mark_timelapse_start_offer(self, filename=None):
        normalized = os.path.basename(str(filename or self._last_filename or "")).strip()
        self._timelapse_start_prompt_pending = True
        self._timelapse_start_prompt_filename = normalized or None

    def _should_prompt_for_timelapse_start(self):
        if not getattr(self._timelapse, "enabled", False):
            return False
        if self._state != PrintState.IDLE:
            return False
        window_until = getattr(self, "_timelapse_start_prompt_window_until", 0.0)
        return bool(window_until) and time.monotonic() <= window_until

    @staticmethod
    def _print_state_value_label(value):
        return MQTT_PRINT_STATE_LABELS.get(value, f"unknown_{value}")

    def _transition_from_paused_to_printing(self):
        if self._state != PrintState.PAUSED:
            return False

        if getattr(self, "_pause_reason", None) == "filament_runout":
            self._filament_issue = None
            self._filament_issue_code = None
            if getattr(self, "_filament_state", "unknown") in ("unknown", "not_loaded", "changing"):
                self._filament_state = "loaded"

        self._state = PrintState.PRINTING
        self._pause_reason = None
        self._ha.update_state(print_status="printing")
        self._sync_timelapse_capture_pause()
        return True

    def _transition_to_active(self, payload, progress, filename=None):
        """Activate print state.  Handles both fresh activation and upgrade
        from the pre-print window.  No-op if already fully active.

        Both the ct=1000 (state change) and ct=1001 (progress) handlers call
        this so print activation logic lives in exactly one place.  Returns
        True if the print was activated, False if already active.
        """
        if self._state not in (PrintState.IDLE, PrintState.PREPARING, PrintState.PRE_PRINT, PrintState.FAILED):
            return False

        previous_state = self._state
        defer_timelapse_start = (
            previous_state == PrintState.IDLE
            and self._should_prompt_for_timelapse_start()
        )

        self._state = PrintState.PRINTING
        self._print_started_at = time.monotonic()
        self._failure_sent = False
        self._last_progress_bucket = None
        self._pause_reason = None

        effective_filename = filename or self._last_filename

        if effective_filename:
            archive_info = self._claim_pending_archive_info(effective_filename)
            record_kwargs = {"task_id": self._last_task_id}
            if archive_info:
                record_kwargs["archive_relpath"] = archive_info.get("archive_relpath")
                record_kwargs["archive_size"] = archive_info.get("archive_size")
            if self._preview_url:
                record_kwargs["preview_url"] = self._preview_url
            self._history.record_start(effective_filename, **record_kwargs)
            self._pending_history_start = False
            log.info(f"Print started, filename={effective_filename!r}")
            if defer_timelapse_start:
                self._mark_timelapse_start_offer(effective_filename)
                log.info(
                    "Timelapse: active print detected on startup; waiting for user confirmation"
                )
            else:
                self._clear_timelapse_start_offer()
                if self._timelapse:
                    self._timelapse.start_capture(effective_filename)
        else:
            self._pending_history_start = True
            if defer_timelapse_start:
                self._mark_timelapse_start_offer()
                log.info("Print started, history deferred and timelapse awaiting user confirmation")
            else:
                log.info("Print started, history deferred (no filename yet)")

        self._ha.update_state(print_status="printing")
        self._send_event(EVENT_PRINT_STARTED, self._build_payload(payload, progress))

        return True

    def _complete_deferred_print_start(self):
        """Record and start helpers when filename arrives after print activation."""
        if self._state == PrintState.PRINTING and self._pending_history_start and self._last_filename:
            archive_info = self._claim_pending_archive_info(self._last_filename)
            record_kwargs = {"task_id": self._last_task_id}
            if archive_info:
                record_kwargs["archive_relpath"] = archive_info.get("archive_relpath")
                record_kwargs["archive_size"] = archive_info.get("archive_size")
            if self._preview_url:
                record_kwargs["preview_url"] = self._preview_url
            self._history.record_start(self._last_filename, **record_kwargs)
            if getattr(self, "_timelapse_start_prompt_pending", False):
                self._mark_timelapse_start_offer(self._last_filename)
            elif self._timelapse:
                self._timelapse.start_capture(self._last_filename)
            self._pending_history_start = False
            log.info(f"Print start completed after filename arrived, filename={self._last_filename!r}")

    @property
    def is_printing(self):
        with self._state_lock:
            return self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED)

    @property
    def is_preparing_print(self):
        # Hardware preparing state (firmware sent value=8) while not actively printing.
        # Distinct from PREPARING enum state (software pending start from FileTransferService).
        with self._state_lock:
            return self._last_state_value == 8 and self._state not in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED)

    @property
    def has_pending_print_start(self):
        with self._state_lock:
            return self._state == PrintState.PREPARING

    @property
    def history(self):
        return self._history

    @property
    def timelapse(self):
        return self._timelapse

    @property
    def ha(self):
        return self._ha

    @property
    def last_message_time(self):
        return self._last_message_time

    @property
    def nozzle_temp(self):
        return self._nozzle_temp

    @property
    def nozzle_temp_updated_at(self):
        return getattr(self, "_nozzle_temp_updated_at", 0.0)

    @property
    def nozzle_temp_target(self):
        return self._nozzle_temp_target

    @property
    def z_offset_steps(self):
        return self._z_offset_steps

    @property
    def z_offset_mm(self):
        if self._z_offset_steps is None:
            return None
        return round(self._z_offset_steps / 100.0, 2)

    def request_status(self):
        self._send_status_query()

    def _claim_pending_archive_info(self, filename=None):
        archive_info = getattr(self, "_pending_archive_info", None)
        if not archive_info:
            return None
        expected_name = os.path.basename(str(archive_info.get("filename") or ""))
        target_name = os.path.basename(str(filename or ""))
        if (
            expected_name
            and target_name
            and expected_name != target_name
            and self._normalize_pending_archive_filename(expected_name)
            != self._normalize_pending_archive_filename(target_name)
        ):
            return None
        self._pending_archive_info = None
        return archive_info

    _FILENAME_SANITIZE_RE = re.compile(r'[^\w.\-]')
    _MULTI_DOT_RE = re.compile(r'\.{2,}')
    _FILAMENT_TYPE_FROM_FILENAME_RE = re.compile(
        r"(?:^|[_\-\s.])(PLA|PETG|ABS|ASA|TPU|PC|PA|NYLON|PVA|HIPS)(?:$|[_\-\s.])",
        re.IGNORECASE,
    )

    @staticmethod
    def _clean_filament_metadata(value):
        cleaned = str(value or "").strip().strip('"').strip()
        return cleaned or None

    def _infer_filament_type_from_filename(self, filename):
        if self._filament_metadata_source == "gcode":
            return
        match = self._FILAMENT_TYPE_FROM_FILENAME_RE.search(os.path.basename(str(filename or "")))
        self._filament_vendor = None
        self._filament_type = match.group(1).upper() if match else None
        self._filament_metadata_source = "filename" if match else None

    def _filament_material_label(self):
        parts = [
            value
            for value in (
                getattr(self, "_filament_vendor", None),
                getattr(self, "_filament_type", None),
            )
            if value
        ]
        return " ".join(parts) if parts else None

    @staticmethod
    def _normalize_pending_archive_filename(filename):
        base = os.path.basename(str(filename or ""))
        normalized = MqttQueue._FILENAME_SANITIZE_RE.sub('_', base).lstrip(".")
        normalized = MqttQueue._MULTI_DOT_RE.sub('.', normalized)
        return normalized.lower()

    def _clear_recent_completion(self):
        self._recent_completion_filename = None
        self._recent_completion_task_id = None
        self._recent_completion_at = 0.0
        self._recent_completion_bare_state_logged = False
        self._recent_completion_stale_update_logged = False

    def _remember_recent_completion(self, filename=None, task_id=None):
        saved_filename = os.path.basename(str(filename or self._last_filename or "")).strip()
        saved_task_id = str(task_id or self._last_task_id or "").strip()
        self._recent_completion_filename = saved_filename or None
        self._recent_completion_task_id = saved_task_id or None
        self._recent_completion_at = time.monotonic()
        self._recent_completion_bare_state_logged = False
        self._recent_completion_stale_update_logged = False

    def _recent_completion_matches(self, *, task_id=None, filename=None):
        recent_completion_at = getattr(self, "_recent_completion_at", 0.0)
        if not recent_completion_at:
            return False
        if (time.monotonic() - recent_completion_at) > RECENT_COMPLETION_GUARD_SEC:
            return False

        recent_task_id = getattr(self, "_recent_completion_task_id", None)
        recent_filename = getattr(self, "_recent_completion_filename", None)
        normalized_task_id = str(task_id or "").strip() or None
        normalized_filename = os.path.basename(str(filename or "")).strip() or None
        if normalized_task_id and recent_task_id == normalized_task_id:
            return True
        if normalized_filename and recent_filename == normalized_filename:
            return True
        return False

    def mark_pending_print_start(self, filename=None, task_id=None, archive_info=None):
        with self._state_lock:
            if filename:
                self._last_filename = filename
                self._infer_filament_type_from_filename(filename)
            if task_id:
                self._last_task_id = task_id
            if archive_info and archive_info.get("archive_relpath"):
                self._pending_archive_info = {
                    "filename": filename,
                    "archive_relpath": archive_info.get("archive_relpath"),
                    "archive_size": archive_info.get("archive_size"),
                }
            self._clear_recent_completion()
            self._state = PrintState.PREPARING
            self._pending_prepare_state_logged = False
            self._stop_requested = False
        log.info("Marked pending print start for %r", self._last_filename)

    def _ensure_stored_file_selection_state(self):
        if not hasattr(self, "_stored_file_selection_cond"):
            self._stored_file_selection_cond = threading.Condition(self._state_lock)
        if not hasattr(self, "_last_selected_storage_file_path"):
            self._last_selected_storage_file_path = None
        if not hasattr(self, "_last_selected_storage_file_name"):
            self._last_selected_storage_file_name = None
        if not hasattr(self, "_pending_stored_file_path"):
            self._pending_stored_file_path = None
        if not hasattr(self, "_stored_file_preview_request_lock"):
            self._stored_file_preview_request_lock = threading.Lock()
        if not hasattr(self, "_stored_file_preview_cache"):
            self._stored_file_preview_cache = {}
        if not hasattr(self, "_stored_file_preview_seq"):
            self._stored_file_preview_seq = 0

    @staticmethod
    def _is_stored_file_source_path(file_path):
        file_path = str(file_path or "")
        return any(file_path.startswith(root) for root in STORED_FILE_SOURCE_ROOTS)

    @staticmethod
    def _is_tmpmodel_path(file_path):
        return str(file_path or "").startswith(STORED_FILE_TMPMODEL_ROOT)

    @classmethod
    def _is_preprint_preview_path(cls, file_path):
        file_path = str(file_path or "")
        return cls._is_tmpmodel_path(file_path) or (
            file_path.startswith("/tmp/")
            and not file_path.startswith("/tmp/udisk/")
        )

    def _note_stored_file_selection(self, *, file_path=None, file_name=None):
        self._ensure_stored_file_selection_state()
        with self._stored_file_selection_cond:
            if file_path and self._is_stored_file_source_path(file_path):
                self._last_selected_storage_file_path = file_path
            if file_name:
                self._last_selected_storage_file_name = str(file_name)
            self._stored_file_selection_cond.notify_all()

    def _await_stored_file_selection(self, file_path, timeout_sec=STORED_FILE_SELECTION_TIMEOUT_SEC):
        self._ensure_stored_file_selection_state()
        file_path = str(file_path or "").strip()
        if not file_path:
            return False

        def selection_ready():
            return self._last_selected_storage_file_path == file_path

        with self._stored_file_selection_cond:
            if selection_ready():
                return True
            self._stored_file_selection_cond.wait_for(selection_ready, timeout=timeout_sec)
            return selection_ready()

    def _cache_stored_file_preview(self, file_path, preview_url):
        self._ensure_stored_file_selection_state()
        file_path = str(file_path or "").strip()
        preview_url = str(preview_url or "").strip()
        if not file_path or not preview_url:
            return
        with self._stored_file_selection_cond:
            self._stored_file_preview_seq += 1
            self._stored_file_preview_cache[file_path] = {
                "url": preview_url,
                "seq": self._stored_file_preview_seq,
            }
            self._stored_file_selection_cond.notify_all()

    def get_cached_stored_file_preview_url(self, file_path):
        self._ensure_stored_file_selection_state()
        file_path = str(file_path or "").strip()
        if not file_path:
            return None
        with self._stored_file_selection_cond:
            cached = self._stored_file_preview_cache.get(file_path) or {}
            return cached.get("url")

    def get_stored_file_preview_url(self, file_path, timeout_sec=2.5, allow_probe=True):
        self._ensure_stored_file_selection_state()
        file_path = str(file_path or "").strip()
        if not file_path:
            raise ValueError("Stored file path is required")

        cached_url = self.get_cached_stored_file_preview_url(file_path)
        if cached_url or not allow_probe:
            return cached_url

        with self._stored_file_preview_request_lock:
            cached_url = self.get_cached_stored_file_preview_url(file_path)
            if cached_url:
                return cached_url

            with self._stored_file_selection_cond:
                baseline_seq = self._stored_file_preview_seq

            self.client.command(self._build_stored_file_list_request_payload(file_path))
            time.sleep(0.12)
            self.client.command(self._build_stored_file_request_payload(file_path))

            def preview_ready():
                cached = self._stored_file_preview_cache.get(file_path) or {}
                return bool(cached.get("url")) and cached.get("seq", 0) > baseline_seq

            with self._stored_file_selection_cond:
                if not preview_ready():
                    self._stored_file_selection_cond.wait_for(preview_ready, timeout=timeout_sec)
                cached = self._stored_file_preview_cache.get(file_path) or {}
                return cached.get("url")

    def _send_start_print_control(self):
        nested_data = {"value": 0}
        if self._control_username:
            nested_data["userName"] = self._control_username
        self.client.command({
            "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
            "data": nested_data,
        })
        self.client.command({
            "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
            "value": 0,
        })

    @staticmethod
    def _derive_control_display_name(value):
        value = str(value or "").strip()
        if not value:
            return None
        if "@" in value:
            local_part = value.split("@", 1)[0].strip()
            if local_part:
                return local_part
        return value

    def _build_stored_file_request_payload(self, file_path):
        payload = {
            "commandType": MqttMsgType.ZZ_MQTT_CMD_GCODE_FILE_REQUEST.value,
            "filePath": file_path,
            "type": 0,
        }
        if self._control_user_id:
            payload["userId"] = self._control_user_id
        return payload

    def _build_stored_file_list_request_payload(self, file_path):
        source = cli.mqtt.infer_storage_source_from_path(file_path)
        value = cli.mqtt.mqtt_file_list_source_value(source=source or "onboard")
        payload = {
            "commandType": MqttMsgType.ZZ_MQTT_CMD_FILE_LIST_REQUEST.value,
            "value": value,
            "isFirst": 1,
            "index": 1,
            "num": STORED_FILE_LIST_PAGE_SIZE,
        }
        if self._control_user_id:
            payload["userId"] = self._control_user_id
        return payload

    def _send_stored_file_start_print_control(self, file_path):
        payload = {
            "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
            "value": 1,
            "printMode": 1,
            "filePath": file_path,
        }
        display_name = self._derive_control_display_name(self._control_username)
        if display_name:
            payload["userName"] = display_name
        if self._control_user_id:
            payload["userId"] = self._control_user_id
        self.client.command(payload)

    @staticmethod
    def _stored_file_start_timeout_sec(file_path):
        file_path = str(file_path or "")
        if file_path.startswith("/usr/data/local/model/"):
            return STORED_FILE_ONBOARD_START_CONFIRM_TIMEOUT_SEC
        return STORED_FILE_START_CONFIRM_TIMEOUT_SEC

    def _stored_file_start_confirmed(self, file_path=None):
        target_name = os.path.basename(str(file_path or "")) or None
        with self._state_lock:
            preview_path = getattr(self, "_preview_file_path", None)
            preview_name = os.path.basename(preview_path) if preview_path else None
            schedule_name = getattr(self, "_last_print_schedule_filename", None)
            return (
                self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED)
                or self.is_preparing_print
                or (target_name is not None and schedule_name == target_name)
                or (
                    preview_path
                    and self._is_preprint_preview_path(preview_path)
                    and (target_name is None or preview_name == target_name)
                )
            )

    def _await_stored_file_start_confirmation(
        self,
        file_path,
        timeout_sec=STORED_FILE_START_CONFIRM_TIMEOUT_SEC,
    ):
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self._stored_file_start_confirmed(file_path):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._print_state_event.clear()
            self._print_state_event.wait(timeout=min(0.1, remaining))
        return self._stored_file_start_confirmed(file_path)

    @staticmethod
    def _extract_z_offset_steps(payload):
        if not isinstance(payload, dict):
            return None
        for key in ("value", "zAxisRecoup", "z_axis_recoup", "zOffset", "z_offset"):
            steps = MqttQueue._safe_int(payload.get(key))
            if steps is not None:
                return steps
        return None

    def _handle_z_offset_update(self, payload):
        if not isinstance(payload, dict):
            return
        if payload.get("commandType") != MqttMsgType.ZZ_MQTT_CMD_Z_AXIS_RECOUP.value:
            return

        steps = self._extract_z_offset_steps(payload)
        if steps is None:
            return

        with self._z_offset_cond:
            self._z_offset_steps = steps
            self._z_offset_updated_at = time.time()
            self._z_offset_seq += 1
            self._z_offset_cond.notify_all()

    def _z_offset_state(self, *, source="cached"):
        return {
            "available": self._z_offset_steps is not None,
            "steps": self._z_offset_steps,
            "mm": self.z_offset_mm,
            "updated_at": self._z_offset_updated_at or None,
            "source": source,
        }

    def get_z_offset_state(self):
        with self._z_offset_cond:
            return self._z_offset_state()

    def wait_for_z_offset_update(self, *, after_seq=None, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self._z_offset_cond:
            while True:
                if self._z_offset_steps is not None and (
                    after_seq is None or self._z_offset_seq > after_seq
                ):
                    state = self._z_offset_state(source="live")
                    state["seq"] = self._z_offset_seq
                    return state

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for MQTT 1021 Z-offset update")
                self._z_offset_cond.wait(timeout=remaining)

    def refresh_z_offset(self, timeout=5.0):
        with self._z_offset_cond:
            seq = self._z_offset_seq
        self._send_status_query()
        return self.wait_for_z_offset_update(after_seq=seq, timeout=timeout)

    def wait_for_z_offset_target(self, target_steps, *, after_seq=None, timeout=8.0):
        deadline = time.monotonic() + timeout
        next_query = 0.0

        while True:
            with self._z_offset_cond:
                if self._z_offset_steps == target_steps and (
                    after_seq is None or self._z_offset_seq > after_seq
                ):
                    state = self._z_offset_state(source="confirmed")
                    state["seq"] = self._z_offset_seq
                    return state

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                self._z_offset_cond.wait(timeout=min(remaining, 0.5))

            now = time.monotonic()
            if now >= next_query:
                self._send_status_query()
                next_query = now + 1.0

        current = self.get_z_offset_state()
        current_steps = current.get("steps")
        current_mm = current.get("mm")
        current_text = f"{current_mm:.2f}" if current_mm is not None else "unknown"
        raise TimeoutError(
            f"Timed out waiting for MQTT 1021 to confirm {target_steps / 100.0:.2f} mm "
            f"(last seen: {current_text} mm / {current_steps} steps)"
        )

    def worker_run(self, timeout):
        now = time.time()

        connection_age = now - getattr(self, "_connection_started_at", now)
        last_activity = max(self._last_message_time, getattr(self, "_connection_started_at", 0))
        silence = now - last_activity
        if silence > MQTT_NO_RESPONSE_TIMEOUT_SEC:
            count = getattr(self, "_silent_reconnect_count", 0) + 1
            self._silent_reconnect_count = count
            if count <= 3:
                holdoff = 1.0
            else:
                holdoff = min(60.0 * (2 ** (count - 4)), 300.0)
            log.warning(
                "MQTT: No messages for %.0f s (connection age %.0f s, silent_count=%d) "
                "— reconnecting in %.0f s",
                silence, connection_age, count, holdoff,
            )
            raise ServiceRestartSignal("MQTT connection stuck: no messages received", delay=holdoff)

        # Poll status every 10 seconds if idle
        if now - self._last_query > 10.0:
            self._send_status_query()
            self._last_query = now

        for msg, body in self.client.fetch(timeout=timeout):
            self._last_message_time = time.time()
            self._silent_reconnect_count = 0
            log.debug(f"TOPIC [{msg.topic}]")
            log.debug(enhex(msg.payload[:]))
            if body and getattr(self, "_debug_log_payloads", False):
                log.info(f"DEBUG MQTT PAYLOAD: {json.dumps(body, default=str)}")

            for obj in body:
                self._handle_z_offset_update(obj)
                # Override total_layer with GCode header value when available
                if (
                    isinstance(obj, dict)
                    and obj.get("commandType") == MqttMsgType.ZZ_MQTT_CMD_MODEL_LAYER.value
                    and self._gcode_layer_count is not None
                ):
                    obj = dict(obj, total_layer=self._gcode_layer_count)
                self.notify(obj)
                self._forward_to_ha(obj)
                with self._state_lock:
                    self._handle_notification(obj)
                self._print_state_event.set()

    def worker_stop(self):
        self._ha.update_state(mqtt_connected=False)
        self._ha.stop()
        if hasattr(self, "client"):
            del self.client

    def _send_status_query(self):
        cmd = {
            "commandType": MqttMsgType.ZZ_MQTT_CMD_APP_QUERY_STATUS.value,
            "value": 0
        }
        try:
            self.client.query(cmd)
        except (OSError, TimeoutError) as e:
            log.warning(f"Failed to query printer status: {e}")

    @staticmethod
    def _safe_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_temp(value):
        temp = MqttQueue._safe_int(value)
        if temp is None:
            return None
        return temp // 100 if temp > 1000 else temp

    @staticmethod
    def _normalize_progress(value, max_value=None):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if max_value is not None:
            try:
                max_value = float(max_value)
            except (TypeError, ValueError):
                max_value = None
        if max_value is not None and max_value > 0:
            if number < 0:
                number = 0
            if number > max_value:
                number = max_value
            return int((number / max_value) * 100)
        is_fractional = isinstance(value, float)
        if isinstance(value, str) and "." in value:
            is_fractional = True
        if number < 0:
            return 0
        if 0 < number <= 1 and is_fractional:
            number *= 100
        elif number > 100:
            if number <= 10000:
                number /= 100
            else:
                number = 100
        return int(number)

    def _extract_progress(self, payload):
        if not isinstance(payload, dict):
            return None
        max_value = self._notifier.progress_max()
        if "progress" in payload:
            return MqttQueue._normalize_progress(payload.get("progress"), max_value=max_value)
        for key, value in payload.items():
            if isinstance(key, str) and "progress" in key.lower():
                progress = MqttQueue._normalize_progress(value, max_value=max_value)
                if progress is not None:
                    return progress
            if isinstance(value, dict) and "progress" in value:
                progress = MqttQueue._normalize_progress(value.get("progress"), max_value=max_value)
                if progress is not None:
                    return progress
        return None

    @staticmethod
    def _extract_filename(payload):
        for key in ("name", "fileName", "filename", "file_name", "gcode", "gcode_name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_time(payload, keys):
        for key in keys:
            if key in payload:
                value = MqttQueue._safe_int(payload.get(key))
                if value is not None:
                    return value
        return None

    @staticmethod
    def _extract_preview_url(payload):
        for key in (
            "preview_url",
            "previewUrl",
            "previewImageUrl",
            "preview_image_url",
            "image_url",
            "imageUrl",
            "img",
            "img_url",
            "imgUrl",
            "url",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if "preview" in key.lower() and value.startswith(("http://", "https://")):
                return value
        return None

    @staticmethod
    def _extract_failure_reason(payload):
        for key in ("error", "errorMsg", "errorMessage", "failReason", "reason"):
            value = payload.get(key)
            if value:
                return str(value)
        status = payload.get("status") or payload.get("state") or payload.get("printStatus")
        if isinstance(status, str):
            status_text = status.lower()
            if any(word in status_text for word in ("fail", "error", "abort", "cancel", "stop")):
                return status
        return None

    @staticmethod
    def _extract_task_id(payload):
        for key in ("task_id", "taskId", "taskID", "taskid"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_status_text(payload):
        for key in ("status", "state", "printStatus", "statusType", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        return None

    @staticmethod
    def _normalize_filament_state(payload):
        if not isinstance(payload, dict):
            return "unknown"

        value = MqttQueue._safe_int(payload.get("value"))
        progress = MqttQueue._safe_int(payload.get("progress"))
        if "stepLen" in payload:
            step_len_raw = payload.get("stepLen")
        else:
            step_len_raw = payload.get("step_len")
        step_len = MqttQueue._safe_int(step_len_raw)

        if value == 0:
            return "loaded"
        # Firmware exposes filament change mode more clearly than a strict
        # "filament present" sensor. Treat any active movement/progress as
        # a changing state, and the idle 0/0/0 mode as loaded/ready.
        if (progress is not None and progress > 0) or (step_len is not None and step_len > 0):
            return "changing"
        if value is None:
            return "unknown"
        return "changing"

    def _filament_detail(self):
        state = getattr(self, "_filament_state", "unknown")
        issue = getattr(self, "_filament_issue", None)
        progress = getattr(self, "_filament_change_progress", None)

        if state == "changing":
            if progress is not None and 0 < progress < 100:
                return f"Filament swap in progress ({progress}%)"
            return "Filament swap in progress"

        if state == "loaded" and getattr(self, "_pause_reason", None) == "filament_runout" and self._state == PrintState.PAUSED:
            return "Filament loaded. Resume the print when ready."

        if issue == "runout":
            if self._state == PrintState.PAUSED:
                return "Paused: Filament runout. Reload filament to continue."
            return "Filament runout or break detected."

        return None

    def _clear_filament_runout_pending(self):
        self._filament_runout_pending = False
        self._filament_runout_pending_at = 0.0

    def _mark_filament_runout_pending(self):
        self._filament_runout_pending = True
        self._filament_runout_pending_at = time.monotonic()

    def _has_recent_filament_runout_pending(self):
        if not getattr(self, "_filament_runout_pending", False):
            return False
        pending_at = getattr(self, "_filament_runout_pending_at", 0.0)
        if not pending_at:
            return False
        return (time.monotonic() - pending_at) <= FILAMENT_RUNOUT_CONFIRM_WINDOW_SEC

    def _record_printer_alert(self, *, alert_type, title, message, level="warning", cooldown_sec=30):
        record = getattr(app, "record_printer_alert", None)
        if callable(record):
            return record(
                printer_index=self.printer_index,
                printer_name=getattr(self, "_printer_name", f"Printer {self.printer_index + 1}"),
                alert_type=alert_type,
                title=title,
                message=message,
                level=level,
                cooldown_sec=cooldown_sec,
            )
        return None

    def _mark_filament_runout(self):
        newly_detected = getattr(self, "_filament_issue", None) != "runout"
        self._clear_filament_runout_pending()
        self._filament_state = "not_loaded"
        self._filament_issue = "runout"
        self._filament_issue_code = FILAMENT_RUNOUT_ERROR_CODE
        if self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED):
            self._pause_reason = "filament_runout"
        if newly_detected:
            self._record_printer_alert(
                alert_type="filament_runout",
                title="Filament runout",
                message="Filament runout or break detected.",
                level="warning",
                cooldown_sec=45,
            )
        return newly_detected

    def _record_print_complete_alert(self, filename=None):
        finished_name = os.path.basename(str(filename or self._last_filename or "")).strip()
        message = f"{finished_name} finished printing." if finished_name else "Print finished."
        self._record_printer_alert(
            alert_type="print_complete",
            title="Print complete",
            message=message,
            level="success",
            cooldown_sec=60,
        )

    def _update_filament_state(self, payload):
        if not isinstance(payload, dict):
            return
        command_type = payload.get("commandType")

        if command_type == MqttMsgType.ZZ_MQTT_CMD_ENTER_OR_QUIT_MATERIEL:
            self._filament_change_value = self._safe_int(payload.get("value"))
            self._filament_change_progress = self._safe_int(payload.get("progress"))
            if "stepLen" in payload:
                step_len_raw = payload.get("stepLen")
            else:
                step_len_raw = payload.get("step_len")
            self._filament_change_step_len = self._safe_int(step_len_raw)
            normalized_state = self._normalize_filament_state(payload)
            in_filament_runout_pause = (
                self._state == PrintState.PAUSED
                and getattr(self, "_pause_reason", None) == "filament_runout"
                and getattr(self, "_filament_issue", None) == "runout"
            )

            if in_filament_runout_pause and normalized_state == "loaded":
                # The firmware can emit a transient value=0 between unload/reload
                # stages during a runout recovery. Keep the UI conservative until
                # the print actually resumes.
                self._filament_state = "not_loaded"
            else:
                self._filament_state = normalized_state

            if getattr(self, "_filament_issue", None) != "runout" and self._filament_state in ("changing", "loaded"):
                self._clear_filament_runout_pending()
            if not in_filament_runout_pause and self._filament_state in ("changing", "loaded"):
                self._filament_issue = None
                self._filament_issue_code = None
            self._sync_timelapse_capture_pause()
            return

        if command_type == 1085 and str(payload.get("errorCode") or "") == FILAMENT_RUNOUT_ERROR_CODE:
            self._mark_filament_runout_pending()
            return

        if command_type == 1086 and str(payload.get("errorCode") or "") == FILAMENT_RUNOUT_ERROR_CODE:
            if getattr(self, "_filament_issue", None) != "runout":
                self._clear_filament_runout_pending()
                self._sync_timelapse_capture_pause()
            return

        if (
            command_type == MqttMsgType.ZZ_MQTT_CMD_EVENT_NOTIFY
            and self._safe_int(payload.get("subType")) == 2
            and self._safe_int(payload.get("value")) == 6
        ):
            self._mark_filament_runout()
            self._sync_timelapse_capture_pause()
            return

    def _forward_to_ha(self, payload):
        """Update cached MQTT state and forward relevant data to Home Assistant."""
        if not isinstance(payload, dict):
            return

        command_type = payload.get("commandType")
        ha_updates = {}

        # Nozzle temperature (command 1003 = 0x03eb)
        if command_type == MqttMsgType.ZZ_MQTT_CMD_NOZZLE_TEMP:
            current_raw = payload.get("currentTemp") if "currentTemp" in payload else payload.get("value")
            target_raw = payload.get("targetTemp") if "targetTemp" in payload else payload.get("target")
            current = self._safe_int(current_raw)
            target = self._safe_int(target_raw)
            if current is not None:
                # Temps may come in 1/100th degree units
                self._nozzle_temp = self._normalize_temp(current)
                self._nozzle_temp_updated_at = time.time()
                ha_updates["nozzle_temp"] = self._nozzle_temp
            if target is not None:
                self._nozzle_temp_target = self._normalize_temp(target)
                ha_updates["nozzle_temp_target"] = self._nozzle_temp_target

        # Bed temperature (command 1004 = 0x03ec)
        elif command_type == MqttMsgType.ZZ_MQTT_CMD_HOTBED_TEMP:
            current_raw = payload.get("currentTemp") if "currentTemp" in payload else payload.get("value")
            target_raw = payload.get("targetTemp") if "targetTemp" in payload else payload.get("target")
            current = self._safe_int(current_raw)
            target = self._safe_int(target_raw)
            if current is not None:
                self._bed_temp = self._normalize_temp(current)
                ha_updates["bed_temp"] = self._bed_temp
            if target is not None:
                self._bed_temp_target = self._normalize_temp(target)
                ha_updates["bed_temp_target"] = self._bed_temp_target

        # Part cooling fan speed (command 1005 = 0x03ed), reported as percent.
        elif command_type == MqttMsgType.ZZ_MQTT_CMD_FAN_SPEED:
            speed_raw = payload.get("value") if "value" in payload else payload.get("speed")
            speed = self._safe_int(speed_raw)
            if speed is not None:
                self._fan_speed = max(0, min(100, speed))
                ha_updates["fan_speed"] = self._fan_speed

        # Print speed (command 1006 = 0x03ee)
        elif command_type == MqttMsgType.ZZ_MQTT_CMD_PRINT_SPEED:
            speed_raw = payload.get("value") if "value" in payload else payload.get("speed")
            speed = self._safe_int(speed_raw)
            if speed is not None:
                ha_updates["print_speed"] = speed

        # Layer info (command 1052 = 0x041c)
        elif command_type == MqttMsgType.ZZ_MQTT_CMD_MODEL_LAYER:
            layer = payload.get("value") or payload.get("layer") or payload.get("currentLayer")
            total_layers = payload.get("totalLayer") or payload.get("total")
            if layer is not None:
                layer_str = str(layer)
                if total_layers is not None:
                    layer_str = f"{layer}/{total_layers}"
                ha_updates["print_layer"] = layer_str

        # Print schedule / event notify — extract progress, filename, times
        elif command_type in (
            MqttMsgType.ZZ_MQTT_CMD_PRINT_SCHEDULE,
            MqttMsgType.ZZ_MQTT_CMD_EVENT_NOTIFY,
        ):
            progress = self._extract_progress(payload)
            if progress is not None:
                ha_updates["print_progress"] = progress

            filename = self._extract_filename(payload)
            if filename:
                ha_updates["print_filename"] = filename

            elapsed = self._extract_time(payload, ("totalTime", "elapsed", "elapsedTime"))
            remaining = self._extract_time(payload, ("time", "remainTime", "remaining", "remainingTime"))
            if elapsed is not None:
                ha_updates["time_elapsed"] = elapsed
            if remaining is not None:
                ha_updates["time_remaining"] = remaining

            # Derive print status
            if self._state == PrintState.PAUSED:
                ha_updates["print_status"] = "paused"
            elif self._state in (PrintState.PRE_PRINT, PrintState.PRINTING):
                ha_updates["print_status"] = "printing"
            elif progress is not None and progress >= 100 and self._state == PrintState.PRINTING:
                ha_updates["print_status"] = "complete"

        if ha_updates and self._ha.enabled:
            self._ha.update_state(**ha_updates)

    def _handle_notification(self, payload):
        if not isinstance(payload, dict):
            return

        command_type = payload.get("commandType")
        self._update_filament_state(payload)
        preview_url = self._extract_preview_url(payload)
        if preview_url:
            self._preview_url = preview_url

        if command_type == MqttMsgType.ZZ_MQTT_CMD_GCODE_FILE_REQUEST.value and payload.get("reply") == 0:
            self._note_stored_file_selection(file_name=payload.get("fileName"))

        # --- commandType 1044: preview image or staged tmpmodel update ---
        if command_type == 1044:
            file_path = payload.get("filePath", "")
            if file_path:
                self._preview_file_path = file_path
                self._last_filename = os.path.basename(file_path)
                log.info(f"History: captured filename from ct 1044: {self._last_filename}")
                if preview_url:
                    self._cache_stored_file_preview(file_path, preview_url)
                self._note_stored_file_selection(file_path=file_path, file_name=self._last_filename)
                if preview_url:
                    self._history.update_preview_url(
                        preview_url,
                        filename=self._last_filename,
                        task_id=self._last_task_id,
                    )
            # If ct=1000 value=1 arrived before ct=1044, record the start now that we have the filename
            self._complete_deferred_print_start()
            # Activate print early so Stop sends value=4 (not the prepare-cancel path)
            # during the G28/calibration window before ct=1000 value=1 arrives.
            # Only do this for staged tmpmodel paths; source storage previews arrive
            # during stored-file selection and should not count as an active print yet.
            if self._is_preprint_preview_path(file_path) and (self._state == PrintState.PREPARING or self.is_preparing_print):
                self._state = PrintState.PRE_PRINT
                self._stop_requested = False
                log.info("Early print activation via ct 1044")
            return

        if (
            command_type in STOP_CONFIRMATION_COMMAND_TYPES
            and payload.get("reply") == 0
            and self._stop_requested
            and self._state in (PrintState.PREPARING, PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED)
        ):
            log.info(
                "History: print cancelled (firmware stop confirmation %s), filename=%r",
                command_type,
                self._last_filename,
            )
            self._record_failure(payload, self._last_progress or 0, "cancelled")
            self._reset_print_state()
            return

        # --- commandType 1000: printer state machine transitions ---
        if command_type == 1000:
            value = payload.get("value")
            # Snapshot the previous state before mutating it so transitions like
            # prepare(8) -> idle(0) can still be classified correctly.
            was_preparing_print = self.is_preparing_print
            was_pending_start = self._state == PrintState.PREPARING
            stop_was_requested = self._stop_requested
            previous_state = self._state
            self._last_state_value = value
            if value == 1 and self._state == PrintState.PAUSED:
                if self._transition_from_paused_to_printing():
                    log.info("Print resumed (ct 1000 value=1)")
            elif value == 1:
                if (
                    previous_state == PrintState.IDLE
                    and not self._last_filename
                    and not self._last_task_id
                    and not was_preparing_print
                    and not was_pending_start
                    and getattr(self, "_recent_completion_at", 0.0)
                    and (time.monotonic() - getattr(self, "_recent_completion_at", 0.0)) <= RECENT_COMPLETION_GUARD_SEC
                ):
                    if not getattr(self, "_recent_completion_bare_state_logged", False):
                        log.info("Ignoring bare ct 1000 value=1 immediately after print completion")
                        self._recent_completion_bare_state_logged = True
                else:
                    self._transition_to_active(payload, progress=0)
            elif value == 2 and self._state == PrintState.PRINTING:
                if self._has_recent_filament_runout_pending() and getattr(self, "_filament_issue", None) != "runout":
                    self._mark_filament_runout()
                self._state = PrintState.PAUSED
                if getattr(self, "_filament_issue", None) == "runout":
                    self._pause_reason = "filament_runout"
                self._sync_timelapse_capture_pause()
                self._ha.update_state(print_status="paused")
                log.info("Print paused (ct 1000 value=2)")
            elif value == 3 and self._state == PrintState.PAUSED:
                if self._transition_from_paused_to_printing():
                    log.info("Print resumed (ct 1000 value=3)")
            elif value == 0 and (self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED) or was_preparing_print or was_pending_start):
                if self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED):
                    if self._stop_requested:
                        # Print was cancelled via stop command
                        log.info(f"History: print cancelled (ct 1000 value=0 after stop), filename={self._last_filename!r}")
                        self._record_failure(payload, self._last_progress or 0, "cancelled")
                    else:
                        # Print ended normally
                        log.info(f"History: print finished (ct 1000 value=0), filename={self._last_filename!r}")
                        self._record_print_complete_alert()
                        self._remember_recent_completion()
                        self._history.record_finish(filename=self._last_filename, task_id=self._last_task_id)
                        if self._timelapse:
                            self._timelapse.finish_capture(final=True)
                        self._ha.update_state(print_status="complete", print_progress=100)
                        self._send_event(
                            EVENT_PRINT_FINISHED,
                            self._build_payload(payload, 100),
                            include_image=True,
                        )
                elif self._stop_requested:
                    # Pre-print setup or a queued start was cancelled before the print became active.
                    log.info(f"History: pre-print start cancelled, filename={self._last_filename!r}")
                    self._record_failure(payload, self._last_progress or 0, "cancelled")
                else:
                    log.info("Pending print start ended without an active print; resetting state")
                self._reset_print_state()
            elif value == 8 and self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED):
                if self._state == PrintState.PRE_PRINT:
                    # value=8 during G28/calibration is normal pre-print state, not a real abort
                    log.info(
                        "History: ignoring pre-print state 8 (not a real abort yet), filename=%r",
                        self._last_filename,
                    )
                else:
                    log.info(
                        "History: print aborted (ct 1000 value=8), filename=%r",
                        self._last_filename,
                    )
                    self._record_failure(payload, self._last_progress or 0, "aborted")
                    self._reset_print_state()
            elif value == 8:
                if self._state == PrintState.PREPARING:
                    if not getattr(self, "_pending_prepare_state_logged", False):
                        log.info("Pending print start entered firmware prepare state (ct 1000 value=8)")
                        self._pending_prepare_state_logged = True
                else:
                    self._state = PrintState.IDLE
            log.debug(
                "Printer state trace: ct=1000 value=%r (%s) internal=%s->%s stop_requested=%s pending_start=%s preparing=%s",
                value,
                self._print_state_value_label(value),
                previous_state.value,
                self._state.value,
                stop_was_requested,
                was_pending_start,
                was_preparing_print,
            )
            return

        if command_type not in (
            MqttMsgType.ZZ_MQTT_CMD_PRINT_SCHEDULE,
            MqttMsgType.ZZ_MQTT_CMD_EVENT_NOTIFY,
        ):
            return

        progress = self._extract_progress(payload)
        if progress is None:
            return
        if progress < 0:
            progress = 0
        if progress > 100:
            progress = 100

        prev_task_id = self._last_task_id
        prev_filename = self._last_filename

        task_id = self._extract_task_id(payload)
        incoming_filename = self._extract_filename(payload)
        if (
            task_id
            and self._state == PrintState.IDLE
            and not self.has_pending_print_start
            and not self.is_preparing_print
            and self._recent_completion_matches(task_id=task_id, filename=incoming_filename)
        ):
            if not getattr(self, "_recent_completion_stale_update_logged", False):
                log.info(
                    "Ignoring stale post-completion update for task_id=%s filename=%r",
                    task_id,
                    incoming_filename,
                )
                self._recent_completion_stale_update_logged = True
            return
        if task_id:
            if self._last_task_id and task_id != self._last_task_id and self._state == PrintState.PRINTING:
                if progress <= 1:
                    self._reset_print_state()
                    self._last_task_id = task_id
                else:
                    task_id = self._last_task_id
            else:
                self._last_task_id = task_id

        filename = incoming_filename
        if filename:
            if command_type == MqttMsgType.ZZ_MQTT_CMD_PRINT_SCHEDULE:
                self._last_print_schedule_filename = filename
                self._last_print_schedule_seen_at = time.monotonic()
            if self._last_filename and filename != self._last_filename and self._state == PrintState.PRINTING:
                if progress <= 1:
                    self._reset_print_state()
                    self._last_filename = filename
                else:
                    filename = self._last_filename
            else:
                self._last_filename = filename
                self._infer_filament_type_from_filename(filename)
            self._complete_deferred_print_start()

        pending_name = os.path.basename(self._pending_stored_file_path) if self._pending_stored_file_path else None
        if (
            command_type == MqttMsgType.ZZ_MQTT_CMD_PRINT_SCHEDULE
            and self._state == PrintState.PREPARING
            and pending_name
            and filename == pending_name
            and progress == 0
        ):
            self._state = PrintState.PRE_PRINT
            self._stop_requested = False
            log.info("Stored file start entered pre-print state via print schedule for %r", filename)

        if self._last_progress is not None and progress < self._last_progress and progress <= 1:
            same_task = task_id and prev_task_id and task_id == prev_task_id
            same_file = filename and prev_filename and filename == prev_filename
            if not (same_task or same_file):
                self._reset_print_state()

        failure_reason = self._extract_failure_reason(payload)
        if failure_reason and self._state == PrintState.PRINTING and not self._failure_sent:
            self._record_failure(payload, progress, failure_reason)
            return

        if 0 < progress < 100 and self._state not in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED):
            self._transition_to_active(payload, progress, filename=filename)

        status_text = self._extract_status_text(payload)
        if self._state == PrintState.PRINTING and status_text:
            if any(word in status_text for word in ("finish", "complete", "done")):
                self._record_print_complete_alert()
                self._send_event(
                    EVENT_PRINT_FINISHED,
                    self._build_payload(payload, 100),
                    include_image=True,
                )
                self._remember_recent_completion()
                self._history.record_finish(filename=self._last_filename, task_id=self._last_task_id)
                if self._timelapse:
                    self._timelapse.finish_capture(final=True)
                self._ha.update_state(print_status="complete", print_progress=100)
                self._reset_print_state()
                return

        if self._state == PrintState.PRINTING and progress >= 100:
            self._record_print_complete_alert()
            self._send_event(
                EVENT_PRINT_FINISHED,
                self._build_payload(payload, 100),
                include_image=True,
            )
            self._remember_recent_completion()
            self._history.record_finish(filename=self._last_filename, task_id=self._last_task_id)
            if self._timelapse:
                self._timelapse.finish_capture(final=True)
            self._ha.update_state(print_status="complete", print_progress=100)
            self._reset_print_state()
            return


        self._emit_progress(payload, progress)
        self._last_progress = progress

    def _emit_progress(self, payload, progress):
        if self._state != PrintState.PRINTING:
            return
        if progress <= 0 or progress >= 100:
            return
        if not self._notifier.is_event_enabled(EVENT_PRINT_PROGRESS):
            return
        interval = self._notifier.progress_interval()

        if self._last_interval and self._last_interval != interval:
            if self._last_progress_bucket is not None:
                # Convert previous bucket index to new interval scale
                progress_covered = self._last_progress_bucket * self._last_interval
                self._last_progress_bucket = progress_covered // interval
        self._last_interval = interval

        bucket = progress // interval
        if bucket <= 0:
            return
        if self._last_progress_bucket is not None and bucket <= self._last_progress_bucket:
            return
        self._last_progress_bucket = bucket
        self._send_event(
            EVENT_PRINT_PROGRESS,
            self._build_payload(payload, progress),
            include_image=True,
        )

    def _build_payload(self, payload, progress, failure_reason=None):
        elapsed = self._extract_time(payload, ("totalTime", "elapsed", "elapsedTime"))
        remaining = self._extract_time(payload, ("time", "remainTime", "remaining", "remainingTime"))
        duration = None
        if elapsed is not None and remaining is not None:
            duration = elapsed + remaining
        elif elapsed is not None:
            duration = elapsed
        elif self._print_started_at is not None:
            duration = int(time.monotonic() - self._print_started_at)

        filename = self._extract_filename(payload) or self._last_filename or "-"
        payload_out = {
            "filename": filename,
            "percent": progress,
            "elapsed_seconds": elapsed if elapsed is not None else "",
            "remaining_seconds": remaining if remaining is not None else "",
            "duration_seconds": duration if duration is not None else "",
            "elapsed": format_duration(elapsed),
            "remaining": format_duration(remaining),
            "duration": format_duration(duration),
        }
        if failure_reason:
            payload_out["reason"] = failure_reason
        return payload_out

    def _send_event(self, event, payload, include_image=False):
        # Snapshot capture and the HTTP POST to Apprise can take ~10s; hand the
        # work off to _notification_worker so callers (often holding
        # _state_lock) never block on it. preview_url is captured now since
        # _reset_print_state() may clear self._preview_url right after this call.
        self._notification_queue.put((event, payload, include_image, self._preview_url))

    def _notification_worker(self):
        while True:
            event, payload, include_image, preview_url = self._notification_queue.get()
            try:
                attachments = None
                cleanup_paths = []
                if include_image and self._notifier.is_event_enabled(event):
                    attachments, cleanup_paths = self._notifier.build_attachments(preview_url=preview_url)
                self._notifier.send(event, payload=payload, attachments=attachments)
                if cleanup_paths:
                    self._notifier.cleanup_attachments(cleanup_paths)
            except Exception:
                log.exception(f"{self.name}: failed to dispatch notification for event {event!r}")
            finally:
                self._notification_queue.task_done()

    def set_debug_logging(self, enabled):
        self._debug_log_payloads = enabled
        log.info(f"Debug logging {'enabled' if enabled else 'disabled'}")

    def get_state(self):
        """Return structured internal state for debug inspection."""
        timelapse_state_getter = getattr(self._timelapse, "get_runtime_state", None)
        if callable(timelapse_state_getter):
            timelapse_state = timelapse_state_getter()
        else:
            capture_thread = getattr(self._timelapse, "_capture_thread", None)
            timelapse_state = {
                "enabled": getattr(self._timelapse, "enabled", None),
                "capturing": bool(capture_thread and capture_thread.is_alive()),
            }
        prompt_filename = (
            getattr(self, "_timelapse_start_prompt_filename", None)
            or self._last_filename
        )
        timelapse_state = dict(timelapse_state)
        timelapse_state["prompt_start"] = bool(
            getattr(self, "_timelapse_start_prompt_pending", False)
            and self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED)
            and not timelapse_state.get("capturing")
        )
        timelapse_state["prompt_filename"] = prompt_filename
        if timelapse_state["prompt_start"] and not timelapse_state.get("detail"):
            timelapse_state["detail"] = "Open Timelapse to continue or dismiss capture for this print."
        return {
            "print": {
                "print_state": self._state.value,
                "active": self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED),
                "in_pre_print_window": self._state == PrintState.PRE_PRINT,
                "pending_start": self._state == PrintState.PREPARING,
                "state": self._last_state_value,
                "state_label": self._print_state_value_label(self._last_state_value),
                "preparing": self.is_preparing_print,
                "started_at": self._print_started_at,
                "last_progress": self._last_progress,
                "last_filename": self._last_filename,
                "last_task_id": self._last_task_id,
                "failure_sent": self._failure_sent,
                "preview_url": self._preview_url,
                "pause_reason": getattr(self, "_pause_reason", None),
                "pause_reason_label": PAUSE_REASON_LABELS.get(getattr(self, "_pause_reason", None)),
            },
            "temperature": {
                "nozzle": self._nozzle_temp,
                "nozzle_target": self._nozzle_temp_target,
                "bed": self._bed_temp,
                "bed_target": self._bed_temp_target,
            },
            "telemetry": {
                "fan_speed": self._fan_speed,
            },
            "z_offset": self.get_z_offset_state(),
            "filament": {
                "state": getattr(self, "_filament_state", "unknown"),
                "label": FILAMENT_STATE_LABELS.get(getattr(self, "_filament_state", "unknown"), "Unknown"),
                "material_label": self._filament_material_label(),
                "vendor": getattr(self, "_filament_vendor", None),
                "type": getattr(self, "_filament_type", None),
                "metadata_source": getattr(self, "_filament_metadata_source", None),
                "loaded": True if getattr(self, "_filament_state", "unknown") == "loaded" else None,
                "issue": getattr(self, "_filament_issue", None),
                "issue_label": FILAMENT_ISSUE_LABELS.get(getattr(self, "_filament_issue", None)),
                "issue_code": getattr(self, "_filament_issue_code", None),
                "detail": self._filament_detail(),
                "pause_reason": getattr(self, "_pause_reason", None),
                "pause_reason_label": PAUSE_REASON_LABELS.get(getattr(self, "_pause_reason", None)),
                "raw_value": getattr(self, "_filament_change_value", None),
                "progress": getattr(self, "_filament_change_progress", None),
                "step_len": getattr(self, "_filament_change_step_len", None),
            },
            "debug_logging": getattr(self, "_debug_log_payloads", False),
            "timelapse": timelapse_state,
        }

    def start_timelapse_for_current_print(self):
        with self._state_lock:
            if not getattr(self._timelapse, "enabled", False):
                raise RuntimeError("Timelapse is disabled.")
            if self._state not in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED):
                raise RuntimeError("No active print is available for timelapse.")
            filename = os.path.basename(str(self._last_filename or "")).strip()
            if not filename:
                raise RuntimeError("Current print filename is not available yet.")
            self._clear_timelapse_start_offer()

        self._timelapse.start_capture(filename)
        return filename

    def dismiss_timelapse_start_offer(self):
        with self._state_lock:
            filename = os.path.basename(
                str(getattr(self, "_timelapse_start_prompt_filename", None) or self._last_filename or "")
            ).strip() or None
            self._clear_timelapse_start_offer()
        discard_pending_resume = getattr(self._timelapse, "discard_pending_resume", None)
        if callable(discard_pending_resume):
            discard_pending_resume(filename)

    def pause_timelapse_for_current_print(self):
        with self._state_lock:
            if not getattr(self._timelapse, "enabled", False):
                raise RuntimeError("Timelapse is disabled.")
            if self._state not in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED):
                raise RuntimeError("No active print is available for timelapse.")

        has_active_capture = getattr(self._timelapse, "has_active_capture", None)
        if not callable(has_active_capture) or not has_active_capture():
            raise RuntimeError("No active timelapse capture is running.")

        self._timelapse.set_manual_pause(True)
        return os.path.basename(str(self._last_filename or "")).strip() or None

    def resume_timelapse_for_current_print(self):
        with self._state_lock:
            if not getattr(self._timelapse, "enabled", False):
                raise RuntimeError("Timelapse is disabled.")
            if self._state not in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED):
                raise RuntimeError("No active print is available for timelapse.")

        has_active_capture = getattr(self._timelapse, "has_active_capture", None)
        if not callable(has_active_capture) or not has_active_capture():
            raise RuntimeError("No active timelapse capture is running.")
        runtime_state_getter = getattr(self._timelapse, "get_runtime_state", None)
        if callable(runtime_state_getter):
            runtime_state = runtime_state_getter()
            if not runtime_state.get("manual_paused"):
                raise RuntimeError("Timelapse is not paused manually.")

        self._timelapse.set_manual_pause(False)
        return os.path.basename(str(self._last_filename or "")).strip() or None

    def stop_timelapse_for_current_print(self):
        with self._state_lock:
            if not getattr(self._timelapse, "enabled", False):
                raise RuntimeError("Timelapse is disabled.")
            prompt_pending = bool(getattr(self, "_timelapse_start_prompt_pending", False))
            if self._state not in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED) and not prompt_pending:
                raise RuntimeError("No active print is available for timelapse.")
            filename = os.path.basename(str(self._last_filename or "")).strip() or None
            self._clear_timelapse_start_offer()

        has_active_capture = getattr(self._timelapse, "has_active_capture", None)
        if prompt_pending and (not callable(has_active_capture) or not has_active_capture()):
            discard_pending_resume = getattr(self._timelapse, "discard_pending_resume", None)
            if callable(discard_pending_resume):
                discard_pending_resume(filename)
            return filename

        if not callable(has_active_capture) or not has_active_capture():
            raise RuntimeError("No active timelapse capture is running.")

        self._timelapse.finish_capture(final=True)
        return filename

    def simulate_event(self, event_type, payload=None):
        """Simulate an MQTT event for testing.

        Supported event types:
          start    — simulate print start
          finish   — simulate print finish
          fail     — simulate print failure
          progress — emit a fake ZZ_MQTT_CMD_PRINT_SCHEDULE message
                     payload: {progress: 0-100, filename: str, elapsed: int, remaining: int}
          temperature — emit fake nozzle/bed temp notification
                        payload: {temp_type: 'nozzle'|'bed', current: int, target: int}
                        (values in 1/100 degrees, e.g. 21000 = 210 deg C)
          speed    — emit fake print speed notification
                     payload: {speed: int}
          layer    — emit fake layer notification
                     payload: {current_layer: int, total_layers: int}
        """
        if not payload:
            payload = {}
        log.info(f"Simulating event: {event_type} with payload {payload}")

        if event_type == "start":
            self._state = PrintState.PRINTING
            self._print_started_at = time.monotonic()
            self._send_event(EVENT_PRINT_STARTED, self._build_payload(payload, 0))
            self._history.record_start(payload.get("filename") or "simulated.gcode")

        elif event_type == "finish":
            self._send_event(EVENT_PRINT_FINISHED, self._build_payload(payload, 100))
            self._history.record_finish(filename=payload.get("filename"))
            self._reset_print_state()

        elif event_type == "fail":
            self._send_event(EVENT_PRINT_FAILED, self._build_payload(payload, 50, failure_reason="Simulation"))
            self._history.record_fail(filename=payload.get("filename"), reason="Simulation")
            self._reset_print_state()

        elif event_type == "progress":
            progress = int(payload.get("progress", 50))
            sim_payload = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_SCHEDULE,
                "progress": progress,
                "name": payload.get("filename", "simulated.gcode"),
                "totalTime": int(payload.get("elapsed", 0)),
                "time": int(payload.get("remaining", 0)),
            }
            self.notify(sim_payload)

        elif event_type == "temperature":
            temp_type = payload.get("temp_type", "nozzle")
            current = int(payload.get("current", 0))
            target = int(payload.get("target", 0))
            if temp_type == "bed":
                cmd_type = MqttMsgType.ZZ_MQTT_CMD_HOTBED_TEMP
            else:
                cmd_type = MqttMsgType.ZZ_MQTT_CMD_NOZZLE_TEMP
            sim_payload = {
                "commandType": cmd_type,
                "currentTemp": current,
                "targetTemp": target,
            }
            self.notify(sim_payload)

        elif event_type == "speed":
            speed = int(payload.get("speed", 250))
            sim_payload = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_SPEED,
                "value": speed,
            }
            self.notify(sim_payload)

        elif event_type == "layer":
            current_layer = int(payload.get("current_layer", 1))
            total_layers = int(payload.get("total_layers", 200))
            sim_payload = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_MODEL_LAYER,
                "value": current_layer,
                "totalLayer": total_layers,
            }
            self.notify(sim_payload)

    def send_gcode(self, gcode):
        if not gcode:
            return

        lines = cli.util.normalize_gcode_lines(gcode)
        first = True
        for line in lines:
            if self._is_duplicate_g28(line):
                log.debug("Ignoring duplicate homing command: %s", line)
                continue
            if not first:
                # pace commands; no need to wait after the last one
                time.sleep(0.1)
            first = False
            cmd = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_GCODE_COMMAND.value,
                "cmdData": line,
                "cmdLen": len(line),
            }
            log.debug("Sending GCode command: %s", line)
            self.client.command(cmd)

    def _is_duplicate_g28(self, line):
        parts = line.split()
        if not parts or parts[0].upper() != "G28":
            return False

        now = time.monotonic()
        key = " ".join(part.upper() for part in parts)
        last_key = getattr(self, "_last_g28_command", None)
        last_at = getattr(self, "_last_g28_command_at", 0.0)
        duplicate = key == last_key and now - last_at < G28_DEDUPE_WINDOW_SEC
        if not duplicate:
            self._last_g28_command = key
            self._last_g28_command_at = now
        return duplicate

    def send_home(self, axis="all"):
        axis = str(axis or "all").lower()
        if axis == "all":
            # On the M5, the native Z home sequence also homes the XY axes.
            # Route "Home All" through that same proven firmware path.
            axis = "z"
        if axis not in HOME_MOVE_ZERO_VALUE_BY_AXIS:
            raise ValueError(f"Unsupported home axis: {axis}")

        value = HOME_MOVE_ZERO_VALUE_BY_AXIS[axis]
        log.debug("Sending native home command axis=%s value=%s", axis, value)
        self.client.command({
            "commandType": MqttMsgType.ZZ_MQTT_CMD_MOVE_ZERO.value,
            "value": value,
        })

    def start_stored_file(self, file_path):
        self._ensure_stored_file_selection_state()
        file_path = str(file_path or "").strip()
        if not file_path:
            raise ValueError("Stored file path is required")

        filename = os.path.basename(file_path)
        with self._stored_file_selection_cond:
            self._pending_stored_file_path = file_path
            self._last_selected_storage_file_path = None
            self._last_selected_storage_file_name = None
        log.debug("Refreshing stored file list context for: %s", file_path)
        self.client.command(self._build_stored_file_list_request_payload(file_path))
        time.sleep(0.12)
        log.debug("Selecting stored GCode file: %s", file_path)
        self.client.command(self._build_stored_file_request_payload(file_path))

        selection_ready = self._await_stored_file_selection(file_path)
        if selection_ready:
            log.info("Stored file selection confirmed for %s", file_path)
        else:
            log.warning("Stored file selection was not confirmed before start for %s", file_path)

        self.mark_pending_print_start(filename)
        log.debug("Starting selected stored GCode file: %s", filename)
        self._send_stored_file_start_print_control(file_path)
        timeout_sec = self._stored_file_start_timeout_sec(file_path)
        start_confirmed = self._await_stored_file_start_confirmation(file_path, timeout_sec=timeout_sec)
        if start_confirmed:
            log.info("Stored file start confirmed for %s", file_path)
        else:
            log.warning(
                "Printer did not confirm stored file start for %s within %.1fs",
                file_path,
                timeout_sec,
            )
            with self._state_lock:
                if self._state == PrintState.PREPARING and not self.is_preparing_print:
                    self._state = PrintState.IDLE
                    self._stop_requested = False
        with self._stored_file_selection_cond:
            if self._pending_stored_file_path == file_path:
                self._pending_stored_file_path = None
        return start_confirmed

    def send_print_control(self, value):
        value = int(value)

        pre_start_window = self.is_preparing_print or self.has_pending_print_start

        log.debug(
            "send_print_control(value=%s, state=%s, preparing=%s, pending_start=%s)",
            value,
            self._state.value,
            self.is_preparing_print,
            self.has_pending_print_start,
        )

        if value in (0, 4) and (self._state in (PrintState.PRE_PRINT, PrintState.PRINTING, PrintState.PAUSED) or pre_start_window):
            with self._state_lock:
                self._stop_requested = True

        if value in (0, 4) and pre_start_window:
            # Firmware variants differ in which cancel payload they accept during the
            # pre-print window (G28 / calibration / pending-start). Send both value=0
            # and value=4 in nested and flat form so the cancel lands regardless.
            for candidate in (0, 4):
                nested_data = {"value": candidate}
                if self._control_username:
                    nested_data["userName"] = self._control_username
                nested_cmd = {
                    "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
                    "data": nested_data,
                }
                flat_cmd = {
                    "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
                    "value": candidate,
                }
                log.debug("Pre-start cancel attempt value=%s (nested + flat)", candidate)
                self.client.command(nested_cmd)
                time.sleep(0.12)
                self.client.command(flat_cmd)
                time.sleep(0.18)
        elif value in (2, 3, 4):
            nested_data = {"value": value}
            if self._control_username:
                nested_data["userName"] = self._control_username
            nested_cmd = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
                "data": nested_data,
            }
            flat_cmd = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
                "value": value,
            }
            log.debug("Print control attempt value=%s (nested + flat)", value)
            self.client.command(nested_cmd)
            time.sleep(0.12)
            self.client.command(flat_cmd)
        else:
            cmd = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
                "value": value,
            }
            self.client.command(cmd)

    def send_auto_leveling(self):
        cmd = {
            "commandType": MqttMsgType.ZZ_MQTT_CMD_AUTO_LEVELING.value,
            "value": 0
        }
        self.client.command(cmd)
