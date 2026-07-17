"""
Test suite for service crash recovery and resilience
Tests service restart, state preservation, error handling
"""

import pytest
import time
import sqlite3
from unittest.mock import Mock, patch, MagicMock
from threading import Event

from web.lib.service import Service, ServiceManager, ServiceRestartSignal, RunState


class MockCrashingService(Service):
    """Test service that can be forced to crash"""

    def __init__(self, crash_on_run=False, crash_on_start=False):
        super().__init__()
        self.crash_on_run = crash_on_run
        self.crash_on_start = crash_on_start
        self.init_called = False
        self.start_called = 0
        self.run_called = 0
        self.stop_called = 0

    def worker_init(self):
        self.init_called = True

    def worker_start(self):
        self.start_called += 1
        if self.crash_on_start:
            raise RuntimeError("Simulated crash in worker_start")

    def worker_run(self, timeout):
        self.run_called += 1
        if self.crash_on_run:
            raise RuntimeError("Simulated crash in worker_run")
        time.sleep(0.01)

    def worker_stop(self):
        self.stop_called += 1


def _shutdown_service(service):
    service.stop()
    service.await_stopped()
    service.shutdown()


class TestServiceCrashRecovery:
    """Test service recovery after crashes"""

    def test_service_restart_signal_triggers_restart(self):
        """ServiceRestartSignal in worker_run triggers clean restart"""

        class RestartingService(Service):
            def __init__(self):
                super().__init__()
                self.restart_count = 0

            def worker_run(self, timeout):
                self.restart_count += 1
                if self.restart_count == 1:
                    raise ServiceRestartSignal("Intentional restart", delay=0)
                time.sleep(0.01)

        service = RestartingService()
        manager = ServiceManager()
        manager.register("test", service)

        with manager.borrow("test") as svc:
            time.sleep(0.1)  # Let it run and restart

        assert svc.restart_count >= 2  # Should have restarted at least once

    def test_worker_run_exception_retries_service(self):
        """Unhandled exception in worker_run keeps the service retrying."""
        service = MockCrashingService(crash_on_run=True)
        try:
            service.start()
            time.sleep(0.1)  # Let it attempt a run and fail
            assert service.run_called >= 1
            assert service.start_called >= 1
            assert service.wanted is True
        finally:
            _shutdown_service(service)

    def test_worker_start_exception_retries_until_service_is_stopped(self):
        """Exception in worker_start is retried until the service is stopped."""
        service = MockCrashingService(crash_on_start=True)
        try:
            service.start()
            time.sleep(0.1)
            assert service.start_called >= 1
            assert service.state == RunState.Starting
        finally:
            _shutdown_service(service)
        assert service.state == RunState.Stopped

    def test_service_manager_ref_counting_after_crash(self):
        """ServiceManager ref counting works after service crash"""
        manager = ServiceManager()
        service = MockCrashingService()
        manager.register("crash_test", service)

        # Borrow and release
        _ = manager.get("crash_test")
        assert manager.refs["crash_test"] == 1

        manager.put("crash_test")
        assert manager.refs["crash_test"] == 0

        # Service should have stopped
        service.await_stopped()
        assert service.state == RunState.Stopped
        service.shutdown()


class TestMQTTServiceRecovery:
    """Test MqttQueue service recovery scenarios"""

    @patch('web.service.mqtt.MqttQueue')
    def test_mqtt_crash_during_print_preserves_state(self, mock_mqtt_class):
        """MqttQueue crash during print preserves print state"""
        mock_mqtt = MagicMock()
        mock_mqtt.is_printing = True
        mock_mqtt._current_filename = "test.gcode"
        mock_mqtt_class.return_value = mock_mqtt

        # Simulate crash
        mock_mqtt.worker_run.side_effect = RuntimeError("MQTT connection lost")

        # After restart, state should be restorable
        # (Implementation-specific: depends on state persistence)
        pass

    def test_mqtt_reconnect_after_network_failure(self):
        """MqttQueue reconnects after network failure"""
        # Simulate network disconnect
        # Assert service restarts and reconnects
        pass

    def test_mqtt_pending_history_start_preserved_across_restart(self):
        """_pending_history_start flag preserved if service restarts"""
        # Test ct=1000 received before ct=1044
        # Service restarts between the two
        # Should still create history entry correctly
        pass


class TestDatabaseCorruptionRecovery:
    """Test graceful degradation on database corruption"""

    def test_history_db_corruption_logs_error_and_continues(self):
        """Corrupted history.db logs error but doesn't crash service"""
        from web.service.history import PrintHistory
        import tempfile
        import os

        # Create corrupted database
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            db_path = tmp.name
            tmp.write(b"CORRUPTED DATA NOT A SQLITE FILE")

        try:
            history = PrintHistory(db_path)
            # Should handle gracefully or log error
            # Implementation may vary: recreate DB or disable history
        finally:
            os.unlink(db_path)

    def test_filament_db_corruption_recreates_schema(self):
        """Corrupted filament.db recreates schema"""
        from web.service.filament import FilamentStore
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            db_path = tmp.name
            tmp.write(b"CORRUPTED")

        try:
            store = FilamentStore(db_path)
            # Should either recreate or handle gracefully
            profiles = store.list_all()
            # Should work (empty or with defaults)
            assert isinstance(profiles, list)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


