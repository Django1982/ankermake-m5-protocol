import logging
import queue as queue_module
import threading
import time
from types import SimpleNamespace

from web.service.mqtt import MqttQueue, PrintState, EVENT_PRINT_FINISHED


def _queue():
    queue = object.__new__(MqttQueue)
    queue.printer_index = 0
    queue._printer_name = "Thing 1"
    queue._printer_sn = "TEST-SN"
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
        update_preview_url=lambda *args, **kwargs: history_calls.append(("preview", args, kwargs)),
    )
    queue._timelapse = SimpleNamespace(
        start_capture=lambda filename="unknown": timelapse_calls.append(("start", filename)),
        finish_capture=lambda final=False: timelapse_calls.append(("finish", final)),
        fail_capture=lambda: timelapse_calls.append(("fail",)),
        set_capture_paused=lambda paused, reason=None: None,
        enabled=True,
        _capture_thread=None,
    )
    queue._send_event = lambda event, payload, include_image=False: events.append((event, payload, include_image))
    queue._z_offset_steps = None
    queue._z_offset_updated_at = 0.0
    queue._z_offset_seq = 0
    queue._z_offset_cond = __import__("threading").Condition()
    queue._state_lock = __import__("threading").RLock()
    queue._gcode_layer_count = None
    queue._last_message_time = 0.0
    queue._nozzle_temp = None
    queue._nozzle_temp_target = None
    queue._bed_temp = None
    queue._bed_temp_target = None
    queue._filament_state = "unknown"
    queue._filament_change_value = None
    queue._filament_change_progress = None
    queue._filament_change_step_len = None
    queue._control_username = "tester@example.com"
    queue._control_user_id = "user-123"
    queue._debug_log_payloads = False
    queue._record_printer_alert = lambda **kwargs: None
    queue._reset_print_state()
    return queue


def test_forward_to_ha_updates_temperatures_and_progress():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._forward_to_ha({"commandType": 1003, "currentTemp": 21500, "targetTemp": 22000})
    queue._forward_to_ha({"commandType": 1004, "currentTemp": 6500, "targetTemp": 7000})
    queue._state = PrintState.PRINTING
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


def test_forward_to_ha_accepts_zero_temperature_targets():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._forward_to_ha({"commandType": 1003, "currentTemp": 21500, "targetTemp": 22000})
    queue._forward_to_ha({"commandType": 1004, "currentTemp": 6500, "targetTemp": 7000})
    queue._forward_to_ha({"commandType": 1003, "currentTemp": 20500, "targetTemp": 0})
    queue._forward_to_ha({"commandType": 1004, "currentTemp": 6000, "targetTemp": 0})

    assert queue.nozzle_temp == 205
    assert queue.nozzle_temp_target == 0
    assert queue._bed_temp == 60
    assert queue._bed_temp_target == 0
    assert {"nozzle_temp": 205, "nozzle_temp_target": 0} in ha_updates
    assert {"bed_temp": 60, "bed_temp_target": 0} in ha_updates


def test_forward_to_ha_tracks_filament_state_from_material_change_mode():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # _update_filament_state is called only from _handle_notification (under
    # _state_lock), not from _forward_to_ha, so the state update is visible
    # only after _handle_notification runs.
    with queue._state_lock:
        queue._handle_notification({"commandType": 1023, "value": 0, "progress": 0, "stepLen": 0})
    state = queue.get_state()["filament"]

    assert state["state"] == "loaded"
    assert state["label"] == "Loaded"
    assert state["loaded"] is True
    assert state["raw_value"] == 0
    assert state["progress"] == 0
    assert state["step_len"] == 0


def test_handle_notification_tracks_filament_changing_state():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1023, "value": 1, "progress": 25, "stepLen": 40})
    state = queue.get_state()["filament"]

    assert state["state"] == "changing"
    assert state["label"] == "Changing"
    assert state["loaded"] is None
    assert state["raw_value"] == 1
    assert state["progress"] == 25
    assert state["step_len"] == 40


def test_handle_notification_does_not_confirm_filament_runout_on_alarm_alone():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({
        "commandType": 1085,
        "errorCode": "0xFF01030001",
        "errorLevel": "P1",
    })
    state = queue.get_state()["filament"]

    assert state["state"] == "unknown"
    assert state["label"] == "Unknown"
    assert state["loaded"] is None
    assert state["issue"] is None
    assert state["issue_label"] is None
    assert state["detail"] is None


def test_handle_notification_exposes_filament_pause_reason_when_print_pauses():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PRINTING

    queue._handle_notification({
        "commandType": 1085,
        "errorCode": "0xFF01030001",
        "errorLevel": "P1",
    })
    queue._handle_notification({"commandType": 1000, "value": 2})

    state = queue.get_state()
    assert state["print"]["pause_reason"] == "filament_runout"
    assert state["print"]["pause_reason_label"] == "Filament runout"
    assert state["filament"]["pause_reason"] == "filament_runout"
    assert state["filament"]["pause_reason_label"] == "Filament runout"
    assert state["filament"]["detail"] == "Paused: Filament runout. Reload filament to continue."


def test_filament_runout_pause_keeps_not_loaded_until_resume():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PRINTING

    queue._handle_notification({
        "commandType": 1085,
        "errorCode": "0xFF01030001",
        "errorLevel": "P1",
    })
    queue._handle_notification({"commandType": 1000, "value": 2})
    queue._handle_notification({"commandType": 1023, "value": 0, "progress": 100, "stepLen": 20})

    paused_state = queue.get_state()["filament"]
    assert paused_state["state"] == "not_loaded"
    assert paused_state["issue"] == "runout"
    assert paused_state["detail"] == "Paused: Filament runout. Reload filament to continue."

    queue._handle_notification({"commandType": 1000, "value": 3})

    resumed_state = queue.get_state()["filament"]
    assert resumed_state["state"] == "loaded"
    assert resumed_state["issue"] is None
    assert resumed_state["pause_reason"] is None


