"""Timelapse capture service — periodic snapshots assembled into video."""

import json
import logging
import math
import os
import shutil
import subprocess

log = logging.getLogger("timelapse")

import threading
import time
from datetime import datetime

import web.camera
import web.timelapse_settings


_DEFAULT_INTERVAL_SEC = 30
_DEFAULT_MAX_VIDEOS = 10
_SNAPSHOT_TIMEOUT = 10
_RESUME_WINDOW_SEC = 60 * 60  # 60 minutes
_IN_PROGRESS_SUBDIR = "in_progress"
_SNAPSHOT_ARCHIVE_SUBDIR = "snapshots"
_MAX_ORPHAN_AGE_SEC = 24 * 3600  # 24 hours
_RECOVERY_REQUEST_COOLDOWN_SEC = 8.0
_RECOVERY_WAIT_SEC = 4.0


def _resolve_ffmpeg_path():
    """Find ffmpeg using the same fallback path as the web snapshot route."""
    try:
        from web import _ffmpeg_path as web_ffmpeg_path  # Lazy import avoids circular deps
        ffmpeg_path = web_ffmpeg_path()
        if ffmpeg_path:
            return ffmpeg_path
    except Exception as err:
        log.debug(f"Timelapse: web ffmpeg lookup unavailable: {err}")
    return shutil.which("ffmpeg")


