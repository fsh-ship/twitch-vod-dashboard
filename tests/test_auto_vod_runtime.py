import threading
import time
import unittest
from datetime import datetime, timezone

from vod_dashboard.auto_vod_runtime import AutoVodMonitor


class Coordinator:
    def __init__(self, result=None, block=False):
        self.result = result or {"enabled": True, "state_healthy": True, "action": "checked", "error_count": 0}
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = block

    def run_once(self):
        self.calls += 1
        self.entered.set()
        if self.block:
            self.release.wait(1)
        return self.result


class AutoVodRuntimeTests(unittest.TestCase):
    def monitor(self, coordinator, settings=None, **kwargs):
        kwargs.setdefault("startup_delay_seconds", 0)
        return AutoVodMonitor(
            coordinator,
            settings_provider=lambda: settings or {"auto_vod_poll_minutes": 60},
            **kwargs,
        )

    def test_start_is_idempotent_and_status_is_safe_before_first_cycle(self):
        coordinator = Coordinator()
        monitor = self.monitor(coordinator, startup_delay_seconds=0.2)
        self.assertTrue(monitor.start())
        self.assertFalse(monitor.start())
        status = monitor.snapshot()
        self.assertTrue(status["running"])
        self.assertFalse(status["in_progress"])
        self.assertIsNone(status["last_started_at"])
        self.assertTrue(monitor.stop(timeout=1))

    def test_wake_interrupts_wait_and_wakes_coalesce_without_overlap(self):
        coordinator = Coordinator(block=True)
        monitor = self.monitor(coordinator)
        self.assertTrue(monitor.start())
        self.assertTrue(coordinator.entered.wait(1))
        self.assertTrue(monitor.wake())
        self.assertTrue(monitor.wake())
        self.assertEqual(coordinator.calls, 1)
        coordinator.release.set()
        deadline = time.monotonic() + 1
        while coordinator.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(coordinator.calls, 2)
        self.assertTrue(monitor.stop(timeout=1))

    def test_exception_and_disabled_or_unhealthy_results_wait_normally(self):
        class Flaky(Coordinator):
            def run_once(self):
                self.calls += 1
                self.entered.set()
                if self.calls == 1:
                    raise RuntimeError("private")
                return {"enabled": False, "state_healthy": False, "action": "state_unhealthy", "error_count": 1}

        coordinator = Flaky()
        monitor = self.monitor(coordinator)
        monitor.start()
        self.assertTrue(coordinator.entered.wait(1))
        monitor.wake()
        deadline = time.monotonic() + 1
        while coordinator.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        status = monitor.snapshot()
        self.assertEqual(coordinator.calls, 2)
        self.assertEqual(status["last_result"]["action"], "state_unhealthy")
        self.assertTrue(monitor.stop(timeout=1))

    def test_interval_is_read_fresh_for_each_wait_cycle(self):
        settings = {"auto_vod_poll_minutes": 120}
        coordinator = Coordinator()
        monitor = self.monitor(coordinator, settings)
        monitor.start()
        self.assertTrue(coordinator.entered.wait(1))
        deadline = time.monotonic() + 1
        while monitor.snapshot()["next_check_at"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(monitor.snapshot()["next_check_at"])
        self.assertTrue(monitor.stop(timeout=1))


if __name__ == "__main__":
    unittest.main()
