import json
import logging
import threading
import time

from queue import Empty

_STALL_TIMEOUT = 5.0  # seconds without a frame after video is flowing before soft restart
_INITIAL_FRAME_TIMEOUT = 12.0  # give a fresh START_LIVE longer to deliver its first frame
_STALL_MAX_RETRIES = 3  # escalate to hard restart after this many consecutive soft-reset failures
_LIVE_REFRESH_COOLDOWN = 4.0
_EXTERNAL_RECOVERY_COOLDOWN = 3.0

log = logging.getLogger(__name__)

from ..lib.service import Service, ServiceRestartSignal, ServiceStoppedError, RunState
from .. import app
from .pppp import PPPPService

from libflagship.pppp import P2PSubCmdType, P2PCmdType, Xzyh


VIDEO_PROFILES = [
    {
        "id": "sd",
        "label": "SD",
        "display": "SD (848x480)",
        "width": 848,
        "height": 480,
        "live": True,
        "live_mode": 0,
    },
    {
        "id": "hd",
        "label": "HD",
        "display": "HD (720p)",
        "width": 1280,
        "height": 720,
        "live": True,
        "live_mode": 1,
    },
    {
        "id": "fhd",
        "label": "FHD",
        "display": "FHD (1080p) - snapshot only",
        "width": 1920,
        "height": 1080,
        "live": False,
        "live_mode": None,
    },
]

VIDEO_PROFILES_BY_ID = {profile["id"]: profile for profile in VIDEO_PROFILES}
VIDEO_PROFILES_BY_MODE = {
    profile["live_mode"]: profile
    for profile in VIDEO_PROFILES
    if profile.get("live_mode") is not None
}
VIDEO_PROFILE_DEFAULT_ID = "hd" if "hd" in VIDEO_PROFILES_BY_ID else VIDEO_PROFILES[0]["id"]


