import logging
import os
import threading
import time

from ..lib.service import Service
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


import cli.mqtt
from ..notifications import AppriseNotifier, format_duration
from .history import PrintHistory
from .timelapse import TimelapseService
from .homeassistant import HomeAssistantService


class MqttQueue(Service):
    def __init__(self, printer_index):
        self.printer_index = printer_index
        super().__init__()
        self.persistent = True

    def worker_init(self):
        self._notifier = AppriseNotifier(app.config["config"])
        config_root = str(app.config["config"].config_root)
        self._history = PrintHistory(db_path=f"{config_root}/history.db")
        self._timelapse = TimelapseService(app.config["config"])

        # Home Assistant MQTT Discovery
        printer_sn = None
        printer_name = None
        self._control_username = None
        with app.config["config"].open() as cfg:
            if cfg and getattr(cfg, "account", None):
                self._control_username = getattr(cfg.account, "email", None)
            if cfg and cfg.printers:
                printer = cfg.printers[self.printer_index]
                printer_sn = getattr(printer, "sn", None)
                printer_name = getattr(printer, "name", None) or "AnkerMake M5"
        self._ha = HomeAssistantService(app.config["config"], printer_sn=printer_sn, printer_name=printer_name)
        self._ha.start()

        self._reset_print_state()
        self._gcode_layer_count = None  # Override from GCode header, survives print resets
        self._last_message_time = 0.0
        self._nozzle_temp = None
        self._nozzle_temp_target = None
        self._bed_temp = None
        self._bed_temp_target = None
        self._z_offset_steps = None
        self._z_offset_updated_at = 0.0
        self._z_offset_seq = 0
        self._z_offset_cond = threading.Condition()

    def set_gcode_layer_count(self, count: int):
        """Store the layer count extracted from a GCode header for UI display."""
        self._gcode_layer_count = count

    def worker_start(self):
        self.client = cli.mqtt.mqtt_open(
            app.config["config"],
            self.printer_index,
            app.config["insecure"]
        )
        self._reset_print_state()
        self._ha.update_state(mqtt_connected=True)
        self._last_query = 0

    def _reset_print_state(self):
        self._print_active = False
        self._pending_print_start = False
        self._last_state_value = 0
        self._print_started_at = None
        self._last_progress = None
        self._last_progress_bucket = None
        self._last_interval = None
        self._last_filename = None
        self._last_task_id = None
        self._failure_sent = False
        self._preview_url = None
        self._pending_history_start = False
        self._stop_requested = False
        # Preserve debug setting across resets if possible, but init here if missing
        if not hasattr(self, "_debug_log_payloads"):
             self._debug_log_payloads = False

    def _transition_to_active(self, payload, progress, filename=None):
        """Activate print state.  No-op if already active.

        Both the ct=1000 (state change) and ct=1001 (progress) handlers call
        this so print activation logic lives in exactly one place.  Returns
        True if the print was activated, False if already active.
        """
        if self._print_active:
            return False

        self._pending_print_start = False
        self._print_active = True
        self._print_started_at = time.monotonic()
        self._failure_sent = False
        self._last_progress_bucket = None

        effective_filename = filename or self._last_filename

        if effective_filename:
            self._history.record_start(effective_filename, task_id=self._last_task_id)
            self._pending_history_start = False
            log.info(f"Print started, filename={effective_filename!r}")
        else:
            self._pending_history_start = True
            log.info("Print started, history deferred (no filename yet)")

        self._timelapse.start_capture(effective_filename or "unknown")
        self._ha.update_state(print_status="printing")
        self._send_event(EVENT_PRINT_STARTED, self._build_payload(payload, progress))

        return True

    @property
    def is_printing(self):
        return self._print_active

    @property
    def is_preparing_print(self):
        return self._last_state_value == 8 and not self._print_active

    @property
    def has_pending_print_start(self):
        return self._pending_print_start

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

    def mark_pending_print_start(self, filename=None, task_id=None):
        if filename:
            self._last_filename = filename
        if task_id:
            self._last_task_id = task_id
        self._pending_print_start = True
        self._stop_requested = False
        log.info("Marked pending print start for %r", self._last_filename)

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
        # Poll status every 10 seconds if idle
        now = time.time()
        if now - self._last_query > 10.0:
            self._send_status_query()
            self._last_query = now

        for msg, body in self.client.fetch(timeout=timeout):
            self._last_message_time = time.time()
            log.info(f"TOPIC [{msg.topic}]")
            log.debug(enhex(msg.payload[:]))
            if body and getattr(self, "_debug_log_payloads", False):
                import json
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
                self._handle_notification(obj)

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
        except Exception as e:
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

    def _forward_to_ha(self, payload):
        """Update cached MQTT state and forward relevant data to Home Assistant."""
        if not isinstance(payload, dict):
            return

        command_type = payload.get("commandType")
        ha_updates = {}

        # Nozzle temperature (command 1003 = 0x03eb)
        if command_type == MqttMsgType.ZZ_MQTT_CMD_NOZZLE_TEMP:
            current = self._safe_int(payload.get("currentTemp") or payload.get("value"))
            target = self._safe_int(payload.get("targetTemp") or payload.get("target"))
            if current is not None:
                # Temps may come in 1/100th degree units
                self._nozzle_temp = self._normalize_temp(current)
                ha_updates["nozzle_temp"] = self._nozzle_temp
            if target is not None:
                self._nozzle_temp_target = self._normalize_temp(target)
                ha_updates["nozzle_temp_target"] = self._nozzle_temp_target

        # Bed temperature (command 1004 = 0x03ec)
        elif command_type == MqttMsgType.ZZ_MQTT_CMD_HOTBED_TEMP:
            current = self._safe_int(payload.get("currentTemp") or payload.get("value"))
            target = self._safe_int(payload.get("targetTemp") or payload.get("target"))
            if current is not None:
                self._bed_temp = self._normalize_temp(current)
                ha_updates["bed_temp"] = self._bed_temp
            if target is not None:
                self._bed_temp_target = self._normalize_temp(target)
                ha_updates["bed_temp_target"] = self._bed_temp_target

        # Print speed (command 1006 = 0x03ee)
        elif command_type == MqttMsgType.ZZ_MQTT_CMD_PRINT_SPEED:
            speed = self._safe_int(payload.get("value") or payload.get("speed"))
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
            if self._print_active:
                ha_updates["print_status"] = "printing"
            elif progress is not None and progress >= 100:
                ha_updates["print_status"] = "complete"

        if ha_updates and self._ha.enabled:
            self._ha.update_state(**ha_updates)

    def _handle_notification(self, payload):
        if not isinstance(payload, dict):
            return

        command_type = payload.get("commandType")
        preview_url = self._extract_preview_url(payload)
        if preview_url:
            self._preview_url = preview_url

        # --- commandType 1044: file upload started, capture filename ---
        if command_type == 1044:
            file_path = payload.get("filePath", "")
            if file_path:
                self._last_filename = os.path.basename(file_path)
                log.info(f"History: captured filename from ct 1044: {self._last_filename}")
            # If ct=1000 value=1 arrived before ct=1044, record the start now that we have the filename
            if self._print_active and self._pending_history_start and self._last_filename:
                self._history.record_start(self._last_filename, task_id=self._last_task_id)
                self._pending_history_start = False
            return

        # --- commandType 1000: printer state machine transitions ---
        if command_type == 1000:
            value = payload.get("value")
            # Snapshot the previous state before mutating it so transitions like
            # prepare(8) -> idle(0) can still be classified correctly.
            was_preparing_print = self.is_preparing_print
            was_pending_start = self._pending_print_start
            self._last_state_value = value
            if value == 1:
                self._transition_to_active(payload, progress=0)
            elif value == 0 and (self._print_active or was_preparing_print or was_pending_start):
                if self._print_active:
                    if self._stop_requested:
                        # Print was cancelled via stop command
                        log.info(f"History: print cancelled (ct 1000 value=0 after stop), filename={self._last_filename!r}")
                        self._history.record_fail(filename=self._last_filename, reason="cancelled", task_id=self._last_task_id)
                        self._timelapse.fail_capture()
                        self._ha.update_state(print_status="failed")
                        self._send_event(
                            EVENT_PRINT_FAILED,
                            self._build_payload(payload, self._last_progress or 0, failure_reason="cancelled"),
                        )
                        self._failure_sent = True
                    else:
                        # Print ended normally
                        log.info(f"History: print finished (ct 1000 value=0), filename={self._last_filename!r}")
                        self._history.record_finish(filename=self._last_filename, task_id=self._last_task_id)
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
                    self._history.record_fail(filename=self._last_filename, reason="cancelled", task_id=self._last_task_id)
                    self._timelapse.fail_capture()
                    self._ha.update_state(print_status="failed")
                    self._send_event(
                        EVENT_PRINT_FAILED,
                        self._build_payload(payload, self._last_progress or 0, failure_reason="cancelled"),
                    )
                    self._failure_sent = True
                else:
                    log.info("Pending print start ended without an active print; resetting state")
                self._reset_print_state()
            elif value == 8 and self._print_active:
                log.info(
                    "History: print aborted (ct 1000 value=8), filename=%r",
                    self._last_filename,
                )
                self._history.record_fail(
                    filename=self._last_filename,
                    reason="aborted",
                    task_id=self._last_task_id,
                )
                self._timelapse.fail_capture()
                self._ha.update_state(print_status="failed")
                self._send_event(
                    EVENT_PRINT_FAILED,
                    self._build_payload(payload, self._last_progress or 0, failure_reason="aborted"),
                )
                self._failure_sent = True
                self._print_active = False
                self._reset_print_state()
            elif value == 8:
                self._pending_print_start = False
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
        if task_id:
            if self._last_task_id and task_id != self._last_task_id and self._print_active:
                if progress <= 1:
                    self._reset_print_state()
                    self._last_task_id = task_id
                else:
                    task_id = self._last_task_id
            else:
                self._last_task_id = task_id

        filename = self._extract_filename(payload)
        if filename:
            if self._last_filename and filename != self._last_filename and self._print_active:
                if progress <= 1:
                    self._reset_print_state()
                    self._last_filename = filename
                else:
                    filename = self._last_filename
            else:
                self._last_filename = filename

        if self._last_progress is not None and progress < self._last_progress and progress <= 1:
            same_task = task_id and prev_task_id and task_id == prev_task_id
            same_file = filename and prev_filename and filename == prev_filename
            if not (same_task or same_file):
                self._reset_print_state()

        failure_reason = self._extract_failure_reason(payload)
        if failure_reason and self._print_active and not self._failure_sent:
            self._send_event(
                EVENT_PRINT_FAILED,
                self._build_payload(payload, progress, failure_reason=failure_reason),
            )
            self._history.record_fail(filename=self._last_filename, reason=failure_reason, task_id=self._last_task_id)
            self._timelapse.fail_capture()
            self._ha.update_state(print_status="failed")
            self._failure_sent = True
            self._print_active = False
            return

        if 0 < progress < 100 and not self._print_active:
            self._transition_to_active(payload, progress, filename=filename)

        status_text = self._extract_status_text(payload)
        if self._print_active and status_text:
            if any(word in status_text for word in ("finish", "complete", "done")):
                self._send_event(
                    EVENT_PRINT_FINISHED,
                    self._build_payload(payload, 100),
                    include_image=True,
                )
                self._history.record_finish(filename=self._last_filename, task_id=self._last_task_id)
                self._timelapse.finish_capture(final=True)
                self._ha.update_state(print_status="complete", print_progress=100)
                self._reset_print_state()
                return

        if self._print_active and progress >= 100:
            self._send_event(
                EVENT_PRINT_FINISHED,
                self._build_payload(payload, 100),
                include_image=True,
            )
            self._history.record_finish(filename=self._last_filename, task_id=self._last_task_id)
            self._timelapse.finish_capture(final=True)
            self._ha.update_state(print_status="complete", print_progress=100)
            self._reset_print_state()
            return


        self._emit_progress(payload, progress)
        self._last_progress = progress

    def _emit_progress(self, payload, progress):
        if not self._print_active:
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
        attachments = None
        cleanup_paths = []
        if include_image and self._notifier.is_event_enabled(event):
            attachments, cleanup_paths = self._notifier.build_attachments(preview_url=self._preview_url)
        self._notifier.send(event, payload=payload, attachments=attachments)
        if cleanup_paths:
            self._notifier.cleanup_attachments(cleanup_paths)

    def set_debug_logging(self, enabled):
        self._debug_log_payloads = enabled
        log.info(f"Debug logging {'enabled' if enabled else 'disabled'}")

    def get_state(self):
        """Return structured internal state for debug inspection."""
        return {
            "print": {
                "active": self._print_active,
                "pending_start": self.has_pending_print_start,
                "state": self._last_state_value,
                "preparing": self.is_preparing_print,
                "started_at": self._print_started_at,
                "last_progress": self._last_progress,
                "last_filename": self._last_filename,
                "last_task_id": self._last_task_id,
                "failure_sent": self._failure_sent,
                "preview_url": self._preview_url,
            },
            "temperature": {
                "nozzle": self._nozzle_temp,
                "nozzle_target": self._nozzle_temp_target,
                "bed": self._bed_temp,
                "bed_target": self._bed_temp_target,
            },
            "z_offset": self.get_z_offset_state(),
            "debug_logging": getattr(self, "_debug_log_payloads", False),
            "timelapse": {
                "enabled": getattr(self._timelapse, "enabled", None),
                "capturing": bool(getattr(self._timelapse, "_capture_thread", None)),
            },
        }

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
            self._print_active = True
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
        for line in lines:
            cmd = {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_GCODE_COMMAND.value,
                "cmdData": line,
                "cmdLen": len(line),
            }
            self.client.command(cmd)
            time.sleep(0.1)

    def send_print_control(self, value):
        value = int(value)
        if value in (0, 4) and (self._print_active or self.is_preparing_print or self.has_pending_print_start):
            self._stop_requested = True
        prepare_cancel = value in (0, 4) and self.is_preparing_print
        if value == 4 and self.is_preparing_print:
            log.info("Mapping print stop value=4 to value=0 during pre-print preparation")
            value = 0
        if prepare_cancel:
            # During the pre-print preparation phase (ct=1000 value=8) the printer only
            # responds to the app-style nested command format. We send both the nested and
            # the flat format so the cancel lands regardless of which the firmware accepts.
            nested_data = {"value": value}
            if self._control_username:
                nested_data["userName"] = self._control_username
            log.info("Sending app-style nested print control during pre-print preparation")
            self.client.command({
                "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
                "data": nested_data,
            })
        cmd = {
            "commandType": MqttMsgType.ZZ_MQTT_CMD_PRINT_CONTROL.value,
            "value": value
        }
        self.client.command(cmd)

    def send_auto_leveling(self):
        cmd = {
            "commandType": MqttMsgType.ZZ_MQTT_CMD_AUTO_LEVELING.value,
            "value": 0
        }
        self.client.command(cmd)
