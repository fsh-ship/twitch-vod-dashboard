import os
import runpy
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_IMPORT_TMP = None
_OLD_ENV = {}
if "app" not in sys.modules:
    _IMPORT_TMP = tempfile.TemporaryDirectory()
    import_base = Path(_IMPORT_TMP.name)
    for name in (
        "VOD_DASHBOARD_MEDIA_ROOT",
        "VOD_DASHBOARD_DIR",
        "VOD_DASHBOARD_SETTINGS",
        "VOD_DASHBOARD_AUTH_DISABLED",
        "VOD_DASHBOARD_LEGACY_SETTINGS_PATH",
    ):
        _OLD_ENV[name] = os.environ.get(name)
    os.environ["VOD_DASHBOARD_MEDIA_ROOT"] = str(import_base / "media")
    os.environ["VOD_DASHBOARD_DIR"] = str(import_base / "data")
    os.environ["VOD_DASHBOARD_SETTINGS"] = str(
        import_base / "data" / "settings.json"
    )
    os.environ["VOD_DASHBOARD_AUTH_DISABLED"] = "1"
    os.environ.pop("VOD_DASHBOARD_LEGACY_SETTINGS_PATH", None)

import app as dashboard
from vod_dashboard.auto_recording_runtime import AutoRecorderMonitor
from vod_dashboard.live_status import LiveStatusConcurrencyLimiter


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def tearDownModule():
    if _IMPORT_TMP is not None:
        _IMPORT_TMP.cleanup()
        for name, value in _OLD_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class FakeCoordinator:
    def __init__(self, run=None, prepare=None):
        self.prepare_calls = 0
        self.run_calls = 0
        self._run = run or (
            lambda: {
                "enabled": True,
                "state_healthy": True,
                "watched_count": 1,
                "error_count": 0,
                "action": "idle",
            }
        )
        self._prepare = prepare or (
            lambda: {
                "state_healthy": True,
                "action": "restart_reconciled",
            }
        )

    def prepare_after_restart(self):
        self.prepare_calls += 1
        return self._prepare()

    def run_once(self):
        self.run_calls += 1
        return self._run()


