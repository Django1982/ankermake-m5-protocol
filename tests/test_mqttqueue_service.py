import time
from types import SimpleNamespace

from web.service.mqtt import MqttQueue


def _queue():
    queue = object.__new__(MqttQueue)
    queue._ha = SimpleNamespace(enabled=True, update_state=lambda **kwargs: ha_updates.append(kwargs))
    queue._notifier = SimpleNamespace(
        is_event_enabled=lambda event: True,
        progress_interval=lambda default=25: 25,
        progress_max=lambda: None,
    )
    queue._history = SimpleNamespace(
        record_start=lambda *args, **kwargs: history_calls.append(("start", args, kwargs)),
        record_finish=lambda *args, **kwargs: history_calls.append(("finish", args, kwargs)),
        record_fail=lambda *args, **kwargs: history_calls.append(("fail", args, kwargs)),
    )
    queue._timelapse = SimpleNamespace(
        start_capture=lambda filename="unknown": timelapse_calls.append(("start", filename)),
        finish_capture=lambda final=False: timelapse_calls.append(("finish", final)),
        fail_capture=lambda: timelapse_calls.append(("fail",)),
        enabled=True,
        _capture_thread=None,
    )
    queue._send_event = lambda event, payload, include_image=False: events.append((event, payload, include_image))
    queue._z_offset_steps = None
    queue._z_offset_updated_at = 0.0
    queue._z_offset_seq = 0
    queue._z_offset_cond = __import__("threading").Condition()
    queue._gcode_layer_count = None
    queue._last_message_time = 0.0
    queue._nozzle_temp = None
    queue._nozzle_temp_target = None
    queue._bed_temp = None
    queue._bed_temp_target = None
    queue._control_username = "tester@example.com"
    queue._debug_log_payloads = False
    queue._reset_print_state()
    return queue


def test_forward_to_ha_updates_temperatures_and_progress():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._forward_to_ha({"commandType": 1003, "currentTemp": 21500, "targetTemp": 22000})
    queue._forward_to_ha({"commandType": 1004, "currentTemp": 6500, "targetTemp": 7000})
    queue._print_active = True
    queue._forward_to_ha({
        "commandType": 1001,
        "progress": 50,
        "name": "cube.gcode",
        "totalTime": 120,
        "time": 60,
    })

    assert {"nozzle_temp": 215, "nozzle_temp_target": 220} in ha_updates
    assert {"bed_temp": 65, "bed_temp_target": 70} in ha_updates
    assert any(update.get("print_progress") == 50 and update.get("print_filename") == "cube.gcode" for update in ha_updates)


def test_forward_to_ha_keeps_local_temperature_state_when_ha_is_disabled():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._ha.enabled = False

    queue._forward_to_ha({"commandType": 1003, "currentTemp": 21500, "targetTemp": 22000})
    queue._forward_to_ha({"commandType": 1004, "currentTemp": 6500, "targetTemp": 7000})

    assert queue.nozzle_temp == 215
    assert queue.nozzle_temp_target == 220
    assert queue._bed_temp == 65
    assert queue._bed_temp_target == 70
    assert ha_updates == []


def test_emit_progress_respects_bucket_interval():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._print_active = True

    payload = {"name": "cube.gcode"}
    queue._emit_progress(payload, 10)
    queue._emit_progress(payload, 20)
    queue._emit_progress(payload, 26)
    queue._emit_progress(payload, 49)
    queue._emit_progress(payload, 50)

    assert [event[0] for event in events] == ["print_progress", "print_progress"]
    assert events[0][1]["percent"] == 26
    assert events[1][1]["percent"] == 50


def test_handle_notification_start_finish_and_failure_paths(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 0})
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/fail.gcode"})
    queue._handle_notification({
        "commandType": 1001,
        "progress": 25,
        "name": "fail.gcode",
        "errorMessage": "jam",
    })
    queue._handle_notification({
        "commandType": 1001,
        "progress": 26,
        "name": "fail.gcode",
        "errorMessage": "jam",
    })

    assert history_calls[0][0] == "start"
    assert history_calls[1][0] == "finish"
    assert [call[0] for call in history_calls[:4]] == ["start", "finish", "start", "fail"]
    assert timelapse_calls[0] == ("start", "cube.gcode")
    assert timelapse_calls[1] == ("finish", True)
    assert timelapse_calls[2] == ("start", "fail.gcode")
    assert timelapse_calls[3] == ("fail",)
    assert [event[0] for event in events[:5]] == [
        "print_started",
        "print_finished",
        "print_started",
        "print_progress",
        "print_failed",
    ]


