# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-04-13

### Added
 - External camera feed support (RTSP, HTTP, MJPEG) configurable in Setup tab
 - Snapshot gallery with timelapse integration — manual snapshots archived per print
 - Guided automatic filament swap flow: homes, raises Z, parks, heats, unloads, prompts, loads, purges, cools
 - Per-printer timelapse settings
 - Timelapse pause / resume / stop controls for running captures
 - Snapshot collection browser with individual file download and delete
 - Local G-Code archiving with reprint support from History
 - G-Code thumbnails in History, thumb drive, and printer storage lists
 - Selective delete of individual History entries
 - Filament state indicator on Home page
 - Print-complete alerts via notification path
 - Setup page with Windows launcher `.bat` download
 - Slicer login cache auto-import (OrcaSlicer / EufyMake Studio / PrusaSlicer)
 - Collapsible Home page console viewer
 - Camera frame API endpoint for live JPEG capture (`/api/camera/frame`)
 - Print-state lock on G-Code page (file lists do not refresh while printing)
 - Apprise notification integration with full web UI configuration
 - Email/password login flow as alternative to `login.json` import with CAPTCHA support
 - Home Assistant MQTT Discovery integration
 - Print history with SQLite backend, reprint support, and thumbnail previews
 - Bed level map heatmap with before/after comparison in Setup tab

