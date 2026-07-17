import logging
import time
import uuid

from ..lib.service import Service
from .. import app, borrow_mqtt

from libflagship.pppp import FileTransfer
from libflagship.ppppapi import FileUploadInfo, PPPPError

import cli.util
import cli.pppp
from cli.util import patch_gcode_time, extract_filament_info, extract_layer_count
from .pppp import reserve_session

log = logging.getLogger(__name__)

from libflagship.notifications.events import EVENT_GCODE_UPLOADED
from ..notifications import AppriseNotifier, format_bytes


class FileTransferService(Service):

    REPLY_TIMEOUT = 10.0
    # Generous window for the initial connect, since it now retries (see
    # cli.pppp.pppp_open) instead of failing on the printer's first
    # rejection — the printer can need a few seconds to free its PPPP
    # session slot after reserve_session() stops the shared video session.
    # Field data showed a genuinely active video stream can hold the
    # printer's session slot for well over 25s; uploads are not
    # latency-sensitive, so a slow success beats a hard failure.
    CONNECT_TIMEOUT = 60.0
    PROGRESS_INTERVAL = 0.25

    def worker_init(self):
        self._notifier = AppriseNotifier(app.config["config"])

    def worker_run(self, timeout):
        self.idle(timeout=timeout)

    def _notify_upload(self, payload):
        try:
            self.notify(payload)
        except Exception as e:
            log.warning(f"Upload progress notify failed: {e}")

    def send_file(self, fd, user_name, rate_limit_mbps=None, start_print=True, printer_index=None):
        raw = fd.read()
        return self.send_bytes(
            raw,
            fd.filename,
            user_name,
            rate_limit_mbps=rate_limit_mbps,
            start_print=start_print,
            printer_index=printer_index,
        )

    def send_bytes(
        self,
        raw,
        filename,
        user_name,
        rate_limit_mbps=None,
        start_print=True,
        printer_index=None,
        archive_info=None,
    ):
        layer_count = extract_layer_count(raw)
        filament_info = extract_filament_info(raw)
        data = patch_gcode_time(raw)
        start_print_flag = bool(start_print)
        if layer_count is not None or filament_info:
            try:
                with borrow_mqtt(printer_index) as mqtt:
                    if layer_count is not None:
                        mqtt.set_gcode_layer_count(layer_count)
                    if filament_info:
                        mqtt.set_gcode_filament_info(**filament_info)
                if layer_count is not None:
                    log.info(f"GCode layer count from header: {layer_count}")
                if filament_info:
                    log.info(
                        "GCode filament metadata: vendor=%r type=%r",
                        filament_info.get("vendor"),
                        filament_info.get("type"),
                    )
            except Exception as e:
                log.warning(f"Could not store GCode metadata in mqttqueue: {e}")
        user_id = "-"
        try:
            with app.config["config"].open() as cfg:
                if cfg and cfg.account and cfg.account.user_id:
                    user_id = cfg.account.user_id
        except Exception:
            pass
        file_uuid = uuid.uuid4().hex.upper()
        fui = FileUploadInfo.from_data(data, filename, user_name=user_name, user_id=user_id, machine_id=file_uuid)
        log.info(f"Going to upload {fui.size} bytes as {fui.name!r}")
        upload_name = fui.name
        if start_print_flag and archive_info is None:
            try:
                with borrow_mqtt(printer_index) as mqtt:
                    history = getattr(mqtt, "history", None)
                    if history and hasattr(history, "archive_upload"):
                        archive_info = history.archive_upload(upload_name, data)
            except Exception as e:
                log.warning(f"Could not archive uploaded GCode locally: {e}")
        self._notify_upload({"status": "start", "name": upload_name, "size": fui.size, "start_print": start_print_flag})
        if rate_limit_mbps:
            log.info(f"Using upload rate limit: {rate_limit_mbps} Mbps")
        pppp_dump = app.config.get("pppp_dump")
        last_emit = 0.0

        def progress_cb(sent, total):
            nonlocal last_emit
            now = time.monotonic()
            if sent < total and now - last_emit < self.PROGRESS_INTERVAL:
                return
            last_emit = now
            self._notify_upload({
                "status": "progress",
                "name": upload_name,
                "size": total,
                "sent": sent,
                "start_print": start_print_flag,
            })
        effective_printer_index = printer_index if printer_index is not None else app.config.get("printer_index", 0)
        with reserve_session(effective_printer_index):
            try:
                api = cli.pppp.pppp_open(
                    app.config["config"],
                    effective_printer_index,
                    timeout=self.CONNECT_TIMEOUT,
                    dumpfile=pppp_dump,
                )
            except Exception as e:
                self._notify_upload({"status": "error", "name": upload_name, "error": str(e), "start_print": start_print_flag})
                raise ConnectionError(f"No pppp connection to printer: {e}") from e
            try:
                cli.pppp.pppp_send_file(
                    api,
                    fui,
                    data,
                    rate_limit_mbps=rate_limit_mbps,
                    progress_cb=progress_cb,
                    show_progress=False,
                )
                if start_print:
                    log.info("File upload complete. Requesting print start of job.")
                    api.aabb_request(b"", frametype=FileTransfer.END, timeout=15.0)
                    try:
                        with borrow_mqtt(printer_index) as mqtt:
                            try:
                                mqtt.mark_pending_print_start(upload_name, archive_info=archive_info)
                            except TypeError:
                                mqtt.mark_pending_print_start(upload_name)
                    except Exception as e:
                        log.warning(f"Could not mark pending print start in mqttqueue: {e}")
                else:
                    log.info("File upload complete (upload-only)")
            except ConnectionError as e:
                log.error(f"Could not send print job: {e}")
                self._notify_upload({"status": "error", "name": upload_name, "error": str(e), "start_print": start_print_flag})
                raise
            except (PPPPError, OSError, EOFError, TimeoutError) as e:
                log.error(f"Could not send print job: {e}")
                self._notify_upload({"status": "error", "name": upload_name, "error": str(e), "start_print": start_print_flag})
                raise ConnectionError(f"PPPP transfer failed: {e}") from e
            except Exception as e:
                log.error(f"Could not send print job: {e}")
                self._notify_upload({"status": "error", "name": upload_name, "error": str(e), "start_print": start_print_flag})
                raise
            else:
                if start_print:
                    log.info("Successfully sent print job")
                else:
                    log.info("Successfully uploaded file")
                self._notify_upload({
                    "status": "done",
                    "name": upload_name,
                    "size": fui.size,
                    "sent": fui.size,
                    "start_print": start_print_flag,
                })
                self._notify_apprise_upload(upload_name, fui.size, start_print)
            finally:
                api.stop()

    def _notify_apprise_upload(self, filename, size_bytes, start_print):
        payload = {
            "filename": filename,
            "size": format_bytes(size_bytes),
            "size_bytes": size_bytes,
            "start_print": bool(start_print),
        }
        self._notifier.send(EVENT_GCODE_UPLOADED, payload=payload)