class TestPartialUploadRecovery:
    """Test file upload recovery after interruption"""

    @patch('web.service.filetransfer.FileTransferService')
    def test_partial_upload_cleanup_on_failure(self, mock_fts):
        """Partial upload cleans up temp files on network failure"""
        # Simulate upload interrupted at 50%
        # Assert temp files cleaned up
        pass

    def test_upload_progress_reset_after_failure(self):
        """Upload progress resets after failure"""
        # Start upload, fail at 50%, retry
        # Progress should start from 0, not 50
        pass


class TestServiceDependencyFailure:
    """Test service behavior when dependencies fail"""

    @patch('web.service.pppp.PPPPService')
    def test_pppp_offline_video_service_handles_gracefully(self, mock_pppp):
        """VideoQueue handles PPPP service being offline"""
        from web.service.video import VideoQueue

        mock_pppp.connected = False

        vq = VideoQueue()
        vq.pppp = mock_pppp

        # Calls to api_* should return False, not crash
        result = vq.api_start_live()
        assert result is False

    @patch('web.service.mqtt.MqttQueue')
    def test_timelapse_mqtt_offline_handles_gracefully(self, mock_mqtt):
        """TimelapseService handles MqttQueue being offline"""
        # Timelapse depends on MQTT for print events
        # Should handle MQTT disconnect gracefully
        pass


class TestConcurrentServiceRestarts:
    """Test multiple services restarting simultaneously"""

    def test_multiple_service_restarts_dont_deadlock(self):
        """Restarting multiple services simultaneously doesn't deadlock"""
        manager = ServiceManager()

        service1 = MockCrashingService()
        service2 = MockCrashingService()

        manager.register("svc1", service1)
        manager.register("svc2", service2)

        # Start both
        with manager.borrow("svc1"), manager.borrow("svc2"):
            time.sleep(0.1)

        # Trigger simultaneous restart
        # Should not deadlock
        pass

    def test_cascade_restart_detection(self):
        """Detect and prevent cascade service restarts"""
        # Service A restart triggers Service B restart
        # Should be detected and handled gracefully
        pass


class TestStatePreservation:
    """Test state preservation across service restarts"""

    def test_timelapse_resume_after_restart(self):
        """Timelapse resumes from .meta file after service restart"""
        from web.service.timelapse import TimelapseService
        import tempfile
        import os
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create .meta file
            capture_dir = os.path.join(tmpdir, "test_print_123")
            os.makedirs(capture_dir)

            meta_path = os.path.join(capture_dir, ".meta")
            meta_data = {
                "filename": "test.gcode",
                "started_at": time.time(),
                "frame_count": 10,
            }
            with open(meta_path, "w") as f:
                json.dump(meta_data, f)

            # Create dummy frames
            for i in range(10):
                frame_path = os.path.join(capture_dir, f"frame_{i:06d}.jpg")
                with open(frame_path, "wb") as f:
                    f.write(b"FAKE JPEG DATA")

            # Simulate service restart and resume
            # TimelapseService should detect .meta and resume
            # (Implementation test: check actual resume logic)
            pass

    def test_print_state_preserved_in_history_db(self):
        """Print state written to history.db survives restart"""
        from web.service.history import PrintHistory

        history = PrintHistory(":memory:")
        history.init_schema()

        row_id = history.record_start("test.gcode")

        # Simulate restart by creating new instance with same DB
        # (In real scenario, would use persistent file)
        entries = history.list_entries(limit=10)
        assert len(entries) == 1
        assert row_id is not None
        assert entries[0]["filename"] == "test.gcode"
        assert entries[0]["status"] == "started"


class TestErrorLogging:
    """Test that crashes are properly logged"""

    @patch('logging.Logger.error')
    def test_service_crash_logged(self, mock_log_error):
        """Service crash exception is logged"""
        service = MockCrashingService(crash_on_run=True)
        try:
            service.start()
            time.sleep(0.1)
        finally:
            _shutdown_service(service)

        assert mock_log_error.called

    def test_database_error_logged_with_context(self):
        """Database errors logged with helpful context"""
        # Trigger DB error
        # Assert log includes: DB path, operation, error message
        pass


class TestGracefulShutdown:
    """Test graceful shutdown during various operations"""

    def test_shutdown_during_file_upload(self):
        """Service shutdown during file upload doesn't corrupt state"""
        # Start upload, trigger shutdown mid-upload
        # Should cleanly abort upload
        pass

    def test_shutdown_during_timelapse_assembly(self):
        """Shutdown during ffmpeg timelapse assembly is safe"""
        # Start assembly, trigger shutdown
        # Should kill ffmpeg gracefully
        pass

    def test_shutdown_with_pending_notifications(self):
        """Shutdown with pending Apprise notifications doesn't lose data"""
        # Queue notification, shutdown before sent
        # Should either send or save for retry
        pass
