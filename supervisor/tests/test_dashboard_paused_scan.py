import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import dashboard


class PausedMarkerTest(unittest.TestCase):
    def test_directory_changes_invalidate_cached_pause_state(self):
        """Regression: sidebar poll rescanned thousands of scratch files."""
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(dashboard._paused_marker_cache, clear=True), \
                mock.patch("supervisor.dashboard.list_beneath", wraps=dashboard.list_beneath) as listing:
            root = Path(temporary)
            scratch = root / ".tmp"
            self.assertFalse(dashboard._paused_marker(root))
            scratch.mkdir()
            self.assertFalse(dashboard._paused_marker(root))
            calls = listing.call_count
            self.assertFalse(dashboard._paused_marker(root))
            self.assertEqual(listing.call_count, calls)

            timestamp = scratch.stat().st_mtime_ns
            marker = scratch / "paused-x"
            marker.touch()
            timestamp += 1_000_000_000
            os.utime(scratch, ns=(timestamp, timestamp))
            self.assertTrue(dashboard._paused_marker(root))
            self.assertEqual(listing.call_count, calls + 1)
            self.assertTrue(dashboard._paused_marker(root))
            self.assertEqual(listing.call_count, calls + 1)

            marker.unlink()
            timestamp += 1_000_000_000
            os.utime(scratch, ns=(timestamp, timestamp))
            self.assertFalse(dashboard._paused_marker(root))
            self.assertEqual(listing.call_count, calls + 2)


if __name__ == "__main__":
    unittest.main()