def test_filament_change_and_runout_pause_timelapse_snapshots():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PRINTING
    pause_calls = []
    queue._timelapse.set_capture_paused = lambda paused, reason=None: pause_calls.append((paused, reason))

    queue._handle_notification({"commandType": 1023, "value": 2, "progress": 25, "stepLen": 20})
    queue._handle_notification({
        "commandType": 1085,
        "errorCode": "0xFF01030001",
        "errorLevel": "P1",
    })
    queue._handle_notification({"commandType": 1000, "value": 2})
    queue._handle_notification({"commandType": 1000, "value": 3})

    assert pause_calls[0] == (True, "filament_change")
    assert (True, "filament_runout") in pause_calls
    assert pause_calls[-1] == (False, None)


def test_handle_notification_records_cross_printer_runout_alert(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue.printer_index = 1
    queue._printer_name = "Thing 2"
    alerts = []
    del queue._record_printer_alert
    monkeypatch.setattr(
        "web.service.mqtt.app.record_printer_alert",
        lambda **kwargs: alerts.append(kwargs),
        raising=False,
    )

    queue._handle_notification({
        "commandType": 1085,
        "errorCode": "0xFF01030001",
        "errorLevel": "P1",
    })
    queue._handle_notification({
        "commandType": 1000,
        "subType": 2,
        "value": 6,
    })

    assert len(alerts) == 1
    assert alerts[0]["printer_index"] == 1
    assert alerts[0]["printer_name"] == "Thing 2"
    assert alerts[0]["alert_type"] == "filament_runout"


def test_handle_notification_records_cross_printer_print_complete_alert(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue.printer_index = 1
    queue._printer_name = "Thing 2"
    alerts = []
    del queue._record_printer_alert
    monkeypatch.setattr(
        "web.service.mqtt.app.record_printer_alert",
        lambda **kwargs: alerts.append(kwargs),
        raising=False,
    )

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/completed.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert len(alerts) == 1
    assert alerts[0]["printer_index"] == 1
    assert alerts[0]["printer_name"] == "Thing 2"
    assert alerts[0]["alert_type"] == "print_complete"
    assert alerts[0]["title"] == "Print complete"
    assert alerts[0]["message"] == "completed.gcode finished printing."
    assert alerts[0]["level"] == "success"


def test_cancelled_print_does_not_record_print_complete_alert():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    alerts = []
    queue._record_printer_alert = lambda **kwargs: alerts.append(kwargs)

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cancelled.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._stop_requested = True
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert alerts == []


def test_filament_runout_alarm_clears_if_printer_sends_clear_without_pause():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({
        "commandType": 1085,
        "errorCode": "0xFF01030001",
        "errorLevel": "P1",
    })
    queue._handle_notification({
        "commandType": 1086,
        "errorCode": "0xFF01030001",
        "errorLevel": "P1",
    })
    queue._handle_notification({"commandType": 1000, "value": 2})

    state = queue.get_state()
    assert state["filament"]["issue"] is None
    assert state["print"]["pause_reason"] is None


def test_get_state_reports_timelapse_not_capturing_for_dead_thread():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    class DeadThread:
        def is_alive(self):
            return False

    queue._timelapse._capture_thread = DeadThread()

    assert queue.get_state()["timelapse"]["capturing"] is False


def test_event_notify_filament_break_sets_not_loaded_until_change_cycle_completes():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({
        "commandType": 1000,
        "subType": 2,
        "value": 6,
    })
    assert queue.get_state()["filament"]["state"] == "not_loaded"

    queue._handle_notification({"commandType": 1023, "value": 2, "progress": 50, "stepLen": 20})
    assert queue.get_state()["filament"]["state"] == "changing"

    queue._handle_notification({"commandType": 1023, "value": 0, "progress": 100, "stepLen": 20})
    state = queue.get_state()["filament"]
    assert state["state"] == "loaded"
    assert state["label"] == "Loaded"
    assert state["issue"] is None


def test_emit_progress_respects_bucket_interval():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PRINTING

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


def test_deferred_filename_starts_timelapse_after_print_state(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    assert queue._pending_history_start is True
    assert history_calls == []
    assert timelapse_calls == []

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/deferred.gcode"})

    assert queue._pending_history_start is False
    assert history_calls == [("start", ("deferred.gcode",), {"task_id": None})]
    assert timelapse_calls == [("start", "deferred.gcode")]


def test_handle_notification_print_start_does_not_crash_when_timelapse_unavailable(monkeypatch):
    """If TimelapseService failed to initialize, _timelapse is None.
    A direct print start (filename already known) must not crash the
    MQTT worker on the missing start_capture() call."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._timelapse = None
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    assert history_calls == [("start", ("cube.gcode",), {"task_id": None})]


def test_deferred_filename_print_start_does_not_crash_when_timelapse_unavailable(monkeypatch):
    """Same as above, but for the deferred-filename path
    (_complete_deferred_print_start), which also called
    self._timelapse.start_capture() unguarded."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._timelapse = None
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    assert queue._pending_history_start is True

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/deferred.gcode"})

    assert queue._pending_history_start is False
    assert history_calls == [("start", ("deferred.gcode",), {"task_id": None})]


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


def test_send_print_control_shotgun_during_prepare_state():
    # During the prepare phase (ct=1000 value=8, not yet active), stop sends both
    # value=0 and value=4 in nested+flat form for firmware compatibility.
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
        {"commandType": 1008, "data": {"value": 4, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 4},
    ]
    assert queue._stop_requested is True


def test_send_pause_resume_print_control_uses_nested_and_flat_payloads():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    queue._state = PrintState.PRINTING

    queue.send_print_control(2)
    queue._state = PrintState.PAUSED
    queue.send_print_control(3)

    assert sent == [
        {"commandType": 1008, "data": {"value": 2, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 2},
        {"commandType": 1008, "data": {"value": 3, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 3},
    ]
    assert queue._stop_requested is False


def test_send_stop_print_control_uses_nested_and_flat_payloads_for_active_print():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    queue._state = PrintState.PRINTING

    queue.send_print_control(4)

    assert sent == [
        {"commandType": 1008, "data": {"value": 4, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 4},
    ]
    assert queue._stop_requested is True


def test_send_gcode_dedupes_identical_g28_commands(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    now = [100.0]
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: now[0])
    monkeypatch.setattr("web.service.mqtt.time.sleep", lambda seconds: None)

    queue.send_gcode("G28")
    queue.send_gcode("G28")
    queue.send_gcode("G28 Z")
    now[0] += 10.1
    queue.send_gcode("G28")

    assert [cmd["cmdData"] for cmd in sent] == ["G28", "G28 Z", "G28"]


def test_send_home_uses_native_move_zero_payloads():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))

    queue.send_home("xy")
    queue.send_home("z")
    queue.send_home("all")

    assert sent == [
        {"commandType": 1026, "value": 0},
        {"commandType": 1026, "value": 2},
        {"commandType": 1026, "value": 2},
    ]


def test_start_stored_file_selects_filepath_then_starts_print(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    monkeypatch.setattr(queue, "_await_stored_file_selection", lambda file_path, timeout_sec=2.0: True)
    timeouts = []

    def confirm(file_path, timeout_sec=12.0):
        timeouts.append(timeout_sec)
        return True

    monkeypatch.setattr(queue, "_await_stored_file_start_confirmation", confirm)

    started = queue.start_stored_file("/tmp/udisk/udisk1/Top parts 5h_PETG_M5 grey.gcode")

    assert sent == [
        {
            "commandType": 1009,
            "value": 0,
            "isFirst": 1,
            "index": 1,
            "num": 47,
            "userId": "user-123",
        },
        {
            "commandType": 1010,
            "filePath": "/tmp/udisk/udisk1/Top parts 5h_PETG_M5 grey.gcode",
            "type": 0,
            "userId": "user-123",
        },
        {
            "commandType": 1008,
            "value": 1,
            "printMode": 1,
            "userName": "tester",
            "filePath": "/tmp/udisk/udisk1/Top parts 5h_PETG_M5 grey.gcode",
            "userId": "user-123",
        },
    ]
    assert started is True
    assert timeouts == [12.0]
    assert queue.get_state()["print"]["pending_start"] is True
    assert queue.get_state()["print"]["last_filename"] == "Top parts 5h_PETG_M5 grey.gcode"


def test_start_stored_file_uses_longer_confirmation_timeout_for_onboard_storage(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    monkeypatch.setattr(queue, "_await_stored_file_selection", lambda file_path, timeout_sec=2.0: True)
    timeouts = []

    def confirm(file_path, timeout_sec=12.0):
        timeouts.append(timeout_sec)
        return True

    monkeypatch.setattr(queue, "_await_stored_file_start_confirmation", confirm)

    started = queue.start_stored_file(
        "/usr/data/local/model/AnkerMake Model/Autodesk_Kickstarter_Geometry.gcode"
    )

    assert started is True
    assert timeouts == [20.0]
    assert sent[0] == {
        "commandType": 1009,
        "value": 1,
        "isFirst": 1,
        "index": 1,
        "num": 47,
        "userId": "user-123",
    }


def test_stored_file_selection_waits_for_exact_filepath_preview():
    queue = _queue()
    queue._ensure_stored_file_selection_state()
    target_path = "/usr/data/local/model/AnkerMake Model/Autodesk_Kickstarter_Geometry.gcode"
    queue._pending_stored_file_path = target_path
    queue._note_stored_file_selection(file_name="Autodesk_Kickstarter_Geometry.gcode")

    assert queue._await_stored_file_selection(target_path, timeout_sec=0.0) is False

    queue._note_stored_file_selection(file_path=target_path, file_name="Autodesk_Kickstarter_Geometry.gcode")

    assert queue._await_stored_file_selection(target_path, timeout_sec=0.0) is True


def test_uploaded_archive_is_attached_to_next_history_start():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue.mark_pending_print_start(
        "cube.gcode",
        archive_info={"archive_relpath": "saved/cube.gcode", "archive_size": 1234},
    )
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})

    start_records = [call for call in history_calls if call[0] == "start"]
    assert len(start_records) == 1
    assert start_records[0][2] == {
        "task_id": None,
        "archive_relpath": "saved/cube.gcode",
        "archive_size": 1234,
    }


def test_uploaded_archive_matches_normalized_filename_variants():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue.mark_pending_print_start(
        "Cube_File.gcode",
        archive_info={"archive_relpath": "saved/Cube_File.gcode", "archive_size": 1234},
    )
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/Cube File.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})

    start_records = [call for call in history_calls if call[0] == "start"]
    assert len(start_records) == 1
    assert start_records[0][1] == ("Cube File.gcode",)
    assert start_records[0][2] == {
        "task_id": None,
        "archive_relpath": "saved/Cube_File.gcode",
        "archive_size": 1234,
    }


def test_derive_control_display_name_uses_email_local_part():
    assert MqttQueue._derive_control_display_name("tester@example.com") == "tester"
    assert MqttQueue._derive_control_display_name("plain-user") == "plain-user"
    assert MqttQueue._derive_control_display_name("") is None


def test_1044_captures_filename_without_marking_prepare_state():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/prepare.gcode"})

    state = queue.get_state()["print"]
    assert state["preparing"] is False
    assert state["active"] is False
    assert state["last_filename"] == "prepare.gcode"


def test_stored_file_source_preview_does_not_activate_pre_print_window():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue.mark_pending_print_start("Top parts 5h_PETG_M5 grey.gcode")
    queue._handle_notification({
        "commandType": 1044,
        "filePath": "/tmp/udisk/udisk1/Top parts 5h_PETG_M5 grey.gcode",
    })

    state = queue.get_state()["print"]
    assert state["pending_start"] is True
    assert state["in_pre_print_window"] is False
    assert state["active"] is False


def test_1044_preview_url_is_cached_for_stored_file_paths():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({
        "commandType": 1044,
        "filePath": "/tmp/udisk/udisk1/preview-me.gcode",
        "url": "https://example.test/preview.png",
    })

    assert queue.get_cached_stored_file_preview_url("/tmp/udisk/udisk1/preview-me.gcode") == "https://example.test/preview.png"
    assert ("preview", ("https://example.test/preview.png",), {"filename": "preview-me.gcode", "task_id": None}) in history_calls


def test_get_stored_file_preview_url_requests_selection_and_returns_cached_preview(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._ensure_stored_file_selection_state()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    monkeypatch.setattr("web.service.mqtt.time.sleep", lambda seconds: None)

    def fake_wait_for(predicate, timeout=None):
        queue._cache_stored_file_preview("/tmp/udisk/udisk1/file.gcode", "https://example.test/thumb.png")
        return True

    monkeypatch.setattr(queue._stored_file_selection_cond, "wait_for", fake_wait_for)

    preview_url = queue.get_stored_file_preview_url("/tmp/udisk/udisk1/file.gcode")

    assert preview_url == "https://example.test/thumb.png"
    assert sent == [
        {
            "commandType": 1009,
            "value": 0,
            "isFirst": 1,
            "index": 1,
            "num": 47,
            "userId": "user-123",
        },
        {
            "commandType": 1010,
            "filePath": "/tmp/udisk/udisk1/file.gcode",
            "type": 0,
            "userId": "user-123",
        },
    ]


def test_tmpmodel_preview_activates_pre_print_window():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue.mark_pending_print_start("Top parts 5h_PETG_M5 grey.gcode")
    queue._handle_notification({
        "commandType": 1044,
        "filePath": "/usr/data/local/tmpmodel/Top parts 5h_PETG_M5 grey.gcode",
    })

    state = queue.get_state()["print"]
    assert state["pending_start"] is False
    assert state["in_pre_print_window"] is True
    assert state["active"] is True


def test_tmpmodel_preview_activates_pre_print_window_after_prepare_value_8():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue.mark_pending_print_start("Top parts 5h_PETG_M5 grey.gcode")
    queue._handle_notification({"commandType": 1000, "value": 8})
    queue._handle_notification({
        "commandType": 1044,
        "filePath": "/usr/data/local/tmpmodel/Top parts 5h_PETG_M5 grey.gcode",
    })

    state = queue.get_state()["print"]
    assert state["pending_start"] is False
    assert state["in_pre_print_window"] is True
    assert state["active"] is True


def test_print_schedule_can_confirm_onboard_stored_file_pre_print_window():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue.mark_pending_print_start("Autodesk_Kickstarter_Geometry.gcode")
    queue._pending_stored_file_path = "/usr/data/local/model/Autodesk_Kickstarter_Geometry.gcode"
    queue._handle_notification({
        "commandType": 1001,
        "progress": 0,
        "name": "Autodesk_Kickstarter_Geometry.gcode",
        "time": 120,
    })

    state = queue.get_state()["print"]
    assert state["pending_start"] is False
    assert state["in_pre_print_window"] is True
    assert state["last_filename"] == "Autodesk_Kickstarter_Geometry.gcode"


def test_value_8_marks_prepare_state_before_print_start():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1000, "value": 8})

    state = queue.get_state()["print"]
    assert state["preparing"] is True
    assert state["active"] is False


def test_value_8_preserves_pending_start_state():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue.mark_pending_print_start("queued.gcode")
    queue._handle_notification({"commandType": 1000, "value": 8})

    state = queue.get_state()["print"]
    assert state["pending_start"] is True
    assert state["preparing"] is True


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

    # pending_start counts as pre_start_window → shotgun (value=0 and value=4, nested+flat)
    assert sent == [
        {"commandType": 1008, "data": {"value": 0, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 0},
        {"commandType": 1008, "data": {"value": 4, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 4},
    ]
    assert history_calls == [("fail", (), {"filename": "queued.gcode", "reason": "cancelled", "task_id": None})]
    assert timelapse_calls == [("fail",)]
    assert events == [("print_failed", {"filename": "queued.gcode", "percent": 0, "elapsed_seconds": "", "remaining_seconds": "", "duration_seconds": "", "elapsed": "", "remaining": "", "duration": "", "reason": "cancelled"}, False)]
    assert queue.get_state()["print"]["pending_start"] is False
    assert queue._stop_requested is False


def test_early_stop_during_pre_print_window_sends_value4_and_cancels():
    """Stop during G28/calibration (ct=1044 received, ct=1000 value=1 not yet) must send
    value=4 (not value=0) and correctly record cancellation when the printer confirms with
    ct=1000 value=0."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))

    queue.mark_pending_print_start("cube.gcode")
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})

    # Printer is now in pre-print window: active=True, in_pre_print_window=True
    state = queue.get_state()["print"]
    assert state["active"] is True
    assert state["in_pre_print_window"] is True

    # G28 calibration phase arrives
    queue._handle_notification({"commandType": 1000, "value": 8})
    assert queue.get_state()["print"]["active"] is True  # still active, not aborted

    # User clicks Stop
    queue.send_print_control(4)

    # Must send flat value=4, not value=0
    assert sent[-1] == {"commandType": 1008, "value": 4}

    # Printer confirms cancel
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert history_calls == [("fail", (), {"filename": "cube.gcode", "reason": "cancelled", "task_id": None})]
    assert timelapse_calls == [("fail",)]
    assert queue._stop_requested is False
    assert queue.get_state()["print"]["active"] is False
    assert queue.get_state()["print"]["in_pre_print_window"] is False


def test_stop_confirmation_reply_cancels_active_print_without_value_zero():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})

    history_calls.clear()
    timelapse_calls.clear()
    events.clear()

    queue._stop_requested = True
    queue._handle_notification({"commandType": 1057, "reply": 0})

    assert queue._state == PrintState.IDLE
    assert history_calls == [("fail", (), {"filename": "cube.gcode", "reason": "cancelled", "task_id": None})]
    assert timelapse_calls == [("fail",)]
    assert len(events) == 1
    assert events[0][0] == "print_failed"
    assert events[0][2] is False
    assert events[0][1]["filename"] == "cube.gcode"
    assert events[0][1]["reason"] == "cancelled"
    assert queue.get_state()["timelapse"]["prompt_start"] is False


def test_startup_attached_print_prompts_before_starting_timelapse(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    now = [100.0]
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: now[0])
    queue = _queue()
    queue._timelapse_start_prompt_window_until = now[0] + 20.0

    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "startup.gcode",
    })

    assert queue._state == PrintState.PRINTING
    assert history_calls == [("start", ("startup.gcode",), {"task_id": None})]
    assert timelapse_calls == []
    state = queue.get_state()
    assert state["timelapse"]["prompt_start"] is True
    assert state["timelapse"]["prompt_filename"] == "startup.gcode"
    assert state["timelapse"]["detail"] == "Open Timelapse to continue or dismiss capture for this print."

    filename = queue.start_timelapse_for_current_print()

    assert filename == "startup.gcode"
    assert timelapse_calls == [("start", "startup.gcode")]
    assert queue.get_state()["timelapse"]["prompt_start"] is False


def test_active_print_after_startup_window_auto_starts_timelapse(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    now = [100.0]
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: now[0])
    queue = _queue()
    queue._timelapse_start_prompt_window_until = now[0] - 1.0

    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "auto-start.gcode",
    })

    assert queue._state == PrintState.PRINTING
    assert timelapse_calls == [("start", "auto-start.gcode")]
    assert queue.get_state()["timelapse"]["prompt_start"] is False


def test_dismiss_timelapse_offer_discards_pending_resume():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    discarded = []
    queue._timelapse = SimpleNamespace(
        enabled=True,
        _capture_thread=None,
        discard_pending_resume=lambda filename=None: discarded.append(filename) or True,
        get_runtime_state=lambda: {"enabled": True, "capturing": False},
    )
    queue._state = PrintState.PRINTING
    queue._last_filename = "active.gcode"
    queue._timelapse_start_prompt_pending = True
    queue._timelapse_start_prompt_filename = "active.gcode"

    queue.dismiss_timelapse_start_offer()

    assert discarded == ["active.gcode"]
    assert queue.get_state()["timelapse"]["prompt_start"] is False


def test_build_payload_get_state_and_simulate_event(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 150.0)
    queue._state = PrintState.PRINTING
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

    assert queue._state == PrintState.PRINTING
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


def test_pre_print_window_upgrades_to_full_print():
    """When ct=1044 activates the pre-print window (G28/calibration),
    ct=1000 value=1 should upgrade to full print activation with all
    side effects (history, timelapse, event)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Simulate the pre-print window state set by ct=1044
    queue._state = PrintState.PRE_PRINT
    queue._last_filename = "cube.gcode"

    # ct=1000 value=1 should upgrade from pre-print to full print
    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    start_events = [e for e in events if e[0] == "print_started"]
    start_records = [c for c in history_calls if c[0] == "start"]
    assert len(start_events) == 1, "Pre-print upgrade should fire start event"
    assert len(start_records) == 1, "Pre-print upgrade should record history start"
    assert len(timelapse_calls) > 0, "Pre-print upgrade should start timelapse"


def test_stale_post_completion_updates_for_same_task_are_ignored(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    now = [100.0]
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: now[0])

    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
        "task_id": "task-dup",
    })
    queue._handle_notification({"commandType": 1000, "value": 0})

    history_calls.clear()
    timelapse_calls.clear()
    events.clear()

    now[0] += 5.0
    queue._handle_notification({
        "commandType": 1001,
        "progress": 9900,
        "name": "cube.gcode",
        "task_id": "task-dup",
    })
    queue._handle_notification({
        "commandType": 1001,
        "progress": 10000,
        "name": "cube.gcode",
        "task_id": "task-dup",
    })

    assert queue._state == PrintState.IDLE
    assert history_calls == []
    assert timelapse_calls == []
    assert events == []


def test_bare_ct1000_start_is_ignored_immediately_after_completion(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    now = [100.0]
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: now[0])

    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
        "task_id": "task-bare",
    })
    queue._handle_notification({"commandType": 1000, "value": 0})

    history_calls.clear()
    timelapse_calls.clear()
    events.clear()

    now[0] += 2.0
    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.IDLE
    assert queue._pending_history_start is False
    assert history_calls == []
    assert timelapse_calls == []
    assert events == []


