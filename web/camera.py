import os
import queue
import subprocess
import tempfile
import threading
from urllib.parse import quote

import cli.model


def _scrub_url_credentials(text):
    """Remove embedded credentials from URLs in ffmpeg error output."""
    import re
    return re.sub(r'([a-z]+://)([^:@/\s]+:[^@/\s]+@)', r'\1***@', text)


CAMERA_SOURCE_PRINTER = "printer"
CAMERA_SOURCE_EXTERNAL = "external"
DEFAULT_EXTERNAL_REFRESH_SEC = 3
DEFAULT_PRINTER_INTEGRATION_ENABLED = False
PRINTER_INTEGRATION_STREAM_PATH = "/api/camera/printer-stream"
PRINTER_STREAM_INPUT_FPS = 15
PRINTERS_WITHOUT_CAMERA = {"V8110"}
RTSP_LOW_LATENCY_INPUT_ARGS = ["-fflags", "nobuffer", "-probesize", "32768", "-analyzeduration", "0"]
_ALLOWED_CAMERA_URL_SCHEMES = {"http", "https", "rtsp", "rtmp"}
_MJPEG_STALE_READ_TIMEOUT_SEC = 10.0
_MJPEG_READER_QUEUE_SIZE = 4
_MJPEG_READ_DONE = object()
_PROCESS_STREAM_STALE_READ_TIMEOUT_SEC = 15.0
_PROCESS_STREAM_READER_QUEUE_SIZE = 8
_PROCESS_STREAM_READ_DONE = object()


class CameraCaptureError(RuntimeError):
    pass


def _validate_camera_url(url: str, field_name: str) -> None:
    if not url:
        return
    from urllib.parse import urlparse

    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_CAMERA_URL_SCHEMES:
        raise ValueError(
            f"{field_name} uses disallowed scheme '{scheme}'. Allowed: {', '.join(sorted(_ALLOWED_CAMERA_URL_SCHEMES))}"
        )


def default_camera_settings():
    return {
        "source": CAMERA_SOURCE_PRINTER,
        "external": {
            "name": "",
            "stream_url": "",
            "snapshot_url": "",
            "refresh_sec": DEFAULT_EXTERNAL_REFRESH_SEC,
        },
        "integration": {
            "enabled": DEFAULT_PRINTER_INTEGRATION_ENABLED,
        },
    }


def printer_supports_camera(printer_or_model):
    model = getattr(printer_or_model, "model", printer_or_model)
    if not model:
        return False
    return str(model) not in PRINTERS_WITHOUT_CAMERA


def _normalize_source(value, default=CAMERA_SOURCE_PRINTER):
    fallback = default if default in {CAMERA_SOURCE_PRINTER, CAMERA_SOURCE_EXTERNAL} else CAMERA_SOURCE_PRINTER
    source = str(value or fallback).strip().lower()
    if source not in {CAMERA_SOURCE_PRINTER, CAMERA_SOURCE_EXTERNAL}:
        return fallback
    return source


def _normalize_refresh_sec(value):
    try:
        refresh_sec = int(value)
    except (TypeError, ValueError):
        refresh_sec = DEFAULT_EXTERNAL_REFRESH_SEC
    return max(1, min(refresh_sec, 30))


def normalize_external_camera_settings(data):
    merged = cli.model.merge_dict_defaults(data, default_camera_settings()["external"])
    return {
        "name": str(merged.get("name") or "").strip(),
        "stream_url": str(merged.get("stream_url") or "").strip(),
        "snapshot_url": str(merged.get("snapshot_url") or "").strip(),
        "refresh_sec": _normalize_refresh_sec(merged.get("refresh_sec")),
    }


def normalize_printer_integration_settings(data):
    merged = cli.model.merge_dict_defaults(data, default_camera_settings()["integration"])
    return {
        "enabled": bool(merged.get("enabled")),
    }


def _printer_from_config(cfg, printer_index):
    printers = getattr(cfg, "printers", None) or []
    if printer_index < 0 or printer_index >= len(printers):
        return None
    return printers[printer_index]


