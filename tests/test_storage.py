from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage import LocalSnapshotStore


class StorageTest(unittest.TestCase):
    def test_automatic_snapshot_is_member_visible_and_publish_is_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LocalSnapshotStore(str(Path(folder) / "snapshots.sqlite3"))
            payload = {"sport": "mlb", "events": [{"event": {"event_id": "one"}}]}
            store.save_automatic("mlb", "2026-10-19", payload)
            visible, label = store.get_member_snapshot("mlb", "2026-10-19")
            self.assertEqual(visible, payload)
            self.assertIn("自動", label)
            store.publish_automatic("mlb", "2026-10-19")
            visible, label = store.get_member_snapshot("mlb", "2026-10-19")
            self.assertEqual(visible, payload)
            self.assertIn("發布", label)


if __name__ == "__main__":
    unittest.main()
