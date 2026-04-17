import os
from datetime import datetime, timezone

from web.service.history import PrintHistory


def test_record_start_skips_placeholder_names(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    assert history.record_start("unknown") is None
    assert history.get_count() == 0


def test_record_start_reuses_same_task_id_and_interrupts_orphans(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    first_id = history.record_start("part-a.gcode", task_id="task-1")
    resumed_id = history.record_start("part-a.gcode", task_id="task-1")
    history.record_start("part-b.gcode", task_id="task-2")

    entries = history.get_history(limit=10)

    assert resumed_id == first_id
    assert entries[0]["filename"] == "part-b.gcode"
    assert entries[0]["status"] == "started"
    assert entries[1]["filename"] == "part-a.gcode"
    assert entries[1]["status"] == "interrupted"
    assert entries[1]["duration_sec"] >= 0


def test_record_start_does_not_interrupt_active_jobs_from_other_printers(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    first_id = history0.record_start("thing1-part.gcode", task_id="thing1-task")
    second_id = history1.record_start("thing2-part.gcode", task_id="thing2-task")

    entries0 = history0.get_history(limit=10)
    entries1 = history1.get_history(limit=10)
    all_entries = PrintHistory(db_path=db_path).get_history(limit=10)

    assert [entry["id"] for entry in entries0] == [first_id]
    assert [entry["id"] for entry in entries1] == [second_id]
    assert history0.get_count() == 1
    assert history1.get_count() == 1
    assert history0.get_entry(first_id)["printer_index"] == 0
    assert history1.get_entry(second_id)["printer_index"] == 1
    assert history0.get_entry(second_id) is None
    assert history1.get_entry(first_id) is None
    assert {entry["id"] for entry in all_entries} == {first_id, second_id}


def test_printer_scoped_history_claims_legacy_active_task_row(tmp_path):
    db_path = tmp_path / "history.db"
    legacy_history = PrintHistory(db_path=db_path)
    row_id = legacy_history.record_start("legacy.gcode", task_id="legacy-task")

    printer_history = PrintHistory(db_path=db_path, printer_index=1)
    resumed_id = printer_history.record_start("legacy.gcode", task_id="legacy-task")
    printer_history.record_finish(task_id="legacy-task", progress=100)

    entry = printer_history.get_entry(row_id)

    assert resumed_id == row_id
    assert entry["status"] == "finished"
    assert entry["printer_index"] == 1


def test_record_finish_and_fail_update_active_entries(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    history.record_start("success.gcode", task_id="task-success")
    history.record_finish(task_id="task-success", progress=100)

    history.record_start("failed.gcode", task_id="task-fail")
    history.record_fail(task_id="task-fail", reason="jam")

    entries = history.get_history(limit=10)

    assert entries[0]["filename"] == "failed.gcode"
    assert entries[0]["status"] == "failed"
    assert entries[0]["failure_reason"] == "jam"
    assert entries[1]["filename"] == "success.gcode"
    assert entries[1]["status"] == "finished"
    assert entries[1]["progress"] == 100


def test_record_start_dedupes_completed_task_id(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    first_id = history.record_start(
        "cube.gcode",
        task_id="task-finished",
        archive_relpath="saved/cube.gcode",
        archive_size=1234,
    )
    history.record_finish(task_id="task-finished", progress=100)

    duplicate_id = history.record_start(
        "cube.gcode",
        task_id="task-finished",
        preview_url="https://example.test/cube.png",
    )

    entries = history.get_history(limit=10)

    assert duplicate_id == first_id
    assert history.get_count() == 1
    assert entries[0]["status"] == "finished"
    assert entries[0]["archive_relpath"] == "saved/cube.gcode"
    assert entries[0]["archive_size"] == 1234
    assert entries[0]["preview_url"] == "https://example.test/cube.png"


def test_history_prunes_to_max_entries(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db", max_entries=2)

    history.record_start("one.gcode", task_id="1")
    history.record_start("two.gcode", task_id="2")
    history.record_start("three.gcode", task_id="3")

    entries = history.get_history(limit=10)

    assert history.get_count() == 2
    assert [entry["filename"] for entry in entries] == ["three.gcode", "two.gcode"]


def test_history_clear_and_fallback_finish_latest_active(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    history.record_start("one.gcode", task_id="1")
    history.record_start("two.gcode", task_id="2")
    history.record_finish()

    entries = history.get_history(limit=10)
    assert entries[0]["filename"] == "two.gcode"
    assert entries[0]["status"] == "finished"

    history.clear()
    assert history.get_count() == 0


def test_archive_upload_and_reprint_flags(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    archive_info = history.archive_upload(
        "cube.gcode",
        (
            b"; thumbnail begin 32x32 10\n"
            b"; iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5ZQAAAAASUVORK5CYII=\n"
            b"; thumbnail end\n"
            b"G28\nM104 S200\n"
        ),
    )
    row_id = history.record_start(
        "cube.gcode",
        task_id="task-archive",
        archive_relpath=archive_info["archive_relpath"],
        archive_size=archive_info["archive_size"],
    )

    entry = history.get_entry(row_id)

    assert entry["archive_available"] is True
    assert entry["can_reprint"] is True
    assert entry["thumbnail_available"] is True
    assert history.get_archive_path(row_id) is not None
    assert history.get_thumbnail_path(row_id) is not None


def test_history_entry_can_fallback_to_archive_from_same_task_id(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    archive_info = history.archive_upload("cube.gcode", b"G28\nM104 S200\n")
    original_id = history.record_start(
        "cube.gcode",
        task_id="task-archive-fallback",
        archive_relpath=archive_info["archive_relpath"],
        archive_size=archive_info["archive_size"],
    )
    history.record_finish(task_id="task-archive-fallback", progress=100)

    started_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    finished_at = started_at
    with history._connect() as conn:
        cursor = conn.execute(
            "INSERT INTO print_history "
            "(filename, status, started_at, finished_at, duration_sec, progress, task_id) "
            "VALUES (?, 'finished', ?, ?, ?, ?, ?)",
            ("cube.gcode", started_at, finished_at, 10, 100, "task-archive-fallback"),
        )
        duplicate_id = cursor.lastrowid
        conn.commit()

    duplicate_entry = history.get_entry(duplicate_id)

    assert original_id != duplicate_id
    assert duplicate_entry["archive_relpath"] == archive_info["archive_relpath"]
    assert duplicate_entry["archive_available"] is True
    assert duplicate_entry["can_reprint"] is True
    assert history.get_archive_path(duplicate_id) is not None


def test_recent_unclaimed_archive_survives_other_printer_prune(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    archive0 = history0.archive_upload("thing1.gcode", b"G28\nM104 S200\n")
    archive1 = history1.archive_upload("thing2.gcode", b"G28\nM104 S210\n")

    archive0_path = history0._archive_abspath(archive0["archive_relpath"])
    assert archive0_path is not None
    assert os.path.exists(archive0_path)

    history1.record_start(
        "thing2.gcode",
        task_id="thing2-task",
        archive_relpath=archive1["archive_relpath"],
        archive_size=archive1["archive_size"],
    )

    assert os.path.exists(archive0_path)

    row0_id = history0.record_start(
        "thing1.gcode",
        task_id="thing1-task",
        archive_relpath=archive0["archive_relpath"],
        archive_size=archive0["archive_size"],
    )
    entry0 = history0.get_entry(row0_id)

    assert entry0["archive_available"] is True
    assert entry0["can_reprint"] is True
    assert history0.get_archive_path(row0_id) is not None


def test_history_clear_removes_archived_gcode_files(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    archive_info = history.archive_upload("cube.gcode", b"G28\n")
    row_id = history.record_start(
        "cube.gcode",
        archive_relpath=archive_info["archive_relpath"],
        archive_size=archive_info["archive_size"],
    )
    assert history.get_archive_path(row_id) is not None

    history.clear()

    assert history.get_archive_path(row_id) is None


def test_printer_scoped_clear_only_removes_current_printer_entries_and_archives(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    archive0 = history0.archive_upload("thing1.gcode", b"G28\n")
    archive1 = history1.archive_upload("thing2.gcode", b"G28\n")

    row0_id = history0.record_start(
        "thing1.gcode",
        task_id="thing1-task",
        archive_relpath=archive0["archive_relpath"],
        archive_size=archive0["archive_size"],
    )
    history0.record_finish(task_id="thing1-task", progress=100)
    row1_id = history1.record_start(
        "thing2.gcode",
        task_id="thing2-task",
        archive_relpath=archive1["archive_relpath"],
        archive_size=archive1["archive_size"],
    )
    history1.record_finish(task_id="thing2-task", progress=100)

    archive0_path = history0.get_archive_path(row0_id)
    archive1_path = history1.get_archive_path(row1_id)
    assert archive0_path is not None and os.path.exists(archive0_path)
    assert archive1_path is not None and os.path.exists(archive1_path)

    history0.clear()

    assert history0.get_count() == 0
    assert history1.get_count() == 1
    assert history0.get_entry(row0_id) is None
    assert history1.get_entry(row1_id) is not None
    assert not os.path.exists(archive0_path)
    assert os.path.exists(archive1_path)


def test_history_preview_url_marks_entry_thumbnail_available(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    row_id = history.record_start(
        "usb-file.gcode",
        preview_url="https://example.test/preview.png",
    )

    entry = history.get_entry(row_id)

    assert entry["thumbnail_available"] is True
    assert entry["preview_url"] == "https://example.test/preview.png"


def test_delete_entries_removes_selected_rows_and_unreferenced_archives(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    first_archive = history.archive_upload("one.gcode", b"G28\n")
    first_id = history.record_start(
        "one.gcode",
        archive_relpath=first_archive["archive_relpath"],
        archive_size=first_archive["archive_size"],
    )
    second_archive = history.archive_upload("two.gcode", b"G28\n")
    second_id = history.record_start(
        "two.gcode",
        archive_relpath=second_archive["archive_relpath"],
        archive_size=second_archive["archive_size"],
    )

    first_archive_path = os.path.join(tmp_path, "gcode_archive", first_archive["archive_relpath"])
    second_archive_path = os.path.join(tmp_path, "gcode_archive", second_archive["archive_relpath"])

    deleted = history.delete_entries([first_id])

    assert deleted == 1
    assert history.get_entry(first_id) is None
    assert history.get_entry(second_id) is not None
    assert first_archive_path is not None and not os.path.exists(first_archive_path)
    assert second_archive_path is not None and os.path.exists(second_archive_path)


def test_printer_scoped_delete_entries_only_removes_matching_printer_rows(tmp_path):
    db_path = tmp_path / "history.db"
    history0 = PrintHistory(db_path=db_path, printer_index=0)
    history1 = PrintHistory(db_path=db_path, printer_index=1)

    archive0 = history0.archive_upload("thing1.gcode", b"G28\n")
    archive1 = history1.archive_upload("thing2.gcode", b"G28\n")

    row0_id = history0.record_start(
        "thing1.gcode",
        task_id="thing1-task",
        archive_relpath=archive0["archive_relpath"],
        archive_size=archive0["archive_size"],
    )
    history0.record_finish(task_id="thing1-task", progress=100)
    row1_id = history1.record_start(
        "thing2.gcode",
        task_id="thing2-task",
        archive_relpath=archive1["archive_relpath"],
        archive_size=archive1["archive_size"],
    )
    history1.record_finish(task_id="thing2-task", progress=100)

    archive0_path = history0.get_archive_path(row0_id)
    archive1_path = history1.get_archive_path(row1_id)
    assert archive0_path is not None and os.path.exists(archive0_path)
    assert archive1_path is not None and os.path.exists(archive1_path)

    assert history0.delete_entries([row1_id]) == 0
    assert history1.get_entry(row1_id) is not None
    assert os.path.exists(archive1_path)

    assert history0.delete_entries([row0_id]) == 1
    assert history0.get_entry(row0_id) is None
    assert history1.get_entry(row1_id) is not None
    assert not os.path.exists(archive0_path)
    assert os.path.exists(archive1_path)