def resolve_camera_settings(cfg, printer_index=0, source_override=None):
    printer = _printer_from_config(cfg, printer_index)
    printer_name = getattr(printer, "name", None)
    printer_sn = getattr(printer, "sn", None)
    printer_supported = printer_supports_camera(printer)

    root = cli.model.merge_dict_defaults(getattr(cfg, "camera", None), cli.model.default_camera_config())
    per_printer = root.get("per_printer")
    if not isinstance(per_printer, dict):
        per_printer = {}

    raw_entry = per_printer.get(printer_sn, {}) if printer_sn else {}
    entry = cli.model.merge_dict_defaults(raw_entry, default_camera_settings())
    configured_source = _normalize_source(entry.get("source"))
    source = configured_source
    if source_override is not None:
        source = _normalize_source(source_override, default=configured_source)
    external = normalize_external_camera_settings(entry.get("external"))
    integration = normalize_printer_integration_settings(entry.get("integration"))
    external_configured = bool(external["stream_url"] or external["snapshot_url"])

    effective_source = None
    if source == CAMERA_SOURCE_PRINTER and printer_supported:
        effective_source = CAMERA_SOURCE_PRINTER
    elif source == CAMERA_SOURCE_EXTERNAL and external_configured:
        effective_source = CAMERA_SOURCE_EXTERNAL
    elif source == CAMERA_SOURCE_PRINTER and not printer_supported and external_configured:
        effective_source = CAMERA_SOURCE_EXTERNAL

    if effective_source == CAMERA_SOURCE_PRINTER:
        detail = "Using the printer camera."
    elif source == CAMERA_SOURCE_EXTERNAL and not external_configured:
        detail = "External camera is selected, but no stream or snapshot URL is configured yet."
    elif not printer_supported and not external_configured:
        detail = "This printer does not expose a built-in camera. Configure an external feed in Setup -> Camera."
    elif effective_source == CAMERA_SOURCE_EXTERNAL:
        detail = "Using external camera live stream." if external["stream_url"] else "Using external camera snapshot preview."
    else:
        detail = "No camera source is ready yet."

    return {
        "printer_index": printer_index,
        "printer_name": printer_name,
        "printer_sn": printer_sn,
        "source": source,
        "configured_source": configured_source,
        "effective_source": effective_source,
        "printer_supported": printer_supported,
        "feature_available": bool(printer_supported or external_configured),
        "detail": detail,
        "external": {
            **external,
            "configured": external_configured,
        },
        "integration": integration,
    }


def update_camera_settings(cfg, printer_index, payload):
    if not isinstance(payload, dict):
        payload = {}

    printer = _printer_from_config(cfg, printer_index)
    if not printer:
        raise ValueError("No active printer is configured.")

    current = resolve_camera_settings(cfg, printer_index)
    source = _normalize_source(payload.get("source", current.get("source")))
    external_payload = payload.get("external")
    integration_payload = payload.get("integration")
    merged_external = normalize_external_camera_settings(
        cli.model.merge_dict_defaults(
            external_payload if isinstance(external_payload, dict) else {},
            current.get("external"),
        )
    )
    merged_integration = normalize_printer_integration_settings(
        cli.model.merge_dict_defaults(
            integration_payload if isinstance(integration_payload, dict) else {},
            current.get("integration"),
        )
    )
    _validate_camera_url(merged_external.get("stream_url"), "stream_url")
    _validate_camera_url(merged_external.get("snapshot_url"), "snapshot_url")

    root = cli.model.merge_dict_defaults(getattr(cfg, "camera", None), cli.model.default_camera_config())
    per_printer = root.get("per_printer")
    if not isinstance(per_printer, dict):
        per_printer = {}
    per_printer[printer.sn] = {
        "source": source,
        "external": merged_external,
        "integration": merged_integration,
    }
    root["per_printer"] = per_printer
    cfg.camera = root
    return resolve_camera_settings(cfg, printer_index)


def runtime_camera_state(camera_settings):
    external = camera_settings.get("external") or {}
    stream_url = str(external.get("stream_url") or "")
    return {
        "source": camera_settings.get("source"),
        "effective_source": camera_settings.get("effective_source"),
        "printer_supported": bool(camera_settings.get("printer_supported")),
        "feature_available": bool(camera_settings.get("feature_available")),
        "detail": camera_settings.get("detail"),
        "external_name": external.get("name") or None,
        "external_configured": bool(external.get("configured")),
        "external_refresh_sec": external.get("refresh_sec") or DEFAULT_EXTERNAL_REFRESH_SEC,
        "external_stream_preview": bool(stream_url),
        "integration_enabled": bool((camera_settings.get("integration") or {}).get("enabled")),
    }


def create_temp_snapshot_file():
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    temp_path = temp_file.name
    temp_file.close()
    return temp_path


def build_printer_video_url(host, port, api_key=None, *, for_timelapse=False, printer_index=None):
    url = f"http://{host}:{port}/video"
    query = []
    if for_timelapse:
        query.append("for_timelapse=1")
    if printer_index is not None:
        query.append(f"printer_index={int(printer_index)}")
    if api_key:
        query.append(f"apikey={quote(api_key, safe='')}")
    if query:
        url = f"{url}?{'&'.join(query)}"
    return url