class TimelapseService:
    """Captures periodic snapshots during a print and assembles a timelapse video."""

    def __init__(self, config_manager, captures_dir=None, printer_index=None):
        self._config_manager = config_manager
        self._printer_index = 0 if printer_index is None else int(printer_index)
        default_captures = os.path.join(str(config_manager.config_root), "captures")
        self._captures_dir_explicit = captures_dir is not None
        initial_captures_dir = captures_dir or os.getenv("TIMELAPSE_CAPTURES_DIR", default_captures) or default_captures
        self._captures_dir = self._normalize_captures_dir(
            initial_captures_dir
        )
        self._printer_scope = self._resolve_printer_scope()
        try:
            os.makedirs(self._captures_dir, exist_ok=True)
        except PermissionError as err:
            log.warning(f"Timelapse: cannot create captures dir {self._captures_dir!r}: {err} — falling back to config dir")
            self._captures_dir = os.path.join(str(config_manager.config_root), "captures")
            os.makedirs(self._captures_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._capture_thread = None
        self._stop_event = threading.Event()
        self._current_dir = None
        self._current_filename = None
        self._frame_count = 0
        self._capture_pause_reason = None
        self._automatic_pause_reason = None
        self._manual_pause_requested = False
        self._last_recovery_request_at = 0.0
        self._recovery_active = False
        self._recovery_reason = None
        self._capture_camera = None

        # Set defaults to ensure attributes exist even if config is None
        self._enabled = False
        self._interval = _DEFAULT_INTERVAL_SEC
        self._max_videos = _DEFAULT_MAX_VIDEOS
        self._save_persistent = True
        self._light_mode = None  # None | "session" | "snapshot"
        self._camera_source = "follow"  # follow | printer | external

        # Track whether timelapse currently holds the printer-video session.
        self._video_enabled_by_timelapse = False
        self._enable_generation = None
        self._light_was_on = None  # original light state before timelapse touched it

        # Resume-window state: hold completed capture state while waiting to see
        # if the same print resumes (e.g. after a filament change).
        self._resume_dir = None
        self._resume_filename = None
        self._resume_frame_count = 0
        self._finalize_timer = None

        self.reload_config()
        self._scan_in_progress_captures()

    @staticmethod
    def _normalize_captures_dir(path):
        raw_path = str(path or "").strip()
        if not raw_path:
            return None
        return os.path.abspath(os.path.expanduser(raw_path))

    def _apply_captures_dir(self, path):
        target = self._normalize_captures_dir(path)
        if not target:
            return False
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as err:
            log.warning(f"Timelapse: could not use output directory {target!r}: {err}")
            return False
        if target != self._captures_dir:
            log.info(f"Timelapse: output directory set to {target}")
        self._captures_dir = target
        return True

    @staticmethod
    def _safe_scope_component(value):
        value = str(value or "").strip()
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in value)
        return safe.strip("._") or None

    def _resolve_printer_scope(self):
        identifier = None
        try:
            if hasattr(self._config_manager, "open"):
                with self._config_manager.open() as cfg:
                    printers = getattr(cfg, "printers", None) or []
                    if 0 <= self._printer_index < len(printers):
                        printer = printers[self._printer_index]
                        for attr in ("sn", "id", "p2p_duid", "name"):
                            candidate = getattr(printer, attr, None)
                            if candidate:
                                identifier = candidate
                                break
        except Exception as err:
            log.debug(f"Timelapse: could not resolve printer scope: {err}")
        safe = self._safe_scope_component(identifier) or f"index_{self._printer_index}"
        return f"printer_{safe}"

    def reload_config(self, config=None):
        """Update configuration from Config object or ConfigManager."""
        # If no config passed, use stored config_manager
        if config is None:
            config = self._config_manager

        # If config is a ConfigManager (has .open() method), load the actual config
        if config and hasattr(config, 'open'):
            with config.open() as cfg:
                config = cfg

        if not config:
            return

        cfg = web.timelapse_settings.resolve_timelapse_settings(config, self._printer_index)
        self._enabled = cfg.get("enabled", False)
        self._interval = max(1, int(cfg.get("interval", _DEFAULT_INTERVAL_SEC)))
        self._max_videos = int(cfg.get("max_videos", _DEFAULT_MAX_VIDEOS))
        self._save_persistent = cfg.get("save_persistent", True)
        if not self._captures_dir_explicit:
            self._apply_captures_dir(cfg.get("output_dir"))
        raw_light = cfg.get("light", None)
        if raw_light == "snapshot":
            self._light_mode = "snapshot"
        elif raw_light in (True, "session", "on"):
            self._light_mode = "session"
        else:
            self._light_mode = None
        self._camera_source = cfg.get("camera_source", "follow") or "follow"
        config_message = (
            "Timelapse: config loaded - "
            f"enabled={self._enabled}, interval={self._interval}s, "
            f"light_mode={self._light_mode}, camera_source={self._camera_source}"
        )
        log.debug(config_message)

    @property
    def enabled(self):
        return self._enabled

    def get_runtime_state(self):
        with self._lock:
            capture_thread = self._capture_thread
            pause_reason = self._combined_pause_reason_locked()
            return {
                "enabled": self._enabled,
                "capturing": bool(capture_thread and capture_thread.is_alive()),
                "active_capture": bool(self._current_dir or self._resume_dir or (capture_thread and capture_thread.is_alive())),
                "paused": pause_reason is not None,
                "pause_reason": pause_reason,
                "manual_paused": self._manual_pause_requested,
                "recovering": self._recovery_active,
                "recovery_reason": self._recovery_reason,
                "resume_available": bool(self._resume_dir),
                "resume_filename": self._resume_filename,
                "resume_frame_count": self._resume_frame_count,
                "detail": self._runtime_detail(),
            }

    def _runtime_detail(self):
        pause_reason = self._combined_pause_reason_locked()
        if self._recovery_active:
            return "Recovering video stream..."
        if pause_reason == "filament_runout":
            return "Paused for filament runout."
        if pause_reason == "filament_change":
            return "Paused for filament change."
        if pause_reason == "manual":
            return "Paused manually."
        return None

    def _combined_pause_reason_locked(self):
        if self._automatic_pause_reason:
            return self._automatic_pause_reason
        if self._manual_pause_requested:
            return "manual"
        return None

    def _apply_pause_state_locked(self, *, automatic_reason=None, manual_pause=None):
        previous = self._combined_pause_reason_locked()
        if automatic_reason is not None:
            self._automatic_pause_reason = automatic_reason
        if manual_pause is not None:
            self._manual_pause_requested = bool(manual_pause)
        current = self._combined_pause_reason_locked()
        self._capture_pause_reason = current
        return previous, current

    @staticmethod
    def _log_pause_state_change(previous, current):
        if previous == current:
            return
        if current:
            log.info(f"Timelapse: capture paused ({current})")
        else:
            log.info("Timelapse: capture resumed")

    def _set_recovery_state(self, active, reason=None):
        recovery_reason = str(reason or "").strip() or None
        with self._lock:
            changed = (
                self._recovery_active != bool(active)
                or self._recovery_reason != recovery_reason
            )
            self._recovery_active = bool(active)
            self._recovery_reason = recovery_reason if active else None
        if not changed:
            return
        if active and recovery_reason:
            log.info(f"Timelapse: recovery active ({recovery_reason})")
        elif active:
            log.info("Timelapse: recovery active")
        else:
            log.info("Timelapse: recovery cleared")

    def set_capture_paused(self, paused, reason=None):
        with self._lock:
            previous = self._combined_pause_reason_locked()
            self._automatic_pause_reason = str(reason or "paused").strip() if paused else None
            current = self._combined_pause_reason_locked()
            self._capture_pause_reason = current
        self._log_pause_state_change(previous, current)

    def set_manual_pause(self, paused):
        with self._lock:
            previous, current = self._apply_pause_state_locked(manual_pause=paused)
        self._log_pause_state_change(previous, current)

    def is_capture_paused(self):
        with self._lock:
            return self._combined_pause_reason_locked() is not None

    def has_active_capture(self):
        with self._lock:
            return bool(
                self._current_dir
                or self._resume_dir
                or (self._capture_thread and self._capture_thread.is_alive())
            )

    def _enable_video_for_timelapse(self):
        """Start video streaming for timelapse capture if not already active."""
        import web
        from web import app
        from web.lib.service import RunState, ServiceStoppedError

        vq = web.get_video_service(self._printer_index)
        if not vq:
            log.warning("Timelapse: videoqueue service not available, snapshots will be skipped")
            return

        self._video_enabled_by_timelapse = True
        self._enable_generation = getattr(vq, "_enable_generation", None)

        if not getattr(vq, "owns_video_for_timelapse", lambda: False)():
            log.info("Timelapse: acquiring video streaming for capture")
        vq.set_timelapse_enabled(True)

        # If service is Running but PPPP not connected (was started with video_enabled=False),
        # stop and restart so worker_start() runs with video_enabled=True.
        if vq.state == RunState.Running and getattr(vq, "pppp", None) is None:
            log.info("Timelapse: restarting VideoQueue to establish PPPP connection")
            vq.stop()
            vq.await_stopped()
            vq.start()
        elif vq.state == RunState.Stopped:
            vq.start()

        if vq.wanted and vq.state != RunState.Running:
            try:
                vq.await_ready()
                log.info("Timelapse: video service ready")
            except ServiceStoppedError:
                log.warning("Timelapse: video service failed to start, snapshots may fail")
                self._video_enabled_by_timelapse = False

        self._enable_light_for_session()

    def _enable_light_for_session(self):
        if self._light_mode != "session":
            return
        import web

        vq = web.get_video_service(self._printer_index)
        self._light_was_on = getattr(vq, "saved_light_state", None) if vq else None
        if self._light_was_on is not True:
            log.info("Timelapse: turning on light for capture session")
            web.set_printer_light_state(True, self._printer_index)

    def _prepare_capture_services(self):
        import web

        if self._capture_camera.get("effective_source") == web.camera.CAMERA_SOURCE_PRINTER:
            self._enable_video_for_timelapse()
        else:
            self._enable_light_for_session()

    def _resolve_capture_camera(self):
        source_override = self._camera_source if self._camera_source in {"printer", "external"} else None
        with self._config_manager.open() as cfg:
            return web.camera.resolve_camera_settings(
                cfg,
                printer_index=self._printer_index,
                source_override=source_override,
            )

    def _disable_video_for_timelapse(self):
        """Disable video streaming if timelapse enabled it."""
        import web

        vq = web.get_video_service(self._printer_index)

        if self._light_mode == "session" and self._light_was_on is not True:
            restore = self._light_was_on if self._light_was_on is not None else False
            log.info(f"Timelapse: restoring light state to {restore}")
            web.set_printer_light_state(restore, self._printer_index)
        self._light_was_on = None

        if not self._video_enabled_by_timelapse:
            return
        if vq:
            log.info("Timelapse: releasing video streaming after capture")
            vq.set_timelapse_enabled(False)
        self._video_enabled_by_timelapse = False
        self._enable_generation = None

    def _await_video_frame(self, timeout=2.5, max_age=1.5):
        """Wait until a recent video frame has been received from the camera."""
        import web

        vq = web.get_video_service(self._printer_index)
        if not vq or not hasattr(vq, "last_frame_at"):
            self._set_recovery_state(False)
            return True
        now = time.monotonic()
        if self._has_recent_video_frame(vq, now=now, max_age=max_age):
            self._set_recovery_state(False)
            return True
        deadline = now + timeout
        while time.monotonic() < deadline:
            if self._has_recent_video_frame(vq, max_age=max_age):
                self._set_recovery_state(False)
                return True
            time.sleep(0.1)
        requested = self._request_video_recovery(
            "timelapse has no recent video frame",
            force_pppp_recycle=not bool(getattr(getattr(vq, "pppp", None), "connected", False)),
        )
        if requested:
            recovery_deadline = time.monotonic() + _RECOVERY_WAIT_SEC
            while time.monotonic() < recovery_deadline:
                if self._has_recent_video_frame(vq, max_age=max_age):
                    self._set_recovery_state(False)
                    log.info("Timelapse: video frame recovered after restart request")
                    return True
                time.sleep(0.1)
        log.debug("Timelapse: _await_video_frame timed out")
        return False

    @staticmethod
    def _has_recent_video_frame(vq, now=None, max_age=1.5):
        last_frame = getattr(vq, "last_frame_at", None)
        if not last_frame:
            return False
        now = time.monotonic() if now is None else now
        return (now - last_frame) <= max_age

    def _request_video_recovery(self, reason, force_pppp_recycle=False):
        import web

        vq = web.get_video_service(self._printer_index)
        if not vq:
            return False

        request_recovery = getattr(vq, "request_live_recovery", None)
        if not callable(request_recovery):
            return False

        now = time.monotonic()
        if (now - self._last_recovery_request_at) < _RECOVERY_REQUEST_COOLDOWN_SEC:
            return False

        requested = bool(
            request_recovery(
                reason=reason,
                force_pppp_recycle=force_pppp_recycle,
            )
        )
        if requested:
            self._last_recovery_request_at = now
            self._set_recovery_state(True, reason)
            log.info(f"Timelapse: requested video recovery ({reason})")
        return requested

    def _cancel_finalize_timer(self):
        """Cancel any pending delayed assembly timer."""
        if self._finalize_timer:
            self._finalize_timer.cancel()
            self._finalize_timer = None

    def _cancel_pending_resume(self):
        """Cancel pending resume and delete its temporary frame directory."""
        self._cancel_finalize_timer()
        if self._resume_dir:
            try:
                if os.path.isdir(self._resume_dir):
                    shutil.rmtree(self._resume_dir)
            except OSError as err:
                log.warning(f"Timelapse: cleanup of pending resume dir failed: {err}")
            self._resume_dir = None
            self._resume_filename = None
            self._resume_frame_count = 0

    def discard_pending_resume(self, filename=None):
        """Discard a pending resumable capture, optionally scoped by filename."""
        with self._lock:
            if self._capture_thread and self._capture_thread.is_alive():
                return False

            requested = os.path.basename(str(filename or "")).strip() or None
            current = os.path.basename(str(self._resume_filename or "")).strip() or None
            if requested and current and requested != current:
                return False
            if not self._resume_dir:
                return False

            log.info(f"Timelapse: discarded pending capture for '{self._resume_filename}'")
            self._cancel_pending_resume()
            self._automatic_pause_reason = None
            self._manual_pause_requested = False
            self._capture_pause_reason = None
            self._recovery_active = False
            self._recovery_reason = None
            self._disable_video_for_timelapse()
            return True

    def _schedule_finalize(self, dir_path, filename, frame_count, suffix=""):
        """Save capture state and schedule delayed assembly.

        If start_capture() is called with the same filename within
        _RESUME_WINDOW_SEC (e.g. after a filament change), the capture
        resumes seamlessly instead of creating a separate video.
        """
        # Discard any previous pending resume for a different file
        if self._resume_dir and self._resume_dir != dir_path:
            try:
                if os.path.isdir(self._resume_dir):
                    shutil.rmtree(self._resume_dir)
            except OSError:
                pass

        self._cancel_finalize_timer()
        self._resume_dir = dir_path
        self._resume_filename = filename
        self._resume_frame_count = frame_count

        def _finalize():
            with self._lock:
                if self._resume_dir != dir_path:
                    return  # Resumed or superseded — don't assemble here
                saved_dir = self._resume_dir
                saved_filename = self._resume_filename
                saved_frame_count = self._resume_frame_count
                self._resume_dir = None
                self._resume_filename = None
                self._resume_frame_count = 0
                self._finalize_timer = None

            # Assemble outside the lock (ffmpeg can take a while)
            # Pass locals directly — avoids writing to self outside the lock
            self._finalize_capture_dir(
                saved_dir,
                saved_filename,
                saved_frame_count,
                suffix=suffix,
            )

        timer = threading.Timer(_RESUME_WINDOW_SEC, _finalize)
        timer.daemon = True
        timer.start()
        self._finalize_timer = timer
        pause_log = log.info if frame_count >= 2 else log.debug
        pause_log(
            f"Timelapse: capture paused for '{filename}' ({frame_count} frames), "
            f"resumable for {_RESUME_WINDOW_SEC // 60} min"
        )

    def start_capture(self, filename="unknown"):
        """Begin periodic snapshot capture for a new print."""
        if not self._enabled:
            return
        if not filename or filename.strip().lower() in {"unknown", "unknown.gcode", ""}:
            log.debug(f"Timelapse: skipping placeholder filename {filename!r}")
            return
        if not _resolve_ffmpeg_path():
            log.warning("Timelapse: ffmpeg not available, skipping")
            return

        with self._lock:
            self._capture_camera = self._resolve_capture_camera()
            if not self._capture_camera.get("effective_source"):
                log.warning(
                    "Timelapse: skipping capture because no camera source is ready for this printer"
                )
                return

            active_same_capture = (
                self._current_dir is not None
                and self._current_filename == filename
                and filename
                and filename != "unknown"
            )
            capture_thread_alive = bool(self._capture_thread and self._capture_thread.is_alive())

            if active_same_capture:
                self._automatic_pause_reason = None
                self._manual_pause_requested = False
                self._capture_pause_reason = None
                self._last_recovery_request_at = 0.0
                self._recovery_active = False
                self._recovery_reason = None
                self._prepare_capture_services()
                if capture_thread_alive:
                    log.info(
                        f"Timelapse: capture already active for '{filename}', "
                        f"keeping existing session ({self._frame_count} frames)"
                    )
                    return
                log.info(
                    f"Timelapse: restarting capture thread for '{filename}' "
                    f"({self._frame_count} existing frames)"
                )
            else:
                self._stop_capture_thread()
                self._automatic_pause_reason = None
                self._manual_pause_requested = False
                self._capture_pause_reason = None
                self._last_recovery_request_at = 0.0
                self._recovery_active = False
                self._recovery_reason = None

            # Check if we can seamlessly resume a previous capture of the same print
            can_resume = (
                not active_same_capture
                and self._resume_dir is not None
                and self._resume_filename == filename
                and filename
                and filename != "unknown"
            )

            if active_same_capture:
                pass
            elif can_resume:
                log.info(
                    f"Timelapse: resuming capture for '{filename}' "
                    f"({self._resume_frame_count} existing frames)"
                )
                self._cancel_finalize_timer()
                self._current_dir = self._resume_dir
                self._current_filename = self._resume_filename
                self._frame_count = self._resume_frame_count
                self._resume_dir = None
                self._resume_filename = None
                self._resume_frame_count = 0
                self._prepare_capture_services()
            else:
                # New capture or different file — discard any pending resume
                self._cancel_pending_resume()
                self._prepare_capture_services()
                self._current_filename = filename
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in filename)
                self._current_dir = os.path.join(
                    self._in_progress_base(),
                    f"{self._printer_scope}_{safe_name}_{ts}",
                )
                os.makedirs(self._current_dir, exist_ok=True)
                self._write_meta(self._current_dir, filename, 0)
                self._frame_count = 0

            self._stop_event.clear()

            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                daemon=True,
                name="timelapse-capture",
            )
            self._capture_thread.start()
            camera_source = self._capture_camera.get("effective_source") or "none"
            log.info(
                f"Timelapse: started capture for '{filename}' "
                f"(interval={self._interval}s, source={camera_source})"
            )

    def finish_capture(self, final=False):
        """Stop capture and assemble video.

        Args:
            final: If True, the print is definitively complete — cancel any
                   pending resume window and assemble the video immediately
                   in a background thread.  If False (default), enter the
                   resume window so that a filament-change pause can be
                   continued seamlessly.
        """
        if not self._enabled:
            return
        assemble_dir = None
        assemble_filename = None
        assemble_frame_count = 0
        with self._lock:
            self._stop_capture_thread()
            self._automatic_pause_reason = None
            self._manual_pause_requested = False
            self._capture_pause_reason = None
            self._recovery_active = False
            self._recovery_reason = None
            if not self._current_dir:
                if not self._resume_dir:
                    self._disable_video_for_timelapse()
                    self._capture_camera = None
                return
            if final:
                self._cancel_pending_resume()
                assemble_dir = self._current_dir
                assemble_filename = self._current_filename
                assemble_frame_count = self._frame_count
                self._current_dir = None
                self._current_filename = None
                self._frame_count = 0
            else:
                self._schedule_finalize(
                    self._current_dir, self._current_filename, self._frame_count
                )
                self._current_dir = None
                self._current_filename = None
                self._frame_count = 0
            self._disable_video_for_timelapse()
            self._capture_camera = None
        if final and assemble_dir:
            if assemble_frame_count > 0:
                t = threading.Thread(
                    target=self._finalize_now,
                    args=(assemble_dir, assemble_filename, assemble_frame_count),
                    daemon=True,
                    name="timelapse-assemble",
                )
                t.start()
            else:
                log.info("Timelapse: no frames captured, skipping finalization")
                self._cleanup_dir(assemble_dir)

    def _finalize_now(self, dir_path, filename, frame_count, suffix=""):
        """Assemble video immediately (runs in dedicated background thread)."""
        self._finalize_capture_dir(dir_path, filename, frame_count, suffix=suffix)

    def fail_capture(self):
        """Stop capture on failure — assemble partial timelapse if frames exist."""
        if not self._enabled:
            return
        assemble_dir = None
        assemble_filename = None
        assemble_frame_count = 0
        with self._lock:
            self._stop_capture_thread()
            self._automatic_pause_reason = None
            self._manual_pause_requested = False
            self._capture_pause_reason = None
            self._recovery_active = False
            self._recovery_reason = None
            self._cancel_pending_resume()
            if self._current_dir and self._frame_count > 0:
                if self._frame_count >= 2:
                    log.info("Timelapse: print failed, assembling partial timelapse")
                else:
                    log.info("Timelapse: print failed, preserving captured snapshot")
                assemble_dir = self._current_dir
                assemble_filename = self._current_filename
                assemble_frame_count = self._frame_count
                self._current_dir = None
                self._current_filename = None
                self._frame_count = 0
            else:
                self._cleanup_temp()
            self._disable_video_for_timelapse()
            self._capture_camera = None
        # Assemble outside the lock — ffmpeg can take up to 120 s
        if assemble_dir:
            self._finalize_capture_dir(
                assemble_dir,
                assemble_filename,
                assemble_frame_count,
                suffix="_partial",
            )

    def _stop_capture_thread(self):
        if self._capture_thread and self._capture_thread.is_alive():
            self._stop_event.set()
            self._capture_thread.join(timeout=_SNAPSHOT_TIMEOUT + 2)
        self._capture_thread = None

    def _capture_loop(self):
        """Periodically capture snapshots from the video stream."""
        try:
            while not self._stop_event.is_set():
                if self.is_capture_paused():
                    self._stop_event.wait(min(self._interval, 1.0))
                    continue
                try:
                    self._take_snapshot()
                except Exception as err:
                    log.warning(f"Timelapse: snapshot failed: {err}")
                self._stop_event.wait(self._interval)
        except Exception as err:
            log.warning(f"Timelapse: capture loop crashed: {err}")
        finally:
            # No lock here: _stop_capture_thread() holds self._lock while
            # joining this thread, so acquiring it here would deadlock the
            # join until its timeout. Plain attribute assignment is atomic
            # under the GIL, so this is safe without the lock.
            self._capture_thread = None

    def _take_snapshot(self):
        """Capture a single frame using ffmpeg."""
        import web
        from web import app  # Lazy import to avoid circular deps
        ffmpeg_path = _resolve_ffmpeg_path()
        if not ffmpeg_path:
            log.warning("Timelapse: ffmpeg not available, skipping snapshot")
            return

        camera_settings = self._capture_camera or self._resolve_capture_camera()
        effective_source = camera_settings.get("effective_source")
        if not effective_source:
            log.warning("Timelapse: no camera source is configured for this printer")
            return

        host = os.getenv("FLASK_HOST") or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = os.getenv("FLASK_PORT") or "4470"
        api_key = app.config.get("api_key")

        # Per-snapshot light control: turn on, wait for camera to adjust, then shoot
        vq = (
            web.get_video_service(self._printer_index)
            if self._light_mode == "snapshot"
            else None
        )
        snap_original_light = None
        if vq:
            snap_original_light = getattr(vq, "saved_light_state", None)
            if snap_original_light is not True:
                log.debug("Timelapse: light on for snapshot, waiting 1.5s")
                web.set_printer_light_state(True, self._printer_index)
                time.sleep(1.5)

        frame_path = os.path.join(self._current_dir, f"frame_{self._frame_count:05d}.jpg")
        try:
            if effective_source == web.camera.CAMERA_SOURCE_PRINTER and not self._await_video_frame():
                log.debug("Timelapse: no recent video frame, skipping snapshot")
                return

            web.camera.capture_camera_snapshot_to_file(
                camera_settings,
                ffmpeg_path,
                frame_path,
                host=host,
                port=port,
                api_key=api_key,
                timeout=_SNAPSHOT_TIMEOUT,
                for_timelapse=(effective_source == web.camera.CAMERA_SOURCE_PRINTER),
            )
            self._set_recovery_state(False)
            self._frame_count += 1
            self._write_meta(self._current_dir, self._current_filename, self._frame_count)
        except web.camera.CameraCaptureError as err:
            try:
                os.remove(frame_path)
            except OSError:
                pass
            log.warning(f"Timelapse: snapshot failed: {err}")
            if effective_source == web.camera.CAMERA_SOURCE_PRINTER:
                self._request_video_recovery("timelapse snapshot failed to decode live stream")
        except subprocess.TimeoutExpired:
            try:
                os.remove(frame_path)
            except OSError:
                pass
            log.warning("Timelapse: snapshot timed out waiting for a camera frame")
            if effective_source == web.camera.CAMERA_SOURCE_PRINTER:
                self._request_video_recovery(
                    "timelapse snapshot timed out waiting for a camera frame",
                    force_pppp_recycle=True,
                )
        except OSError as err:
            try:
                os.remove(frame_path)
            except OSError:
                pass
            log.warning(f"Timelapse: snapshot could not run ffmpeg: {err}")
        finally:
            # Always restore light even if ffmpeg timed out or raised an exception
            if vq and snap_original_light is not True:
                time.sleep(1.0)
                restore = snap_original_light if snap_original_light is not None else False
                log.debug(f"Timelapse: restoring light to {restore} after snapshot")
                web.set_printer_light_state(restore, self._printer_index)

    def _in_progress_base(self):
        """Return (and create) the persistent in-progress frames directory."""
        path = os.path.join(self._captures_dir, _IN_PROGRESS_SUBDIR)
        os.makedirs(path, exist_ok=True)
        return path

    def _snapshot_archive_base(self):
        """Return (and create) the persistent archived snapshots directory."""
        path = os.path.join(self._captures_dir, _SNAPSHOT_ARCHIVE_SUBDIR)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _snapshot_source_label(camera_settings):
        effective_source = (camera_settings or {}).get("effective_source")
        if effective_source == web.camera.CAMERA_SOURCE_PRINTER:
            return "Printer camera"
        if effective_source == web.camera.CAMERA_SOURCE_EXTERNAL:
            external = (camera_settings or {}).get("external") or {}
            external_name = str(external.get("name") or "").strip()
            if external_name:
                return f"External camera ({external_name})"
            return "External camera"
        return "Camera"

    @staticmethod
    def _snapshot_frame_names(dir_path):
        if not dir_path or not os.path.isdir(dir_path):
            return []
        return sorted(
            filename for filename in os.listdir(dir_path)
            if os.path.isfile(os.path.join(dir_path, filename))
            and filename.lower().endswith(".jpg")
        )

    @staticmethod
    def _safe_path_component(value):
        value = os.path.basename(str(value or "")).strip()
        if not value or value in {".", ".."}:
            return None
        return value

    def _write_meta(self, dir_path, filename, frame_count, **extra):
        """Write a small JSON sidecar so state survives container restarts."""
        try:
            payload = self._read_meta(dir_path) or {}
            payload.update({
                "filename": filename,
                "frame_count": frame_count,
                "printer_index": self._printer_index,
                "printer_scope": self._printer_scope,
            })
            for key, value in extra.items():
                if value is not None:
                    payload[key] = value
            with open(os.path.join(dir_path, ".meta"), "w") as f:
                json.dump(payload, f)
        except OSError as err:
            log.warning(f"Timelapse: could not write .meta: {err}")

    def _read_meta(self, dir_path):
        """Read the JSON sidecar, or return None if missing/corrupt."""
        try:
            with open(os.path.join(dir_path, ".meta")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _capture_dir_belongs_to_this_printer(self, dir_name, meta):
        """Return True when an in-progress capture belongs to this printer.

        Legacy capture directories created before printer-scoped metadata are
        only recovered by printer 0 because their owner cannot be known safely.
        """
        if isinstance(meta, dict):
            meta_scope = meta.get("printer_scope")
            if meta_scope:
                return meta_scope == self._printer_scope
            if "printer_index" in meta:
                try:
                    return int(meta.get("printer_index")) == self._printer_index
                except (TypeError, ValueError):
                    return False

        if dir_name.startswith(f"{self._printer_scope}_"):
            return True
        if dir_name.startswith("printer_"):
            return False
        return self._printer_index == 0

    def _media_name_belongs_to_this_printer(self, name, meta=None):
        """Return True when an archived video/snapshot collection is ours.

        Unscoped legacy media is exposed only through printer 0 because older
        files have no reliable owner metadata.
        """
        if isinstance(meta, dict):
            meta_scope = meta.get("printer_scope")
            if meta_scope:
                return meta_scope == self._printer_scope
            if "printer_index" in meta:
                try:
                    return int(meta.get("printer_index")) == self._printer_index
                except (TypeError, ValueError):
                    return False
        if str(name or "").startswith(f"{self._printer_scope}_"):
            return True
        if str(name or "").startswith("printer_"):
            return False
        return self._printer_index == 0

    def _resolve_snapshot_collection_dir(self, collection_id):
        safe_collection = self._safe_path_component(collection_id)
        if not safe_collection:
            return None, None
        with self._lock:
            for dir_path in (self._current_dir, self._resume_dir):
                if (
                    dir_path
                    and os.path.isdir(dir_path)
                    and os.path.basename(dir_path) == safe_collection
                ):
                    return dir_path, True
        for root, read_only in (
            (self._snapshot_archive_base(), False),
            (self._in_progress_base(), True),
        ):
            path = os.path.join(root, safe_collection)
            if os.path.isdir(path):
                meta = self._read_meta(path)
                if not self._media_name_belongs_to_this_printer(safe_collection, meta):
                    continue
                return path, read_only
        return None, None

    def _snapshot_collection_record(self, dir_path, *, state="archived", allow_delete=True):
        if not dir_path or not os.path.isdir(dir_path):
            return None

        meta = self._read_meta(dir_path) or {}
        record_state = state
        meta_state = str(meta.get("status") or "").strip().lower()
        if state == "archived" and meta_state in {"archived", "manual"}:
            record_state = meta_state

        frames = self._snapshot_frame_names(dir_path)
        if not frames:
            return None

        try:
            dir_stat = os.stat(dir_path)
        except OSError:
            return None

        frame_items = []
        for frame_name in frames:
            frame_path = os.path.join(dir_path, frame_name)
            try:
                stat = os.stat(frame_path)
            except OSError:
                continue
            frame_items.append({
                "filename": frame_name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        if not frame_items:
            return None

        return {
            "id": os.path.basename(dir_path),
            "label": meta.get("filename") or os.path.basename(dir_path),
            "video_filename": meta.get("video_filename"),
            "frame_count": len(frame_items),
            "created_at": meta.get("archived_at") or datetime.fromtimestamp(dir_stat.st_mtime).isoformat(),
            "state": record_state,
            "source_label": meta.get("source_label"),
            "allow_delete": bool(allow_delete),
            "frames": frame_items,
        }

    def _archive_snapshot_frames(self, dir_path, filename, frame_count, *, video_filename=None):
        if not self._save_persistent or not dir_path or not os.path.isdir(dir_path) or frame_count <= 0:
            return False

        if video_filename:
            collection_id = os.path.splitext(os.path.basename(video_filename))[0]
        else:
            safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in (filename or "print"))
            collection_id = f"{self._printer_scope}_{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        archive_dir = os.path.join(self._snapshot_archive_base(), collection_id)
        try:
            if os.path.isdir(archive_dir):
                shutil.rmtree(archive_dir, ignore_errors=True)
            shutil.move(dir_path, archive_dir)
            self._write_meta(
                archive_dir,
                filename,
                frame_count,
                video_filename=video_filename,
                archived_at=datetime.now().isoformat(),
                status="archived",
            )
            log.info(
                f"Timelapse: archived {frame_count} snapshot(s) for '{filename}'"
                + (f" with video {video_filename}" if video_filename else "")
            )
            return True
        except OSError as err:
            log.warning(f"Timelapse: could not archive snapshots: {err}")
            return False

    def _prune_old_snapshot_collections(self):
        """Remove oldest archived snapshot collections if over max count."""
        if self._max_videos <= 0:
            return
        try:
            base = self._snapshot_archive_base()
            collections = sorted(
                (
                    os.path.join(base, name)
                    for name in os.listdir(base)
                    if os.path.isdir(os.path.join(base, name))
                    and self._media_name_belongs_to_this_printer(
                        name,
                        self._read_meta(os.path.join(base, name)),
                    )
                ),
                key=os.path.getmtime,
            )
            while len(collections) > self._max_videos:
                oldest = collections.pop(0)
                shutil.rmtree(oldest, ignore_errors=True)
                log.info(f"Timelapse: pruned old snapshot collection {os.path.basename(oldest)}")
        except OSError as err:
            log.warning(f"Timelapse: snapshot prune failed: {err}")

    def save_manual_snapshot(self, source_path, *, camera_settings=None, taken_at=None):
        """Persist a manually captured snapshot so it appears in the Snapshots tab."""
        if not source_path or not os.path.isfile(source_path):
            raise FileNotFoundError("Manual snapshot source file not found")

        taken_at = taken_at or datetime.now()
        safe_timestamp = taken_at.strftime("%Y%m%d_%H%M%S")
        collection_id = f"{self._printer_scope}_manual_snapshot_{taken_at.strftime('%Y%m%d_%H%M%S_%f')}"
        archive_dir = os.path.join(self._snapshot_archive_base(), collection_id)
        frame_name = f"ankerctl_snapshot_{safe_timestamp}.jpg"
        archive_path = os.path.join(archive_dir, frame_name)
        source_label = self._snapshot_source_label(camera_settings)
        display_label = f"Manual snapshot {taken_at.strftime('%Y-%m-%d %H:%M:%S')}"

        try:
            os.makedirs(archive_dir, exist_ok=True)
            shutil.copy2(source_path, archive_path)
            self._write_meta(
                archive_dir,
                display_label,
                1,
                archived_at=taken_at.isoformat(),
                status="manual",
                source_label=source_label,
            )
            self._prune_old_snapshot_collections()
            log.info(f"Timelapse: saved manual snapshot ({source_label}) as {collection_id}")
            return {
                "collection_id": collection_id,
                "filename": frame_name,
                "label": display_label,
                "source_label": source_label,
            }
        except OSError as err:
            log.warning(f"Timelapse: could not save manual snapshot: {err}")
            shutil.rmtree(archive_dir, ignore_errors=True)
            raise

    def _finalize_capture_dir(self, dir_path, filename, frame_count, *, suffix=""):
        """Assemble a video when possible and archive the captured JPG frames."""
        video_filename = None
        if frame_count >= 2:
            video_filename = self._assemble_video_from(dir_path, filename, frame_count, suffix=suffix)
            self._prune_old_videos()
        else:
            log.info("Timelapse: not enough frames for video assembly, keeping snapshots only")

        archived = self._archive_snapshot_frames(
            dir_path,
            filename,
            frame_count,
            video_filename=video_filename,
        )
        if archived:
            self._prune_old_snapshot_collections()
        else:
            self._cleanup_dir(dir_path)
        return video_filename

    def _scan_in_progress_captures(self):
        """On startup, detect persisted in-progress frame directories.

        - Loads the most recent dir (≤24h old) as resume state.
        - Assembles and removes any older/excess orphaned dirs.
        """
        base = os.path.join(self._captures_dir, _IN_PROGRESS_SUBDIR)
        if not os.path.isdir(base):
            return
        now = time.time()
        candidates = []
        for name in sorted(os.listdir(base)):
            dir_path = os.path.join(base, name)
            if not os.path.isdir(dir_path):
                continue
            meta = self._read_meta(dir_path)
            if not self._capture_dir_belongs_to_this_printer(name, meta):
                continue
            frames = [f for f in os.listdir(dir_path) if f.startswith("frame_") and f.endswith(".jpg")]
            frame_count = len(frames)
            if frame_count == 0:
                shutil.rmtree(dir_path, ignore_errors=True)
                continue
            filename = (meta or {}).get("filename", "unknown")
            age = now - os.path.getmtime(dir_path)
            candidates.append((age, dir_path, filename, frame_count))

        if not candidates:
            return

        # Sort by age ascending (youngest first)
        candidates.sort()
        youngest_age, youngest_dir, youngest_filename, youngest_frames = candidates[0]

        # Assemble/delete all but the youngest (shouldn't normally happen)
        for age, dir_path, filename, frame_count in candidates[1:]:
            log.info(f"Timelapse: recovering orphaned capture '{filename}' ({frame_count} frames)")
            threading.Thread(
                target=self._finalize_now,
                args=(dir_path, filename, frame_count),
                kwargs={"suffix": "_recovered"},
                daemon=True,
                name="timelapse-assemble",
            ).start()

        # Handle the youngest candidate
        if youngest_age <= _MAX_ORPHAN_AGE_SEC:
            resume_log = log.info if youngest_frames >= 2 else log.debug
            resume_log(
                f"Timelapse: found persisted capture '{youngest_filename}' "
                f"({youngest_frames} frames, {youngest_age / 3600:.1f}h ago) — "
                f"resumable for {_RESUME_WINDOW_SEC // 60} min"
            )
            self._resume_dir = youngest_dir
            self._resume_filename = youngest_filename
            self._resume_frame_count = youngest_frames
            # Schedule finalize so orphaned frames are eventually assembled
            self._schedule_finalize(youngest_dir, youngest_filename, youngest_frames)
        else:
            log.info(
                f"Timelapse: assembling stale capture '{youngest_filename}' "
                f"({youngest_frames} frames, {youngest_age / 3600:.1f}h old)"
            )
            threading.Thread(
                target=self._finalize_now,
                args=(youngest_dir, youngest_filename, youngest_frames),
                kwargs={"suffix": "_recovered"},
                daemon=True,
                name="timelapse-assemble",
            ).start()

    def _assemble_video(self, suffix=""):
        """Assemble current capture into an MP4 video."""
        self._assemble_video_from(self._current_dir, self._current_filename, self._frame_count, suffix=suffix)

    def _assemble_video_from(self, dir_path, filename, frame_count, suffix=""):
        """Assemble frames from dir_path into an MP4 video (no self state read)."""
        if not self._save_persistent:
            log.info("Timelapse: persistent save disabled, skipping assembly")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in (filename or "print"))
        output_name = f"{self._printer_scope}_{safe_name}_{ts}{suffix}.mp4"
        output_path = os.path.join(self._captures_dir, output_name)

        # Calculate fps to make ~30s videos, min 1fps max 30fps
        fps = max(1, min(30, math.ceil(frame_count / 30)))

        input_pattern = os.path.join(dir_path, "frame_%05d.jpg")
        ffmpeg_path = _resolve_ffmpeg_path()
        if not ffmpeg_path:
            log.warning("Timelapse: ffmpeg not available, skipping assembly")
            return None

        try:
            result = subprocess.run(
                [
                    ffmpeg_path, "-loglevel", "error", "-nostdin", "-y",
                    "-framerate", str(fps),
                    "-i", input_pattern,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    output_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            if result.returncode == 0 and os.path.exists(output_path):
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                log.info(f"Timelapse: assembled {output_name} ({frame_count} frames, {size_mb:.1f}MB)")
                return output_name
            else:
                stderr = result.stderr.decode(errors="ignore").strip()
                log.warning(f"Timelapse: assembly failed: {stderr}")
                try:
                    os.remove(output_path)
                except OSError:
                    pass
        except (subprocess.TimeoutExpired, OSError) as err:
            log.warning(f"Timelapse: assembly failed: {err}")
            try:
                os.remove(output_path)
            except OSError:
                pass
        return None

    def _cleanup_temp(self):
        """Remove current capture's temporary frame directory."""
        self._cleanup_dir(self._current_dir)
        self._current_dir = None
        self._current_filename = None
        self._frame_count = 0

    def _cleanup_dir(self, dir_path):
        """Remove a temporary frame directory."""
        if dir_path and os.path.isdir(dir_path):
            try:
                shutil.rmtree(dir_path)
            except OSError as err:
                log.warning(f"Timelapse: cleanup failed: {err}")

    def _prune_old_videos(self):
        """Remove oldest videos if over max count."""
        if self._max_videos <= 0:
            return
        try:
            videos = sorted(
                [
                    f for f in os.listdir(self._captures_dir)
                    if f.endswith(".mp4")
                    and self._media_name_belongs_to_this_printer(f)
                ],
                key=lambda f: os.path.getmtime(os.path.join(self._captures_dir, f)),
            )
            while len(videos) > self._max_videos:
                oldest = videos.pop(0)
                os.remove(os.path.join(self._captures_dir, oldest))
                removed_collections = self._delete_snapshot_collections_for_video(oldest)
                if removed_collections:
                    log.info(
                        f"Timelapse: pruned old video {oldest} and snapshot collection(s) "
                        f"{', '.join(removed_collections)}"
                    )
                else:
                    log.info(f"Timelapse: pruned old video {oldest}")
        except OSError as err:
            log.warning(f"Timelapse: prune failed: {err}")

    def list_videos(self):
        """Return list of available timelapse videos with metadata."""
        videos = []
        try:
            for f in sorted(os.listdir(self._captures_dir), reverse=True):
                if not f.endswith(".mp4"):
                    continue
                if not self._media_name_belongs_to_this_printer(f):
                    continue
                path = os.path.join(self._captures_dir, f)
                stat = os.stat(path)
                videos.append({
                    "filename": f,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        except OSError:
            pass
        return videos

    def list_snapshots(self):
        """Return snapshot collections and their JPG frames for the Snapshots tab."""
        collections = []
        seen = set()

        with self._lock:
            in_progress = [
                ("capturing", self._current_dir, False),
                ("resume_pending", self._resume_dir, False),
            ]

        for state, dir_path, allow_delete in in_progress:
            if not dir_path or not os.path.isdir(dir_path):
                continue
            collection_id = os.path.basename(dir_path)
            if collection_id in seen:
                continue
            record = self._snapshot_collection_record(
                dir_path,
                state=state,
                allow_delete=allow_delete,
            )
            if record:
                collections.append(record)
                seen.add(collection_id)

        try:
            archive_base = self._snapshot_archive_base()
            archived_dirs = sorted(
                (
                    os.path.join(archive_base, name)
                    for name in os.listdir(archive_base)
                    if os.path.isdir(os.path.join(archive_base, name))
                ),
                key=os.path.getmtime,
                reverse=True,
            )
        except OSError:
            archived_dirs = []

        for dir_path in archived_dirs:
            collection_id = os.path.basename(dir_path)
            if collection_id in seen:
                continue
            meta = self._read_meta(dir_path) or {}
            if not self._media_name_belongs_to_this_printer(collection_id, meta):
                continue
            record = self._snapshot_collection_record(dir_path)
            if record:
                collections.append(record)
                seen.add(collection_id)

        return collections

    def get_video_path(self, filename):
        """Return full path to a video file, or None if not found."""
        path = os.path.join(self._captures_dir, filename)
        if (
            os.path.isfile(path)
            and filename.endswith(".mp4")
            and self._media_name_belongs_to_this_printer(filename)
        ):
            return path
        return None

    def get_snapshot_path(self, collection_id, filename):
        """Return full path to a saved snapshot JPG, or None if not found."""
        dir_path, _read_only = self._resolve_snapshot_collection_dir(collection_id)
        safe_filename = self._safe_path_component(filename)
        if not dir_path or not safe_filename or not safe_filename.lower().endswith(".jpg"):
            return None
        path = os.path.join(dir_path, safe_filename)
        if os.path.isfile(path):
            return path
        return None

    def delete_snapshot(self, collection_id, filename):
        """Delete an archived snapshot JPG."""
        dir_path, read_only = self._resolve_snapshot_collection_dir(collection_id)
        if not dir_path:
            return False
        if read_only:
            raise RuntimeError("Cannot delete snapshots from an active or resumable timelapse capture")

        path = self.get_snapshot_path(collection_id, filename)
        if not path:
            return False

        os.remove(path)
        remaining = self._snapshot_frame_names(dir_path)
        if remaining:
            meta = self._read_meta(dir_path) or {}
            self._write_meta(
                dir_path,
                meta.get("filename", collection_id),
                len(remaining),
            )
        else:
            shutil.rmtree(dir_path, ignore_errors=True)
        log.info(f"Timelapse: deleted snapshot {filename} from {collection_id}")
        return True

    def delete_snapshot_collection(self, collection_id):
        """Delete an entire snapshot collection.

        Archived and manual collections are removed immediately.
        A resumable paused capture may also be discarded here so it does not
        stay stuck in the Snapshots tab with no cleanup path.
        Active in-progress captures remain protected.
        """
        safe_collection = self._safe_path_component(collection_id)
        if not safe_collection:
            return False

        with self._lock:
            current_collection = (
                os.path.basename(self._current_dir)
                if self._current_dir and os.path.isdir(self._current_dir)
                else None
            )
            if current_collection == safe_collection:
                raise RuntimeError("Cannot delete snapshots from an active timelapse capture")

            resume_collection = (
                os.path.basename(self._resume_dir)
                if self._resume_dir and os.path.isdir(self._resume_dir)
                else None
            )
            if resume_collection == safe_collection:
                log.info(f"Timelapse: discarded paused capture collection {safe_collection}")
                self._cancel_pending_resume()
                self._automatic_pause_reason = None
                self._manual_pause_requested = False
                self._capture_pause_reason = None
                self._recovery_active = False
                self._recovery_reason = None
                self._disable_video_for_timelapse()
                return True

        dir_path, read_only = self._resolve_snapshot_collection_dir(safe_collection)
        if not dir_path:
            return False
        if read_only:
            raise RuntimeError("Cannot delete snapshots from an active timelapse capture")

        shutil.rmtree(dir_path, ignore_errors=True)
        log.info(f"Timelapse: deleted snapshot collection {safe_collection}")
        return True

    def _snapshot_collection_dirs_for_video(self, filename):
        safe_video = self._safe_path_component(filename)
        if not safe_video:
            return []

        safe_stem = os.path.splitext(safe_video)[0]
        matches = []
        seen = set()
        try:
            for name in os.listdir(self._snapshot_archive_base()):
                dir_path = os.path.join(self._snapshot_archive_base(), name)
                if not os.path.isdir(dir_path):
                    continue
                meta = self._read_meta(dir_path) or {}
                if not self._media_name_belongs_to_this_printer(name, meta):
                    continue
                meta_video = self._safe_path_component(meta.get("video_filename"))
                if name == safe_stem or meta_video == safe_video:
                    real_path = os.path.realpath(dir_path)
                    if real_path in seen:
                        continue
                    seen.add(real_path)
                    matches.append(dir_path)
        except OSError:
            return []
        return matches

    def _delete_snapshot_collections_for_video(self, filename):
        removed_collections = []
        for collection_dir in self._snapshot_collection_dirs_for_video(filename):
            shutil.rmtree(collection_dir, ignore_errors=True)
            removed_collections.append(os.path.basename(collection_dir))
        return removed_collections

    def delete_video(self, filename):
        """Delete a timelapse video."""
        path = self.get_video_path(filename)
        if path:
            os.remove(path)
            removed_collections = self._delete_snapshot_collections_for_video(filename)
            if removed_collections:
                log.info(
                    f"Timelapse: deleted {filename} and snapshot collection(s) "
                    f"{', '.join(removed_collections)}"
                )
            else:
                log.info(f"Timelapse: deleted {filename}")
            return True
        return False