def test_ct1001_blocked_during_pre_print_window():
    """Progress messages (ct=1001) during the pre-print window should NOT
    upgrade to full print activation. Only ct=1000 value=1 does that."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Simulate the pre-print window state set by ct=1044
    queue._state = PrintState.PRE_PRINT
    queue._last_filename = "cube.gcode"

    # ct=1001 progress should be blocked (state is PRE_PRINT, not IDLE)
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
    })

    # Should still be in pre-print window, no activation side effects
    assert queue._state == PrintState.PRE_PRINT, "Pre-print window should not be cleared by ct=1001"
    start_events = [e for e in events if e[0] == "print_started"]
    start_records = [c for c in history_calls if c[0] == "start"]
    assert len(start_events) == 0, "ct=1001 should not fire start event during pre-print"
    assert len(start_records) == 0, "ct=1001 should not record history during pre-print"


def test_print_state_enum_values():
    """PrintState enum has all expected members."""
    assert set(PrintState.__members__.keys()) == {
        "IDLE", "PREPARING", "PRE_PRINT", "PRINTING", "PAUSED", "FAILED",
    }


def test_state_transitions_through_full_lifecycle(monkeypatch):
    """Walk through IDLE -> PREPARING -> PRE_PRINT -> PRINTING -> DONE -> IDLE
    and verify state at each step.  Also verify that _transition_to_active()
    returns False from FAILED and DONE states."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    # Start: IDLE
    assert queue._state == PrintState.IDLE

    # IDLE -> PREPARING (via mark_pending_print_start)
    queue.mark_pending_print_start("lifecycle.gcode")
    assert queue._state == PrintState.PREPARING

    # PREPARING -> PRE_PRINT (via ct=1044 with pending start)
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/lifecycle.gcode"})
    assert queue._state == PrintState.PRE_PRINT

    # PRE_PRINT -> PRINTING (via ct=1000 value=1)
    queue._handle_notification({"commandType": 1000, "value": 1})
    assert queue._state == PrintState.PRINTING

    # PRINTING -> IDLE (via ct=1000 value=0, normal end)
    queue._handle_notification({"commandType": 1000, "value": 0})
    assert queue._state == PrintState.IDLE

    # Verify _transition_to_active() allowed from FAILED (new print after failure)
    queue._state = PrintState.FAILED
    assert queue._transition_to_active({}, progress=0) is True
    assert queue._state == PrintState.PRINTING


