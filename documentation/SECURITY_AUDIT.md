# Security Audit — ankerctl

**Datum:** 2026-02-17
**Geprüfter Commit:** aktueller `master`-Branch

---

## Zusammenfassung

Dieses Dokument beschreibt Sicherheitslücken und Fehler im `ankermake-m5-protocol`-Repository. Die Findings sind unterteilt in **behebbare Probleme** und **Protokoll-bedingte Limitierungen** (durch das Anker-Druckerprotokoll vorgegeben, nicht änderbar).

| Kategorie | Schweregrad | Gefunden | Behoben |
|-----------|-------------|----------|---------|
| Behebbar  | KRITISCH    | 2        | ✅ 2    |
| Behebbar  | HOCH        | 2        | ✅ 2    |
| Behebbar  | MITTEL      | 5        | ✅ 5    |
| Behebbar  | NIEDRIG     | 1        | ✅ 1    |
| Protokoll-bedingt | INFO | 4        | —       |

> **Alle behebbaren Findings sind umgesetzt.** Details siehe [Umsetzungsplan](./SECURITY_IMPLEMENTATION_PLAN.md).

---

## Behebbare Probleme

### KRITISCH

#### K1 — Web-Server ohne Authentifizierung ✅

**Datei:** `web/__init__.py`
**Status:** Behoben (Phase 3)

Der Flask-Webserver bot keinerlei Authentifizierung. Alle Endpunkte – Druckersteuerung, Datei-Upload, GCode-Ausführung, Konfigurationsänderungen – waren für jeden im Netzwerk zugänglich.

**Betroffene Endpunkte:**

| Endpunkt | Risiko |
|----------|--------|
| `POST /api/files/local` | Beliebige Dateien an Drucker senden |
| `POST /api/printer/gcode` | Beliebigen GCode ausführen |
| `POST /api/printer/control` | Drucker steuern (Start/Stop/Pause) |
| `POST /api/ankerctl/config/login` | Anmeldedaten abfangen |
| `POST /api/ankerctl/config/upload` | Konfiguration überschreiben |
| `GET /api/ankerctl/server/reload` | Server-Neustart auslösen |

**Umsetzung:**
- Optionaler API-Key (`config set-password` / `ANKERCTL_API_KEY` ENV)
- Kein Key → offen (Abwärtskompatibilität)
- Key gesetzt → **Lesen (GET) bleibt offen**, nur Schreibzugriffe (POST) geschützt
- Auth via `X-Api-Key` Header (Slicer), `?apikey=` URL-Parameter (Browser/Session), Session-Cookie
- `SameSite=Strict` + `HttpOnly` auf Session-Cookies (CSRF-Schutz)

---

#### K2 — Konfiguration ohne Dateischutz gespeichert ✅

**Datei:** `cli/config.py`
**Status:** Behoben (Phase 1)

Auth-Token, MQTT-Schlüssel und P2P-Keys werden in `~/.config/ankerctl/default.json` gespeichert. Das Verzeichnis hatte keine eingeschränkten Berechtigungen.

**Umsetzung:** Config-Verzeichnis wird mit `os.chmod(path, 0o700)` abgesichert.

---

### HOCH

#### H1 — Unsichere Zufallszahlen für Sicherheitscodes ✅

**Datei:** `libflagship/seccode.py`
**Status:** Behoben (Phase 1)

`random.randint()` (Mersenne-Twister, vorhersagbar) → `secrets.randbelow()` (kryptographisch sicher).

---

#### H2 — WebSocket-Eingaben ohne Validierung ✅

**Datei:** `web/__init__.py`, `/ws/ctrl`-Handler
**Status:** Behoben (Phase 2)

JSON-Werte wurden direkt an Geräte-APIs weitergeleitet. Jetzt `isinstance()`-Prüfungen für `light` (bool), `quality`/`video_profile` (int), `video_enabled` (bool).

---

### MITTEL

#### M1 — Docker-Container läuft als root ✅

**Datei:** `Dockerfile`, `docker-compose.yaml`
**Status:** Behoben (Phase 2)

Container läuft als non-root User `ankerctl` mit konfigurierbarer UID/GID.

---

#### M2 — Fehlermeldungen leaken interne Details ✅

**Datei:** `web/__init__.py`
**Status:** Behoben (Phase 2)

Benutzerfreundliche Fehlermeldungen, Details nur in Server-Logs via `log.exception()`.

---

#### M3 — MQTT-Checksummen-Fehler wird ignoriert ✅

**Datei:** `libflagship/megajank.py`
**Status:** Behoben (Phase 2)

`print()` + Fallthrough → `raise ValueError()`.

---

#### M4 — Fehlende Größenlimitierung beim Datei-Upload ✅

**Datei:** `web/__init__.py`
**Status:** Behoben (Phase 1)

Konfigurierbares Limit via `UPLOAD_MAX_MB` ENV (Default: 512 MB).

---

#### M5 — Mutable Default-Argument ✅

**Datei:** `libflagship/httpapi.py`
**Status:** Behoben (Phase 1)

`invalid_dsks={}` → `invalid_dsks=None` mit Initialisierung im Body.

---

### NIEDRIG

#### N1 — Duplizierter Code bei Dateisuche ✅

**Datei:** `ankerctl.py`
**Status:** Behoben (Phase 2)

Login-JSON-Autodetect in `_find_login_file()` extrahiert.

---

## Protokoll-bedingte Limitierungen (nicht behebbar)