class AutoRecorderMonitorTests(unittest.TestCase):
    def test_startup_delay_prepare_once_no_overlap_and_post_run_interval(self):
        entered = threading.Event()
        release = threading.Event()
        starts = []
        completions = []
        active = 0
        maximum = 0
        lock = threading.Lock()

        def run():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                starts.append(time.monotonic())
            entered.set()
            if len(starts) == 1:
                release.wait(timeout=2)
            with lock:
                active -= 1
                completions.append(time.monotonic())
            return {
                "enabled": True,
                "state_healthy": True,
                "watched_count": 2,
                "error_count": 0,
                "action": "idle",
            }

        coordinator = FakeCoordinator(run=run)
        monitor = AutoRecorderMonitor(
            coordinator,
            startup_delay_seconds=0.05,
            interval_seconds=0.04,
        )
        started_at = time.monotonic()
        self.assertTrue(monitor.start())
        self.assertFalse(monitor.start())
        time.sleep(0.015)
        self.assertEqual(coordinator.run_calls, 0)
        self.assertTrue(entered.wait(timeout=1))
        self.assertGreaterEqual(starts[0] - started_at, 0.035)
        time.sleep(0.06)
        self.assertEqual(coordinator.run_calls, 1)
        release.set()
        self.assertTrue(wait_until(lambda: coordinator.run_calls >= 2))
        self.assertEqual(maximum, 1)
        self.assertGreaterEqual(starts[1] - completions[0], 0.025)
        self.assertEqual(coordinator.prepare_calls, 1)
        self.assertTrue(monitor.stop(timeout=1))
        self.assertTrue(monitor.stop(timeout=1))
        self.assertEqual(monitor.snapshot()["phase"], "stopped")

    def test_wake_and_stop_interrupt_long_waits(self):
        coordinator = FakeCoordinator()
        monitor = AutoRecorderMonitor(
            coordinator,
            startup_delay_seconds=60,
            interval_seconds=60,
        )
        monitor.start()
        self.assertTrue(monitor.wake())
        self.assertTrue(wait_until(lambda: coordinator.run_calls == 1))
        stopped_at = time.monotonic()
        self.assertTrue(monitor.stop(timeout=1))
        self.assertLess(time.monotonic() - stopped_at, 0.5)
        self.assertFalse(monitor.wake())

    def test_unexpected_exception_is_bounded_and_next_cycle_recovers(self):
        logs = []

        def run():
            if coordinator.run_calls == 1:
                raise RuntimeError(
                    "https://signed.invalid/?token=SECRET C:/private"
                )
            return {
                "enabled": True,
                "state_healthy": True,
                "watched_count": 1,
                "error_count": 0,
                "action": "recording_conflict",
                "streamer": "safe_streamer",
            }

        coordinator = FakeCoordinator(run=run)
        monitor = AutoRecorderMonitor(
            coordinator,
            startup_delay_seconds=0,
            interval_seconds=60,
            log=logs.append,
        )
        monitor.start()
        self.assertTrue(
            wait_until(
                lambda: monitor.snapshot()["last_error_code"]
                == "unexpected_iteration_error"
            )
        )
        degraded = monitor.snapshot()
        self.assertEqual(degraded["last_error_code"], "unexpected_iteration_error")
        self.assertTrue(monitor.wake())
        self.assertTrue(wait_until(lambda: coordinator.run_calls == 2))
        recovered = monitor.snapshot()
        self.assertTrue(recovered["running"])
        self.assertEqual(recovered["phase"], "sleeping")
        self.assertEqual(recovered["last_action"], "recording_conflict")
        self.assertEqual(recovered["last_action_streamer"], "safe_streamer")
        serialized = repr((logs, recovered))
        for secret in ("signed.invalid", "SECRET", "C:/private"):
            self.assertNotIn(secret, serialized)
        monitor.stop(timeout=1)

    def test_degraded_state_survives_and_later_result_recovers(self):
        results = [
            {
                "enabled": True,
                "state_healthy": False,
                "watched_count": 1,
                "error_count": 0,
                "action": "state_unhealthy",
                "reason": "invalid_json",
            },
            {
                "enabled": True,
                "state_healthy": True,
                "watched_count": 1,
                "error_count": 0,
                "action": "idle",
            },
        ]
        coordinator = FakeCoordinator(run=lambda: results.pop(0))
        monitor = AutoRecorderMonitor(
            coordinator, startup_delay_seconds=0, interval_seconds=60
        )
        monitor.start()
        self.assertTrue(
            wait_until(lambda: monitor.snapshot()["phase"] == "degraded")
        )
        self.assertEqual(monitor.snapshot()["phase"], "degraded")
        monitor.wake()
        self.assertTrue(wait_until(lambda: coordinator.run_calls == 2))
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["phase"], "sleeping")
        self.assertTrue(snapshot["state_healthy"])
        monitor.stop(timeout=1)

    def test_unhealthy_restart_prepare_is_degraded_but_monitor_survives(self):
        coordinator = FakeCoordinator(
            prepare=lambda: {
                "state_healthy": False,
                "action": "state_unhealthy",
                "reason": "invalid_json",
            }
        )
        monitor = AutoRecorderMonitor(
            coordinator, startup_delay_seconds=60, interval_seconds=60
        )
        monitor.start()
        self.assertTrue(
            wait_until(lambda: monitor.snapshot()["phase"] == "degraded")
        )
        self.assertEqual(coordinator.prepare_calls, 1)
        self.assertEqual(coordinator.run_calls, 0)
        monitor.wake()
        self.assertTrue(
            wait_until(lambda: monitor.snapshot()["phase"] == "sleeping")
        )
        self.assertEqual(monitor.snapshot()["phase"], "sleeping")
        monitor.stop(timeout=1)

    def test_disabled_result_is_paused_without_hidden_work(self):
        coordinator = FakeCoordinator(
            run=lambda: {
                "enabled": False,
                "state_healthy": None,
                "watched_count": 0,
                "error_count": 0,
                "action": "disabled",
            }
        )
        monitor = AutoRecorderMonitor(
            coordinator, startup_delay_seconds=0, interval_seconds=60
        )
        monitor.start()
        self.assertTrue(wait_until(lambda: coordinator.run_calls == 1))
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["phase"], "paused")
        self.assertFalse(snapshot["enabled"])
        snapshot["phase"] = "tampered"
        self.assertEqual(monitor.snapshot()["phase"], "paused")
        monitor.stop(timeout=1)

    def test_join_timeout_is_bounded_when_iteration_is_blocked(self):
        entered = threading.Event()
        release = threading.Event()

        def run():
            entered.set()
            release.wait(timeout=2)
            return {
                "enabled": True,
                "state_healthy": True,
                "watched_count": 1,
                "error_count": 0,
                "action": "idle",
            }

        monitor = AutoRecorderMonitor(
            FakeCoordinator(run=run),
            startup_delay_seconds=0,
            interval_seconds=60,
        )
        monitor.start()
        self.assertTrue(entered.wait(timeout=1))
        started = time.monotonic()
        self.assertFalse(monitor.stop(timeout=0.02))
        self.assertLess(time.monotonic() - started, 0.3)
        release.set()
        self.assertTrue(wait_until(lambda: not monitor.snapshot()["running"]))
        self.assertTrue(monitor.stop(timeout=1))