def build_printer_integration_stream_url(base_url, api_key=None, *, printer_index=None):
    base = str(base_url or "").rstrip("/")
    url = f"{base}{PRINTER_INTEGRATION_STREAM_PATH}" if base else PRINTER_INTEGRATION_STREAM_PATH
    query = []
    if printer_index is not None:
        query.append(f"printer_index={int(printer_index)}")
    if api_key:
        query.append(f"apikey={quote(api_key, safe='')}")
    if query:
        url = f"{url}?{'&'.join(query)}"
    return url


def _scale_filter(scale):
    if not scale:
        return None
    width, height = scale
    return (
        f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2"
    )


def _run_ffmpeg_snapshot(ffmpeg_path, input_url, output_path, *, timeout, input_args=None, format_hint=None, scale=None):
    attempts = []
    if format_hint:
        attempts.append(["-f", format_hint])
    attempts.append([])

    last_stderr = ""
    scale_filter = _scale_filter(scale)
    for hint_args in attempts:
        cmd = [ffmpeg_path, "-loglevel", "error", "-nostdin", "-y"]
        if input_args:
            cmd.extend(input_args)
        cmd.extend(hint_args)
        cmd.extend(["-i", input_url, "-frames:v", "1"])
        if scale_filter:
            cmd.extend(["-vf", scale_filter])
        cmd.append(output_path)
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return
        last_stderr = result.stderr.decode("utf-8", errors="replace").strip()

    try:
        os.remove(output_path)
    except OSError:
        pass
    raise CameraCaptureError(_scrub_url_credentials(last_stderr) or "Snapshot capture failed")


def _external_input_url(camera_settings):
    external = (camera_settings or {}).get("external") or {}
    return external.get("snapshot_url") or external.get("stream_url") or ""


def external_stream_url(camera_settings):
    external = (camera_settings or {}).get("external") or {}
    return str(external.get("stream_url") or "").strip()


def _rtsp_snapshot_input_arg_attempts():
    return [
        ["-rtsp_transport", "tcp", *RTSP_LOW_LATENCY_INPUT_ARGS],
        [*RTSP_LOW_LATENCY_INPUT_ARGS],
    ]


def _mjpeg_filter(scale):
    filters = []
    scale_filter = _scale_filter(scale)
    if scale_filter:
        filters.append(scale_filter)
    return ",".join(filters) if filters else None


def open_external_mjpeg_stream(ffmpeg_path, input_url, *, scale=None):
    input_url = str(input_url or "").strip()
    if not input_url:
        raise CameraCaptureError("External camera live preview requires a stream URL.")

    cmd = [
        ffmpeg_path,
        "-loglevel",
        "error",
        "-nostdin",
    ]
    if input_url.lower().startswith("rtsp://"):
        cmd.extend(["-rtsp_transport", "tcp", *RTSP_LOW_LATENCY_INPUT_ARGS])
    cmd.extend(["-i", input_url, "-an", "-sn", "-dn"])
    vf = _mjpeg_filter(scale)
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(["-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1"])

    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CameraCaptureError(f"External camera stream could not start ffmpeg: {exc}") from exc


def open_printer_fmp4_stream(ffmpeg_path, *, input_fps=PRINTER_STREAM_INPUT_FPS):
    try:
        fps = max(1, int(float(input_fps)))
    except (TypeError, ValueError):
        fps = PRINTER_STREAM_INPUT_FPS

    cmd = [
        ffmpeg_path,
        "-loglevel",
        "error",
        "-nostdin",
        "-fflags",
        "+genpts",
        "-r",
        str(fps),
        "-f",
        "h264",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "copy",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration",
        "1000000",
        "-f",
        "mp4",
        "pipe:1",
    ]

    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CameraCaptureError(f"Printer integration stream could not start ffmpeg: {exc}") from exc