def test_state_after_reset_is_always_idle():
    """_reset_print_state() returns to IDLE regardless of prior state."""
    queue = _queue()

    for state in PrintState:
        queue._state = state
        queue._reset_print_state()
        assert queue._state == PrintState.IDLE, f"Expected IDLE after reset from {state.name}"


def test_pending_prepare_state_log_is_emitted_once_for_repeated_value8(caplog):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue.mark_pending_print_start("queued.gcode")

    with caplog.at_level(logging.INFO, logger="mqtt"):
        queue._handle_notification({"commandType": 1000, "value": 8})
        queue._handle_notification({"commandType": 1000, "value": 8})
        queue._handle_notification({"commandType": 1000, "value": 8})

    assert caplog.text.count("Pending print start entered firmware prepare state (ct 1000 value=8)") == 1


def test_recent_completion_bare_ct1000_log_is_emitted_once(caplog, monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)
    queue._remember_recent_completion(filename="cube.gcode", task_id="task-1")

    with caplog.at_level(logging.INFO, logger="mqtt"):
        queue._handle_notification({"commandType": 1000, "value": 1})
        queue._handle_notification({"commandType": 1000, "value": 1})
        queue._handle_notification({"commandType": 1000, "value": 1})

    assert caplog.text.count("Ignoring bare ct 1000 value=1 immediately after print completion") == 1


