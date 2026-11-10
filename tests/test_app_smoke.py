import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from storage import LocalSnapshotStore, StorageError
from streamlit.testing.v1 import AppTest
from membership import MemberError


class AppSmoke(unittest.TestCase):
    def test_admin_member_and_secret_rotation(self):
        with tempfile.TemporaryDirectory() as root:
            store=LocalSnapshotStore(str(Path(root)/"db"))
            members=MagicMock()
            members.login.return_value={"id":"test-id","session":"b"*64}
            members.validate.return_value={"ok":True,"label":"測試會員","expires_at":"2099-01-01T00:00:00Z"}
            with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"test-admin-only","APP_MEMBER_PASSWORD":"test-member-only"}),patch("storage.create_store",return_value=(store,"測試資料庫")),patch("membership_ui.repository",return_value=members),patch("membership.MemberRepository",return_value=members),patch("membership_ui.browser_device",return_value="a"*64):
                at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
                self.assertEqual(len(at.exception),0)
                at.radio[0].set_value("管理員").run();at.text_input[0].set_value("test-admin-only");at.button[0].click().run()
                self.assertEqual(len(at.exception),0)
                self.assertEqual(at.session_state["role"],"admin")
                os.environ["APP_ADMIN_PASSWORD"]="rotated-test-admin"
                at.run()
                self.assertEqual(at.radio[0].label,"登入身分")
                at.radio[0].set_value("會員").run();at.text_input[0].set_value("VD-000000000001");at.text_input[1].set_value("test-member-only");at.button[0].click().run()
                self.assertEqual(len(at.exception),0)
                self.assertEqual(at.session_state["role"],"member")
                self.assertFalse(any("建立並保存" in b.label for b in at.button))
                members.validate.return_value=None
                at.run()
                self.assertFalse(at.date_input)
                self.assertEqual(at.radio[0].label,"登入身分")

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
            at.radio[0].set_value("管理員").run();at.text_input[0].set_value("test-admin-only");at.button[0].click().run()
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
        members = MagicMock()
        members.login.return_value = {"id": "test-id", "session": "b"*64}
        members.validate.return_value = {"ok": True, "label": "測試會員", "expires_at": "2099-01-01T00:00:00Z"}
        with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"test-admin-only"}), patch("storage.create_store",return_value=(store,"test")), patch("membership_ui.repository",return_value=members), patch("membership.MemberRepository",return_value=members), patch("membership_ui.browser_device",return_value="a"*64):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
            at.text_input[0].set_value("VD-000000000001");at.text_input[1].set_value("test-member-only");at.button[0].click().run()
            self.assertEqual(len(at.exception),0)
            self.assertTrue(any("STORAGE_NETWORK" in c.value for c in at.caption))
            self.assertFalse(at.get("download_button"))

    def test_membership_error_uses_safe_fallback_diagnostic(self):
        members = MagicMock()
        members.overview.side_effect = MemberError("會員資料庫尚未就緒")
        with patch.dict(os.environ, {"APP_ADMIN_PASSWORD": "test-admin-only"}), patch("storage.create_store", return_value=(MagicMock(), "test")), patch("membership.MemberRepository", return_value=members):
            at = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
            at.radio[0].set_value("管理員").run()
            at.text_input[0].set_value("test-admin-only"); at.button[0].click().run()
            next(r for r in at.radio if r.label == "功能").set_value("會員管理").run()
            self.assertEqual(len(at.exception), 0)
            self.assertTrue(any("MEMBERSHIP_UNAVAILABLE" in c.value for c in at.caption))
            self.assertTrue(at.get("download_button"))

    def test_member_render_failure_does_not_expose_exception_text(self):
        store, members = MagicMock(), MagicMock()
        store.get_member_snapshot.side_effect = RuntimeError("sensitive-test-only-payload")
        members.login.return_value = {"id": "test-id", "session": "b"*64}
        members.validate.return_value = {"ok": True, "label": "測試會員", "expires_at": "2099-01-01T00:00:00Z"}
        with patch.dict(os.environ, {"APP_ADMIN_PASSWORD": "test-admin-only"}), patch("storage.create_store", return_value=(store, "test")), patch("membership_ui.repository", return_value=members), patch("membership.MemberRepository", return_value=members), patch("membership_ui.browser_device", return_value="a"*64):
            at = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
            at.text_input[0].set_value("VD-000000000001")
            at.text_input[1].set_value("test-member-only"); at.button[0].click().run()
            self.assertEqual(len(at.exception), 0)
            self.assertTrue(any("追蹤碼" in e.value for e in at.error))
            self.assertFalse(any("sensitive-test-only-payload" in e.value for e in at.error))

