import json
import logging
import time

from queue import Empty
from multiprocessing import Queue

_STALL_TIMEOUT = 60.0  # seconds without a frame before forcing restart
_LIVE_REFRESH_COOLDOWN = 15.0

log = logging.getLogger(__name__)

from ..lib.service import Service, ServiceRestartSignal, RunState
from .. import app

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
    def __init__(self):
        self.video_enabled = False
        self.last_frame_at = None
        self._enable_generation = 0  # increments each time video is enabled
        super().__init__()

    def api_start_live(self):
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False
        self.pppp.api_command(P2PSubCmdType.START_LIVE, data={
            "encryptkey": "x",
            "accountId": "y",
        })
        return True

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
        log.info(f"VideoQueue: light {'on' if light else 'off'}")
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

        if msg.cmd == P2PCmdType.APP_CMD_VIDEO_FRAME:
            self.last_frame_at = time.monotonic()
            self._live_active = True

        self.notify(msg)

    def _start_live_if_needed(self, force=False):
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False

        now = time.monotonic()
        if not force and self._live_active and (now - self._last_start_live_at) < 10.0:
            log.info("VideoQueue: START_LIVE suppressed (already active recently)")
            return False

        self._last_start_live_at = now
        self._live_started_at = now
        self.last_frame_at = None
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0

        log.info("VideoQueue: calling api_start_live()")
        if not self.api_start_live():
            return False

        # Only mark live after the first frame arrives.
        self._live_active = False
        return True

    def worker_init(self):
        self.saved_light_state = None
        self.saved_video_mode = None
        self.saved_video_profile_id = None
        self.last_frame_at = None
        self._live_started_at = None
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0
        self._last_start_live_at = 0.0
        self._live_active = False
        self.api_id = None
        self.pppp = None

    def worker_start(self):
        if not self.video_enabled:
            return
        self.pppp = app.svc.get("pppp")
        if not self.pppp:
            log.info("VideoQueue: PPPP not available yet in worker_start")
            self.api_id = None
            return
        if not getattr(self.pppp, "connected", False):
            log.info("VideoQueue: PPPP exists but is not connected yet in worker_start")
            self.api_id = None
            return
        if not hasattr(self.pppp, "_api"):
            log.info("VideoQueue: PPPP connected but API not ready yet in worker_start")
            self.api_id = None
            return

        self.api_id = id(self.pppp._api)

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
        if not self.video_enabled:
            return
        self.idle(timeout=timeout)

        if not self.pppp:
            self.pppp = app.svc.get("pppp")
            if not self.pppp:
                log.info("VideoQueue: PPPP not available yet")
                time.sleep(0.5)
                return

        if not getattr(self.pppp, "connected", False):
            raise ServiceRestartSignal("No pppp connection")

        if getattr(self, "api_id", None) is None:
            if not hasattr(self.pppp, "_api"):
                log.info("VideoQueue: PPPP connected but API not ready yet")
                time.sleep(0.5)
                return

            self.api_id = id(self.pppp._api)
            if self._handler not in self.pppp.xzyh_handlers:
                self.pppp.xzyh_handlers.append(self._handler)

            started = self._start_live_if_needed(force=False)
            if not started:
                log.info("VideoQueue: Failed to start live view during late init")
                time.sleep(0.5)
                return

            if self.saved_light_state is not None:
                self.api_light_state(self.saved_light_state)

            if self.saved_video_profile_id is not None:
                applied = self.api_video_profile(self.saved_video_profile_id)
                if not applied and self.saved_video_mode is not None:
                    self.api_video_mode(self.saved_video_mode)
            elif self.saved_video_mode is not None:
                self.api_video_mode(self.saved_video_mode)

            log.info("VideoQueue: Live video started after PPPP became ready")
            return

        if not hasattr(self.pppp, "_api"):
            raise ServiceRestartSignal("PPPP lost during video session")

        if id(self.pppp._api) != self.api_id:
            raise ServiceRestartSignal("New pppp connection detected, restarting video feed")

        if self.handlers:
            now = time.monotonic()
            if self.last_frame_at is not None:
                gap = now - self.last_frame_at
                if gap > _STALL_TIMEOUT:
                    if now - self._last_live_refresh_at >= _LIVE_REFRESH_COOLDOWN:
                        self._last_live_refresh_at = now
                        log.warning(f"VideoQueue: No video frames for {gap:.1f}s; restarting live stream")
                        try:
                            self._live_active = False
                            self.api_stop_live()
                            time.sleep(0.5)
                            if not self.pppp or not getattr(self.pppp, "connected", False):
                                log.warning("VideoQueue: PPPP unavailable during live refresh")
                                time.sleep(0.5)
                                return
                            if not self._start_live_if_needed(force=True):
                                log.warning("VideoQueue: Failed to restart live stream")
                        except Exception as exc:
                            log.warning(f"VideoQueue: Failed to refresh live stream ({exc})")
                    time.sleep(0.5)
                    return
            elif self._live_started_at is not None:
                since_start = now - self._live_started_at
                if since_start > _STALL_TIMEOUT:
                    if now - self._last_no_frame_log_at >= 10.0:
                        log.info(f"VideoQueue: No initial frame yet after {since_start:.1f}s; waiting")
                        self._last_no_frame_log_at = now
                    if now - self._last_live_refresh_at >= _LIVE_REFRESH_COOLDOWN:
                        self._last_live_refresh_at = now
                        log.warning("VideoQueue: Re-requesting live stream (no initial frame)")
                        try:
                            self._live_active = False
                            self.api_stop_live()
                            time.sleep(0.5)
                            if not self.pppp or not getattr(self.pppp, "connected", False):
                                log.warning("VideoQueue: PPPP unavailable during initial-frame refresh")
                                time.sleep(0.5)
                                return
                            if not self._start_live_if_needed(force=True):
                                log.warning("VideoQueue: Failed to restart live stream for initial-frame recovery")
                        except Exception as exc:
                            log.warning(f"VideoQueue: Failed to refresh live stream ({exc})")
                    time.sleep(0.5)
                    return

    def worker_stop(self):
        try:
            self.api_stop_live()
        except Exception as E:
            log.warning(f"{self.name}: Failed to send stop command ({E})")

        if self.pppp and self._handler in self.pppp.xzyh_handlers:
            self.pppp.xzyh_handlers.remove(self._handler)

        if self.pppp:
            app.svc.put("pppp")
            self.pppp = None
        self._live_active = False
        self._last_start_live_at = 0.0
        self.api_id = None
        self._live_started_at = None

    def set_video_enabled(self, enabled):
        if enabled == self.video_enabled:
            return True

        self.video_enabled = enabled
        if not enabled:
            self.last_frame_at = None
        if enabled:
            self._enable_generation += 1
            if self.state == RunState.Stopped:
                self.start()
        else:
            if self.state == RunState.Running:
                self.stop()
        return True