class VideoQueue(Service):
    def __init__(self, printer_index=0):
        self.printer_index = 0 if printer_index is None else int(printer_index)
        self.video_enabled = False
        self.timelapse_enabled = False
        self.light_control_enabled = False
        self.last_frame_at = None
        self._enable_generation = 0  # increments each time video is enabled
        self._viewer_count = 0
        self._lock = threading.Lock()
        self._pppp_ref_held = False
        self._recycle_pppp_on_restart = False
        self._awaiting_pppp_recycle = False
        self._pending_disable = False
        self._in_place_recovery = False
        self._pppp_recycle_requested_at = None
        self._manual_recovery_requested = False
        self._manual_recovery_reason = None
        self._manual_recovery_force_pppp = False
        self._manual_recovery_requested_at = 0.0
        self._frame_event = threading.Event()
        super().__init__()

    def await_recent_frame(self, timeout: float = 3.0) -> bool:
        """Block until a new video frame arrives or *timeout* seconds elapse."""
        self._frame_event.clear()
        return self._frame_event.wait(timeout)

    @property
    def name(self):
        return f"VideoQueue[{getattr(self, 'printer_index', 0)}]"

    def _service_printer_index(self):
        return int(getattr(self, "printer_index", app.config.get("printer_index", 0) or 0))

    def _pppp_service_name(self):
        import web

        return web.resolve_pppp_service_name(self._service_printer_index())

    def _video_requested(self):
        return bool(
            getattr(self, "video_enabled", False)
            or getattr(self, "timelapse_enabled", False)
            or getattr(self, "light_control_enabled", False)
            or getattr(self, "_viewer_count", 0) > 0
        )

    def _sync_persistent_state(self):
        self.persistent = self._video_requested()

    def set_light_control_enabled(self, enabled):
        self.light_control_enabled = bool(enabled)
        self._sync_persistent_state()
        if self.light_control_enabled:
            self.start()
        else:
            self._stop_if_unrequested()

    def _recovery_state_details(self, pppp=None):
        pppp = pppp if pppp is not None else getattr(self, "pppp", None)
        pppp_service = getattr(getattr(app, "svc", None), "svcs", {}).get(self._pppp_service_name())
        pppp_service_state = getattr(pppp_service, "state", None)
        return (
            f"printer_index={self._service_printer_index()}, "
            f"video_enabled={bool(getattr(self, 'video_enabled', False))}, "
            f"timelapse_enabled={bool(getattr(self, 'timelapse_enabled', False))}, "
            f"light_control_enabled={bool(getattr(self, 'light_control_enabled', False))}, "
            f"viewers={int(getattr(self, '_viewer_count', 0))}, "
            f"wanted={bool(getattr(self, 'wanted', False))}, "
            f"live_active={bool(getattr(self, '_live_active', False))}, "
            f"api_id={'set' if getattr(self, 'api_id', None) is not None else 'none'}, "
            f"pppp_connected={bool(getattr(pppp, 'connected', False))}, "
            f"pppp_api={'set' if getattr(pppp, '_api', None) is not None else 'none'}, "
            f"pppp_ref_held={bool(getattr(self, '_pppp_ref_held', False))}, "
            f"awaiting_recycle={bool(getattr(self, '_awaiting_pppp_recycle', False))}, "
            f"pppp_service_state={pppp_service_state}"
        )

    def _clear_live_state(self):
        self._pending_disable = False
        self._live_active = False
        self._live_started_at = None
        self.last_frame_at = None
        self._last_start_live_at = 0.0
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0
        self._stall_retry_count = 0
        self._awaiting_pppp_recycle = False
        self._pppp_recycle_requested_at = None

    def _stop_if_unrequested(self):
        if self._video_requested():
            return True
        self._clear_live_state()
        if self._viewer_count > 0:
            log.info(
                "%s: disabling video; stopping service with %s video client(s) connected",
                self.name,
                self._viewer_count,
            )
        if self.state in (RunState.Starting, RunState.Running):
            self.stop()
        elif self.state in (RunState.Stopping, RunState.Stopped):
            self.wanted = False
        return True

    def api_start_live(self):
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False
        live_auth = self._live_auth_data()
        if not live_auth:
            log.warning("%s: cannot start live view because live auth data is missing", self.name)
            return False
        self.pppp.api_command(P2PSubCmdType.START_LIVE, data={
            "encryptkey": live_auth["encryptkey"],
            "accountId": live_auth["accountId"],
        })
        return True

    def _live_auth_data(self):
        """Return the live-view auth payload without logging sensitive values."""
        try:
            with app.config["config"].open() as cfg:
                if not cfg:
                    return None
                printer_index = self._service_printer_index()
                if printer_index < 0 or printer_index >= len(cfg.printers):
                    return None
                printer = cfg.printers[printer_index]
                account = getattr(cfg, "account", None)
                encryptkey = getattr(printer, "p2p_key", None)
                account_id = getattr(account, "user_id", None)
                if not encryptkey or not account_id:
                    return None
                return {
                    "encryptkey": encryptkey,
                    "accountId": account_id,
                }
        except Exception as exc:
            log.warning("%s: failed to load live auth data: %s", self.name, exc)
            return None

    def api_stop_live(self):
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False
        self.pppp.api_command(P2PSubCmdType.CLOSE_LIVE)
        return True

    def api_light_state(self, light):
        self.saved_light_state = light  # Track desired state regardless of connection
        if not self.pppp:
            return False
        self.pppp.api_command(P2PSubCmdType.LIGHT_STATE_SWITCH, data={
            "open": light,
        })
        log.info("%s: light %s", self.name, "on" if light else "off")
        return True

    def api_video_mode(self, mode):
        try:
            mode = int(mode)
        except (TypeError, ValueError):
            log.warning(f"{self.name}: Invalid video mode {mode!r}")
            return False
        self.saved_video_mode = mode
        profile = VIDEO_PROFILES_BY_MODE.get(mode)
        if profile:
            self.saved_video_profile_id = profile["id"]
        if not self.pppp:
            return False
        self.pppp.api_command(P2PSubCmdType.LIVE_MODE_SET, data={"mode": mode})
        return True

    def api_video_profile(self, profile_id):
        if profile_id is None:
            return False
        profile_key = str(profile_id).lower()
        profile = VIDEO_PROFILES_BY_ID.get(profile_key)
        if not profile:
            log.warning(f"{self.name}: Unknown video profile {profile_id!r}")
            return False
        self.saved_video_profile_id = profile["id"]
        live_mode = profile.get("live_mode")
        if not profile.get("live") or live_mode is None:
            log.info(f"{self.name}: Video profile {profile_id!r} is not supported for live view")
            return False
        self.saved_video_mode = live_mode
        if not self.pppp:
            return True
        self.pppp.api_command(P2PSubCmdType.LIVE_MODE_SET, data={"mode": live_mode})
        return True

    def _handler(self, data):
        chan, msg = data

        if chan != 1:
            return

        if not isinstance(msg, Xzyh):
            return

        if msg.cmd != P2PCmdType.APP_CMD_VIDEO_FRAME:
            log.debug("%s: ignoring non-video XZYH command %r", self.name, msg.cmd)
            return

        self.last_frame_at = time.monotonic()
        if hasattr(self, "_frame_event"):
            self._frame_event.set()
        self._live_active = True
        self._stall_retry_count = 0
        self.notify(msg)

    def _start_live_if_needed(self, force=False):
        if not self._video_requested() or not self.wanted:
            return False
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False

        now = time.monotonic()
        if not force and self._live_active and (now - self._last_start_live_at) < 10.0:
            log.info("%s: START_LIVE suppressed (already active recently)", self.name)
            return False

        self._last_start_live_at = now
        self._live_started_at = now
        self.last_frame_at = None
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0

        log.info(
            "%s: calling api_start_live() (force=%s) [%s]",
            self.name,
            force,
            self._recovery_state_details(self.pppp),
        )
        if not self.api_start_live():
            return False

        # Only mark live after the first frame arrives.
        self._live_active = False
        return True

    def _ensure_pppp_ready(self):
        """Return a ready PPPP service while preserving the existing borrow across
        internal VideoQueue restarts.

        Reacquiring PPPP on every VideoQueue restart causes a ref drop to zero,
        which stops PPPP and then races with the next video start. Keep the PPPP
        borrow alive across ordinary internal video restarts and only release it
        when video is actually being disabled, the app is shutting down, or a
        hard recovery explicitly requests a full PPPP recycle.
        """
        if self._pppp_ref_held and self.pppp is not None:
            try:
                self.pppp.await_ready()
            except ServiceStoppedError:
                try:
                    app.svc.put(self._pppp_service_name())
                except Exception:
                    pass
                self._pppp_ref_held = False
                self.pppp = None
            else:
                return self.pppp

        pppp_name = self._pppp_service_name()
        pppp_svc = getattr(app.svc, "svcs", {}).get(pppp_name)
        if self._awaiting_pppp_recycle and pppp_svc is not None:
            effectively_stopped = (
                not getattr(pppp_svc, "wanted", False)
                and not getattr(pppp_svc, "connected", False)
                and getattr(pppp_svc, "_api", None) is None
            )

            if pppp_svc.state != RunState.Stopped:
                recycle_requested_at = getattr(self, "_pppp_recycle_requested_at", None)
                stuck_after_forced_recycle = (
                    not getattr(pppp_svc, "wanted", False)
                    and recycle_requested_at is not None
                    and (time.monotonic() - recycle_requested_at) >= 2.0
                )
                if not effectively_stopped and not stuck_after_forced_recycle:
                    return None

                if stuck_after_forced_recycle and not effectively_stopped:
                    log.warning("%s: PPPP stop is stuck after forced recycle; marking service reusable", self.name)

                log.info("%s: PPPP recycle is effectively complete; proceeding with fresh reacquire", self.name)
                # The old PPPP worker can get wedged after a video freeze.
                # Replace the managed PPPP service with a fresh instance so a
                # new connection attempt is guaranteed to start from a clean
                # thread/service object instead of reusing stale state.
                app.svc.replace_service(pppp_name, PPPPService(printer_index=self._service_printer_index()))
                pppp_svc = app.svc.svcs.get(pppp_name)

            self._awaiting_pppp_recycle = False
            self._pppp_recycle_requested_at = None
            log.info("%s: PPPP recycle completed; reacquiring fresh PPPP session", self.name)

        # Important: during a recycle handoff, do not block waiting for PPPP to
        # become ready. Borrow it in non-ready mode and let worker_start/
        # worker_run observe the connection state naturally.
        self.pppp = app.svc.get(pppp_name, ready=False)
        self._pppp_ref_held = True
        return self.pppp

    def worker_init(self):
        self.saved_light_state = None
        self.saved_video_mode = None
        self.saved_video_profile_id = None
        self.last_frame_at = None
        self.timelapse_enabled = False
        self._live_started_at = None
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0
        self._last_start_live_at = 0.0
        self._live_active = False
        self.api_id = None
        self.pppp = None
        self._pppp_ref_held = False
        self._stall_retry_count = 0
        self._recycle_pppp_on_restart = False
        self._awaiting_pppp_recycle = False
        self._pending_disable = False
        self._in_place_recovery = False
        self._pppp_recycle_requested_at = None
        self._manual_recovery_requested = False
        self._manual_recovery_reason = None
        self._manual_recovery_force_pppp = False
        self._manual_recovery_requested_at = 0.0

    def request_live_recovery(self, reason="manual recovery", force_pppp_recycle=False):
        """Ask the worker thread to refresh the live stream for a stalled client."""
        if not self._video_requested():
            log.debug(
                "%s: ignoring live recovery request because video is not requested [%s]",
                self.name,
                self._recovery_state_details(),
            )
            return False

        now = time.monotonic()
        if (
            getattr(self, "_manual_recovery_requested", False)
            and (now - getattr(self, "_manual_recovery_requested_at", 0.0)) < _EXTERNAL_RECOVERY_COOLDOWN
        ):
            log.debug(
                "%s: coalescing recovery request (%s) because another recovery is already queued [%s]",
                self.name,
                reason,
                self._recovery_state_details(),
            )
            return False

        self._manual_recovery_requested = True
        self._manual_recovery_reason = str(reason or "manual recovery").strip() or "manual recovery"
        self._manual_recovery_force_pppp = bool(force_pppp_recycle)
        self._manual_recovery_requested_at = now
        log.info(
            "%s: queued recovery request (%s, force_pppp_recycle=%s) [%s]",
            self.name,
            self._manual_recovery_reason,
            self._manual_recovery_force_pppp,
            self._recovery_state_details(),
        )

        if self.state == RunState.Stopped and not self.wanted:
            self.start()
        else:
            self._event.set()
        return True

    def _handle_requested_recovery(self, pppp):
        if not getattr(self, "_manual_recovery_requested", False):
            return False

        reason = getattr(self, "_manual_recovery_reason", None) or "manual recovery"
        force_pppp_recycle = bool(getattr(self, "_manual_recovery_force_pppp", False))
        self._manual_recovery_requested = False
        self._manual_recovery_reason = None
        self._manual_recovery_force_pppp = False

        if (
            force_pppp_recycle
            or pppp is None
            or not getattr(pppp, "connected", False)
            or getattr(pppp, "_api", None) is None
            or getattr(self, "api_id", None) is None
        ):
            log.warning(
                "%s: %s; recycling PPPP in place [%s]",
                self.name,
                reason,
                self._recovery_state_details(pppp),
            )
            self._recycle_pppp_in_place()
            return True

        self._attempt_stall_recovery(
            pppp,
            f"{self.name}: {reason}; refreshing live stream",
            f"{self.name}: failed recovery request ({reason})",
            f"{self.name}: video recovery request exhausted ({reason})",
        )
        return True

    def _recycle_pppp_in_place(self):
        """Recycle the PPPP session without restarting the VideoQueue service.

        Keeping /ws/video open avoids forcing the browser to rebuild the
        websocket/JMuxer pipeline on every hard video recovery.
        """
        if not self._video_requested() or not self.wanted:
            log.info(
                "%s: PPPP recycle skipped because video was disabled [%s]",
                self.name,
                self._recovery_state_details(),
            )
            return

        self._in_place_recovery = True
        try:
            try:
                self.api_stop_live()
            except Exception:
                pass

            old_pppp = self.pppp
            if old_pppp is not None:
                try:
                    with old_pppp._handler_lock:
                        if self._handler in old_pppp.xzyh_handlers:
                            old_pppp.xzyh_handlers.remove(self._handler)
                except Exception as exc:
                    log.debug("%s: failed detaching handler during in-place recovery: %s", self.name, exc)

            if self._pppp_ref_held:
                log.info(
                    "%s: recycling PPPP in place for video recovery [%s]",
                    self.name,
                    self._recovery_state_details(),
                )
                self._awaiting_pppp_recycle = True
                self._pppp_recycle_requested_at = time.monotonic()
                app.svc.put(self._pppp_service_name())
                self._pppp_ref_held = False

            self.pppp = None
            self.api_id = None
            self._live_active = False
            self._last_start_live_at = 0.0
            self._live_started_at = None
            self.last_frame_at = None
            self._last_no_frame_log_at = 0.0
            self._last_live_refresh_at = 0.0
            self._stall_retry_count = 0
            log.info(
                "%s: PPPP recycle requested in place; waiting for clean stop before reacquire [%s]",
                self.name,
                self._recovery_state_details(),
            )
        finally:
            self._in_place_recovery = False

    def worker_start(self):
        self._stall_retry_count = 0
        if not self._video_requested():
            return

        self.pppp = self._ensure_pppp_ready()
        if not self.pppp:
            log.debug("%s: PPPP not available yet in worker_start", self.name)
            self.api_id = None
            return
        if not getattr(self.pppp, "connected", False):
            log.debug("%s: PPPP exists but is not connected yet in worker_start", self.name)
            self.api_id = None
            return
        if getattr(self.pppp, "_api", None) is None:
            log.debug("%s: PPPP connected but API not ready yet in worker_start", self.name)
            self.api_id = None
            return

        self.api_id = id(self.pppp._api)

        with self.pppp._handler_lock:
            if self._handler not in self.pppp.xzyh_handlers:
                self.pppp.xzyh_handlers.append(self._handler)
        self._start_live_if_needed(force=False)

        if self.saved_light_state is not None:
            self.api_light_state(self.saved_light_state)

        if self.saved_video_profile_id is not None:
            applied = self.api_video_profile(self.saved_video_profile_id)
            if not applied and self.saved_video_mode is not None:
                self.api_video_mode(self.saved_video_mode)
        elif self.saved_video_mode is not None:
            self.api_video_mode(self.saved_video_mode)

    def worker_run(self, timeout):
        if not self._video_requested():
            return
        self.idle(timeout=timeout)
        if not self._video_requested() or not self.wanted:
            return

        if self._in_place_recovery:
            self.idle(0.1)
            return

        if not self.pppp:
            self.pppp = self._ensure_pppp_ready()
            if self._handle_requested_recovery(self.pppp):
                self.idle(0.1)
                return
            if not self.pppp:
                log.debug("%s: PPPP not available yet", self.name)
                self.idle(0.5)
                return

        # Snapshot the PPPP reference to avoid TOCTOU races if the service
        # restarts between checks.  All subsequent accesses use this local.
        pppp = self.pppp
        if pppp is None:
            raise ServiceRestartSignal("PPPP reference lost during video session")

        if self._handle_requested_recovery(pppp):
            self.idle(0.1)
            return

        if not getattr(pppp, "connected", False):
            log.debug("%s: PPPP exists but is not connected yet", self.name)
            self.idle(0.5)
            return

        if getattr(self, "api_id", None) is None:
            if getattr(pppp, "_api", None) is None:
                log.debug("%s: PPPP connected but API not ready yet", self.name)
                self.idle(0.5)
                return

            self.api_id = id(pppp._api)
            with pppp._handler_lock:
                if self._handler not in pppp.xzyh_handlers:
                    pppp.xzyh_handlers.append(self._handler)

            started = self._start_live_if_needed(force=False)
            if not started:
                log.info("%s: failed to start live view during late init", self.name)
                self.idle(0.5)
                return

            if self.saved_light_state is not None:
                self.api_light_state(self.saved_light_state)

            if self.saved_video_profile_id is not None:
                applied = self.api_video_profile(self.saved_video_profile_id)
                if not applied and self.saved_video_mode is not None:
                    self.api_video_mode(self.saved_video_mode)
            elif self.saved_video_mode is not None:
                self.api_video_mode(self.saved_video_mode)

            log.info("%s: live video started after PPPP became ready", self.name)
            return

        if getattr(pppp, "_api", None) is None:
            raise ServiceRestartSignal("PPPP lost during video session")

        if id(pppp._api) != self.api_id:
            raise ServiceRestartSignal("New pppp connection detected, restarting video feed")

        if self.handlers or self.timelapse_enabled:
            now = time.monotonic()
            if self.last_frame_at is not None:
                gap = now - self.last_frame_at
                if gap > _STALL_TIMEOUT:
                    if now - self._last_live_refresh_at >= _LIVE_REFRESH_COOLDOWN:
                        self._attempt_stall_recovery(
                            pppp,
                            f"{self.name}: No video frames for {gap:.1f}s; restarting live stream",
                            f"{self.name}: Failed to restart live stream",
                            f"{self.name}: Video stall recovery exhausted",
                        )
                    self.idle(0.5)
                    return
            elif self._live_started_at is not None:
                since_start = now - self._live_started_at
                if since_start > _INITIAL_FRAME_TIMEOUT:
                    if now - self._last_no_frame_log_at >= 10.0:
                        log.info("%s: no initial frame yet after %.1fs; waiting", self.name, since_start)
                        self._last_no_frame_log_at = now
                    if now - self._last_live_refresh_at >= _LIVE_REFRESH_COOLDOWN:
                        self._attempt_stall_recovery(
                            pppp,
                            f"{self.name}: Re-requesting live stream (no initial frame)",
                            f"{self.name}: Failed to restart live stream for initial-frame recovery",
                            f"{self.name}: Video stall recovery exhausted (no initial frame)",
                        )
                    self.idle(0.5)
                    return

    def _attempt_stall_recovery(self, pppp, warn_msg, retry_fail_msg, exhaust_msg):
        """Stop and restart the live stream after a stall.

        Each invocation counts as one consecutive no-frame recovery attempt.
        The counter is only reset when an actual frame arrives in _handler().
        After enough consecutive no-frame recoveries, escalate to a full
        VideoQueue/PPPP restart instead of looping forever on START_LIVE.
        """
        self._last_live_refresh_at = time.monotonic()
        self._stall_retry_count += 1
        attempt = self._stall_retry_count
        log.warning("%s (attempt %s/%s)", warn_msg, attempt, _STALL_MAX_RETRIES)
        try:
            self._live_active = False
            self.api_stop_live()
            time.sleep(0.25)
            if not self._video_requested() or not self.wanted:
                log.info("%s: live refresh cancelled because video was disabled", self.name)
                return
            if not pppp or not getattr(pppp, "connected", False):
                log.warning("%s: PPPP unavailable during live refresh", self.name)
                if attempt >= _STALL_MAX_RETRIES:
                    raise ServiceRestartSignal(exhaust_msg)
                time.sleep(0.25)
                return
            if not self._start_live_if_needed(force=True):
                log.warning("%s (attempt %s/%s)", retry_fail_msg, attempt, _STALL_MAX_RETRIES)
            if attempt >= _STALL_MAX_RETRIES:
                log.warning("%s; recycling PPPP in place", exhaust_msg)
                self._recycle_pppp_in_place()
        except ServiceRestartSignal:
            raise
        except Exception as exc:
            log.warning("%s: failed to refresh live stream (%s)", self.name, exc)

    def worker_stop(self):
        try:
            self.api_stop_live()
        except Exception as E:
            log.warning(f"{self.name}: Failed to send stop command ({E})")

        if self.pppp:
            with self.pppp._handler_lock:
                if self._handler in self.pppp.xzyh_handlers:
                    self.pppp.xzyh_handlers.remove(self._handler)

        release_pppp = (not self.wanted) or (not self._video_requested()) or getattr(app.svc, "shutting_down", False) or self._recycle_pppp_on_restart
        if self.pppp and self._pppp_ref_held and release_pppp:
            if self._recycle_pppp_on_restart:
                log.info("%s: releasing PPPP so it can be recycled for video recovery", self.name)
                self._awaiting_pppp_recycle = True
            app.svc.put(self._pppp_service_name())
            self._pppp_ref_held = False
            self.pppp = None
        elif self.pppp and self._pppp_ref_held:
            log.info("%s: keeping PPPP borrowed across video worker restart", self.name)

        self._live_active = False
        self._last_start_live_at = 0.0
        self.api_id = None
        self._live_started_at = None
        if self._recycle_pppp_on_restart and self.wanted and self._video_requested():
            log.info("%s: PPPP will be reacquired on the next worker start", self.name)
        self._recycle_pppp_on_restart = False


    def viewer_connected(self):
        with self._lock:
            self._viewer_count += 1
            viewer_count = self._viewer_count
        return viewer_count

    def viewer_disconnected(self):
        stop_requested = False
        with self._lock:
            if self._viewer_count > 0:
                self._viewer_count -= 1
            else:
                log.warning(
                    "%s: viewer_disconnected called with viewer_count already 0 — likely a lifecycle pairing bug",
                    self.name,
                )
            viewer_count = self._viewer_count
            if viewer_count == 0 and self._pending_disable:
                log.info("%s: last viewer disconnected; clearing stale deferred disable", self.name)
                self._pending_disable = False
            elif viewer_count == 0 and not self._video_requested() and self.state == RunState.Running:
                log.info("%s: last viewer disconnected; stopping disabled video service", self.name)
                stop_requested = True
        if stop_requested:
            self.stop()
        return viewer_count

    def set_video_enabled(self, enabled):
        enabled = bool(enabled)
        should_start = False
        should_stop_if_unrequested = False
        with self._lock:
            if enabled:
                self._pending_disable = False
                was_enabled = bool(getattr(self, "video_enabled", False))
                self.video_enabled = True
                self._sync_persistent_state()
                if not was_enabled:
                    self._enable_generation += 1
                if self.state == RunState.Stopped or not self.wanted:
                    should_start = True
            else:
                was_enabled = bool(getattr(self, "video_enabled", False))
                self.video_enabled = False
                self._sync_persistent_state()
                if self._video_requested():
                    if was_enabled and self._viewer_count > 0:
                        log.info(
                            "%s: live view disabled; keeping stream active for timelapse",
                            self.name,
                        )
                elif was_enabled or self._pending_disable:
                    should_stop_if_unrequested = True

        if should_start:
            self.start()
            return True
        if should_stop_if_unrequested:
            return self._stop_if_unrequested()
        return True

    def set_timelapse_enabled(self, enabled):
        enabled = bool(enabled)
        was_enabled = bool(getattr(self, "timelapse_enabled", False))
        self.timelapse_enabled = enabled
        self._sync_persistent_state()

        if enabled:
            if self.state == RunState.Stopped:
                self.start()
            elif not self.wanted:
                self.start()
            return True

        if self._video_requested():
            return True
        if not was_enabled and not self._pending_disable:
            return True
        return self._stop_if_unrequested()

    def owns_video_for_timelapse(self):
        return bool(getattr(self, "timelapse_enabled", False))