def test_recent_completion_stale_progress_log_is_emitted_once(caplog, monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)
    queue._remember_recent_completion(filename="cube.gcode", task_id="task-1")

    payload = {
        "commandType": 1001,
        "progress": 0,
        "task_id": "task-1",
        "name": "cube.gcode",
    }

    with caplog.at_level(logging.INFO, logger="mqtt"):
        queue._handle_notification(payload)
        queue._handle_notification(payload)
        queue._handle_notification(payload)

    assert caplog.text.count("Ignoring stale post-completion update for task_id=task-1 filename='cube.gcode'") == 1


def test_duplicate_ct1001_failure_only_fires_once():
    """A second ct=1001 with errorMessage on the same print session
    must not produce a second failure event.  _failure_sent guards this."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Activate a print via ct=1001 progress
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
    })
    assert queue._state == PrintState.PRINTING

    # First failure — should fire
    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
        "errorMessage": "jam",
    })

    fail_events = [e for e in events if e[0] == "print_failed"]
    fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(fail_events) == 1, f"Expected 1 fail event, got {len(fail_events)}"
    assert len(fail_records) == 1, f"Expected 1 fail record, got {len(fail_records)}"
    assert queue._failure_sent is True

    # Second failure — should be suppressed by _failure_sent
    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
        "errorMessage": "jam",
    })

    dup_fail_events = [e for e in events if e[0] == "print_failed"]
    dup_fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(dup_fail_events) == 0, "Second failure should be suppressed"
    assert len(dup_fail_records) == 0, "Second failure should not record history"


def test_new_print_starts_after_ct1001_failure():
    """After a ct=1001 failure leaves state as FAILED, a new print must
    be able to start.  _transition_to_active() must allow activation
    from FAILED state (equivalent to old _print_active=False behavior)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Activate and then fail via ct=1001
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "first.gcode",
    })
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "first.gcode",
        "errorMessage": "jam",
    })
    assert queue._state == PrintState.FAILED

    # New print starts via ct=1001 progress — must not be blocked by FAILED state
    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 500,
        "name": "second.gcode",
    })

    assert queue._state == PrintState.PRINTING, f"Expected PRINTING, got {queue._state}"
    start_events = [e for e in events if e[0] == "print_started"]
    assert len(start_events) == 1, "New print after failure should fire start event"


