import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vod_dashboard.auto_vod_storage import (
    GIB,
    MINIMUM_FREE_BYTES,
    assess_auto_vod_storage,
    required_free_bytes,
)


class AutoVodStoragePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "downloads" / "nested"

    def tearDown(self):
        self.temp.cleanup()

    def test_fixed_fifty_gib_reserve_dominates_a_226_gib_filesystem(self):
        total = 226 * GIB
        self.assertEqual(required_free_bytes(total), MINIMUM_FREE_BYTES)
        status = assess_auto_vod_storage(
            self.root,
            disk_usage=lambda path: SimpleNamespace(
                free=MINIMUM_FREE_BYTES, total=total
            ),
        )
        self.assertEqual(status.state, "sufficient")
        self.assertTrue(status.allows_start)

    def test_fifteen_percent_dominates_on_a_large_filesystem(self):
        total = 400 * GIB
        reserve = 60 * GIB
        self.assertEqual(required_free_bytes(total), reserve)
        status = assess_auto_vod_storage(
            self.root,
            disk_usage=lambda path: SimpleNamespace(free=reserve - 1, total=total),
        )
        self.assertEqual(status.state, "insufficient")
        self.assertEqual(status.required_free_bytes, reserve)

    def test_exact_threshold_is_allowed_and_lower_is_not(self):
        total = 100 * GIB
        reserve = required_free_bytes(total)
        exact = assess_auto_vod_storage(
            self.root,
            disk_usage=lambda path: SimpleNamespace(free=reserve, total=total),
        )
        below = assess_auto_vod_storage(
            self.root,
            disk_usage=lambda path: SimpleNamespace(free=reserve - 1, total=total),
        )
        self.assertEqual(exact.state, "sufficient")
        self.assertEqual(below.state, "insufficient")

    def test_measurement_error_fails_closed_without_path_or_exception_data(self):
        secret = "C:/private/secret-volume"

        def broken(path):
            raise OSError(f"unavailable: {secret}")

        status = assess_auto_vod_storage(Path(secret), disk_usage=broken)
        self.assertEqual(status.state, "unavailable")
        self.assertFalse(status.allows_start)
        self.assertIsNone(status.free_bytes)
        self.assertIsNone(status.total_bytes)
        self.assertIsNone(status.required_free_bytes)
        self.assertNotIn(secret, repr(status))


if __name__ == "__main__":
    unittest.main()