def iter_mjpeg_frames(proc, *, chunk_size=8192, max_buffer=4 * 1024 * 1024, stale_timeout=_MJPEG_STALE_READ_TIMEOUT_SEC):
    stdout = getattr(proc, "stdout", None)
    if stdout is None:
        return

    chunks = queue.Queue(maxsize=_MJPEG_READER_QUEUE_SIZE)

    def _reader():
        try:
            while True:
                chunk = stdout.read(chunk_size)
                while True:
                    try:
                        chunks.put(chunk, timeout=0.5)
                        break
                    except queue.Full:
                        if getattr(proc, "poll", lambda: None)() is not None:
                            return
                if not chunk:
                    break
        except Exception as exc:
            try:
                chunks.put(exc, timeout=0.5)
            except queue.Full:
                pass
        finally:
            try:
                chunks.put(_MJPEG_READ_DONE, timeout=0.5)
            except queue.Full:
                pass

    threading.Thread(target=_reader, daemon=True, name="external-mjpeg-reader").start()

    buffer = bytearray()
    while True:
        try:
            read_timeout = max(0.001, float(stale_timeout))
        except (TypeError, ValueError):
            read_timeout = _MJPEG_STALE_READ_TIMEOUT_SEC
        try:
            chunk = chunks.get(timeout=read_timeout)
        except queue.Empty:
            break
        if chunk is _MJPEG_READ_DONE:
            break
        if isinstance(chunk, Exception):
            break
        if not chunk:
            break
        buffer.extend(chunk)

        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                if len(buffer) > max_buffer:
                    del buffer[:-2]
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start > 0:
                    del buffer[:start]
                break
            frame = bytes(buffer[start:end + 2])
            del buffer[:end + 2]
            yield frame


def iter_process_chunks(
    proc,
    *,
    stream_attr="stdout",
    chunk_size=65536,
    stale_timeout=_PROCESS_STREAM_STALE_READ_TIMEOUT_SEC,
):
    stream = getattr(proc, stream_attr, None)
    if stream is None:
        return

    chunks = queue.Queue(maxsize=_PROCESS_STREAM_READER_QUEUE_SIZE)

    def _reader():
        try:
            while True:
                chunk = stream.read(chunk_size)
                while True:
                    try:
                        chunks.put(chunk, timeout=0.5)
                        break
                    except queue.Full:
                        if getattr(proc, "poll", lambda: None)() is not None:
                            return
                if not chunk:
                    break
        except Exception as exc:
            try:
                chunks.put(exc, timeout=0.5)
            except queue.Full:
                pass
        finally:
            try:
                chunks.put(_PROCESS_STREAM_READ_DONE, timeout=0.5)
            except queue.Full:
                pass

    threading.Thread(target=_reader, daemon=True, name=f"{stream_attr}-reader").start()

    while True:
        try:
            read_timeout = max(0.001, float(stale_timeout))
        except (TypeError, ValueError):
            read_timeout = _PROCESS_STREAM_STALE_READ_TIMEOUT_SEC
        try:
            chunk = chunks.get(timeout=read_timeout)
        except queue.Empty:
            break
        if chunk is _PROCESS_STREAM_READ_DONE:
            break
        if isinstance(chunk, Exception):
            break
        if not chunk:
            break
        yield chunk


def stop_external_mjpeg_stream(proc):
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass


def stop_subprocess(proc):
    stop_external_mjpeg_stream(proc)


def capture_camera_snapshot_to_file(
    camera_settings,
    ffmpeg_path,
    output_path,
    *,
    host,
    port,
    api_key=None,
    timeout=30,
    for_timelapse=False,
    scale=None,
):
    effective_source = (camera_settings or {}).get("effective_source")
    if effective_source == CAMERA_SOURCE_PRINTER:
        input_url = build_printer_video_url(
            host,
            port,
            api_key,
            for_timelapse=for_timelapse,
            printer_index=(camera_settings or {}).get("printer_index"),
        )
        _run_ffmpeg_snapshot(
            ffmpeg_path,
            input_url,
            output_path,
            timeout=timeout,
            format_hint="h264",
            scale=scale,
        )
        return

    if effective_source == CAMERA_SOURCE_EXTERNAL:
        input_url = _external_input_url(camera_settings)
        if not input_url:
            raise CameraCaptureError("External camera is selected, but no stream or snapshot URL is configured.")
        rtsp_url = input_url.lower().startswith("rtsp://")
        attempt_input_args = _rtsp_snapshot_input_arg_attempts() if rtsp_url else [[]]
        last_error = None
        for input_args in attempt_input_args:
            try:
                _run_ffmpeg_snapshot(
                    ffmpeg_path,
                    input_url,
                    output_path,
                    timeout=timeout,
                    input_args=input_args,
                    scale=scale,
                )
                return
            except CameraCaptureError as exc:
                last_error = exc
        raise last_error or CameraCaptureError("Snapshot capture failed")

    source = (camera_settings or {}).get("source")
    if source == CAMERA_SOURCE_EXTERNAL:
        raise CameraCaptureError("External camera is selected, but no stream or snapshot URL is configured.")
    raise CameraCaptureError("No camera source is available for this printer.")