def test_pause_and_resume():
    """ct=1000 value=2 pauses a printing job, value=3 resumes it."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Start a print
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    assert queue._state == PrintState.PRINTING

    # Pause
    ha_updates.clear()
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED
    assert queue.is_printing is True, "Paused print should still count as active"
    assert any(u.get("print_status") == "paused" for u in ha_updates), "HA should report paused"

    # Resume
    ha_updates.clear()
    queue._handle_notification({"commandType": 1000, "value": 3})
    assert queue._state == PrintState.PRINTING
    assert any(u.get("print_status") == "printing" for u in ha_updates), "HA should report printing after resume"


def test_pause_and_resume_when_resume_ack_is_printing_value():
    """Some firmware confirms resume with ct=1000 value=1 instead of value=3."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    history_calls.clear()
    timelapse_calls.clear()
    events.clear()
    ha_updates.clear()
    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    assert any(u.get("print_status") == "printing" for u in ha_updates), "HA should report printing after resume"
    assert history_calls == []
    assert timelapse_calls == []
    assert events == []


def test_pause_only_from_printing():
    """value=2 is ignored if not currently PRINTING."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # value=2 from IDLE should be a no-op
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.IDLE


def test_resume_only_from_paused():
    """value=3 is ignored if not currently PAUSED."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Start printing, then try resume without pausing first
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    assert queue._state == PrintState.PRINTING

    queue._handle_notification({"commandType": 1000, "value": 3})
    assert queue._state == PrintState.PRINTING, "Resume without pause should be no-op"


