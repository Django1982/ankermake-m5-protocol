# Fix: Bind PPPP Sockets to Fixed Local Ports (UFW Compatibility)

**Affects:** `libflagship/ppppapi.py`
**Related issues:** [Django1982/ankermake-m5-protocol#77](https://github.com/Django1982/ankermake-m5-protocol/issues/77) (original Windows report), [Django1982/ankerctl_go_remake#66](https://github.com/Django1982/ankerctl_go_remake/issues/66) (Linux/ufw confirmation)
**Reference implementation:** Go remake, branch `fix/pppp-bind-fixed-local-port`, file `internal/pppp/client/client.go`


## Problem

When ufw (or any stateful firewall) is active with a default-deny-incoming policy, PPPP connections fail silently. The root cause is that Python's `socket.socket()` without an explicit `bind()` acquires an ephemeral local port assigned by the OS at the moment of the first `sendto`. The printer's UDP responses are addressed back to that ephemeral port. Two things go wrong:

1. **Stateful rules do not cover broadcasts.** The LAN discovery socket sends to `255.255.255.255:32108`. Broadcast packets are not tracked by conntrack, so the printer's `PunchPkt` reply is not automatically allowed as "related" traffic — it hits the default-deny rule and is dropped.
2. **Static port-based rules cannot match ephemeral ports.** Even for non-broadcast traffic, an `allow in proto udp to any port X` rule only works if `X` is the fixed port the printer sends responses to. With ephemeral local ports the rule never matches.

Binding each socket to a fixed local port before first use makes the reply addresses predictable. Two static ufw rules then cover all PPPP traffic.


## What to Change

File: `libflagship/ppppapi.py`

There are three factory classmethods on `AnkerPPPPBaseApi` that create UDP sockets. Two of them need a fixed local bind; one must remain ephemeral.

### Socket inventory

Read the constants at the top of `ppppapi.py` before touching anything:

```python
PPPP_LAN_PORT = 32108   # LAN discovery port (LanSearch / PunchPkt)
PPPP_WAN_PORT = 32100   # PPPP session port (file upload, camera, control)
```

| Classmethod | Remote target (current code) | Direction | Local port to bind |
|---|---|---|---|
| `open_lan` | `<printer-ip>:PPPP_LAN_PORT` (32108) | LAN session / handshake | `32108` |
| `open_broadcast` | `255.255.255.255:PPPP_LAN_PORT` (32108) | LAN discovery broadcast | `32108` |
| `open_wan` | `<cloud-relay-host>:PPPP_WAN_PORT` (32100) | WAN/cloud relay | **leave ephemeral** |

> **Note:** In the current Python code, `open_lan` calls `open(duid, host, PPPP_LAN_PORT)` — it targets the printer on port 32108, not 32100. The Go remake uses port 32100 for the session after the handshake completes, which is a separate divergence. Do not change remote ports here. Only add local `bind()` calls. If you observe `open_lan` targeting a different port in your checkout, trust the source file and adjust the local bind port to match.

The WAN socket connects to Anker's cloud relay. Cloud responses transit NAT which tracks the connection regardless of local port, so it does not need a fixed bind. Binding it would also risk conflicting with a concurrent LAN socket on the same port.

### Rule of thumb

- `open_lan` → bind locally to the same port it targets remotely (`PPPP_LAN_PORT`, 32108)
- `open_broadcast` → bind locally to `PPPP_LAN_PORT` (32108)
- `open_wan` → no bind (ephemeral, unchanged)


## Implementation

### Step 1: Add a helper to `_configure_udp_socket`

The cleanest approach is to extend `_configure_udp_socket` with an optional `local_port` parameter and call `sock.bind()` inside it, so buffer/broadcast/bind setup stays in one place.

```python
def _configure_udp_socket(sock, *, broadcast=False, local_port=None):
    for opt_name, value in (
        ("SO_RCVBUF", PPPP_SOCKET_RCVBUF),
        ("SO_SNDBUF", PPPP_SOCKET_SNDBUF),
        ("SO_REUSEADDR", 1),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, value)
        except OSError:
            pass

    if broadcast:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass

    if local_port is not None:
        try:
            sock.bind(('', local_port))
        except OSError as e:
            import errno
            if e.errno == errno.EADDRINUSE:
                raise RuntimeError(
                    f"PPPP local port {local_port} already in use — "
                    "is another ankerctl instance running?"
                ) from e
            raise

    return sock
```

`SO_REUSEADDR` is already set unconditionally before the bind, which is correct — it must precede `bind()` to take effect.

### Step 2: Pass `local_port` in the two affected classmethods

**`open_lan` — bind locally to `PPPP_LAN_PORT` (32108)**

The current `open_lan` delegates to `open(duid, host, PPPP_LAN_PORT)`. Stop delegating and create the socket directly so you can pass `local_port`:

```python
@classmethod
def open_lan(cls, duid, host):
    sock = _configure_udp_socket(
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        local_port=PPPP_LAN_PORT,   # bind locally to 32108
    )
    return cls(sock, duid, addr=(host, PPPP_LAN_PORT))
```

The remote address `(host, PPPP_LAN_PORT)` is unchanged. Only the local `bind()` is added.

**`open_broadcast` — bind to port 32108**

```python
@classmethod
def open_broadcast(cls):
    sock = _configure_udp_socket(
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        broadcast=True,
        local_port=PPPP_LAN_PORT,   # 32108
    )
    addr = ("255.255.255.255", PPPP_LAN_PORT)
    return cls(sock, duid=None, addr=addr)
```

**`open_wan` — no change**

`open_wan` calls `open()` which calls `_configure_udp_socket` without `local_port`. Since the new parameter defaults to `None`, `open_wan` is unaffected without any code change.

### Complete diff summary

```
_configure_udp_socket:   add optional `local_port` parameter; bind + EADDRINUSE guard
open_lan:                stop delegating to open(); create socket directly with local_port=PPPP_LAN_PORT (32108)
open_broadcast:          add local_port=PPPP_LAN_PORT (32108) to _configure_udp_socket call
open_wan / open:         no change
```


## Edge Cases

**Port already in use (EADDRINUSE)**
Raised when a second ankerctl instance is running, or the previous process did not close the socket cleanly. The helper raises `RuntimeError` with a human-readable message. Do not swallow this — let it propagate to the caller so the user sees the error.

**Do not change remote ports**
Only the local `bind()` changes. The remote addresses (`host:32108` for `open_lan`, `255.255.255.255:32108` for `open_broadcast`) are set by the caller and must remain exactly as they are.

**`SO_REUSEADDR` ordering**
`SO_REUSEADDR` is set before `bind()` in `_configure_udp_socket`. This is already the case and must stay that way. Reversing the order silently fails on Linux.

**WAN socket conflict**
If a user has both LAN and WAN sessions active simultaneously (unusual but possible in the CLI), the WAN socket must remain ephemeral to avoid conflicting with the LAN socket on port 32100.

**macOS / Windows**
`sock.bind(('', port))` works identically on macOS and Windows. The EADDRINUSE guard uses `errno.EADDRINUSE` which is available on all platforms. On Windows `SO_REUSEADDR` semantics differ slightly but are not relevant here since the fix targets Linux/ufw environments.


## Testing

### Automated tests

Run the existing test suite:

```bash
cd /data_hdd/ankermake-m5-protocol-django1982
pytest
```

Tests live in `tests/` per `pyproject.toml`. There are currently no PPPP-specific socket tests in that directory. The suite will confirm no regressions in packet parsing or crypto logic.

If you want to verify the bind directly in a test, this is the minimum viable check:

```python
import socket
from libflagship.ppppapi import AnkerPPPPBaseApi, PPPP_LAN_PORT, PPPP_WAN_PORT

def test_open_lan_binds_fixed_port():
    api = AnkerPPPPBaseApi.open_lan("FAKE-DUID", "127.0.0.1")
    name = api.sock.getsockname()
    assert name[1] == PPPP_LAN_PORT, f"expected local port {PPPP_LAN_PORT}, got {name[1]}"
    api.sock.close()

def test_open_broadcast_binds_fixed_port():
    api = AnkerPPPPBaseApi.open_broadcast()
    name = api.sock.getsockname()
    assert name[1] == PPPP_LAN_PORT, f"expected local port {PPPP_LAN_PORT}, got {name[1]}"
    api.sock.close()
```

### Manual end-to-end verification with ufw

```bash
# Enable ufw with default-deny incoming, allow all outbound
sudo ufw enable
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Add the rule that becomes sufficient after the fix (LAN mode)
sudo ufw allow in proto udp to any port 32108

# Run ankerctl and verify it connects to the printer
python -m cli pppp-test   # or equivalent discovery command
```

Expected result: LAN discovery succeeds, `PunchPkt` is received, handshake completes to `Connected` state. Without the fix, discovery hangs at `Connecting` and times out.


## ufw Rules Required After Fix

Once sockets are bound to fixed local ports, the following ufw rules cover all PPPP LAN traffic:

```bash
sudo ufw allow in proto udp to any port 32108
```

Both the `open_lan` session socket and the `open_broadcast` discovery socket bind locally to port 32108 (`PPPP_LAN_PORT`). The printer sends all LAN responses back to that port. A single rule is therefore sufficient for LAN-only use.

If you also use WAN/cloud mode (`open_wan`), that socket remains ephemeral — add a rule for port 32100 only if you confirm the cloud relay also responds to a predictable local port:

```bash
sudo ufw allow in proto udp to any port 32100   # only if needed for WAN mode
```

These rules should be documented in the project README under a "Firewall / ufw" section so users do not need to diagnose silent PPPP failures.