def test_handle_notification_aborts_active_print_on_value_8(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/active.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 8})

    assert history_calls == [
        ("start", ("active.gcode",), {"task_id": None}),
        ("fail", (), {"filename": "active.gcode", "reason": "aborted", "task_id": None}),
    ]
    assert timelapse_calls == [("start", "active.gcode"), ("fail",)]
    assert events[-1][0] == "print_failed"
    assert events[-1][2] is False
    assert events[-1][1]["filename"] == "active.gcode"
    assert events[-1][1]["reason"] == "aborted"
    assert queue.get_state()["print"]["state"] == 0


def test_send_print_control_maps_stop_to_restart_during_prepare_state():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    queue._handle_notification({"commandType": 1000, "value": 8})

    queue.send_print_control(4)

    assert sent == [
        {"commandType": 1008, "data": {"value": 0, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 0},
    ]
    assert queue._stop_requested is True


def test_1044_captures_filename_without_marking_prepare_state():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/prepare.gcode"})

    state = queue.get_state()["print"]
    assert state["preparing"] is False
    assert state["active"] is False
    assert state["last_filename"] == "prepare.gcode"


def test_value_8_marks_prepare_state_before_print_start():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1000, "value": 8})

    state = queue.get_state()["print"]
    assert state["preparing"] is True
    assert state["active"] is False


def test_upload_only_idle_transition_does_not_create_fake_print_history():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/upload-only.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert history_calls == []
    assert timelapse_calls == []
    assert events == []
    assert queue.get_state()["print"]["preparing"] is False


def test_prepare_state_cancels_on_value_zero_after_stop():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({
        "commandType": 1001,
        "progress": 0,
        "name": "warmup.gcode",
        "task_id": "task-1",
    })
    queue._handle_notification({"commandType": 1000, "value": 8})
    queue._stop_requested = True
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert history_calls == [("fail", (), {"filename": "warmup.gcode", "reason": "cancelled", "task_id": "task-1"})]
    assert timelapse_calls == [("fail",)]
    assert events == [("print_failed", {"filename": "warmup.gcode", "percent": 0, "elapsed_seconds": "", "remaining_seconds": "", "duration_seconds": "", "elapsed": "", "remaining": "", "duration": "", "reason": "cancelled"}, False)]
    assert queue.get_state()["print"]["preparing"] is False


def test_pending_start_stop_cancels_before_print_becomes_active():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))

    queue.mark_pending_print_start("queued.gcode")

    assert queue.get_state()["print"]["pending_start"] is True

    queue.send_print_control(4)
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert sent == [{"commandType": 1008, "value": 4}]
    assert history_calls == [("fail", (), {"filename": "queued.gcode", "reason": "cancelled", "task_id": None})]
    assert timelapse_calls == [("fail",)]
    assert events == [("print_failed", {"filename": "queued.gcode", "percent": 0, "elapsed_seconds": "", "remaining_seconds": "", "duration_seconds": "", "elapsed": "", "remaining": "", "duration": "", "reason": "cancelled"}, False)]
    assert queue.get_state()["print"]["pending_start"] is False
    assert queue._stop_requested is False


def test_build_payload_get_state_and_simulate_event(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 150.0)
    queue._print_active = True
    queue._print_started_at = 100.0
    queue._last_filename = "cube.gcode"

    built = queue._build_payload({"elapsed": 20, "remaining": 30}, 40)
    state_before = queue.get_state()
    queue.set_debug_logging(True)
    queue.simulate_event("start", {"filename": "simulated.gcode"})
    queue.simulate_event("finish", {"filename": "simulated.gcode"})
    queue.simulate_event("fail", {"filename": "simulated.gcode"})

    assert built["filename"] == "cube.gcode"
    assert built["duration_seconds"] == 50
    assert state_before["print"]["active"] is True
    assert queue.get_state()["debug_logging"] is True
    assert [call[0] for call in history_calls[-3:]] == ["start", "finish", "fail"]


def test_out_of_order_ct1001_before_ct1000():
    """ct=1001 (progress) arriving before ct=1000 (state=1) must not
    produce duplicate history records or duplicate PRINT_STARTED events.
    The consolidated _transition_to_active() guards against this."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Progress arrives first (out of order) — should activate print
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,       # 25% on the 0-10000 scale
        "name": "cube.gcode",
    })

    assert queue._print_active is True
    start_events = [e for e in events if e[0] == "print_started"]
    start_records = [c for c in history_calls if c[0] == "start"]
    assert len(start_events) == 1, f"Expected 1 start event, got {len(start_events)}"
    assert len(start_records) == 1, f"Expected 1 history start, got {len(start_records)}"

    # State change arrives later — should be a no-op (already active)
    events.clear()
    history_calls.clear()
    queue._handle_notification({"commandType": 1000, "value": 1})

    late_start_events = [e for e in events if e[0] == "print_started"]
    late_start_records = [c for c in history_calls if c[0] == "start"]
    assert len(late_start_events) == 0, "ct=1000 should not fire a second start event"
    assert len(late_start_records) == 0, "ct=1000 should not record a second history start"
