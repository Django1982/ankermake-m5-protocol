# AnkerMake M5 Protocol

Welcome! This repository contains `ankerctl`, a command-line interface and web UI for monitoring, controlling, and interfacing with AnkerMake M5 3D printers.

**Note:** While changes are tested carefully, bugs still happen. If you run into one, please open a [GitHub Issue](https://github.com/Django1982/ankermake-m5-protocol/issues/new/choose).

This repository continues the work started by the original `Ankermgmt/ankermake-m5-protocol` team. Thanks to the original authors for the reverse engineering work and the foundation this fork builds on.

The `ankerctl` program uses [`libflagship`](documentation/developer-docs/libflagship.md), a library for communicating with the multiple protocols required to connect to an AnkerMake M5 printer. `libflagship` is maintained in this repo under [`libflagship/`](libflagship/).

![Screenshot of ankerctl](/documentation/web-interface.png "Screenshot of ankerctl web interface")

## Current Features

- Print directly from slicers such as **PrusaSlicer, SuperSlicer, OrcaSlicer, Bambu Studio**, and other compatible slicers that can submit jobs to a custom print host.
- Connect to AnkerMake M5 printers and AnkerMake cloud APIs without relying on closed-source Anker software.
- Multi-printer support with per-printer status, controls, history, media views, and settings.
- Send raw G-code commands to the printer and view responses in real time.
- Low-level access to MQTT, PPPP, and HTTPS APIs for debugging and advanced integrations.
- Upload G-code to the printer, start prints from compatible printer storage entries, and reprint compatible archived jobs from history.
- View printer storage, USB storage, and print history with thumbnail previews when available.
- Automatic print history backed by SQLite, including filename, timestamps, duration, result, thumbnails, and reprint availability.
- Automatic timelapse capture during prints, including pause, resume, stop, partial-save-on-failure behavior, per-printer camera source selection (`follow`, printer, or external), and MP4 assembly at print end. Requires `ffmpeg`.
- Dedicated **Snapshots** page for timelapse frames and manual snapshots, including preview, download, and delete actions.
- Stream the built-in printer camera to your computer, with support for optional external camera feeds and a camera setup page.
- Optional printer camera integration endpoint that remuxes the built-in camera feed to H.264 / fragmented MP4 with `ffmpeg` for Frigate and other FFmpeg-based consumers.
- Manual snapshot capture from the Home page, with snapshots saved into the Snapshots gallery.
- Live ankerctl console viewer on the Home page, with recent history and live updates.
- Filament status indicators and filament-change awareness in printer status.
- Safer filament management tools, including validation for blank profile names, clearer in-page error handling, and protection against unsafe legacy filament-apply actions during active prints.
- Sticky printer alerts and notifications, including support for events from printers other than the currently selected one.
- Push notifications via [Apprise](https://github.com/caronc/apprise) for print start, finish, failure, upload, and progress, with optional image attachments.
- Home Assistant MQTT Discovery integration for printer state, temperatures, progress, and light control.
- Optional API key protection for write operations and sensitive API endpoints.
- Debug tab (enable with `ANKERCTL_DEV_MODE=true`) with a state inspector, service health panel, event simulation, and log viewer.
- Bed Level Map (Setup -> Tools) that reads the 7x7 bilinear compensation grid via `M420 V`, renders it as a heatmap, and supports before/after comparison snapshots.
- Improved account import and login flows:
  - CLI import supports legacy `login.json` / `user_info` files and the newer **eufyMake Studio Windows WebView LevelDB cache** (`.ldb` files).
  - Windows autodetect prefers the newest usable slicer session record instead of older stale cache blobs.
  - Import can recover certain truncated WebView auth-token cases by validating candidate prefixes against the API.
  - The web UI includes a one-click **Import From eufyMake Studio** flow plus manual upload fallback.
  - The Setup page shows clear success and failure banners for both import and direct login flows.
  - Manual login trims inputs, normalizes country codes, stays on-page on failure, and prevents duplicate submits while fetching.
- Includes a downloadable Windows launcher.

Let us know what you want to see next. Pull requests are always welcome.

## Installation

Choose **one** installation method:

- [Install from Git](documentation/install-from-git.md) - recommended
- [Install from Docker](documentation/install-from-docker.md)

Suggested order of operations:

1. Choose an installation method.
2. Complete the install steps for that method.
3. Import your AnkerMake account configuration or sign in directly.
4. Start `ankerctl` from the CLI or launch the web server.

> **Note**
> Minimum supported Python version is **3.10**.

> **Warning**
> Docker installation currently works on **Linux only**.

## Importing Configuration

There are now **three** supported ways to get account and printer data into `ankerctl`:

1. **CLI import** from a cached AnkerMake/eufyMake session
2. **Web UI import** from an uploaded file or from an open eufyMake Studio session on the same machine
3. **Direct login** with your email and password

### Option 1: CLI import

Open a terminal in the `ankerctl` folder and run:

```sh
python3 ankerctl.py config import
```

When no filename is provided, `ankerctl` tries to auto-detect a supported cached login source.

#### What the CLI importer can read

- Legacy `login.json`
- Legacy `user_info`
- Newer **eufyMake Studio Windows WebView LevelDB cache files** (`.ldb`)

On modern Windows installs, the importer can scan the LevelDB session cache and select a usable eufyMake Studio session automatically.

> **Important**
> When importing from the newer eufyMake Studio cache on Windows, keep **eufyMake Studio open and signed in** while you run the import.

#### Example commands

**Auto-detect supported cache files**

```sh
python3 ankerctl.py config import
```

**Windows legacy cache example**

```sh
python3 ankerctl.py config import %APPDATA%\Roaming\eufyMake Studio Profile\cache\offline\user_info
```

**Windows newer eufyMake Studio LevelDB folder example**

```sh
python3 ankerctl.py config import %LOCALAPPDATA%\eufyMake Studio Profile\EBWebView\Default\Local Storage\leveldb\000123.ldb
```

**macOS example**

```sh
./ankerctl.py config import $HOME/Library/Application Support/eufyMake Studio Profile\EBWebView\Default\Local Storage\leveldb\000123.ldb
```

**Linux/Wine example**

```sh
./ankerctl.py config import ~/.wine/drive_c/users/username/AppData/Roaming/eufyMake Studio Profile/EBWebView/Default/Local Storage/leveldb/000123.ldb
```

Type `ankerctl.py config import -h` for all options.

To learn more about the underlying account and printer data used during import, see:

- [MQTT Overview](documentation/developer-docs/mqtt-overview.md)
- [Example Files](documentation/developer-docs/example-file-usage)

#### Example successful import output

```sh
[*] Loading cache..
[*] Initializing API..
[*] Requesting profile data..
[*] Requesting printer list..
[*] Requesting pppp keys..
[*] Adding printer [AK7ABC0123401234]
[*] Finished import
```

After import, your configuration is stored in `ankerctl`'s managed config file. You can inspect the saved account and printer info with:

```sh
./ankerctl.py config show
[*] Account:
    user_id: 01234567890abcdef012...<REDACTED>
    email:   bob@example.org
    region:  eu

[*] Printers:
    sn: AK7ABC0123401234
    duid: EUPRAKM-001234-ABCDE
```

### Option 2: Web UI import

The web UI supports two import paths in **Setup -> Account**:

#### A. One-click import from an open eufyMake Studio session

Use **Import From eufyMake Studio**.

This is the easiest option on supported systems because the server will:

- look for the correct slicer cache automatically
- choose the newest usable session record
- import the account and printer data for you
- show a clear success or failure banner after reload

> **Important**
> Keep **eufyMake Studio open and signed in** before using this button.

#### B. Manual upload fallback

If one-click import is unavailable or fails, you can still upload a supported file manually from **Setup -> Account**.

Supported upload sources include:

- `login.json`
- `user_info`
- `000123.ldb`
- supported eufyMake Studio cache files such as `.ldb`

The Setup page now refers to this more accurately as a **login file or slicer cache**, not only a login file.

#### What success and failure look like in the web UI

A successful import shows a green banner similar to:

```text
Configuration imported from open eufyMake Studio for your@email.com with 2 printers.
```

A failed import shows a red banner with the actual reason.

### Option 3: Direct login

You can also fetch account and printer data directly from the AnkerMake servers.

#### CLI direct login

```sh
./ankerctl.py config login DE
```

You will be prompted for your email and password. If AnkerMake requires a CAPTCHA, the CLI will open it in your browser and ask for the answer.

#### Web UI direct login

In **Setup -> Account**, use the manual login form.

Recent improvements to this flow include:

- trimmed email, country, and CAPTCHA fields
- automatic uppercase normalization for country codes
- better inline error handling without bouncing away from the page
- a disabled Fetch button with spinner while the request is running
- clearer success and failure banners

A successful direct login shows a green banner similar to:

```text
Configuration fetched from AnkerMake server for your@email.com with 2 printers.
```

A failed login stays on the same page and shows the actual error in red.

> **Note**
> The cached login info contains sensitive details. In particular, the `user_id` field is used when connecting to MQTT servers and effectively behaves like a password. For that reason the value is redacted when printed to the screen.

Once import or login succeeds, `ankerctl` is ready to use.

## Usage

### Web Interface

Start the web server from the folder where you installed `ankerctl`. The web server must be running whenever you want to:

- use the web interface
- send jobs from a slicer
- use browser-based controls, history, timelapse, snapshots, or camera pages

#### Docker installation

```sh
# Build the image (match UID/GID to your host user)
docker build -t django01982/ankerctl:local --build-arg UID=$(id -u) --build-arg GID=$(id -g) .

# Copy .env.example to .env and adjust values, then start
cp .env.example .env
docker compose up
```

#### Git/Python installation

```sh
./ankerctl.py webserver run
```

Then open:

```text
http://localhost:4470
```

in a browser on the same machine.

> **Important**
> If account configuration has not been imported yet, go to **Setup -> Account** and either:
>
> - click **Import From eufyMake Studio**
> - upload a supported login file or slicer cache
> - or sign in directly with email and password

### Slicer Integration

ankerctl can receive jobs from slicers that support a custom print host / HTTP upload workflow.

Tested slicers include:

- PrusaSlicer
- SuperSlicer
- OrcaSlicer
- Bambu Studio

The web server must be running before the slicer can send a job.

#### Important behavior

At the moment, slicer-hosted upload is intended for immediate use, so the common workflow is:

- **Send and Print** to upload and start the print right away

Additional slicer-specific instructions are available in the web interface Instructions page.

![Screenshot of PrusaSlicer](/static/img/setup/prusaslicer-2.png "Screenshot of prusa slicer")

### Authentication (API Key)

ankerctl supports optional API key authentication.

When enabled:

- **write operations** and **sensitive API endpoints** require the key
- the normal read-only web UI remains viewable
- slicers must send the same key if they are uploading through the host interface

#### Enable via CLI

```sh
# Generate a random API key
./ankerctl.py config set-password

# Or set a specific key
./ankerctl.py config set-password my-secret-key

# Remove key (disable authentication)
./ankerctl.py config remove-password
```

#### Enable via Docker environment variable

```yaml
# In .env (see .env.example)
ANKERCTL_API_KEY=my-secret-key
```

#### Using the key

- **Slicer:** enter the key in the slicer's API Key field so it is sent as the `X-Api-Key` header
- **Browser:** append `?apikey=your-key` to the URL once; a session cookie is set automatically
- **No key set:** authentication stays disabled for backward compatibility

## Environment Variables

ankerctl is configured through environment variables. For Docker deployments, copy `.env.example` to `.env`, adjust the values, and let Docker Compose load them automatically.

| Variable | Default | Description |
|----------|---------|-------------|
| **Server** | | |
| `FLASK_HOST` | `127.0.0.1` | IP address the web server binds to |
| `FLASK_PORT` | `4470` | Port the web server listens on |
| `FLASK_SECRET_KEY` | *(auto-generated)* | Session cookie secret; set this explicitly if you want it to persist across restarts |
| `PRINTER_INDEX` | `0` | Select printer by index when multiple printers are configured |
| **Upload** | | |
| `UPLOAD_MAX_MB` | `2048` | Maximum upload file size in MB |
| `UPLOAD_RATE_MBPS` | `10` | Upload speed to printer in Mbit/s (choices: 5, 10, 25, 50, 100) |
| **Security** | | |
| `ANKERCTL_API_KEY` | *(unset)* | API key for write-operation authentication |
| **Feature Flags** | | |
| `ANKERCTL_DEV_MODE` | `false` | Enable the Debug tab and `/api/debug/*` endpoints |
| `ANKERCTL_LOG_DIR` | *(unset)* | Directory for log files; enables file logging when set |
| **Apprise Notifications** | | |
| `APPRISE_ENABLED` | `false` | Enable Apprise notifications |
| `APPRISE_SERVER_URL` | *(unset)* | Apprise API server URL |
| `APPRISE_KEY` | *(unset)* | Apprise notification key/ID |
| `APPRISE_TAG` | *(unset)* | Apprise tag filter |
| `APPRISE_EVENT_PRINT_STARTED` | `true` | Notify when a print starts |
| `APPRISE_EVENT_PRINT_FINISHED` | `true` | Notify when a print finishes |
| `APPRISE_EVENT_PRINT_FAILED` | `true` | Notify when a print fails |
| `APPRISE_EVENT_GCODE_UPLOADED` | `true` | Notify when G-code is uploaded |
| `APPRISE_EVENT_PRINT_PROGRESS` | `true` | Notify on progress updates |
| `APPRISE_PROGRESS_INTERVAL` | `25` | Progress notification interval (%) |
| `APPRISE_PROGRESS_INCLUDE_IMAGE` | `false` | Attach a camera snapshot to progress notifications |
| `APPRISE_PROGRESS_MAX` | `0` | Override progress scale (0 = auto) |
| `APPRISE_SNAPSHOT_QUALITY` | `hd` | Snapshot quality: `sd`, `hd`, or `fhd` (1920x1080) |
| `APPRISE_SNAPSHOT_FALLBACK` | `true` | Use the G-code preview if live capture fails |
| `APPRISE_SNAPSHOT_LIGHT` | `false` | Turn on the printer light for the snapshot |
| **Print History** | | |
| `PRINT_HISTORY_RETENTION_DAYS` | `90` | Number of days to keep history entries |
| `PRINT_HISTORY_MAX_ENTRIES` | `500` | Maximum number of history entries to keep |
| **Timelapse** | | |
| `TIMELAPSE_ENABLED` | `false` | Enable automatic timelapse capture (requires `ffmpeg`) |
| `TIMELAPSE_INTERVAL_SEC` | `30` | Seconds between captures |
| `TIMELAPSE_MAX_VIDEOS` | `10` | Maximum number of timelapse videos to keep |
| `TIMELAPSE_SAVE_PERSISTENT` | `true` | Save assembled videos persistently |
| `TIMELAPSE_CAPTURES_DIR` | `/captures` | Directory used for timelapse video storage |
| `TIMELAPSE_LIGHT` | *(unset)* | Timelapse light mode: `snapshot` (per-frame) or `session` (whole capture) |
| `TIMELAPSE_CAMERA_SOURCE` | `follow` | Timelapse camera source: `follow`, `printer`, or `external` |
| **Home Assistant MQTT Discovery** | | |
| `HA_MQTT_ENABLED` | `false` | Enable Home Assistant MQTT Discovery integration |
| `HA_MQTT_HOST` | `localhost` | Home Assistant MQTT broker host |
| `HA_MQTT_PORT` | `1883` | Home Assistant MQTT broker port |
| `HA_MQTT_USER` | *(unset)* | MQTT broker username |
| `HA_MQTT_PASSWORD` | *(unset)* | MQTT broker password |
| `HA_MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant discovery topic prefix |
| `HA_MQTT_TOPIC_PREFIX` | `ankerctl` | State/command topic prefix |

> **Tip**
> See [`.env.example`](.env.example) for a ready-to-use template with comments.

## Notifications (Apprise)

ankerctl supports push notifications via [Apprise](https://github.com/caronc/apprise), which supports many notification services including Discord, Telegram, Slack, Pushover, and email.

### Setup options

1. Configure notifications in the **Setup -> Notifications** page
2. Or configure them with environment variables

ankerctl requires an **Apprise API server** rather than only the CLI package. You can:

- run the [Apprise API Docker container](https://github.com/caronc/apprise-api)
- use a hosted Apprise API instance

### Example configuration

```sh
# Connection settings
APPRISE_ENABLED=true
APPRISE_SERVER_URL=http://apprise:8000  # Your Apprise API server URL
APPRISE_KEY=ankerctl                     # Apprise notification key/ID
APPRISE_TAG=critical                     # Optional: Apprise tag filter

# Event toggles (set to true/false)
APPRISE_EVENT_PRINT_STARTED=true         # Notify when print starts
APPRISE_EVENT_PRINT_FINISHED=true        # Notify when print completes
APPRISE_EVENT_PRINT_FAILED=true          # Notify when print fails
APPRISE_EVENT_GCODE_UPLOADED=true        # Notify when G-code uploaded
APPRISE_EVENT_PRINT_PROGRESS=true        # Notify on print progress updates

# Progress notification settings
APPRISE_PROGRESS_INTERVAL=25             # Progress interval (e.g., every 25%)
APPRISE_PROGRESS_INCLUDE_IMAGE=false     # Attach camera snapshot to progress notifications
APPRISE_SNAPSHOT_QUALITY=hd              # Snapshot quality: 'sd', 'hd', or 'fhd' (1920x1080)
APPRISE_SNAPSHOT_FALLBACK=true           # Use G-code preview if live snapshot fails
APPRISE_PROGRESS_MAX=0                   # Override progress scale (0=auto)
```

### Testing your setup

1. Open **Setup -> Notifications**
2. Enter your Apprise server URL and key
3. Enable the events you want
4. Click **Send test**
5. Confirm the notification arrives

### Image attachments

When image attachments are enabled for progress or finish notifications, ankerctl will:

1. try to capture a live camera snapshot
2. fall back to the G-code preview if live capture fails and fallback is enabled
3. send a text-only notification if neither image source works

> **Note**
> Live snapshots require a working PPPP connection and video stream.

## Print History

ankerctl automatically records every print to a local SQLite database and shows it in the **History** tab.

### What is tracked

- filename
- start time
- finish time
- duration
- result (`finished`, `failed`, `cancelled`)

### Configuration

- `PRINT_HISTORY_RETENTION_DAYS` - entries older than this are pruned automatically
- `PRINT_HISTORY_MAX_ENTRIES` - oldest entries are pruned when the maximum is reached

### API endpoints

- `GET /api/history` - list entries (supports `?limit=` and `?offset=`)
- `DELETE /api/history` - clear all history (requires API key if configured)

No setup is required. History is recorded automatically.

## Timelapse

ankerctl can capture a timelapse video automatically for every print.

### Requirements

`ffmpeg` must be installed and available in `PATH`.

### Features

- captures a snapshot every `TIMELAPSE_INTERVAL_SEC` seconds
- assembles frames into an MP4 at print end
- saves a partial video if the print fails
- supports a **resume window** so a resumed print can continue appending to the same timelapse
- prunes old videos when `TIMELAPSE_MAX_VIDEOS` is reached

### Timelapse light behavior

Configure `TIMELAPSE_LIGHT` globally or through the Setup page.

- `snapshot` - turn the light on for each frame, then back off
- `session` - keep the light on for the whole capture session
- unset - do not change the light automatically

### Timelapse camera source

Configure `TIMELAPSE_CAMERA_SOURCE` globally or choose the source in **Setup -> Timelapse** per printer.

- `follow` - use the Home page selected camera source
- `printer` - always use the built-in printer camera
- `external` - always use the configured external camera

This lets the Home page viewer switch between camera feeds without changing an active timelapse capture source.

Videos can be listed, downloaded, and deleted from the **Timelapse** tab.

### API endpoints

- `GET /api/timelapses` - list available videos with metadata
- `GET /api/timelapse/<filename>` - download a video
- `DELETE /api/timelapse/<filename>` - delete a video
- `GET /api/settings/timelapse`
- `POST /api/settings/timelapse`

## Camera Support

ankerctl supports:

- the built-in printer camera
- manual snapshot capture
- timelapse frame capture
- optional external camera feeds configured through the web UI

Use **Home** to choose the viewing and manual-snapshot source. Use **Setup -> Timelapse** to choose a separate timelapse source when you do not want viewing changes to affect capture.

## Home Assistant Integration

ankerctl supports [Home Assistant MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/), publishing printer state directly to your Home Assistant instance.

### Requires

A running MQTT broker such as Mosquitto that both ankerctl and Home Assistant can reach.

### Published entities

- print progress, state, filename, speed, and current layer
- nozzle and bed temperatures
- elapsed and remaining time
- MQTT connected and PPPP connected binary sensors
- printer light switch, including bidirectional control
- camera stream entity

### Setup

1. Configure the `HA_MQTT_*` variables shown above, or
2. Use **Setup -> Home Assistant** in the web UI

### API endpoints

- `GET /api/settings/mqtt`
- `POST /api/settings/mqtt`

## Debug Tab (Development Mode)

Enable the Debug tab by setting:

```sh
ANKERCTL_DEV_MODE=true
```

A **Debug** tab then appears in the web UI.

> **Warning**
> Do not enable this in production. The Debug tab exposes internal state and allows simulated events.

### Included tools

- **State Inspector** - live JSON dump of current print state
- **Controls** - toggle verbose MQTT payload logging
- **Simulation** - fire synthetic events without a real printer
- **Services** - live service health panel with restart actions
- **Log Viewer** - browse log files from `ANKERCTL_LOG_DIR` with filtering

When an API key is configured, all `/api/debug/*` endpoints require authentication.

## Bed Level Map

The **Setup -> Tools** area includes a **Bed Level Map** tool that reads the bilinear compensation grid directly from the printer.

### How to use it

1. Run a `G29` auto-level cycle
2. Open **Setup -> Tools**
3. Click **Read from printer**
4. Review the heatmap and compare saved before/after snapshots

### Live progress

While `G29` is running, the UI can show how many of the 49 probe points have completed.

### API endpoint

- `GET /api/printer/bed-leveling` - returns `{grid, min, max, rows, cols}`

Do not call this during an active print.

## Command-Line Examples

```sh
# Run the web server
./ankerctl.py webserver run

# Set an API key for web authentication
./ankerctl.py config set-password

# Attempt to detect printers on the local network
./ankerctl.py pppp lan-search

# Monitor MQTT events
./ankerctl.py mqtt monitor

# Start an interactive G-code prompt
./ankerctl.py mqtt gcode

# Rename the printer
./ankerctl.py mqtt rename-printer BoatyMcBoatFace

# Print a G-code file
./ankerctl.py pppp print-file boaty.gcode

# Capture 4 MB of camera video
./ankerctl.py pppp capture-video -m 4mb output.h264

# Select which configured printer to use
./ankerctl.py -p <index>
```

## Helpful Links

- [GitHub Repository](https://github.com/Django1982/ankermake-m5-protocol)
- [GitHub Issues](https://github.com/Django1982/ankermake-m5-protocol/issues)
- [eufyMake Support](https://support.eufymake.com/)
- [Installation from Git](documentation/install-from-git.md)
- [Installation from Docker](documentation/install-from-docker.md)

## Legal

This project is **not** endorsed, affiliated with, or supported by AnkerMake. All information here has been gathered from reverse engineering using publicly available knowledge and resources.

The goal of this project is to make the AnkerMake M5 usable and accessible using only Free and Open Source Software (FOSS).

This project is [licensed under the GNU GPLv3](LICENSE), copyright © 2023 Christian Iversen.

Some icons are from [IconFinder](https://www.iconfinder.com/iconsets/3d-printing-line) and are licensed under [Creative Commons](https://creativecommons.org/licenses/by/3.0/).
