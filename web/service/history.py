"""Print history service backed by SQLite."""

import contextlib
import os
import sqlite3
import threading
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone

import cli.util

log = logging.getLogger("history")


_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_MAX_ENTRIES = 500
_PENDING_ARCHIVE_GRACE_SECONDS = 60 * 60

_PLACEHOLDER_NAMES = frozenset({"unknown", "unknown.gcode", ""})

# M-6: Schema version — bump when adding columns or indexes.
# Migration path: version 0 → current via _migrate_schema().
_CURRENT_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS print_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    printer_index INTEGER,
    status      TEXT NOT NULL DEFAULT 'started',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    duration_sec INTEGER,
    progress    INTEGER DEFAULT 0,
    failure_reason TEXT,
    task_id     TEXT,
    archive_relpath TEXT,
    archive_size INTEGER,
    preview_url TEXT
);
"""

# Explicit column list used in SELECT statements (M-3).
_HISTORY_COLUMNS = (
    "id", "filename", "printer_index", "status", "started_at",
    "finished_at", "duration_sec", "progress", "failure_reason",
    "task_id", "archive_relpath", "archive_size", "preview_url",
)
_HISTORY_SELECT = ", ".join(_HISTORY_COLUMNS)


class PrintHistory:
    """Thread-safe SQLite print history store."""

    def __init__(self, db_path=None, retention_days=None, max_entries=None, printer_index=None):
        self._db_path = db_path or os.path.expanduser("~/.config/ankerctl/history.db")
        self._archive_dir = self._default_archive_dir()
        self._retention_days = int(os.getenv("PRINT_HISTORY_RETENTION_DAYS", retention_days or _DEFAULT_RETENTION_DAYS))
        self._max_entries = int(os.getenv("PRINT_HISTORY_MAX_ENTRIES", max_entries or _DEFAULT_MAX_ENTRIES))
        self._printer_index = self._normalize_printer_index(printer_index)
        # K-1: RLock supports nested calls; persistent connection avoids per-call open/close.
        self._lock = threading.RLock()
        self._conn = None
        self._init_db()

    @staticmethod
    def _normalize_printer_index(printer_index):
        if printer_index is None:
            return None
        try:
            return int(printer_index)
        except (TypeError, ValueError):
            return None

    def _default_archive_dir(self):
        db_path = os.fspath(self._db_path)
        if db_path == ":memory:":
            return None
        return os.path.join(os.path.dirname(os.path.abspath(db_path)), "gcode_archive")

    def _recreate_db_after_corruption(self, exc):
        db_path = os.fspath(self._db_path)
        if db_path == ":memory:":
            raise exc
        log.warning(f"History: database corruption detected at {db_path}: {exc}. Recreating database.")
        try:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            os.unlink(db_path)
        except FileNotFoundError:
            pass
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass

    def _open_connection(self):
        """Open (or reopen) the persistent SQLite connection."""
        db_path = os.fspath(self._db_path)
        conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=10,  # wait up to 10s for write lock (covers async prune contention)
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        return conn

    def _init_db(self):
        try:
            self._conn = self._open_connection()
            self._conn.executescript(_SCHEMA)
            self._migrate_schema(self._conn)
        except sqlite3.DatabaseError as exc:
            self._recreate_db_after_corruption(exc)
            self._conn = self._open_connection()
            self._conn.executescript(_SCHEMA)
            self._migrate_schema(self._conn)

    @contextlib.contextmanager
    def _connect(self):
        """Yield the persistent connection; auto-commits on clean exit, rolls back on exception."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("History: database connection is closed")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def close(self):
        """Close the persistent connection. Call from worker_stop / teardown."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _migrate_schema(self, conn):
        """Upgrade schema using PRAGMA user_version as the version counter (M-6)."""
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= _CURRENT_SCHEMA_VERSION:
                return

            # Version 0 → 1: add optional columns that may be missing from old installs.
            if version < 1:
                cursor = conn.execute("PRAGMA table_info(print_history)")
                columns = {row[1] for row in cursor.fetchall()}
                if "printer_index" not in columns:
                    log.info("History: migrating schema, adding printer_index column")
                    conn.execute("ALTER TABLE print_history ADD COLUMN printer_index INTEGER")
                if "task_id" not in columns:
                    log.info("History: migrating schema, adding task_id column")
                    conn.execute("ALTER TABLE print_history ADD COLUMN task_id TEXT")
                if "archive_relpath" not in columns:
                    log.info("History: migrating schema, adding archive_relpath column")
                    conn.execute("ALTER TABLE print_history ADD COLUMN archive_relpath TEXT")
                if "archive_size" not in columns:
                    log.info("History: migrating schema, adding archive_size column")
                    conn.execute("ALTER TABLE print_history ADD COLUMN archive_size INTEGER")
                if "preview_url" not in columns:
                    log.info("History: migrating schema, adding preview_url column")
                    conn.execute("ALTER TABLE print_history ADD COLUMN preview_url TEXT")

            # Version 1 → 2: ensure indexes exist.
            if version < 2:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON print_history(task_id)")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_print_history_printer_status "
                    "ON print_history(printer_index, status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_print_history_task_printer "
                    "ON print_history(task_id, printer_index)"
                )

            conn.execute(f"PRAGMA user_version = {_CURRENT_SCHEMA_VERSION}")
        except Exception as e:
            log.warning(f"History: schema migration failed: {e}")

    def _select_existing_task_row(self, conn, task_id):
        if not task_id:
            return None
        # M-3: explicit columns instead of SELECT *
        cols = "id, status, archive_relpath, archive_size, preview_url, printer_index"
        if self._printer_index is None:
            return conn.execute(
                f"SELECT {cols} FROM print_history WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return conn.execute(
            f"SELECT {cols} FROM print_history "
            "WHERE task_id=? AND (printer_index=? OR printer_index IS NULL) "
            "ORDER BY CASE WHEN printer_index IS NULL THEN 1 ELSE 0 END, id DESC LIMIT 1",
            (task_id, self._printer_index),
        ).fetchone()

    def _update_row_printer_scope(self, conn, row_id):
        if self._printer_index is None:
            return
        conn.execute(
            "UPDATE print_history SET printer_index=COALESCE(printer_index, ?) WHERE id=?",
            (self._printer_index, row_id),
        )

    def _prune(self):
        """Remove old entries beyond retention and max count. Thread-safe; safe to call from any thread."""
        with self._lock:
            with self._connect() as conn:
                if self._retention_days > 0:
                    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=self._retention_days)).isoformat()
                    # status filter: the shared DB means another printer's long-running
                    # print may exceed retention age while still active — never prune it.
                    conn.execute("DELETE FROM print_history WHERE started_at < ? AND status != 'started'", (cutoff,))

                if self._max_entries > 0:
                    conn.execute("""
                        DELETE FROM print_history WHERE status != 'started' AND id NOT IN (
                            SELECT id FROM print_history ORDER BY id DESC LIMIT ?
                        )
                    """, (self._max_entries,))
                self._delete_unreferenced_archives(conn)

    def _ensure_archive_dir(self):
        if not self._archive_dir:
            return None
        os.makedirs(self._archive_dir, exist_ok=True)
        return self._archive_dir

    @staticmethod
    def _sanitize_archive_filename(filename):
        base = os.path.basename(str(filename or "").strip()) or "upload.gcode"
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
        return safe or "upload.gcode"

    def archive_upload(self, filename, data):
        archive_dir = self._ensure_archive_dir()
        if not archive_dir:
            return None
        payload = bytes(data or b"")
        if not payload:
            raise ValueError("Cannot archive an empty GCode upload")

        safe_name = self._sanitize_archive_filename(filename)
        stored_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}"
        archive_path = os.path.join(archive_dir, stored_name)
        with open(archive_path, "wb") as fh:
            fh.write(payload)
        thumbnail_bytes = cli.util.extract_gcode_thumbnail(payload)
        if thumbnail_bytes:
            thumbnail_path = self._thumbnail_abspath(self._thumbnail_relpath(stored_name))
            if thumbnail_path:
                with open(thumbnail_path, "wb") as fh:
                    fh.write(thumbnail_bytes)
        return {
            "archive_relpath": stored_name,
            "archive_size": len(payload),
        }

    def _archive_abspath(self, archive_relpath):
        archive_dir = self._archive_dir
        if not archive_dir or not archive_relpath:
            return None
        archive_dir = os.path.realpath(archive_dir)
        archive_path = os.path.realpath(os.path.join(archive_dir, str(archive_relpath)))
        if not archive_path.startswith(archive_dir + os.sep):
            return None
        return archive_path

    @staticmethod
    def _thumbnail_relpath(archive_relpath):
        if not archive_relpath:
            return None
        return f"{archive_relpath}.thumbnail.png"

    def _thumbnail_abspath(self, thumbnail_relpath):
        return self._archive_abspath(thumbnail_relpath)

    def _batch_archive_fallbacks(self, conn, entries):
        """K-5: Single query to find archive fallbacks for a batch of entries.

        Returns a dict mapping task_id → (archive_relpath, archive_size) for
        entries that lack an archive_relpath but have a matching row elsewhere.
        """
        # Collect task_ids of entries that need a fallback.
        # Works with both sqlite3.Row (index access) and plain dicts (.get).
        def _row_get(row, key, default=None):
            try:
                return row[key]
            except (KeyError, IndexError):
                return default

        needs_fallback = {}
        for e in entries:
            if _row_get(e, "archive_relpath"):
                continue
            task_id = str(_row_get(e, "task_id") or "").strip()
            if task_id and task_id not in needs_fallback:
                needs_fallback[task_id] = _row_get(e, "printer_index")

        if not needs_fallback:
            return {}

        placeholders = ",".join("?" * len(needs_fallback))
        rows = conn.execute(
            "SELECT task_id, archive_relpath, archive_size, printer_index "
            f"FROM print_history WHERE task_id IN ({placeholders}) "
            "AND archive_relpath IS NOT NULL AND archive_relpath != '' "
            "ORDER BY CASE WHEN printer_index IS NULL THEN 1 ELSE 0 END, id DESC",
            tuple(needs_fallback.keys()),
        ).fetchall()

        result = {}
        for row in rows:
            tid = row["task_id"]
            if tid in result:
                continue  # already have the best match (first row wins due to ORDER BY)
            result[tid] = row
        return result

    def _find_archive_fallback(self, conn, entry):
        # Single-entry path kept for backward compatibility with callers that
        # don't use the batch variant.
        fallbacks = self._batch_archive_fallbacks(conn, [entry])
        task_id = str(entry.get("task_id") or "").strip()
        return fallbacks.get(task_id)

    def _decorate_entry(self, row, conn=None, _archive_fallbacks=None):
        entry = dict(row)
        if conn is not None and not entry.get("archive_relpath"):
            # Use pre-built fallback dict when available (batch path), else single lookup.
            if _archive_fallbacks is not None:
                task_id = str(entry.get("task_id") or "").strip()
                fallback = _archive_fallbacks.get(task_id) if task_id else None
            else:
                fallback = self._find_archive_fallback(conn, entry)
            if fallback:
                entry["archive_relpath"] = fallback["archive_relpath"]
                if entry.get("archive_size") is None:
                    entry["archive_size"] = fallback["archive_size"]
        archive_path = self._archive_abspath(entry.get("archive_relpath"))
        thumbnail_path = self._thumbnail_abspath(self._thumbnail_relpath(entry.get("archive_relpath")))
        entry["archive_available"] = bool(archive_path and os.path.exists(archive_path))
        entry["can_reprint"] = entry["archive_available"]
        entry["thumbnail_available"] = bool(
            (thumbnail_path and os.path.exists(thumbnail_path))
            or entry.get("preview_url")
        )
        return entry

    def _delete_unreferenced_archives(self, conn):
        archive_dir = self._archive_dir
        if not archive_dir or not os.path.isdir(archive_dir):
            return
        now_ts = datetime.now(timezone.utc).timestamp()
        keep = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT archive_relpath FROM print_history WHERE archive_relpath IS NOT NULL AND archive_relpath != ''"
            ).fetchall()
            if row[0]
        }
        keep |= {
            self._thumbnail_relpath(name)
            for name in keep
            if self._thumbnail_relpath(name)
        }
        for name in os.listdir(archive_dir):
            if name in keep:
                continue
            path = os.path.join(archive_dir, name)
            try:
                if os.path.isfile(path):
                    age_seconds = max(0.0, now_ts - os.path.getmtime(path))
                    if age_seconds < _PENDING_ARCHIVE_GRACE_SECONDS:
                        continue
                    os.unlink(path)
            except FileNotFoundError:
                continue
            except Exception as exc:
                log.warning(f"History: could not delete unreferenced archive {path}: {exc}")

    def _delete_archive_relpaths(self, archive_relpaths):
        for archive_relpath in archive_relpaths or ():
            if not archive_relpath:
                continue
            for relpath in (archive_relpath, self._thumbnail_relpath(archive_relpath)):
                path = self._archive_abspath(relpath)
                if not path:
                    continue
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    log.warning(f"History: could not delete archive file {path}: {exc}")

    def record_start(self, filename, task_id=None, archive_relpath=None, archive_size=None, preview_url=None):
        """Record a print start. Returns the row id.

        If an open 'started' entry exists with the same task_id, it is resumed.
        If a completed entry already exists with the same task_id, it is reused instead of
        creating a duplicate row for stale post-finish firmware updates. Otherwise, any
        existing open entries are closed (orphaned) and a new one is created.

        Returns None immediately if *filename* is a placeholder (empty, 'unknown', etc.)
        to avoid polluting history with uninformative entries.
        """
        if not filename or filename.strip().lower() in _PLACEHOLDER_NAMES:
            log.debug(f"History: skipping placeholder filename {filename!r}")
            return None

        with self._lock:
            with self._connect() as conn:
                # 1. Reuse existing entry for the same task_id. The printer can emit
                # late duplicate updates for an already-finished job; task_id is the
                # safest stable identity we have for deduplicating those.
                if task_id:
                    existing = self._select_existing_task_row(conn, task_id)
                    if existing:
                        self._update_row_printer_scope(conn, existing["id"])
                        if archive_relpath or archive_size is not None or preview_url:
                            if self._printer_index is None:
                                conn.execute(
                                    "UPDATE print_history "
                                    "SET archive_relpath=COALESCE(archive_relpath, ?), "
                                    "    archive_size=COALESCE(archive_size, ?), "
                                    "    preview_url=COALESCE(preview_url, ?) "
                                    "WHERE id=?",
                                    (archive_relpath, archive_size, preview_url, existing["id"]),
                                )
                            else:
                                conn.execute(
                                    "UPDATE print_history "
                                    "SET archive_relpath=COALESCE(archive_relpath, ?), "
                                    "    archive_size=COALESCE(archive_size, ?), "
                                    "    preview_url=COALESCE(preview_url, ?), "
                                    "    printer_index=COALESCE(printer_index, ?) "
                                    "WHERE id=?",
                                    (archive_relpath, archive_size, preview_url, self._printer_index, existing["id"]),
                                )
                        if existing["status"] == "started":
                            log.info(f"History: resuming entry id={existing['id']} for task_id={task_id}")
                        else:
                            log.info(
                                "History: ignoring duplicate start for completed task_id=%s "
                                "(existing id=%s status=%s)",
                                task_id,
                                existing["id"],
                                existing["status"],
                            )
                        return existing["id"]

                # Fallback: Resume via filename if no task_id or legacy (only if same filename)
                # This is risky if job restarts, but we prefer task_id now.
                # If we have task_id, we trust it above filename.
                
                # 2. Close any *other* open entries (orphans)
                # If we reached here, we are starting a NEW print session.
                orp_sql = "SELECT id, started_at FROM print_history WHERE status='started'"
                orp_params = ()
                if self._printer_index is not None:
                    orp_sql += " AND printer_index=?"
                    orp_params = (self._printer_index,)
                orphans = conn.execute(orp_sql, orp_params).fetchall()
                
                if orphans:
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    for row in orphans:
                        # Optional: If we match via filename here? No, let's strictly rely on task_id for resume if possible.
                        # If task_id was NOT provided, maybe we fall back to filename matching?
                        # But caller (mqtt) usually provides task_id now.
                        
                        started = datetime.fromisoformat(row["started_at"])
                        duration = int((now - started).total_seconds())
                        conn.execute(
                            "UPDATE print_history SET status='interrupted', finished_at=?,"
                            " duration_sec=? WHERE id=?",
                            (now.isoformat(), duration, row["id"]),
                        )
                    log.info(f"History: marked {len(orphans)} orphaned entries as 'interrupted' before new print")

                # 3. Create new entry
                cur = conn.execute(
                    "INSERT INTO print_history "
                    "(filename, printer_index, status, started_at, task_id, archive_relpath, archive_size, preview_url) "
                    "VALUES (?, ?, 'started', ?, ?, ?, ?, ?)",
                    (
                        filename,
                        self._printer_index,
                        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        task_id,
                        archive_relpath,
                        archive_size,
                        preview_url,
                    )
                )
                row_id = cur.lastrowid
        # M-4: start prune AFTER the transaction commits to avoid write-lock contention
        threading.Thread(target=self._prune, daemon=True, name="history-prune").start()
        return row_id


    def record_finish(self, filename=None, progress=100, task_id=None):
        """Mark the most recent matching print as finished."""
        with self._lock:
            with self._connect() as conn:
                row = self._find_active(conn, filename, task_id)
                if not row:
                    log.debug("No active print to finish")
                    return
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                started = datetime.fromisoformat(row["started_at"])
                duration = int((now - started).total_seconds())
                if self._printer_index is None:
                    conn.execute(
                        "UPDATE print_history SET status='finished', finished_at=?, duration_sec=?, progress=? WHERE id=?",
                        (now.isoformat(), duration, progress, row["id"])
                    )
                else:
                    conn.execute(
                        "UPDATE print_history "
                        "SET status='finished', finished_at=?, duration_sec=?, progress=?, "
                        "printer_index=COALESCE(printer_index, ?) WHERE id=?",
                        (now.isoformat(), duration, progress, self._printer_index, row["id"])
                    )

    def record_fail(self, filename=None, reason=None, task_id=None):
        """Mark the most recent matching print as failed."""
        with self._lock:
            with self._connect() as conn:
                row = self._find_active(conn, filename, task_id)
                if not row:
                    log.debug("No active print to fail")
                    return
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                started = datetime.fromisoformat(row["started_at"])
                duration = int((now - started).total_seconds())
                if self._printer_index is None:
                    conn.execute(
                        "UPDATE print_history SET status='failed', finished_at=?, duration_sec=?, failure_reason=? WHERE id=?",
                        (now.isoformat(), duration, reason, row["id"])
                    )
                else:
                    conn.execute(
                        "UPDATE print_history "
                        "SET status='failed', finished_at=?, duration_sec=?, failure_reason=?, "
                        "printer_index=COALESCE(printer_index, ?) WHERE id=?",
                        (now.isoformat(), duration, reason, self._printer_index, row["id"])
                    )

    def _find_active(self, conn, filename=None, task_id=None):
        """Find the most recent active print, optionally matching task_id or filename."""
        if self._printer_index is not None:
            # 1. Try task_id exact printer match.
            if task_id:
                row = conn.execute(
                    "SELECT * FROM print_history WHERE status='started' AND task_id=? AND printer_index=? "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id, self._printer_index),
                ).fetchone()
                if row:
                    return row

                # Legacy migration fallback: allow a matching unscoped row to be claimed.
                row = conn.execute(
                    "SELECT * FROM print_history WHERE status='started' AND task_id=? AND printer_index IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if row:
                    return row

            # 2. Try filename exact printer match, then legacy fallback.
            if filename:
                row = conn.execute(
                    "SELECT * FROM print_history WHERE status='started' AND filename=? AND printer_index=? "
                    "ORDER BY id DESC LIMIT 1",
                    (filename, self._printer_index),
                ).fetchone()
                if row:
                    return row

                row = conn.execute(
                    "SELECT * FROM print_history WHERE status='started' AND filename=? AND printer_index IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (filename,),
                ).fetchone()
                if row:
                    return row

            # 3. Fallback: only active rows for this printer.
            return conn.execute(
                "SELECT * FROM print_history WHERE status='started' AND printer_index=? ORDER BY id DESC LIMIT 1",
                (self._printer_index,),
            ).fetchone()

        # 1. Try task_id match (strongest)
        if task_id:
            row = conn.execute(
                "SELECT * FROM print_history WHERE status='started' AND task_id=?",
                (task_id,)
            ).fetchone()
            if row:
                return row

        # 2. Try filename match (legacy/fallback)
        if filename:
            row = conn.execute(
                "SELECT * FROM print_history WHERE status='started' AND filename=? ORDER BY id DESC LIMIT 1",
                (filename,)
            ).fetchone()
            if row:
                return row

        # 3. Fallback: Any active print (weakest)
        return conn.execute(
            "SELECT * FROM print_history WHERE status='started' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def init_schema(self):
        """Backward-compatible alias used by older tests and callers."""
        self._init_db()

    def get_history(self, limit=50, offset=0, printer_index=None):
        """Return recent print history as list of dicts.

        Pass printer_index to restrict to one printer while still including
        legacy rows that predate the printer_index column. None returns all.
        """
        params = []
        where = ""
        order_by = " ORDER BY id DESC"
        if printer_index is not None:
            where = " WHERE printer_index=? OR printer_index IS NULL"
            params.append(int(printer_index))
            order_by = " ORDER BY CASE WHEN printer_index IS NULL THEN 1 ELSE 0 END, id DESC"
        params.extend([limit, offset])
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT {_HISTORY_SELECT} FROM print_history{where}{order_by} LIMIT ? OFFSET ?",
                    params,
                ).fetchall()
                # K-5: one batch query for archive fallbacks instead of N+1 per row
                fallbacks = self._batch_archive_fallbacks(conn, rows)
                return [self._decorate_entry(r, conn=conn, _archive_fallbacks=fallbacks) for r in rows]

    def get_entry(self, entry_id):
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT {_HISTORY_SELECT} FROM print_history WHERE id=?",
                    (int(entry_id),),
                ).fetchone()
                if not row:
                    return None
                return self._decorate_entry(row, conn=conn)

    def get_archive_path(self, entry_id):
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        archive_path = self._archive_abspath(entry.get("archive_relpath"))
        if not archive_path or not os.path.exists(archive_path):
            return None
        return archive_path

    def get_thumbnail_path(self, entry_id):
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        thumbnail_path = self._thumbnail_abspath(self._thumbnail_relpath(entry.get("archive_relpath")))
        if not thumbnail_path or not os.path.exists(thumbnail_path):
            return None
        return thumbnail_path

    def update_preview_url(self, preview_url, filename=None, task_id=None):
        if not preview_url:
            return False
        with self._lock:
            with self._connect() as conn:
                row = self._find_active(conn, filename, task_id)
                if not row:
                    return False
                if self._printer_index is None:
                    conn.execute(
                        "UPDATE print_history SET preview_url=? WHERE id=?",
                        (preview_url, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE print_history SET preview_url=?, printer_index=COALESCE(printer_index, ?) WHERE id=?",
                        (preview_url, self._printer_index, row["id"]),
                    )
                return True

    def list_entries(self, limit=50, offset=0):
        """Backward-compatible alias for get_history()."""
        return self.get_history(limit=limit, offset=offset)

    def get_count(self, printer_index=None):
        """Return total number of entries.

        Pass printer_index to filter by printer while still counting legacy
        rows that have no printer_index.
        """
        where, params = "", ()
        if printer_index is not None:
            where = " WHERE printer_index=? OR printer_index IS NULL"
            params = (int(printer_index),)
        with self._lock:
            with self._connect() as conn:
                return conn.execute(
                    f"SELECT COUNT(*) FROM print_history{where}", params
                ).fetchone()[0]

    def delete_entries(self, entry_ids):
        ids = []
        for entry_id in entry_ids or []:
            try:
                normalized = int(entry_id)
            except (TypeError, ValueError):
                continue
            if normalized > 0 and normalized not in ids:
                ids.append(normalized)

        if not ids:
            return 0

        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            with self._connect() as conn:
                archive_relpaths = [
                    row[0]
                    for row in conn.execute(
                        f"SELECT DISTINCT archive_relpath FROM print_history "
                        f"WHERE id IN ({placeholders}) AND archive_relpath IS NOT NULL AND archive_relpath != ''",
                        tuple(ids),
                    ).fetchall()
                    if row[0]
                ]
                cursor = conn.execute(
                    f"DELETE FROM print_history WHERE id IN ({placeholders})",
                    tuple(ids),
                )
                deleted = cursor.rowcount or 0
                if archive_relpaths:
                    still_referenced = {
                        row[0]
                        for row in conn.execute(
                            "SELECT DISTINCT archive_relpath FROM print_history "
                            f"WHERE archive_relpath IN ({','.join('?' for _ in archive_relpaths)})",
                            tuple(archive_relpaths),
                        ).fetchall()
                        if row[0]
                    }
                    self._delete_archive_relpaths(
                        [relpath for relpath in archive_relpaths if relpath not in still_referenced]
                    )
                self._delete_unreferenced_archives(conn)
                if deleted:
                    log.info("History: deleted %d selected entr%s", deleted, "y" if deleted == 1 else "ies")
                return deleted

    def _has_active_entry_locked(self, conn, printer_index=None):
        sql = "SELECT 1 FROM print_history WHERE status='started'"
        params = ()
        if printer_index is not None:
            sql += " AND printer_index=?"
            params = (int(printer_index),)
        return conn.execute(sql + " LIMIT 1", params).fetchone() is not None

    def has_active_entry(self, printer_index=None):
        """Return True if an in-progress (status='started') entry exists.

        When printer_index is provided, only that printer's rows are checked.
        """
        with self._lock:
            with self._connect() as conn:
                return self._has_active_entry_locked(conn, printer_index)

    def clear(self, printer_index=None):
        """Delete history entries.

        When printer_index is provided, only rows for that printer are removed.
        Legacy rows with NULL printer_index are preserved. In-progress entries
        (status='started') and their archived files are never deleted.
        """
        with self._lock:
            with self._connect() as conn:
                delete_archive_dir = False
                # Fast path: nothing is printing anywhere, so wipe table + archive dir.
                if printer_index is None and not self._has_active_entry_locked(conn):
                    conn.execute("DELETE FROM print_history")
                    log.info("Print history cleared")
                    delete_archive_dir = True
                else:
                    if printer_index is None:
                        where = "status != 'started'"
                        params = ()
                    else:
                        where = "printer_index=? AND status != 'started'"
                        params = (int(printer_index),)
                    archive_relpaths = [
                        row[0]
                        for row in conn.execute(
                            "SELECT DISTINCT archive_relpath FROM print_history "
                            f"WHERE {where} AND archive_relpath IS NOT NULL AND archive_relpath != ''",
                            params,
                        ).fetchall()
                        if row[0]
                    ]
                    conn.execute(f"DELETE FROM print_history WHERE {where}", params)
                    if archive_relpaths:
                        still_referenced = {
                            row[0]
                            for row in conn.execute(
                                "SELECT DISTINCT archive_relpath FROM print_history "
                                f"WHERE archive_relpath IN ({','.join('?' for _ in archive_relpaths)})",
                                tuple(archive_relpaths),
                            ).fetchall()
                            if row[0]
                        }
                        self._delete_archive_relpaths(
                            [relpath for relpath in archive_relpaths if relpath not in still_referenced]
                        )
                    self._delete_unreferenced_archives(conn)
                    if printer_index is None:
                        log.info("Print history cleared (in-progress entries preserved)")
                    else:
                        log.info("Print history cleared for printer %d", int(printer_index))
        if delete_archive_dir and self._archive_dir and os.path.isdir(self._archive_dir):
            shutil.rmtree(self._archive_dir, ignore_errors=True)