Die folgenden Findings sind durch das Anker-Druckerprotokoll vorgegeben. Sie können nicht geändert werden, ohne die Kompatibilität mit dem Drucker zu brechen.

| # | Finding | Datei | Begründung |
|---|---------|-------|------------|
| P1 | Hardcodierter AES-IV `3DPrintAnkerMake` | `libflagship/megajank.py` | MQTT-Protokoll des Druckers |
| P2 | Hardcodierter AES-Schlüssel (ECB) | `libflagship/logincache.py` | AnkerMake-Slicer-Format |
| P3 | MD5-basierter `Gtoken`-Header | `libflagship/httpapi.py` | Anker-Cloud-API |
| P4 | `network_mode: host` in Docker | `docker-compose.yaml` | PPPP-UDP-Protokoll |

---

## Accepted Risks

The following findings were reviewed and **consciously accepted**. They will not be fixed.
Each entry documents the rationale and the conditions under which the risk is acceptable.

> **Prerequisite for all entries:** ankerctl is a **LAN-only tool**. It must not be exposed
> to the internet or untrusted networks. All risk assessments below assume a trusted home/office LAN.

---

### AR1 — WebSocket streams unauthenticated (read-only)

**Affected endpoints:** `/ws/mqtt`, `/ws/pppp-state`, `/ws/upload`, `/ws/video`, `/ws/ctrl`

**What an attacker can do:**
- `/ws/mqtt` — read printer telemetry (temperatures, speeds, layer counts, print state). Same data visible on the printer's LCD panel.
- `/ws/pppp-state` — read connection status string (`dormant` / `connected` / `disconnected`).
- `/ws/upload` — read file transfer progress percentages.
- `/ws/video` — receive H.264 video frames from printer camera (M5 only, requires VideoQueue running).
- `/ws/ctrl` — toggle printer light, change video quality, enable/disable camera stream.

**What an attacker cannot do via WebSocket:**
- Execute GCode or control printer motion/heating
- Upload files or start/stop prints
- Modify configuration or credentials
- Access API keys or auth tokens

**Why accepted:**
- Auth on WS streams prevents OrcaSlicer/PrusaSlicer's built-in browser from connecting to the status streams, causing a permanently broken UI (red/yellow blinking badges). Slicer browsers cannot pass API keys.
- The commands available via `/ws/ctrl` (light, video quality) are cosmetic and immediately reversible. Worst case: a neighbour toggles your printer light.
- Data leaked via `/ws/mqtt` is operational telemetry only — no credentials, no identifiers, no file paths.
- Pentester assessment (2026-03-01): no escalation path from WS stream data to printer control or credential theft.

**Residual risk:** Low. Requires LAN access. No path to printer damage or data exfiltration.

---

### AR2 — `?apikey=` query parameter visible in logs

**Affected endpoints:** All endpoints accepting `?apikey=` (HTTP API + WebSocket).

**What an attacker can do:**
- Read the API key from server access logs, reverse proxy logs, or browser history if the key was passed as a URL parameter.

**Why accepted:**
- The `?apikey=` pattern is required for OctoPrint-compatible slicer integration (PrusaSlicer, OrcaSlicer pass the key this way).
- Removing it would break slicer integration entirely.
- Logs are server-side only; a LAN attacker with log access has bigger problems.
- The `X-Api-Key` header and session cookie are available as safer alternatives for browser-based use.

**Residual risk:** Very low on a trusted LAN. Mitigated by using `X-Api-Key` header for any scripted/automated access.

---

### AR3 — No CSRF protection on state-changing endpoints

**Affected endpoints:** All `POST`/`DELETE` endpoints.

**What an attacker can do:**
- Trick an authenticated browser session into making a state-changing request (e.g. via a malicious link).

**Why accepted:**
- `SameSite=Strict` is set on the session cookie, which blocks CSRF for cookie-based sessions in all modern browsers.
- API key auth (`X-Api-Key` header / `?apikey=`) is not subject to CSRF by design (custom headers are blocked cross-origin by CORS).
- The tool is LAN-only; a CSRF attack requires the victim to visit an attacker-controlled page while authenticated, which is an unlikely scenario in a home/office LAN.

**Residual risk:** Very low. `SameSite=Strict` closes the primary CSRF vector.

---

### AR4 — No rate limiting on API endpoints

**Affected endpoints:** All endpoints, especially `/api/ankerctl/config/login`.

**What an attacker can do:**
- Brute-force the API key or attempt credential stuffing against the login endpoint.

**Why accepted:**
- The API key is a 24-character `secrets.token_urlsafe()` value — brute-force is not feasible (≈144 bits of entropy).
- The login endpoint calls Anker's cloud API, which applies its own rate limiting and CAPTCHA.
- Adding rate limiting (e.g. Flask-Limiter) would introduce a dependency and complexity not warranted for a LAN tool.

**Residual risk:** Very low. Key entropy makes brute-force infeasible; upstream rate limiting covers the login endpoint.

---

### AR5 — Session cookie without `Secure` flag

**Affected:** Flask session cookie.

**What an attacker can do:**
- Intercept the session cookie over plain HTTP (no TLS).

**Why accepted:**
- ankerctl runs over plain HTTP on the LAN by default. There is no TLS termination.
- Setting `Secure` would break all cookie-based authentication for HTTP deployments.
- Users who expose ankerctl over a reverse proxy with TLS should set `SESSION_COOKIE_SECURE=True` manually.

**Residual risk:** Low on a wired/trusted LAN. Users on untrusted Wi-Fi should use a reverse proxy with TLS.
