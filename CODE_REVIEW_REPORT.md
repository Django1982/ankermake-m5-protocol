# Code-Review-Report — Bugs, Effizienz, Performance

**Datum:** 2026-06-11
**Umfang:** `web/` (Services + Flask-App), `web/lib/service.py`, `libflagship/ppppapi.py`, `static/ankersrv.js` (gezielter Pass)
**Stand:** master @ `64b8b2b`

Frühere Review-Runden (K-/M-/S-Findings, JS-Perf-Pass) sind bereits eingearbeitet — dieser Report listet nur **neue bzw. noch offene** Punkte.

---

## HIGH — Bugs mit spürbarer Auswirkung

### H1: Timelapse-Stop blockiert garantiert ~12 s (Join unter Lock)

**Dateien:** `web/service/timelapse.py` — `finish_capture()` (Z. 654–655), `fail_capture()` (Z. 707–708), `start_capture()`, `_stop_capture_thread()` (Z. 739–743), `_capture_loop()` (Z. 759–761)

`finish_capture()`/`fail_capture()`/`start_capture()` rufen `_stop_capture_thread()` **innerhalb** von `with self._lock:` auf. `_stop_capture_thread()` joint den Capture-Thread (Timeout `_SNAPSHOT_TIMEOUT + 2` = 12 s). Der Capture-Thread kann aber nicht beenden, weil sein `finally`-Block (`_capture_loop`) denselben Lock braucht:

```python
finally:
    with self._lock:          # blockiert — Lock hält gerade finish_capture()
        self._capture_thread = None
```

Der Join läuft daher **immer** in den vollen 12-s-Timeout, wenn der Capture-Thread lebt.

**Verschärfung:** `finish_capture(final=True)` wird im MQTT-Worker innerhalb von `_handle_notification()` aufgerufen — und das läuft unter `MqttQueue._state_lock` (`mqtt.py` Z. 884–885). Folge bei jedem Print-Ende mit aktivem Timelapse:
- MQTT-Nachrichtenverarbeitung friert ~12 s ein,
- alle API-Routen, die `mqtt.is_printing` o. Ä. lesen (nehmen `_state_lock`), hängen ebenfalls.

**Fix:** Thread-Stop aus dem Lock herausziehen — Muster:
```python
def finish_capture(self, final=False):
    ...
    with self._lock:
        thread = self._capture_thread
        self._capture_thread = None
        self._stop_event.set()
        ... # Zustands-Snapshot wie bisher
    if thread and thread.is_alive():
        thread.join(timeout=_SNAPSHOT_TIMEOUT + 2)
```
Alternativ (minimal): im `_capture_loop`-`finally` den Lock weglassen (Attribut-Zuweisung ist atomar) — dann kehrt der Join zurück, sobald der Thread fertig ist. Beides zusammen ist am robustesten.

---

### H2: Apprise-Notifications mit Snapshot laufen synchron im MQTT-Worker unter `_state_lock`

**Dateien:** `web/service/mqtt.py` — `_send_event()` (Z. ~1609–1616), Aufrufe mit `include_image=True` bei `EVENT_PRINT_FINISHED` (Z. 1523–1527, 1538–1542); `web/notifications.py` — `_capture_live_snapshot()`

`_handle_notification()` (unter `_state_lock`) ruft `_send_event(..., include_image=True)` auf. Das macht synchron:
1. ggf. Licht an + `time.sleep(1.5)`,
2. Frame-Wait bis 2,5 s,
3. ffmpeg-Snapshot bis 6 s (`_SNAPSHOT_TIMEOUT`),
4. HTTP-POST an den Apprise-Server.

Macht im Worst Case 10–15 s, in denen der MQTT-Worker keine Nachrichten verarbeitet und `_state_lock` gehalten wird (API-Routen blockieren mit). Zusammen mit H1 kann ein Print-Ende den Worker > 25 s einfrieren. Auch Events ohne Bild (`EVENT_PRINT_STARTED` etc.) machen einen synchronen HTTP-POST im Worker.

**Fix:** Notifications in einen Hintergrund-Dispatcher entkoppeln, z. B.:
- `_send_event()` legt `(event, payload, include_image)` in eine `queue.Queue`,
- ein dedizierter Daemon-Thread (`notify-worker`) baut Attachments und sendet.
Damit ist `_handle_notification()` nach Mikrosekunden fertig. Reihenfolge der Events bleibt durch die Queue erhalten.

---

### H3: `self._timelapse.start_capture(...)` ohne None-Guard → Crash-Loop des MQTT-Workers

**Datei:** `web/service/mqtt.py` — `_transition_to_active()` Z. 364, `_complete_deferred_print_start()` Z. 392

`worker_init()` setzt bei fehlgeschlagener Timelapse-Initialisierung explizit `self._timelapse = None` (Z. 126–129). Alle anderen Aufrufstellen sind mit `if self._timelapse:` bzw. `getattr(...)` abgesichert — diese beiden nicht. Konsequenz: Wenn der TimelapseService-Init scheitert (kaputtes Captures-Verzeichnis, Berechtigungen), wirft **jeder Print-Start** einen `AttributeError` in `worker_run()` → Service-Restart → nächste ct=1000-Meldung → erneuter Crash (Endlosschleife, Print-Start landet u. U. nie in der History).

