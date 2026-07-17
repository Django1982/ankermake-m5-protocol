from libflagship.notifications.apprise_client import (
    AppriseClient,
    _attachment_name_from_url,
    _normalize_attachments,
)


def test_normalize_attachments_filters_empty_values():
    assert _normalize_attachments(None) is None
    assert _normalize_attachments(["one.png", "", None, "two.png"]) == ["one.png", "two.png"]
    assert _normalize_attachments("single.png") == ["single.png"]


def test_attachment_name_from_url_uses_path_basename():
    assert _attachment_name_from_url("https://example.test/files/frame.jpg?sig=1") == "frame.jpg"
    assert _attachment_name_from_url("https://example.test") == "attachment"


def test_apprise_client_applies_environment_overrides():
    client = AppriseClient(
        {
            "enabled": False,
            "server_url": "https://config.example/",
            "key": "config-key",
            "events": {"print_started": False},
            "templates": {"print_started": "Started {filename}"},
        },
        env={
            "APPRISE_ENABLED": "true",
            "APPRISE_SERVER_URL": "https://env.example/",
            "APPRISE_KEY": "env-key",
            "APPRISE_EVENT_PRINT_STARTED": "1",
            "APPRISE_PROGRESS_MAX": "90",
        },
    )

    assert client.is_enabled() is True
    assert client.is_event_enabled("print_started") is True
    assert client.settings["progress"]["max_value"] == 90
    assert client._notify_url() == "https://env.example/notify/env-key"


def test_apprise_client_render_template_keeps_missing_placeholders():
    client = AppriseClient(
        {
            "enabled": True,
            "server_url": "https://notify.example",
            "key": "secret",
            "events": {"print_started": True},
            "templates": {"print_started": "Started {filename} for {owner}"},
        },
        env={},
    )

    assert client.render_template("print_started", {"filename": "cube.gcode"}) == "Started cube.gcode for {owner}"


def test_apprise_client_send_short_circuits_when_disabled():
    client = AppriseClient({}, env={})

    ok, message = client.send("print_started", payload={"filename": "cube.gcode"})

    assert ok is False
    assert "disabled" in message.lower()


def test_capture_live_snapshot_survives_light_restore_failure(monkeypatch, tmp_path):
    import os
    import time

    import web as web_module
    import web.camera as web_camera
    from web.lib.service import RunState
    from web.notifications import AppriseNotifier

    class FakeVideoQueue:
        def __init__(self):
            self.saved_light_state = None
            self.video_enabled = True
            self.wanted = True
            self.state = RunState.Running
            self.last_frame_at = time.monotonic()
            self.light_calls = []

        def api_light_state(self, state):
            self.light_calls.append(state)
            if len(self.light_calls) > 1:
                raise ConnectionError("pppp session dropped")

    vq = FakeVideoQueue()

    def fake_capture(camera_settings, ffmpeg_path, out_path, **kwargs):
        with open(out_path, "wb") as fh:
            fh.write(b"jpeg-data")

    monkeypatch.setattr(
        web_module,
        "_resolve_camera_settings",
        lambda printer_index=None: {"effective_source": web_camera.CAMERA_SOURCE_PRINTER},
    )
    monkeypatch.setattr(web_module, "_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(web_module, "get_video_service", lambda printer_index=None: vq)
    monkeypatch.setattr(web_module, "_local_web_host_port", lambda: ("127.0.0.1", 4470))
    monkeypatch.setattr(web_camera, "capture_camera_snapshot_to_file", fake_capture)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    notifier = AppriseNotifier(None, settings={"progress": {"snapshot_light": True}})
    result = notifier._capture_live_snapshot()

    try:
        assert result is not None
        assert os.path.getsize(result) > 0
    finally:
        if result:
            os.remove(result)
    assert vq.light_calls == [True, False]
