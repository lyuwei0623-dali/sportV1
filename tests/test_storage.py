from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage import LocalSnapshotStore, SupabaseSnapshotStore


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
            self.assertIn("管理員確認", label)

    def test_manual_release_has_priority_over_later_automatic_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LocalSnapshotStore(str(Path(folder) / "snapshots.sqlite3"))
            automatic = {"sport": "football", "events": [{"event": {"event_id": "one"}}], "version": "auto"}
            manual = {**automatic, "release_type": "manual_calibrated", "version": "manual"}
            store.save_automatic("football", "2026-10-19", automatic)
            store.save_manual_published("football", "2026-10-19", manual)
            store.save_automatic("football", "2026-10-19", {**automatic, "version": "new-auto"})
            visible, label = store.get_member_snapshot("football", "2026-10-19")
            self.assertEqual(visible, manual)
            self.assertEqual(label, "人工校正正式推薦")

    def test_current_secret_key_uses_apikey_header_without_bearer_jwt(self):
        store = SupabaseSnapshotStore("https://example.supabase.co", "sb_secret_test")
        self.assertEqual(store.headers["apikey"], "sb_secret_test")
        self.assertNotIn("Authorization", store.headers)


if __name__ == "__main__":
    unittest.main()