**Fix:** Beide Stellen mit `if self._timelapse:` schützen (eine Zeile pro Stelle).

---

## MEDIUM — Robustheit / Performance

### M1: `/ws/mqtt` und `/ws/upload` nutzen unbegrenzte Stream-Queues

**Dateien:** `web/lib/service.py` — `stream(name, maxsize=0)`; `web/__init__.py` — `stream_mqtt()` (Z. 745–755), `/ws/upload` (Z. 2556)

`ServiceManager.stream()` legt bei `maxsize=0` eine unbegrenzte `Queue` an. Video nutzt `maxsize=30`, MQTT/Upload nicht. Ein hängender WebSocket-Client (Tab im Hintergrund, halbtotes TCP) lässt die Queue unbegrenzt wachsen — bei 10-s-Statuspolls + Temperatur-Ticks Speicherleck über Stunden/Tage.

**Fix:** `stream_mqtt()` und `/ws/upload` mit `maxsize` (z. B. 500) aufrufen; Drop-Oldest-Verhalten existiert bereits in `_enqueue_stream_item()`.

### M2: Orphan-Timelapse-Assembly blockiert Service-Start (ffmpeg bis 120 s synchron im Init)

**Datei:** `web/service/timelapse.py` — `_scan_in_progress_captures()` (Z. 1182–1204), aufgerufen aus `__init__` → `MqttQueue.worker_init()`

Beim Start werden verwaiste Capture-Verzeichnisse **synchron** assembliert (`_finalize_capture_dir` → ffmpeg, Timeout 120 s pro Verzeichnis). Mehrere Orphans nach einem Crash können den MQTT-Service-Start minutenlang verzögern.

**Fix:** Recovery-Assembly in einen Daemon-Thread auslagern (analog `timelapse-assemble` in `finish_capture`).

### M3: DB-Recreate nach Korruption löscht WAL/SHM-Dateien nicht

**Dateien:** `web/service/history.py` — `_recreate_db_after_corruption()` (Z. 84–98); gleiches Muster in `web/service/filament.py` (Z. 312–326)

Es wird nur die `.db` gelöscht. Im WAL-Modus (beide Stores nutzen `journal_mode=WAL`) bleiben `history.db-wal`/`-shm` liegen — die neue DB kann die alten Sidecars adoptieren und erneut scheitern.

**Fix:** Zusätzlich `db_path + "-wal"` und `db_path + "-shm"` mit `unlink(missing_ok=True)`-Semantik entfernen.

### M4: Upload-Pfad hält komplette GCode-Datei (mehrfach) im RAM

**Dateien:** `web/service/filetransfer.py` — `send_file()` (`raw = fd.read()`), `web/__init__.py` — Reprint (Z. 4646–4647), `libflagship/ppppapi.py` — `FileUploadInfo.from_data` (MD5 über Gesamtdaten), `Channel.write` (`payload[:]`-Kopie)

Bei `UPLOAD_MAX_MB=2048` können das 2 GB+ sein, plus Kopien (`payload[:]`, 1-KB-Chunking). Auf Raspberry-Pi-Klasse-Hosts (typisches Deployment) reicht eine große Datei für OOM.

**Fix (pragmatisch):** `UPLOAD_MAX_MB`-Default senken (z. B. 512) und in `Channel.write` `memoryview(payload)` statt Slice-Kopien nutzen. Vollständiges Streaming wäre größerer Umbau (MD5 inkrementell, Chunked-Send), als Folge-Task.

### M5: PPPP-`Wire` über `multiprocessing.Pipe` — unnötiger Overhead im Video-Hotpath

**Datei:** `libflagship/ppppapi.py` — `Wire` (Z. 119–148), `Channel`

Jeder empfangene 1-KB-DRW-Chunk geht durch `Pipe.send()`/`recv()` (Pickle + 2 Syscalls + Kopie in `buf`). Bei HD-Video (~1–2 MB/s ≈ 1000–2000 Chunks/s) messbarer CPU-Overhead; auf ARM-Hosts relevant. Außerdem teilen sich `Channel.write()` (Service-Thread) und `poll()` (API-Thread) `txqueue`/`backlog` ohne Lock (`txq.sort()` kann mit `append` kollidieren — selten, aber real).

**Fix:** `Wire` auf `bytearray` + `threading.Condition` umstellen (gleiches Interface: `peek/read/write`); `Channel.lock` auch um `write()`-Backlog-Append und `poll()`-Backlog-Merge legen. Achtung: generiert nicht aus `transwarp` — Datei ist handgeschrieben, direkt editierbar.

### M6: Doppeltes Channel-1-Draining pro Worker-Iteration

**Datei:** `web/service/pppp.py` — `worker_run()` (Z. 331–336, 343–376)