def test_stop_during_pause():
    """Stopping a paused print should cancel it properly."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Start, pause, then stop
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    queue._stop_requested = True
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert queue._state == PrintState.IDLE
    fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(fail_records) == 1, "Stopped paused print should record failure"
    assert fail_records[0][2]["reason"] == "cancelled"


def test_normal_finish_from_paused():
    """value=0 without stop_requested from PAUSED records a normal finish."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    # Normal finish (no stop_requested)
    history_calls.clear()
    events.clear()
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert queue._state == PrintState.IDLE
    finish_records = [c for c in history_calls if c[0] == "finish"]
    fail_records = [c for c in history_calls if c[0] == "fail"]
    finish_events = [e for e in events if e[0] == "print_finished"]
    assert len(finish_records) == 1, "Normal finish from paused should record finish"
    assert len(fail_records) == 0, "Normal finish from paused should not record failure"
    assert len(finish_events) == 1, "Normal finish from paused should send finish event"


def test_abort_during_pause():
    """value=8 from PAUSED is a real abort (not pre-print ignore)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    history_calls.clear()
    events.clear()
    timelapse_calls.clear()
    queue._handle_notification({"commandType": 1000, "value": 8})

    assert queue._state == PrintState.IDLE
    fail_records = [c for c in history_calls if c[0] == "fail"]
    fail_events = [e for e in events if e[0] == "print_failed"]
    assert len(fail_records) == 1, "Abort during pause should record failure"
    assert fail_records[0][2]["reason"] == "aborted"
    assert len(fail_events) == 1, "Abort during pause should send fail event"
    assert ("fail",) in timelapse_calls, "Abort during pause should fail timelapse"


def test_ct1001_progress_ignored_during_pause():
    """Progress messages during PAUSED should not emit progress events
    or change state."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    events.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 5000,
        "name": "cube.gcode",
    })

    assert queue._state == PrintState.PAUSED, "State should remain PAUSED"
    progress_events = [e for e in events if e[0] == "print_progress"]
    assert len(progress_events) == 0, "No progress events during pause"


def test_ct1001_failure_during_pause_is_swallowed():
    """ct=1001 with errorMessage during PAUSED is silently ignored.
    The failure guard checks _state == PRINTING, so PAUSED errors
    don't trigger duplicate failure handling. The printer should send
    ct=1000 value=0 or value=8 for real failures."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 5000,
        "name": "cube.gcode",
        "errorMessage": "jam",
    })

    assert queue._state == PrintState.PAUSED, "Should remain PAUSED"
    fail_events = [e for e in events if e[0] == "print_failed"]
    fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(fail_events) == 0, "ct=1001 failure ignored during pause"
    assert len(fail_records) == 0, "ct=1001 failure ignored during pause"


def test_pause_and_resume_guards_reject_invalid_states():
    """value=2 is only valid from PRINTING; value=3 only from PAUSED.
    All other states should be no-ops."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # value=2 from each non-PRINTING state
    for state in (PrintState.IDLE, PrintState.PREPARING, PrintState.PRE_PRINT,
                  PrintState.PAUSED, PrintState.FAILED):
        queue._state = state
        queue._handle_notification({"commandType": 1000, "value": 2})
        assert queue._state == state, f"value=2 from {state.name} should be no-op"

    # value=3 from each non-PAUSED state
    for state in (PrintState.IDLE, PrintState.PREPARING, PrintState.PRE_PRINT,
                  PrintState.PRINTING, PrintState.FAILED):
        queue._state = state
        queue._handle_notification({"commandType": 1000, "value": 3})
        assert queue._state == state, f"value=3 from {state.name} should be no-op"


def test_transition_to_active_blocked_from_paused():
    """_transition_to_active() should not fire from PAUSED state."""
    queue = _queue()
    queue._state = PrintState.PAUSED
    assert queue._transition_to_active({}, progress=50) is False
    assert queue._state == PrintState.PAUSED


def test_forward_to_ha_reports_paused_during_ct1001():
    """_forward_to_ha derives 'paused' status for ct=1001 messages
    while in PAUSED state (separate code path from ct=1000 handler)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PAUSED

    queue._forward_to_ha({
        "commandType": 1001,
        "progress": 5000,
        "name": "cube.gcode",
    })

    paused_updates = [u for u in ha_updates if u.get("print_status") == "paused"]
    assert len(paused_updates) >= 1, "HA should report 'paused' for ct=1001 during pause"


def test_get_state_during_pause():
    """get_state() output is correct during PAUSED."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PAUSED
    queue._last_state_value = 8  # worst case for is_preparing_print

    state = queue.get_state()["print"]
    assert state["print_state"] == "paused"
    assert state["active"] is True
    assert state["in_pre_print_window"] is False
    assert state["preparing"] is False, "is_preparing_print should be False during PAUSED"
    assert state["state_label"] == "preparing_or_aborted"


def test_worker_run_raises_restart_signal_when_mqtt_stuck():
    """worker_run raises ServiceRestartSignal when no messages for 90s."""
    import pytest
    from web.lib.service import ServiceRestartSignal

    queue = _queue()
    queue._connection_started_at = time.time() - 100.0
    queue._last_message_time = time.time() - 100.0

    class _FakeClient:
        def fetch(self, timeout):
            return []
        def query(self, cmd):
            pass

    queue.client = _FakeClient()
    queue._last_query = time.time()

    with pytest.raises(ServiceRestartSignal):
        queue.worker_run(timeout=0.1)


def test_worker_run_no_restart_within_grace_period():
    """worker_run must NOT restart within the 90s grace window after (re)connect."""
    queue = _queue()
    queue._connection_started_at = time.time() - 30.0
    queue._last_message_time = time.time() - 30.0

    class _FakeClient:
        def fetch(self, timeout):
            return []
        def query(self, cmd):
            pass

    queue.client = _FakeClient()
    queue._last_query = time.time()

    # Must not raise
    queue.worker_run(timeout=0.1)


