import json
import os
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import web
from web.service.timelapse import TimelapseService, _IN_PROGRESS_SUBDIR, _resolve_ffmpeg_path


class FakeConfigManager:
    def __init__(self, root, enabled=True):
        self.config_root = root
        self._cfg = SimpleNamespace(
            printers=[SimpleNamespace(sn="SN1", name="Printer", model="V8111")],
            timelapse={
                "enabled": enabled,
                "interval": 5,
                "max_videos": 2,
                "save_persistent": True,
                "light": None,
            }
        )

    @contextmanager
    def open(self):
        yield self._cfg


class _ImmediateThread:
    """threading.Thread stand-in that runs its target synchronously on start()."""

    def __init__(self, target=None, args=(), kwargs=None, **_unused):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def test_timelapse_meta_and_video_file_helpers(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    meta_dir = tmp_path / "capture"
    meta_dir.mkdir()
    svc._write_meta(meta_dir, "cube.gcode", 3)

    meta = svc._read_meta(meta_dir)
    assert meta["filename"] == "cube.gcode"
    assert meta["frame_count"] == 3
    assert meta["printer_index"] == 0
    assert meta["printer_scope"] == "printer_SN1"

    video_a = tmp_path / "a.mp4"
    video_b = tmp_path / "b.mp4"
    video_a.write_bytes(b"a")
    time.sleep(0.01)
    video_b.write_bytes(b"bb")

    snapshot_dir = tmp_path / "snapshots" / "a"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "frame_00000.jpg").write_bytes(b"frame")
    svc._write_meta(
        snapshot_dir,
        "cube.gcode",
        1,
        video_filename="a.mp4",
        archived_at="2026-04-10T12:00:00",
        status="archived",
    )

    videos = svc.list_videos()
    assert [video["filename"] for video in videos] == ["b.mp4", "a.mp4"]
    assert svc.get_video_path("a.mp4") == str(video_a)
    assert svc.get_video_path("../a.mp4") is None
    assert svc.delete_video("a.mp4") is True
    assert not video_a.exists()
    assert not snapshot_dir.exists()