Pro Iteration wird Kanal 1 bis zu dreimal gescannt (`skip_rx_gap`-Pfad, unbedingtes `_drain_xzyh(chan=1)`, danach ggf. erneut über den DRW-Pfad). Jeder Aufruf macht Peek/Parse-Arbeit unter `fd.lock`. Funktional korrekt, aber redundant im heißesten Pfad.

**Fix:** Das unbedingte `self._drain_xzyh(chan=1)` (Z. 336) entfernen — der DRW-Pfad und der `skip_rx_gap`-Pfad decken alles ab.

---

## LOW — Kleinkram / Hygiene

| # | Datei / Stelle | Befund | Fix |
|---|---|---|---|
| L1 | `static/ankersrv.js` `AutoWebSocket._message` (Z. 1296–1310) | Jede Text-WS-Nachricht wird doppelt `JSON.parse`d (Auth-Check + Handler) | Auth-Check nur, solange `!this.is_open`, oder geparstes Objekt an Handler durchreichen |
| L2 | `static/ankersrv.js` Video `message` (Z. 1814) | `event.data.slice(0)` kopiert jeden Frame unnötig (ArrayBuffer ist bereits eigenständig) | `data: event.data` direkt pushen |
| L3 | `web/service/mqtt.py` `send_gcode()` | `time.sleep(0.1)` pro Zeile im Flask-Request-Thread; Multi-Line-GCode (Filament-Swap-Templates) verlangsamt API-Antworten | Sleep nur zwischen Zeilen (nicht nach der letzten); ggf. auf 0.05 senken |
| L4 | `web/service/history.py` / `filament.py` `_connect()` | Kein None-Check auf `self._conn` — Race mit `close()` beim Shutdown → `AttributeError` | `if conn is None: raise ServiceStoppedError(...)` o. Ä. |
| L5 | `web/service/pppp.py` (Z. 2) | `import logging as log` loggt über den Root-Logger statt benanntem Logger (`pppp.log`-Filterung, Modulkontext fehlt) | `log = logging.getLogger("pppp")` |
| L6 | `libflagship/ppppapi.py` `Channel.poll()` | Retransmits werden unsortiert re-appended → Deadline-Reihenfolge driftet | Nach Retransmit-Schleife `txq.sort()` oder `heapq` nutzen |
| L7 | `web/service/history.py` `_recreate_db_after_corruption` | Bei zweitem Fehlschlag von `_open_connection()` in `_init_db` ungefangene Exception im Service-Init | zweiten Versuch in try/except mit Log + degradiertem No-History-Modus |

---

## Positiv aufgefallen

- Frühere Findings (N+1-Queries in History, Handler-Snapshot in `Service.notify`, Interval-Registry + Video-Pump-Cleanup im JS, Path-Traversal-Guards, WS-Auth, CSP/Security-Header, API-Key-Middleware) sind sauber umgesetzt.
- SQLite-Stores: WAL, persistente Connection, parametrisierte Queries, Schema-Migration über `user_version` — solide.
- `skip_rx_gap`-Realtime-Recovery im Video-Pfad ist ein guter Kompromiss.

---

## Fix-Plan (empfohlene Reihenfolge)

### Phase 1 — Korrektheit (1 PR, klein & hochwirksam)
1. **H3:** None-Guards für `_timelapse.start_capture` (2 Zeilen, `mqtt.py:364, 392`).
2. **H1:** `_stop_capture_thread`-Join aus dem Lock ziehen + `finally` ohne Lock (`timelapse.py`).
3. **M3 / L4 / L7:** WAL/SHM-Cleanup + `_connect`-Guard in `history.py` und `filament.py`.

*Tests:* Unit-Test „finish_capture kehrt < 1 s zurück bei aktivem Capture-Thread“; Test „Print-Start mit `_timelapse=None` crasht Worker nicht“.

### Phase 2 — Worker-Entkopplung (1 PR)
4. **H2:** Notification-Dispatcher-Thread mit Queue in `MqttQueue` (Snapshot + HTTP raus aus `_state_lock`).
5. **M2:** Orphan-Assembly beim Start in Hintergrund-Thread.

*Tests:* Simulierter `EVENT_PRINT_FINISHED` mit langsamem Notifier-Stub → `worker_run`-Iterationszeit bleibt < 200 ms.

### Phase 3 — Ressourcen & Hotpath (1–2 PRs)
6. **M1:** `maxsize` für MQTT-/Upload-Streams.
7. **M6:** redundantes Drain in `pppp.py` entfernen.
8. **M5:** `Wire` auf `bytearray`+`Condition`, `Channel`-Locking vereinheitlichen.
9. **M4:** `memoryview` in `Channel.write`, Upload-Limit-Default überdenken.

*Tests:* bestehende PPPP-/Video-Tests + manueller Smoke (`pppp lan-search`, Live-Video, Datei-Upload).

### Phase 4 — Kosmetik (Sammel-PR)
10. **L1–L3, L5, L6** in einem kleinen Cleanup-PR.

Geschätzter Gesamtaufwand: Phase 1 ~1–2 h, Phase 2 ~2–3 h, Phase 3 ~3–5 h, Phase 4 ~1 h.
