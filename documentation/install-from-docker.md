# Installation (Docker)

## login.json pre-requisites for Linux Install

### eufyMake Studio installed on another Machine

1. Install [eufyMake Studio](https://www.eufymake.com/software) on a supported Operating System.  Make sure you open it and login via the “Account” dropdown in the top toolbar.

2. Retreive the ```login.json``` file (Windows: ```user_info```) from the supported operating system:

  Windows Default Location:
  ```sh
  %APPDATA%\Roaming\eufyMake Studio Profile\cache\offline\user_info
  ```
   
  MacOS Default Location:
  ```sh
  $HOME/Library/Application\ Support/AnkerMake/AnkerMake_64bit_fp/login.json
   ```

3. Take said ```login.json``` file and store it in a location your docker instance will be able to access it from.

4. Now follow the Docker Compose Instructions below.

### Native Linux

1. Install [eufyMake Studio](https://www.eufymake.com/software) on Linux via emulation such as Wine.  Make sure you open it and login via the “Account” dropdown in the top toolbar.
   
2. Retreive the ```login.json``` file (Windows: ```user_info```) ```~/.wine/drive_c/users/$USER/AppData/Roaming/eufyMake Studio Profile/cache/offline/user_info```

3. Take said ```login.json``` file and store it in a location your docker instance will be able to access it from.

4. Now follow the Docker Compose Instructions below.

## Docker Compose Instructions

To start `ankerctl` using docker compose, run:

```sh
docker compose pull
docker compose up
```

### Customizing UID/GID

The Docker container runs as a non-root user `ankerctl` (UID/GID 1000 by default).
If your host user has different IDs, customize them in `docker-compose.yaml`:

```yaml
build:
    context: .
    args:
        UID: 1001   # your host UID (run: id -u)
        GID: 1001   # your host GID (run: id -g)
```

Or build directly:

```sh
docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
```

### Enabling API Key Authentication

By default, the web server is open (no authentication). To enable API key authentication, set the `ANKERCTL_API_KEY` environment variable in `docker-compose.yaml`:

```yaml
environment:
    - FLASK_HOST=127.0.0.1
    - FLASK_PORT=4470
    - ANKERCTL_API_KEY=your-secret-key-here
```

When set, all API endpoints require authentication via one of:

- **Slicer:** Set the API key as `X-Api-Key` header (OctoPrint-compatible)
- **Browser:** Append `?apikey=your-secret-key-here` to the URL once — a session cookie will be set automatically

## Firewall (ufw / Linux host)

The Docker compose stack uses `network_mode: host`, so the container shares the host's network namespace and the host firewall applies. If you run `ufw` (or any stateful firewall) with the default-deny-incoming policy, the printer's PPPP responses are dropped silently and discovery hangs.

Allow the LAN PPPP port:

```sh
sudo ufw allow in proto udp to any port 32108
```

This single rule covers both LAN discovery (broadcast) and the LAN session — both sockets bind locally to UDP `32108` since the fix for [issue #77](https://github.com/Django1982/ankermake-m5-protocol/issues/77). For the full background see the **Firewall / ufw** section in the main [README](../README.md#firewall--ufw).