class LiveStatusLimiterTests(unittest.TestCase):
    def test_shared_callers_never_exceed_two_and_do_not_deadlock(self):
        limiter = LiveStatusConcurrencyLimiter(2)
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        maximum = 0
        completed = []

        def operation(caller):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            release.wait(timeout=2)
            with lock:
                active -= 1
                completed.append(caller)

        threads = [
            threading.Thread(
                target=limiter.run,
                args=(operation, caller),
                daemon=True,
            )
            for caller in ("ui-1", "ui-2", "monitor-1", "monitor-2")
        ]
        for thread in threads:
            thread.start()
        self.assertTrue(wait_until(lambda: maximum == 2))
        release.set()
        for thread in threads:
            thread.join(timeout=1)
        self.assertEqual(maximum, 2)
        self.assertCountEqual(
            completed, ["ui-1", "ui-2", "monitor-1", "monitor-2"]
        )

    def test_exception_releases_limiter_slot(self):
        limiter = LiveStatusConcurrencyLimiter(1)

        def fail():
            raise RuntimeError("SECRET")

        with self.assertRaises(RuntimeError):
            limiter.run(fail)
        self.assertEqual(limiter.run(lambda: "released"), "released")

    def test_production_factory_and_api_wrapper_share_limited_checker(self):
        expected = {"streamer": "nika", "state": "offline"}

        def execute(operation, *args, **kwargs):
            return operation(*args, **kwargs)

        with mock.patch.object(
            dashboard.dashboard_live_status.LIVE_STATUS_LIMITER,
            "run",
            side_effect=execute,
        ) as limited, mock.patch.object(
            dashboard.dashboard_twitch,
            "run_ytdlp_live_status",
            return_value=expected,
        ) as low_level:
            self.assertEqual(
                dashboard.run_ytdlp_live_status("nika", {}), expected
            )
        limited.assert_called_once()
        self.assertIs(limited.call_args.args[0], low_level)

        monitor = dashboard.create_auto_recorder_monitor()
        self.assertIs(
            monitor._coordinator._live_status_checker,
            dashboard.run_ytdlp_live_status,
        )

    def test_limiter_does_not_wrap_recording_command_construction(self):
        settings = dashboard.load_settings()
        with mock.patch.object(
            dashboard.dashboard_live_status.LIVE_STATUS_LIMITER,
            "run",
            side_effect=AssertionError("live limiter must not be used"),
        ):
            command = dashboard.build_live_recording_command(
                "nika", settings
            )
        self.assertEqual(command[-1], "https://www.twitch.tv/nika")


class GunicornLifecycleHookTests(unittest.TestCase):
    def setUp(self):
        self.config = runpy.run_path(
            str(REPOSITORY_ROOT / "gunicorn.conf.py")
        )

    @staticmethod
    def worker(count=1):
        return SimpleNamespace(
            cfg=SimpleNamespace(workers=count), log=mock.Mock()
        )

    def test_post_worker_init_starts_once_and_rejects_multiple_workers(self):
        worker = self.worker()
        with mock.patch.object(dashboard, "start_auto_recorder_monitor") as start:
            self.config["post_worker_init"](worker)
        start.assert_called_once_with()

        unsupported = self.worker(count=2)
        with mock.patch.object(dashboard, "start_auto_recorder_monitor") as start:
            self.config["post_worker_init"](unsupported)
        start.assert_not_called()
        unsupported.log.error.assert_called_once()

    def test_worker_exit_invokes_bounded_runtime_shutdown(self):
        with mock.patch.object(dashboard, "shutdown_worker_runtime") as shutdown:
            self.config["worker_exit"](mock.Mock(), self.worker())
        shutdown.assert_called_once_with()

    def test_app_monitor_start_wrapper_is_idempotent(self):
        fake_monitor = mock.Mock()
        previous = dashboard.AUTO_RECORDER_MONITOR
        try:
            dashboard.AUTO_RECORDER_MONITOR = None
            with mock.patch.object(
                dashboard,
                "create_auto_recorder_monitor",
                return_value=fake_monitor,
            ) as factory:
                first = dashboard.start_auto_recorder_monitor()
                second = dashboard.start_auto_recorder_monitor()
            self.assertIs(first, second)
            factory.assert_called_once_with()
            self.assertEqual(fake_monitor.start.call_count, 2)
        finally:
            dashboard.AUTO_RECORDER_MONITOR = previous

    def test_worker_shutdown_orders_monitor_before_recording_cleanup(self):
        calls = []
        manager = mock.Mock()
        manager.stop_recording_for_shutdown.side_effect = (
            lambda timeout: calls.append(("recording", timeout)) or True
        )
        with mock.patch.object(
            dashboard,
            "stop_auto_recorder_monitor",
            side_effect=lambda timeout: calls.append(("monitor", timeout)) or True,
        ), mock.patch.object(
            dashboard,
            "_job_manager_for_compatibility",
            return_value=manager,
        ):
            self.assertTrue(dashboard.shutdown_worker_runtime())
        self.assertEqual(calls, [("monitor", 5.0), ("recording", 50.0)])


if __name__ == "__main__":
    unittest.main()