def test_timelapse_snapshot_helpers_list_download_and_delete(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    active_dir = tmp_path / _IN_PROGRESS_SUBDIR / "cube_active"
    active_dir.mkdir(parents=True)
    (active_dir / "frame_00000.jpg").write_bytes(b"active")
    svc._write_meta(active_dir, "cube.gcode", 1)
    svc._current_dir = str(active_dir)

    archived_dir = tmp_path / "snapshots" / "cube_video_20260410"
    archived_dir.mkdir(parents=True)
    (archived_dir / "frame_00000.jpg").write_bytes(b"frame-a")
    (archived_dir / "frame_00001.jpg").write_bytes(b"frame-b")
    svc._write_meta(
        archived_dir,
        "cube.gcode",
        2,
        video_filename="cube_video_20260410.mp4",
        archived_at="2026-04-10T12:00:00",
    )

    collections = svc.list_snapshots()
    assert [collection["id"] for collection in collections] == ["cube_active", "cube_video_20260410"]
    assert collections[0]["allow_delete"] is False
    assert collections[1]["allow_delete"] is True
    assert collections[1]["video_filename"] == "cube_video_20260410.mp4"
    assert svc.get_snapshot_path("cube_active", "frame_00000.jpg") == str(active_dir / "frame_00000.jpg")
    assert svc.get_snapshot_path("cube_video_20260410", "frame_00001.jpg") == str(archived_dir / "frame_00001.jpg")

    try:
        svc.delete_snapshot("cube_active", "frame_00000.jpg")
        assert False, "Expected delete_snapshot to reject active captures"
    except RuntimeError:
        pass

    assert svc.delete_snapshot("cube_video_20260410", "frame_00000.jpg") is True
    assert not (archived_dir / "frame_00000.jpg").exists()
    assert svc._read_meta(archived_dir)["frame_count"] == 1
    assert svc.delete_snapshot("cube_video_20260410", "frame_00001.jpg") is True
    assert not archived_dir.exists()


def test_timelapse_can_discard_resume_pending_snapshot_collection(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    resume_dir = tmp_path / _IN_PROGRESS_SUBDIR / "cube_resume"
    resume_dir.mkdir(parents=True)
    (resume_dir / "frame_00000.jpg").write_bytes(b"resume")
    svc._write_meta(resume_dir, "cube.gcode", 1)
    svc._resume_dir = str(resume_dir)
    svc._resume_filename = "cube.gcode"
    svc._resume_frame_count = 1

    collections = svc.list_snapshots()
    assert collections[0]["id"] == "cube_resume"
    assert collections[0]["state"] == "resume_pending"
    assert collections[0]["allow_delete"] is False

    assert svc.delete_snapshot_collection("cube_resume") is True
    assert not resume_dir.exists()
    assert svc._resume_dir is None
    assert svc._resume_filename is None
    assert svc._resume_frame_count == 0


def test_timelapse_manual_snapshot_save_list_and_delete(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    source = tmp_path / "manual-source.jpg"
    source.write_bytes(b"manual-jpg")

    saved = svc.save_manual_snapshot(
        str(source),
        camera_settings={
            "effective_source": "external",
            "external": {"name": "Workbench Cam"},
        },
    )

    collections = svc.list_snapshots()
    assert len(collections) == 1
    assert collections[0]["id"] == saved["collection_id"]
    assert collections[0]["state"] == "manual"
    assert collections[0]["allow_delete"] is True
    assert collections[0]["source_label"] == "External camera (Workbench Cam)"
    assert collections[0]["frame_count"] == 1
    assert collections[0]["frames"][0]["filename"] == saved["filename"]
    assert svc.get_snapshot_path(saved["collection_id"], saved["filename"]).endswith(saved["filename"])

    assert svc.delete_snapshot(saved["collection_id"], saved["filename"]) is True
    assert svc.list_snapshots() == []


def test_timelapse_manual_pause_coexists_with_automatic_pause(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    svc.set_manual_pause(True)
    state = svc.get_runtime_state()
    assert state["paused"] is True
    assert state["pause_reason"] == "manual"

    svc.set_capture_paused(True, reason="filament_runout")
    state = svc.get_runtime_state()
    assert state["paused"] is True
    assert state["pause_reason"] == "filament_runout"

    svc.set_manual_pause(False)
    state = svc.get_runtime_state()
    assert state["paused"] is True
    assert state["pause_reason"] == "filament_runout"

    svc.set_capture_paused(False)
    state = svc.get_runtime_state()
    assert state["paused"] is False
    assert state["pause_reason"] is None


def test_timelapse_prunes_old_videos(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    for name in ("old.mp4", "mid.mp4", "new.mp4"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        time.sleep(0.01)

    old_snapshots = tmp_path / "snapshots" / "old"
    old_snapshots.mkdir(parents=True)
    (old_snapshots / "frame_00000.jpg").write_bytes(b"old-frame")
    svc._write_meta(
        old_snapshots,
        "old.gcode",
        1,
        video_filename="old.mp4",
        archived_at="2026-04-10T12:00:00",
        status="archived",
    )

    svc._prune_old_videos()

    remaining = sorted(p.name for p in tmp_path.glob("*.mp4"))
    assert remaining == ["mid.mp4", "new.mp4"]
    assert not old_snapshots.exists()


def test_timelapse_resolves_ffmpeg_through_web_fallback(monkeypatch):
    import web

    monkeypatch.setattr("web.service.timelapse.shutil.which", lambda name: None)
    monkeypatch.setattr(web, "_ffmpeg_path", lambda: r"C:\ffmpeg\ffmpeg.exe")

    assert _resolve_ffmpeg_path() == r"C:\ffmpeg\ffmpeg.exe"


def test_timelapse_scan_recovers_or_resumes_in_progress(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    base = tmp_path / _IN_PROGRESS_SUBDIR
    base.mkdir()

    young = base / "young_capture"
    young.mkdir()
    (young / "frame_00000.jpg").write_bytes(b"x")
    (young / "frame_00001.jpg").write_bytes(b"y")
    with open(young / ".meta", "w") as fh:
        json.dump({"filename": "young.gcode", "frame_count": 2}, fh)

    old = base / "old_capture"
    old.mkdir()
    (old / "frame_00000.jpg").write_bytes(b"x")
    (old / "frame_00001.jpg").write_bytes(b"y")
    with open(old / ".meta", "w") as fh:
        json.dump({"filename": "old.gcode", "frame_count": 2}, fh)
    stale_time = time.time() - (25 * 3600)
    os.utime(old, (stale_time, stale_time))

    scheduled = []
    assembled = []
    pruned = []

    monkeypatch.setattr(TimelapseService, "_schedule_finalize", lambda self, d, f, c, suffix="": scheduled.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_assemble_video_from", lambda self, d, f, c, suffix="": assembled.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_prune_old_videos", lambda self: pruned.append(True))
    # Orphan assembly now runs in a background thread (M2); run it
    # synchronously here so the assertions below are deterministic.
    monkeypatch.setattr("web.service.timelapse.threading.Thread", _ImmediateThread)

    svc = TimelapseService(cfg, captures_dir=tmp_path)

    assert svc._resume_filename == "young.gcode"
    assert svc._resume_frame_count == 2
    assert scheduled and scheduled[0][1] == "young.gcode"
    assert assembled and assembled[0][1] == "old.gcode" and assembled[0][3] == "_recovered"
    assert pruned == [True]


def test_timelapse_scan_assembles_orphans_in_background(monkeypatch, tmp_path):
    """Orphan-capture assembly must not block service startup (M2)."""
    cfg = FakeConfigManager(tmp_path)
    base = tmp_path / _IN_PROGRESS_SUBDIR
    base.mkdir()

    young = base / "young_capture"
    young.mkdir()
    (young / "frame_00000.jpg").write_bytes(b"x")
    (young / "frame_00001.jpg").write_bytes(b"y")
    with open(young / ".meta", "w") as fh:
        json.dump({"filename": "young.gcode", "frame_count": 2}, fh)

    old = base / "old_capture"
    old.mkdir()
    (old / "frame_00000.jpg").write_bytes(b"x")
    (old / "frame_00001.jpg").write_bytes(b"y")
    with open(old / ".meta", "w") as fh:
        json.dump({"filename": "old.gcode", "frame_count": 2}, fh)
    stale_time = time.time() - (25 * 3600)
    os.utime(old, (stale_time, stale_time))

    started = threading.Event()
    release = threading.Event()
    done = threading.Event()
    finalized = []

    def slow_finalize(self, d, f, c, suffix=""):
        started.set()
        release.wait(timeout=2)
        finalized.append((d, f, c, suffix))
        done.set()

    monkeypatch.setattr(TimelapseService, "_schedule_finalize", lambda self, d, f, c, suffix="": None)
    monkeypatch.setattr(TimelapseService, "_finalize_capture_dir", slow_finalize)

    start = time.monotonic()
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert started.wait(timeout=2)
    release.set()
    assert done.wait(timeout=2)
    assert finalized and finalized[0][1] == "old.gcode" and finalized[0][3] == "_recovered"


def test_timelapse_scan_only_recovers_matching_printer_capture(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    cfg._cfg.printers.append(SimpleNamespace(sn="SN2", name="Printer 2", model="V8111"))
    base = tmp_path / _IN_PROGRESS_SUBDIR
    base.mkdir()

    printer_zero = base / "printer_SN1_zero_capture"
    printer_zero.mkdir()
    (printer_zero / "frame_00000.jpg").write_bytes(b"x")
    (printer_zero / "frame_00001.jpg").write_bytes(b"y")
    with open(printer_zero / ".meta", "w") as fh:
        json.dump({
            "filename": "zero.gcode",
            "frame_count": 2,
            "printer_index": 0,
            "printer_scope": "printer_SN1",
        }, fh)

    printer_one = base / "printer_SN2_one_capture"
    printer_one.mkdir()
    (printer_one / "frame_00000.jpg").write_bytes(b"x")
    (printer_one / "frame_00001.jpg").write_bytes(b"y")
    with open(printer_one / ".meta", "w") as fh:
        json.dump({
            "filename": "one.gcode",
            "frame_count": 2,
            "printer_index": 1,
            "printer_scope": "printer_SN2",
        }, fh)

    scheduled = []
    finalized = []

    monkeypatch.setattr(
        TimelapseService,
        "_schedule_finalize",
        lambda self, d, f, c, suffix="": scheduled.append((self._printer_index, d, f, c, suffix)),
    )
    monkeypatch.setattr(
        TimelapseService,
        "_finalize_capture_dir",
        lambda self, d, f, c, suffix="": finalized.append((self._printer_index, d, f, c, suffix)),
    )

    svc0 = TimelapseService(cfg, captures_dir=tmp_path, printer_index=0)
    assert svc0._resume_filename == "zero.gcode"
    assert scheduled == [(0, str(printer_zero), "zero.gcode", 2, "")]
    assert finalized == []

    scheduled.clear()
    svc1 = TimelapseService(cfg, captures_dir=tmp_path, printer_index=1)
    assert svc1._resume_filename == "one.gcode"
    assert scheduled == [(1, str(printer_one), "one.gcode", 2, "")]
    assert finalized == []


def test_timelapse_archived_media_listing_is_scoped_per_printer(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    cfg._cfg.printers.append(SimpleNamespace(sn="SN2", name="Printer 2", model="V8111"))

    svc0 = TimelapseService(cfg, captures_dir=tmp_path, printer_index=0)
    svc1 = TimelapseService(cfg, captures_dir=tmp_path, printer_index=1)

    (tmp_path / "printer_SN1_zero.mp4").write_bytes(b"zero")
    (tmp_path / "printer_SN2_one.mp4").write_bytes(b"one")
    (tmp_path / "legacy.mp4").write_bytes(b"legacy")

    snapshots = tmp_path / "snapshots"
    zero_snapshots = snapshots / "printer_SN1_zero"
    zero_snapshots.mkdir(parents=True)
    (zero_snapshots / "frame_00000.jpg").write_bytes(b"zero")
    svc0._write_meta(zero_snapshots, "zero.gcode", 1)

    one_snapshots = snapshots / "printer_SN2_one"
    one_snapshots.mkdir(parents=True)
    (one_snapshots / "frame_00000.jpg").write_bytes(b"one")
    svc1._write_meta(one_snapshots, "one.gcode", 1)

    assert [video["filename"] for video in svc0.list_videos()] == [
        "printer_SN1_zero.mp4",
        "legacy.mp4",
    ]
    assert [video["filename"] for video in svc1.list_videos()] == ["printer_SN2_one.mp4"]
    assert [collection["id"] for collection in svc0.list_snapshots()] == ["printer_SN1_zero"]
    assert [collection["id"] for collection in svc1.list_snapshots()] == ["printer_SN2_one"]
    assert svc0.get_video_path("legacy.mp4") == str(tmp_path / "legacy.mp4")
    assert svc1.get_video_path("legacy.mp4") is None


def test_timelapse_finish_and_fail_paths(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._current_dir = str(tmp_path / "current")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 3
    os.makedirs(svc._current_dir, exist_ok=True)

    finalize_calls = []
    disable_calls = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_cancel_pending_resume", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_disable_video_for_timelapse", lambda self: disable_calls.append(True))
    monkeypatch.setattr(TimelapseService, "_assemble_video_from", lambda self, d, f, c, suffix="": finalize_calls.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_prune_old_videos", lambda self: finalize_calls.append(("prune",)))
    monkeypatch.setattr(TimelapseService, "_cleanup_dir", lambda self, d: finalize_calls.append(("cleanup", d)))

    svc.finish_capture(final=True)

    assert ("prune",) in finalize_calls
    assert any(call[:4] == (str(tmp_path / "current"), "cube.gcode", 3, "") for call in finalize_calls if len(call) == 4)
    assert disable_calls == [True]

    svc._current_dir = str(tmp_path / "failed")
    svc._current_filename = "failed.gcode"
    svc._frame_count = 2
    os.makedirs(svc._current_dir, exist_ok=True)
    finalize_calls.clear()
    disable_calls.clear()

    svc.fail_capture()

    assert any(call[:4] == (str(tmp_path / "failed"), "failed.gcode", 2, "_partial") for call in finalize_calls if len(call) == 4)
    assert disable_calls == [True]


def test_timelapse_start_capture_resumes_pending_session(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    resume_dir = str(tmp_path / _IN_PROGRESS_SUBDIR / "resume")
    os.makedirs(resume_dir, exist_ok=True)
    svc._resume_dir = resume_dir
    svc._resume_filename = "cube.gcode"
    svc._resume_frame_count = 4

    calls = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target

        def start(self):
            calls.append("thread-start")

    monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: calls.append("stop-thread"))
    monkeypatch.setattr(TimelapseService, "_cancel_finalize_timer", lambda self: calls.append("cancel-finalize"))
    monkeypatch.setattr(TimelapseService, "_cancel_pending_resume", lambda self: calls.append("cancel-pending"))
    monkeypatch.setattr(TimelapseService, "_enable_video_for_timelapse", lambda self: calls.append("enable-video"))
    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)

    svc.start_capture("cube.gcode")

    assert svc._current_dir == resume_dir
    assert svc._current_filename == "cube.gcode"
    assert svc._frame_count == 4
    assert svc._resume_dir is None
    assert calls == ["stop-thread", "cancel-finalize", "enable-video", "thread-start"]


def test_timelapse_start_capture_same_active_file_keeps_existing_session(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    active_dir = str(tmp_path / _IN_PROGRESS_SUBDIR / "active_same")
    os.makedirs(active_dir, exist_ok=True)
    svc._current_dir = active_dir
    svc._current_filename = "cube.gcode"
    svc._frame_count = 7

    calls = []

    class AliveThread:
        def is_alive(self):
            return True

    class UnexpectedThread:
        def __init__(self, *args, **kwargs):
            calls.append("new-thread")

        def start(self):
            calls.append("thread-start")

    svc._capture_thread = AliveThread()

    monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        TimelapseService,
        "_resolve_capture_camera",
        lambda self: {"effective_source": web.camera.CAMERA_SOURCE_PRINTER},
    )
    monkeypatch.setattr(TimelapseService, "_enable_video_for_timelapse", lambda self: calls.append("enable-video"))
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: calls.append("stop-thread"))
    monkeypatch.setattr("web.service.timelapse.threading.Thread", UnexpectedThread)

    svc.start_capture("cube.gcode")

    assert svc._current_dir == active_dir
    assert svc._current_filename == "cube.gcode"
    assert svc._frame_count == 7
    assert calls == ["enable-video"]


def test_timelapse_start_capture_same_active_file_restarts_dead_thread(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    active_dir = str(tmp_path / _IN_PROGRESS_SUBDIR / "active_restart")
    os.makedirs(active_dir, exist_ok=True)
    svc._current_dir = active_dir
    svc._current_filename = "cube.gcode"
    svc._frame_count = 5

    calls = []

    class DeadThread:
        def is_alive(self):
            return False

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target

        def start(self):
            calls.append("thread-start")

    svc._capture_thread = DeadThread()

    monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        TimelapseService,
        "_resolve_capture_camera",
        lambda self: {"effective_source": web.camera.CAMERA_SOURCE_PRINTER},
    )
    monkeypatch.setattr(TimelapseService, "_enable_video_for_timelapse", lambda self: calls.append("enable-video"))
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: calls.append("stop-thread"))
    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)

    svc.start_capture("cube.gcode")

    assert svc._current_dir == active_dir
    assert svc._current_filename == "cube.gcode"
    assert svc._frame_count == 5
    assert calls == ["enable-video", "thread-start"]


def test_timelapse_discard_pending_resume(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    resume_dir = tmp_path / _IN_PROGRESS_SUBDIR / "resume_discard"
    resume_dir.mkdir(parents=True, exist_ok=True)
    (resume_dir / "frame_00000.jpg").write_bytes(b"x")
    svc._resume_dir = str(resume_dir)
    svc._resume_filename = "cube.gcode"
    svc._resume_frame_count = 1

    discarded = svc.discard_pending_resume("cube.gcode")

    assert discarded is True
    assert svc._resume_dir is None
    assert svc._resume_filename is None
    assert svc._resume_frame_count == 0
    assert not resume_dir.exists()


def test_timelapse_take_snapshot_retries_and_restores_light(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._light_mode = "snapshot"
    svc._current_dir = str(tmp_path / "capture")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 0
    os.makedirs(svc._current_dir, exist_ok=True)

    light_calls = []
    videoqueue = SimpleNamespace(saved_light_state=False, api_light_state=lambda state: light_calls.append(state))
    old_svc = getattr(__import__("web").app, "svc")
    old_api_key = __import__("web").app.config.get("api_key")
    __import__("web").app.svc = SimpleNamespace(svcs={"videoqueue": videoqueue})
    __import__("web").app.config["api_key"] = "secret-key"

    captures = []

    def fake_capture(camera_settings, ffmpeg_path, frame_path, **kwargs):
        captures.append({
            "camera_settings": camera_settings,
            "ffmpeg_path": ffmpeg_path,
            "frame_path": frame_path,
            **kwargs,
        })
        with open(frame_path, "wb") as fh:
            fh.write(b"jpg")

    try:
        monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "resolved-ffmpeg")
        monkeypatch.setattr(TimelapseService, "_await_video_frame", lambda self: True)
        monkeypatch.setattr("web.camera.capture_camera_snapshot_to_file", fake_capture)
        monkeypatch.setattr("web.service.timelapse.time.sleep", lambda seconds: None)
        svc._take_snapshot()
    finally:
        __import__("web").app.svc = old_svc
        __import__("web").app.config["api_key"] = old_api_key

    assert len(captures) == 1
    assert captures[0]["ffmpeg_path"] == "resolved-ffmpeg"
    assert captures[0]["api_key"] == "secret-key"
    assert captures[0]["extra_headers"] == "X-Api-Key: secret-key\r\n"
    assert captures[0]["for_timelapse"] is True
    assert captures[0]["camera_settings"]["effective_source"] == "printer"
    assert light_calls == [True, False]
    assert svc._frame_count == 1
    assert os.path.exists(os.path.join(svc._current_dir, "frame_00000.jpg"))


def test_timelapse_assemble_uses_resolved_ffmpeg(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "frame_00000.jpg").write_bytes(b"jpg")
    (frames_dir / "frame_00001.jpg").write_bytes(b"jpg")
    runs = []

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        runs.append(cmd)
        with open(cmd[-1], "wb") as fh:
            fh.write(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "resolved-ffmpeg")
    monkeypatch.setattr("web.service.timelapse.subprocess.run", fake_run)

    svc._assemble_video_from(str(frames_dir), "cube.gcode", 2)

    assert runs[0][0] == "resolved-ffmpeg"
    assert list(tmp_path.glob("*.mp4"))


def test_capture_thread_crash_clears_ref(tmp_path):
    """If _capture_loop() crashes from an uncaught exception, the
    try/finally should clear _capture_thread so start_capture() knows
    the thread is dead."""
    class CaptureLoopCrash(BaseException):
        """Uncatchable by except Exception — simulates a fatal crash."""

    config_mgr = SimpleNamespace(config_root=str(tmp_path))
    svc = TimelapseService(config_mgr, captures_dir=str(tmp_path))
    svc._interval = 0

    call_count = [0]

    def crashing_snapshot():
        call_count[0] += 1
        if call_count[0] >= 2:
            raise CaptureLoopCrash("Simulated fatal crash")

    svc._take_snapshot = crashing_snapshot
    svc._capture_thread = object()

    with pytest.raises(CaptureLoopCrash, match="Simulated fatal crash"):
        svc._capture_loop()

    assert svc._capture_thread is None, "_capture_thread should be None after crash"


def test_stop_capture_thread_clears_stale_dead_thread_reference(tmp_path):
    import threading

    config_mgr = SimpleNamespace(config_root=str(tmp_path))
    svc = TimelapseService(config_mgr, captures_dir=str(tmp_path))

    stale_thread = threading.Thread(target=lambda: None, daemon=True)
    stale_thread.start()
    stale_thread.join(timeout=2)
    assert stale_thread.is_alive() is False

    svc._capture_thread = stale_thread
    svc._stop_capture_thread()

    assert svc._capture_thread is None


def test_stop_capture_thread_returns_quickly_with_active_capture_thread(tmp_path):
    """_stop_capture_thread() is called from finish_capture()/fail_capture()/
    start_capture() while holding self._lock. _capture_loop()'s finally block
    must clear _capture_thread without acquiring that lock, otherwise the
    join() below stalls for the full _SNAPSHOT_TIMEOUT + 2 (12s)."""
    import threading

    config_mgr = SimpleNamespace(config_root=str(tmp_path))
    svc = TimelapseService(config_mgr, captures_dir=str(tmp_path))
    svc._interval = 9999
    svc.is_capture_paused = lambda: False
    svc._take_snapshot = lambda: None

    svc._stop_event.clear()
    svc._capture_thread = threading.Thread(target=svc._capture_loop, daemon=True)
    svc._capture_thread.start()

    # Give the capture thread time to enter its long wait().
    time.sleep(0.1)

    with svc._lock:
        start = time.monotonic()
        svc._stop_capture_thread()
        elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"_stop_capture_thread blocked for {elapsed:.2f}s"
    assert svc._capture_thread is None


def test_capture_loop_skips_snapshots_while_capture_is_paused(tmp_path):
    import threading

    config_mgr = SimpleNamespace(config_root=str(tmp_path))
    svc = TimelapseService(config_mgr, captures_dir=str(tmp_path))
    svc._interval = 0.01

    snapshot_called = threading.Event()

    def fake_snapshot():
        snapshot_called.set()
        svc._stop_event.set()

    svc._take_snapshot = fake_snapshot
    svc.set_capture_paused(True, reason="filament_change")
    svc._capture_thread = threading.Thread(target=svc._capture_loop, daemon=True)
    svc._capture_thread.start()

    time.sleep(0.05)
    assert snapshot_called.is_set() is False

    svc.set_capture_paused(False)
    svc._capture_thread.join(timeout=2)

    assert snapshot_called.is_set() is True


def test_await_video_frame_requests_recovery_when_frames_go_stale(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    requests = []
    videoqueue = SimpleNamespace(
        last_frame_at=None,
        pppp=SimpleNamespace(connected=False),
        request_live_recovery=lambda **kwargs: requests.append(kwargs) or True,
    )
    old_svc = web.app.svc
    web.app.svc = SimpleNamespace(svcs={"videoqueue": videoqueue})

    try:
        monkeypatch.setattr("web.service.timelapse.time.sleep", lambda seconds: None)
        assert svc._await_video_frame(timeout=0.01, max_age=1.5) is False
    finally:
        web.app.svc = old_svc

    assert len(requests) == 1
    assert requests[0]["reason"] == "timelapse has no recent video frame"
    assert requests[0]["force_pppp_recycle"] is True


def test_snapshot_timeout_requests_video_recovery(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._current_dir = str(tmp_path / "capture")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 0
    os.makedirs(svc._current_dir, exist_ok=True)

    requests = []
    videoqueue = SimpleNamespace(
        request_live_recovery=lambda **kwargs: requests.append(kwargs) or True,
    )
    old_svc = web.app.svc
    old_api_key = web.app.config.get("api_key")
    web.app.svc = SimpleNamespace(svcs={"videoqueue": videoqueue})
    web.app.config["api_key"] = None

    def fake_capture(*args, **kwargs):
        raise __import__("subprocess").TimeoutExpired(cmd="ffmpeg", timeout=10)

    try:
        monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "resolved-ffmpeg")
        monkeypatch.setattr(TimelapseService, "_await_video_frame", lambda self: True)
        monkeypatch.setattr("web.camera.capture_camera_snapshot_to_file", fake_capture)
        monkeypatch.setattr("web.service.timelapse.time.sleep", lambda seconds: None)
        svc._take_snapshot()
    finally:
        web.app.svc = old_svc
        web.app.config["api_key"] = old_api_key

    assert len(requests) == 1
    assert requests[0]["reason"] == "timelapse snapshot timed out waiting for a camera frame"
    assert requests[0]["force_pppp_recycle"] is True


def test_timelapse_external_camera_snapshot_skips_printer_video_wait(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    cfg._cfg.timelapse["camera_source"] = "external"
    cfg._cfg.camera = {
        "per_printer": {
            "SN1": {
                "source": "printer",
                "external": {
                    "name": "Workbench Cam",
                    "snapshot_url": "http://cam.local/snapshot.jpg",
                    "stream_url": "",
                    "refresh_sec": 2,
                },
            }
        }
    }
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._current_dir = str(tmp_path / "capture")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 0
    os.makedirs(svc._current_dir, exist_ok=True)

    captures = []
    old_svc = web.app.svc
    old_api_key = web.app.config.get("api_key")
    web.app.svc = SimpleNamespace(svcs={})
    web.app.config["api_key"] = "secret-key"

    def fail_if_called(self):
        raise AssertionError("_await_video_frame should not be called for external cameras")

    def fake_capture(camera_settings, ffmpeg_path, frame_path, **kwargs):
        captures.append({
            "camera_settings": camera_settings,
            "ffmpeg_path": ffmpeg_path,
            **kwargs,
        })
        with open(frame_path, "wb") as fh:
            fh.write(b"jpg")

    try:
        monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "resolved-ffmpeg")
        monkeypatch.setattr(TimelapseService, "_await_video_frame", fail_if_called)
        monkeypatch.setattr("web.camera.capture_camera_snapshot_to_file", fake_capture)
        svc._take_snapshot()
    finally:
        web.app.svc = old_svc
        web.app.config["api_key"] = old_api_key

    assert len(captures) == 1
    assert captures[0]["camera_settings"]["source"] == "external"
    assert cfg._cfg.camera["per_printer"]["SN1"]["source"] == "printer"


def test_timelapse_external_camera_snapshot_uses_printer_light(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    cfg._cfg.timelapse["camera_source"] = "external"
    cfg._cfg.camera = {
        "per_printer": {
            "SN1": {
                "source": "printer",
                "external": {
                    "name": "Workbench Cam",
                    "snapshot_url": "http://cam.local/snapshot.jpg",
                    "stream_url": "",
                    "refresh_sec": 2,
                },
            }
        }
    }
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._light_mode = "snapshot"
    svc._current_dir = str(tmp_path / "capture")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 0
    os.makedirs(svc._current_dir, exist_ok=True)

    captures = []
    light_calls = []
    old_svc = web.app.svc
    old_api_key = web.app.config.get("api_key")
    web.app.svc = SimpleNamespace(svcs={"videoqueue": SimpleNamespace(saved_light_state=False)})
    web.app.config["api_key"] = "secret-key"

    def fail_if_called(self):
        raise AssertionError("_await_video_frame should not be called for external cameras")

    def fake_capture(camera_settings, ffmpeg_path, frame_path, **kwargs):
        captures.append({
            "camera_settings": camera_settings,
            "ffmpeg_path": ffmpeg_path,
            **kwargs,
        })
        with open(frame_path, "wb") as fh:
            fh.write(b"jpg")

    try:
        monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "resolved-ffmpeg")
        monkeypatch.setattr(TimelapseService, "_await_video_frame", fail_if_called)
        monkeypatch.setattr("web.camera.capture_camera_snapshot_to_file", fake_capture)
        monkeypatch.setattr("web.set_printer_light_state", lambda state, printer_index=None: light_calls.append(state) or True)
        monkeypatch.setattr("web.service.timelapse.time.sleep", lambda seconds: None)
        svc._take_snapshot()
    finally:
        web.app.svc = old_svc
        web.app.config["api_key"] = old_api_key

    assert len(captures) == 1
    assert captures[0]["for_timelapse"] is False
    assert captures[0]["camera_settings"]["effective_source"] == "external"
    assert light_calls == [True, False]


def test_timelapse_service_uses_per_printer_settings_when_present(tmp_path):
    cfg = FakeConfigManager(tmp_path, enabled=False)
    cfg._cfg.printers.append(SimpleNamespace(sn="SN2", name="Printer 2", model="V8111"))
    cfg._cfg.timelapse = {
        "enabled": False,
        "interval": 5,
        "max_videos": 2,
        "save_persistent": True,
        "light": None,
        "camera_source": "follow",
        "per_printer": {
            "SN2": {
                "enabled": True,
                "interval": 11,
                "max_videos": 4,
                "save_persistent": False,
                "light": "snapshot",
                "camera_source": "external",
            }
        },
    }

    svc0 = TimelapseService(cfg, captures_dir=tmp_path / "captures0", printer_index=0)
    svc1 = TimelapseService(cfg, captures_dir=tmp_path / "captures1", printer_index=1)

    assert svc0.enabled is False
    assert svc0._interval == 5
    assert svc0._max_videos == 2
    assert svc0._save_persistent is True
    assert svc0._light_mode is None
    assert svc0._camera_source == "follow"

    assert svc1.enabled is True
    assert svc1._interval == 11
    assert svc1._max_videos == 4
    assert svc1._save_persistent is False
    assert svc1._light_mode == "snapshot"
    assert svc1._camera_source == "external"


def test_timelapse_reload_config_applies_saved_output_dir(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    initial_dir = tmp_path / "captures-initial"
    updated_dir = tmp_path / "captures-updated"
    cfg._cfg.timelapse["output_dir"] = str(initial_dir)

    svc = TimelapseService(cfg)

    assert svc._captures_dir == str(initial_dir)
    assert initial_dir.is_dir()

    cfg._cfg.timelapse["output_dir"] = str(updated_dir)
    svc.reload_config()

    assert svc._captures_dir == str(updated_dir)
    assert updated_dir.is_dir()

    (updated_dir / "updated.mp4").write_bytes(b"video")
    assert svc.get_video_path("updated.mp4") == str(updated_dir / "updated.mp4")


def test_timelapse_explicit_captures_dir_overrides_saved_output_dir(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    explicit_dir = tmp_path / "explicit"
    configured_dir = tmp_path / "configured"
    cfg._cfg.timelapse["output_dir"] = str(configured_dir)

    svc = TimelapseService(cfg, captures_dir=explicit_dir)

    assert svc._captures_dir == str(explicit_dir)
    assert explicit_dir.is_dir()
    assert not configured_dir.exists()


def test_timelapse_runtime_state_reports_recovery(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    svc._set_recovery_state(True, "timelapse has no recent video frame")
    state = svc.get_runtime_state()

    assert state["recovering"] is True
    assert state["recovery_reason"] == "timelapse has no recent video frame"
    assert state["detail"] == "Recovering video stream..."

    svc._set_recovery_state(False)
    cleared = svc.get_runtime_state()
    assert cleared["recovering"] is False
    assert cleared["detail"] is None


def test_start_capture_finalizes_stale_current_dir_for_different_filename(monkeypatch, tmp_path):
    """A capture left open for job_a (finish/fail never called, simulating a
    missed MQTT event across a reconnect) must be archived, not silently
    overwritten, when start_capture() is called for a genuinely new job_b."""
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    stale_dir = str(tmp_path / _IN_PROGRESS_SUBDIR / "stale_job_a")
    os.makedirs(stale_dir, exist_ok=True)
    svc._current_dir = stale_dir
    svc._current_filename = "job_a.gcode"
    svc._frame_count = 1

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            pass

        def start(self):
            pass

    finalized = []
    monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        TimelapseService,
        "_resolve_capture_camera",
        lambda self: {"effective_source": web.camera.CAMERA_SOURCE_PRINTER},
    )
    monkeypatch.setattr(TimelapseService, "_enable_video_for_timelapse", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: None)
    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)
    monkeypatch.setattr(
        svc, "_finalize_now",
        lambda d, f, c, **kw: finalized.append((d, f, c, kw.get("suffix"))),
    )

    svc.start_capture("job_b.gcode")

    assert finalized == [(stale_dir, "job_a.gcode", 1, "_recovered")]
    assert svc._current_filename == "job_b.gcode"
    assert svc._current_dir != stale_dir
    assert svc._frame_count == 0


def test_start_capture_discards_stale_current_dir_without_frames(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    stale_dir = str(tmp_path / _IN_PROGRESS_SUBDIR / "stale_empty")
    os.makedirs(stale_dir, exist_ok=True)
    svc._current_dir = stale_dir
    svc._current_filename = "job_a.gcode"
    svc._frame_count = 0

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            pass

        def start(self):
            pass

    finalized = []
    monkeypatch.setattr("web.service.timelapse._resolve_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        TimelapseService,
        "_resolve_capture_camera",
        lambda self: {"effective_source": web.camera.CAMERA_SOURCE_PRINTER},
    )
    monkeypatch.setattr(TimelapseService, "_enable_video_for_timelapse", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: None)
    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)
    monkeypatch.setattr(
        svc, "_finalize_now",
        lambda d, f, c, **kw: finalized.append((d, f, c, kw.get("suffix"))),
    )

    svc.start_capture("job_b.gcode")

    assert finalized == []
    assert not os.path.isdir(stale_dir)
    assert svc._current_filename == "job_b.gcode"


def test_handle_mqtt_service_stopped_restores_light_when_capture_active(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._current_dir = str(tmp_path / _IN_PROGRESS_SUBDIR / "active")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 3
    svc._light_mode = "session"
    svc._light_was_on = False

    light_calls = []
    monkeypatch.setattr(web, "get_video_service", lambda idx: None)
    monkeypatch.setattr(web, "set_printer_light_state", lambda state, idx: light_calls.append((state, idx)))

    svc.handle_mqtt_service_stopped()

    assert light_calls == [(False, 0)]
    assert svc._light_was_on is None


def test_handle_mqtt_service_stopped_noop_without_active_capture(monkeypatch, tmp_path):
    """Regression guard: with no capture active, the light must NOT be touched —
    _disable_video_for_timelapse() would force it off (default restore=False)."""
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    assert svc._current_dir is None
    svc._light_mode = "session"

    light_calls = []
    monkeypatch.setattr(web, "get_video_service", lambda idx: None)
    monkeypatch.setattr(web, "set_printer_light_state", lambda state, idx: light_calls.append((state, idx)))

    svc.handle_mqtt_service_stopped()

    assert light_calls == []


def test_prune_passes_hold_dedicated_prune_lock(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    locked_during = []
    real_listdir = os.listdir

    def spy_listdir(path):
        locked_during.append(svc._prune_lock.locked())
        return real_listdir(path)

    monkeypatch.setattr("web.service.timelapse.os.listdir", spy_listdir)

    svc._prune_old_videos()
    svc._prune_old_snapshot_collections()

    assert locked_during
    assert all(locked_during)
