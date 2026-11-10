import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from storage import LocalSnapshotStore, StorageError
from unittest.mock import MagicMock
from streamlit.testing.v1 import AppTest


class AppSmoke(unittest.TestCase):
    def test_admin_member_and_secret_rotation(self):
        with tempfile.TemporaryDirectory() as root:
            store=LocalSnapshotStore(str(Path(root)/"db"))
            with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"test-admin-only","APP_MEMBER_PASSWORD":"test-member-only"}),patch("storage.create_store",return_value=(store,"測試資料庫")):
                at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
                self.assertEqual(len(at.exception),0)
                at.radio[0].set_value("管理員");at.text_input[0].set_value("test-admin-only");at.button[0].click().run()
                self.assertEqual(len(at.exception),0)
                self.assertEqual(at.session_state["role"],"admin")
                os.environ["APP_ADMIN_PASSWORD"]="rotated-test-admin"
                at.run()
                self.assertEqual(at.radio[0].label,"登入身分")
                at.radio[0].set_value("會員");at.text_input[0].set_value("test-member-only");at.button[0].click().run()
                self.assertEqual(len(at.exception),0)
                self.assertEqual(at.session_state["role"],"member")
                self.assertFalse(any("建立並保存" in b.label for b in at.button))

    def test_missing_credentials_fail_closed(self):
        with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"","APP_MEMBER_PASSWORD":""}):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
            self.assertEqual(len(at.exception),0)
            self.assertTrue(at.error)
            self.assertFalse(at.date_input)

    def test_storage_failure_keeps_admin_diagnostic_and_skips_providers(self):
        store = MagicMock()
        store.check_read_access.side_effect = StorageError("資料庫驗證失敗", code="STORAGE_AUTH")
        with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"test-admin-only","APP_MEMBER_PASSWORD":"test-member-only"}), patch("storage.create_store",return_value=(store,"Supabase 已設定（連線及寫入尚未驗證）")), patch("service.build_nba_snapshot") as builder:
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
            at.radio[0].set_value("管理員");at.text_input[0].set_value("test-admin-only");at.button[0].click().run()
            self.assertEqual(len(at.exception),0)
            self.assertTrue(any("STORAGE_AUTH" in c.value for c in at.caption))
            self.assertEqual(at.session_state["last_diagnostic"]["code"], "STORAGE_AUTH")
            self.assertTrue(at.get("download_button"))
            self.assertFalse(any("建立並保存" in b.label for b in at.button))
            builder.assert_not_called()
            store.get_automatic_snapshot.assert_not_called()

    def test_member_storage_failure_shows_safe_tracking_code(self):
        store = MagicMock()
        store.get_member_snapshot.side_effect = StorageError("資料庫網路失敗", code="STORAGE_NETWORK")
        with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"test-admin-only","APP_MEMBER_PASSWORD":"test-member-only"}), patch("storage.create_store",return_value=(store,"test")):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
            at.text_input[0].set_value("test-member-only");at.button[0].click().run()
            self.assertEqual(len(at.exception),0)
            self.assertTrue(any("STORAGE_NETWORK" in c.value for c in at.caption))
            self.assertFalse(at.get("download_button"))