def test_worker_run_raises_restart_signal_when_no_message_ever_received():
    """Reconnect even when _last_message_time is 0.0 (never received anything)."""
    import pytest
    from web.lib.service import ServiceRestartSignal

    queue = _queue()
    queue._connection_started_at = time.time() - 100.0
    queue._last_message_time = 0.0  # never received a message — the real hot-loop scenario

    class _FakeClient:
        def fetch(self, timeout):
            return []
        def query(self, cmd):
            pass

    queue.client = _FakeClient()
    queue._last_query = time.time()

    with pytest.raises(ServiceRestartSignal):
        queue.worker_run(timeout=0.1)


def test_worker_run_increments_silent_count_and_uses_backoff():
    """Each stuck reconnect increments the counter and grows the delay."""
    import pytest
    from web.lib.service import ServiceRestartSignal

    queue = _queue()
    queue._connection_started_at = time.time() - 100.0
    queue._last_message_time = time.time() - 100.0
    queue._silent_reconnect_count = 3  # pre-seeded: in production this accumulates via worker_init (not worker_start)

    class _FakeClient:
        def fetch(self, timeout): return []
        def query(self, cmd): pass

    queue.client = _FakeClient()
    queue._last_query = time.time()

    with pytest.raises(ServiceRestartSignal) as exc_info:
        queue.worker_run(timeout=0.1)

    sig = exc_info.value
    assert queue._silent_reconnect_count == 4
    assert sig.delay == 60.0, f"Expected 60s holdoff for count=4, got {sig.delay}"


def test_worker_run_resets_silent_count_on_message():
    """Receiving any message resets _silent_reconnect_count to 0."""
    from types import SimpleNamespace

    queue = _queue()
    queue._connection_started_at = time.time() - 5.0
    queue._last_message_time = time.time() - 5.0
    queue._silent_reconnect_count = 7

    from libflagship.mqtt import MqttMsgType
    fake_msg = SimpleNamespace(topic="test", payload=b"")
    fake_body = [{"commandType": MqttMsgType.ZZ_MQTT_CMD_APP_QUERY_STATUS.value}]

    class _FakeClient:
        def fetch(self, timeout): return [(fake_msg, fake_body)]
        def query(self, cmd): pass

    queue.client = _FakeClient()
    queue._last_query = time.time()
    queue.notify = lambda obj: None
    queue._forward_to_ha = lambda obj: None
    queue._handle_notification = lambda obj: None
    queue._handle_z_offset_update = lambda obj: None

    queue.worker_run(timeout=0.1)
    assert queue._silent_reconnect_count == 0


def test_silent_reconnect_count_accumulates_across_reconnects():
    """Counter must persist across worker_start() calls so back-off actually escalates."""
    import pytest
    from web.lib.service import ServiceRestartSignal

    queue = _queue()

    class _FakeClient:
        def fetch(self, timeout): return []
        def query(self, cmd): pass

    def _make_stuck_queue():
        queue._connection_started_at = time.time() - 100.0
        queue._last_message_time = time.time() - 100.0
        queue.client = _FakeClient()
        queue._last_query = time.time()

    # Simulate worker_init (initialises _silent_reconnect_count = 0)
    queue._silent_reconnect_count = 0

    delays = []
    for _ in range(5):
        # Simulate worker_start() — the key check: it must NOT reset the counter
        queue._connection_started_at = time.time() - 100.0
        _make_stuck_queue()

        with pytest.raises(ServiceRestartSignal) as exc_info:
            queue.worker_run(timeout=0.1)
        delays.append(exc_info.value.delay)

    # First 3 should be 1s, 4th should be 60s, 5th should be 120s
    assert delays[0] == 1.0
    assert delays[1] == 1.0
    assert delays[2] == 1.0
    assert delays[3] == 60.0
    assert delays[4] == 120.0, f"Expected 120s on 5th silence, got {delays[4]}"


def _enable_real_send_event(queue):
    """Restore the real _send_event/_notification_worker (the _queue() helper
    stubs _send_event with a no-op lambda) and start the dispatcher thread."""
    del queue._send_event
    queue._notification_queue = queue_module.Queue()
    worker = threading.Thread(target=queue._notification_worker, daemon=True)
    worker.start()
    return worker


def test_send_event_does_not_block_on_slow_notifier():
    """_send_event() must only enqueue; the slow snapshot+HTTP work happens
    in the background _notification_worker thread (H2)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    _enable_real_send_event(queue)

    started = threading.Event()
    release = threading.Event()
    sent = []

    def slow_send(event, payload=None, attachments=None):
        started.set()
        release.wait(timeout=2)
        sent.append((event, payload))
        return True, "ok"

    queue._notifier = SimpleNamespace(
        is_event_enabled=lambda event: True,
        progress_interval=lambda default=25: 25,
        progress_max=lambda: None,
        build_attachments=lambda preview_url=None: (None, []),
        send=slow_send,
        cleanup_attachments=lambda paths: None,
    )

    start = time.monotonic()
    queue._send_event(EVENT_PRINT_FINISHED, {"filename": "cube.gcode"}, include_image=True)
    elapsed = time.monotonic() - start

    assert elapsed < 0.05
    assert started.wait(timeout=1)
    release.set()
    queue._notification_queue.join()
    assert sent == [(EVENT_PRINT_FINISHED, {"filename": "cube.gcode"})]


def test_handle_notification_print_finished_does_not_block_on_slow_notifier(monkeypatch):
    """A simulated EVENT_PRINT_FINISHED with a slow notifier stub must not
    slow down _handle_notification (worker_run iteration time stays low)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    _enable_real_send_event(queue)
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    release = threading.Event()

    def slow_send(event, payload=None, attachments=None):
        release.wait(timeout=2)
        return True, "ok"

    queue._notifier = SimpleNamespace(
        is_event_enabled=lambda event: True,
        progress_interval=lambda default=25: 25,
        progress_max=lambda: None,
        build_attachments=lambda preview_url=None: (None, []),
        send=slow_send,
        cleanup_attachments=lambda paths: None,
    )

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})

    start = time.monotonic()
    queue._handle_notification({"commandType": 1000, "value": 0})
    elapsed = time.monotonic() - start

    assert elapsed < 0.2
    release.set()
    queue._notification_queue.join()