### Security
 - SSRF closed: External camera URLs validated against an allowlist (`http`, `https`, `rtsp`, `rtmp`) before being passed to ffmpeg
 - Auth gap closed: `GET /api/settings/camera` now requires authentication
 - Injection fix: Newlines and null bytes rejected in Windows launcher `install_dir`
 - XSS fix: Auto-leveling progress value escaped before DOM insertion (CodeQL #21)
 - Stack trace exposure fix: Exception object no longer flows into login error response (CodeQL #25)
 - ReDoS fix: HTML tag stripping regex quantifier bounded to prevent polynomial backtracking (CodeQL #24)
 - `GET /api/settings/mqtt` and `GET /api/notifications/settings` now require auth
 - `/ws/ctrl` WebSocket enforces API key auth inline
 - Timelapse endpoints have path traversal protection

### Fixed
 - Race condition: `_viewer_count` in VideoQueue protected by threading lock
 - Resource leak: Partial MP4 deleted on ffmpeg assembly failure
 - Thread safety: Capture thread join timeout increased to exceed ffmpeg snapshot timeout
 - Ghost temperature flicker: MQTT parser correctly preserves `0°C` cooldown targets
 - PPPP status correctly shows yellow when using stale fallback IP
 - History not saving when prints start in close succession
 - PPPP live video stalls and recovery log noise
 - Stop button freeze and active-print cancel reliability
 - Homing buttons now match official app MQTT payloads
 - MQTT disconnected false-positive on second printer
 - Timelapse delete now removes matching snapshot collection
 - `multiprocessing.Queue` replaced with `queue.Queue` in service framework
 - HomeAssistantService heartbeat thread leak on MQTT reconnect
 - `/ws/ctrl` handler now catches all connection and parse errors gracefully

## [1.0.1] - 2026-04-14

### Fixed
 - Dead message overwrite in filament swap start (legacy path message was silently discarded)
 - Duplicate state update call in filament swap unload phase (first call was never visible)
 - Degree symbol inconsistency in filament swap status messages (`C` → `°C`)
 - ffmpeg stderr no longer leaks embedded URL credentials in camera capture error responses

## [Unreleased]

## [1.11.1] - 2026-08-21

### Dependencies
 - `pip` 26.1.2 → 26.2.1
 - `platformdirs` 4.10.0 → 4.11.3
 - `setuptools` 83.0.0 → 84.0.0
 - `tqdm` 4.68.3 → 4.70.0
 - `docker/login-action` 4.4.0 → 4.5.1
 - `actions/setup-python` 6 → 7

## [1.11.0] - 2026-07-17

### Added
 - Fan speed (part-cooling) shown on the Home page print status card, sourced from MQTT command 1005 and exposed through Home Assistant discovery
 - Per-printer "All-Metal Hotend" setting (Setup → Printer) that raises the nozzle temperature ceiling from the stock 260°C to 300°C; bed ceiling is 100°C either way

### Fixed
 - Nozzle/bed temperature bounds corrected to the real AnkerMake M5 spec — previous limits were untested placeholder guesses
 - GCode upload could fail or hang indefinitely while the camera/video stream was active, requiring four rounds of live-hardware retesting to fully resolve: connect timeout too short under real load, an unbounded hang in the file-transfer handshake, the printer never being told a released video session had ended (so it held its one session slot until its own internal timeout), and an intermittent single-packet drop on the first handshake packet after a fast reconnect
 - Filament type/vendor metadata reverted to "unknown" shortly after print start on a transient MQTT reconnect (e.g. brief WiFi hiccup) — it has no telemetry source to self-heal from, unlike other print-state fields
 - Z-offset input had no bounds check at all; now clamped to ±2mm
 - Print history clear/auto-prune could delete the database record and archived GCode file of an actively printing job
 - A single malformed Home Assistant `mqtt_port` value could permanently hang the entire MQTT service
 - HA MJPEG camera duplicate-prevention check could never fire, registering a duplicate entity on every reconnect
 - Saved bed-leveling grids had no per-printer scoping; "load last" could silently show another printer's mesh
 - Race condition in the shared service-restart path could leave any background service (video, PPPP, timelapse, ...) permanently stopped after overlapping restart requests
 - Filament profile nozzle temperature had no upper bound in 3 of 4 code paths
 - API key leaked via ffmpeg process arguments on every timelapse snapshot capture

### Security
 - Removed setup-path exemption that let a pre-configured API key bypass auth on setup endpoints
 - `auth_token`/`dsk_key`/`mqtt_key` no longer logged unredacted at DEBUG level
 - `login.json` import now caps upload size instead of accepting arbitrarily large files
 - Config writes are now atomic — a crash mid-write can no longer corrupt `default.json`

## [1.10.10] - 2026-06-09

### Fixed
 - PPPP sockets now bind to fixed local UDP port `32108` (`PPPP_LAN_PORT`) before the first `sendto`, so that printer replies are delivered to a predictable port. This makes `ankerctl` work behind a stateful firewall (e.g. ufw with default-deny-incoming) where ephemeral local ports were silently dropping the printer's `PunchPkt` and discovery replies. A single ufw rule (`sudo ufw allow in proto udp to any port 32108`) now suffices for LAN mode. WAN/cloud sessions remain ephemeral. (issue [#77](https://github.com/Django1982/ankermake-m5-protocol/issues/77); see [`documentation/issue77_code_fix.md`](documentation/issue77_code_fix.md) for the full design)
 - `_configure_udp_socket()` gained an optional `local_port` parameter with an `EADDRINUSE` guard that raises a clear `RuntimeError` when a second `ankerctl` instance holds port `32108`.
 - H.264 video stream stalled after 5-15 seconds when using the web UI camera feed. Root cause: a 10 ms per-iteration floor added to the service framework in 1.10.8 capped PPPPService at 100 DRW ACK/s — well below what 720p streaming requires (~150-200/s), causing the printer's send window to fill and video to freeze. PPPPService now opts out of the floor via `_min_iteration_sec = 0.0` and drains all buffered UDP packets per iteration (up to 4096) to match pre-1.10.8 ACK throughput. ([#89](https://github.com/Django1982/ankermake-m5-protocol/pull/89))
 - MQTT back-off counter was reset on each reconnect instead of persisting across reconnect cycles, causing the back-off to never advance past the first step when the printer stayed offline for extended periods. ([#85](https://github.com/Django1982/ankermake-m5-protocol/pull/85))
 - Reconnect button now appears in the web UI after 90 seconds of MQTT silence; uses `fetch()` instead of the removed `$.post` dependency.

## [1.10.9] - 2026-05-02

### Added
 - `--mqtt-ca-cert` CLI flag and `ANKERCTL_MQTT_CA_CERT` env var for custom CA certificate pinning — allows connecting to a self-signed MQTT broker (e.g. local Mosquitto) without disabling all TLS verification via `-k`
 - Offline operation stack: `docker-compose_offline.yaml` with self-hosted Mosquitto broker as drop-in replacement for Anker's cloud MQTT; includes cert setup script and `documentation/offline-feasibility.md`

### Fixed
 - Webserver did not forward the custom MQTT CA cert path to the MQTT service (CI regression introduced in previous commit)

### Changed
 - HA addon `config.yaml` version is now bumped automatically by CI on each release — no more manual version drift

## [1.10.8] - 2026-04-25

### Fixed
 - HA MQTT: service no longer stays disconnected after a printer-MQTT reconnect — `worker_start()` now calls `ha.start()` so the HA broker connection is re-established whenever the printer MQTT session restarts
 - HA MQTT: `start()` is now a no-op when a client is already running, preventing accidental double-start from external callers

## [1.10.7] - 2026-04-24

### Fixed
 - HA MQTT: service no longer permanently disabled when the broker is unreachable at container startup — `OSError` from `connect_async` now falls through to `loop_start()` so paho's background thread retries automatically
 - HA MQTT: `reload_config()` now calls `start()` when the service is enabled but the client is not running (was `pass`), so a manual settings save can recover a dead HA connection
 - HA MQTT: removed redundant `start()` call in `MqttQueue.worker_init()` that caused a double-connect with the same `client_id`, kicking the first connection in a reconnect loop

## [1.10.6] - 2026-04-20

### Fixed
 - HA addon: fix config persistence on restart by symlinking `/home/ankerctl/.config/ankerctl` to `/data/.config/ankerctl`, keeping `default.json` and related SQLite state under the Supervisor-managed persistent volume

## [1.10.5] - 2026-04-20

### Fixed
 - HA Supervised addon: add `full_access: true` so AppArmor does not block PPPP UDP LAN discovery — without it the printer IP is never found and camera/video/file-transfer do not work

## [1.10.4] - 2026-04-20

### Fixed
 - HA Supervisor addon CI: BUILD_FROM image name is now lowercased before passing to Docker (GitHub repository name has uppercase owner which Docker rejects)

## [1.10.3] - 2026-04-20

### Fixed
 - HA Supervisor addon CI: BUILD_FROM image name is now lowercased before passing to Docker (GitHub repository name has uppercase owner which Docker rejects)

## [1.10.2] - 2026-04-20

### Fixed
 - HA Supervised addon: server no longer binds to 127.0.0.1 — Docker CMD now explicitly passes `--host 0.0.0.0`
 - HA Supervised addon: dedicated HA image (`ankermake-m5-protocol-ha`) is now built and published by CI, ensuring `run.sh` runs as entrypoint and addon options (FLASK_HOST etc.) are applied correctly

## [1.10.1] - 2026-04-18

### Fixed
 - HA Supervisor addon now installs correctly: Docker image tags are no longer prefixed with `v` (was `v1.10.0`, now `1.10.0`), matching what Supervisor expects
 - `hassio-addon/config.yaml` version bumped to `1.10.1` to pick up the fixed image tag

## [1.10.0] - 2026-04-18

### Added
 - Home Assistant Supervisor addon packaging (`hassio-addon/`) and repository metadata
 - MJPEG camera stream endpoint for printer cameras, including Home Assistant / Frigate integration support
 - Automatic Home Assistant MJPEG camera registration
 - Multi-printer filter for print history

### Changed
 - Home Assistant MQTT connection is now non-blocking and automatically reconnecting
 - Snapshot and stream startup path now activates the video session immediately to reduce cold-start delay
 - Filament page motion speeds were tuned for the current legacy filament flow

### Security
 - Internal ffmpeg camera access now passes the API key via `X-Api-Key` header instead of URL query parameters, reducing process-list exposure
 - `/api/camera/stream` now clamps `fps` to a maximum of `30`
 - Negative `printer_index` values are rejected explicitly

### Fixed
 - Snapshot and MJPEG stream access now recover correctly from cold-start conditions
 - MJPEG stream sends headers immediately and prewarms the generator to avoid startup stalls
 - VideoQueue enable/disable decisions are now synchronized correctly, fixing viewer lifecycle races
 - Viewer-count underflow is logged instead of being silently hidden
 - Follow-up test and behavior fixes for video, auth, snapshot, and history flows
 - Removed the unused `MjpegHub` service that was never wired into the runtime

## [0.9.0] - 2023-04-17

 - First version with github actions for building docker image. (thanks to @cisien)
 - Add python version checking code, to prevent confusing errors if python version is too old.

## [0.8.0] - 2023-04-06

 - First version with built-in webserver! (thanks to @lazemss for the idea and proof-of-concept)
 - Webserver implements a few OctoPrint endpoints, allowing printing directly from PrusaSlicer.
 - Added static web contents, including step-by-step guide for setting up PrusaSlicer.

## [0.7.0] - 2023-04-04

 - First version with camera streaming support!
 - Fixed many bugs in the file upload code, including ability to send files larger than 512K.
 - Fixed file transfers on Windows platforms.

## [0.6.0] - 2023-04-03

 - First version that can send print jobs to the printer over pppp!
 - Completely reworked pppp api implementation.
 - Added support for upgrading config files automatically, when possible.
 - Major code refactoring and improvements.

## [0.5.0] - 2023-03-26

 - Officially licensed as GPLv3.
 - Improved documentation.
 - Much improved documentation (thanks to @austinrdennis).
 - Added `mqtt gcode` command, making it possible to send custom gcode to the printer!
 - Added `mqtt rename-printer` command.
 - Added `pppp lan-search` command.
 - Added `http calc-check-code` command.
 - Added `http calc-sec-code` command.

## [0.4.0] - 2023-03-22

 - First version with the command line tool: `ankerctl.py`.
 - Added `mqtt monitor` command.
 - Added `config import` command.
 - Added `config show` command.
 - Many fixes and improvements from @spuder.

## [0.3.0] - 2023-03-12

 - Examples moved to `examples/`.
 - Added example program that imports `login.json` from Ankermake Slicer.

## [0.3.0] - 2023-03-09

 - First version with a demo program, showing how to parse pppp packets.

## [0.1.0] - 2023-03-07

 - Early code for libflagship, and first version with a README.
