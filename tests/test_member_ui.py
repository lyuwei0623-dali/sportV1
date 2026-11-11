import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from streamlit.testing.v1 import AppTest
from storage import LocalSnapshotStore
from membership import MemberError

APP=str(Path(__file__).resolve().parents[1]/"app.py")


class MemberUITests(unittest.TestCase):
    def test_old_shared_password_cannot_bypass_membership(self):
        repo=MagicMock()
        repo.login.side_effect=MemberError("會員編號或密碼不正確")
        with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"admin-test","APP_MEMBER_PASSWORD":"old-shared"}),patch("membership_ui.browser_device",return_value="a"*64),patch("membership_ui.repository",return_value=repo):
            at=AppTest.from_file(APP).run()
            at.text_input[0].set_value("VD-000000000001")
            at.text_input[1].set_value("old-shared")
            at.button[0].click().run()
            self.assertEqual(len(at.exception),0)
            self.assertTrue(at.error)
            self.assertFalse(at.date_input)

    def test_missing_device_disables_member_login_but_not_admin(self):
        with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"admin-test"}),patch("membership_ui.browser_device",return_value=None):
            at=AppTest.from_file(APP).run()
            self.assertTrue(at.button[0].disabled)
            at.radio[0].set_value("管理員").run()
            self.assertFalse(at.button[0].disabled)

    def test_admin_management_count_issue_and_confirmed_disable(self):
        repo=MagicMock()
        repo.overview.return_value={"total_issued":1,"current":1,"active":1,"expired":0,"disabled":0,"deleted":0,
          "members":[{"id":"id-1","login_id":"VD-000000000001","label":"test member","state":"有效","expires_at":"2099-01-01T00:00:00Z","bound":False,"last_login_at":None}]}
        repo.manage.return_value={"ok":True}
        repo.issue.return_value={"id":"id-2","login_id":"VD-000000000002","password":"one-time-demo","expires_at":"2099-01-01T00:00:00Z"}
        with tempfile.TemporaryDirectory() as root:
            store=LocalSnapshotStore(str(Path(root)/"db"))
            with patch.dict(os.environ,{"APP_ADMIN_PASSWORD":"admin-test"}),patch("storage.create_store",return_value=(store,"test")),patch("membership.MemberRepository",return_value=repo),patch("membership_ui.browser_device",return_value="a"*64):
                at=AppTest.from_file(APP).run()
                at.radio[0].set_value("管理員").run()
                at.text_input[0].set_value("admin-test");at.button[0].click().run()
                at.radio[0].set_value("會員管理").run()
                self.assertEqual(len(at.exception),0)
                self.assertEqual(at.metric[0].value,"1")
                at.selectbox[1].set_value("停用會員")
                next(b for b in at.button if b.label=="確認執行").click().run()
                repo.manage.assert_not_called()
                at.checkbox[0].check()
                next(b for b in at.button if b.label=="確認執行").click().run()
                repo.manage.assert_called_once_with("id-1","disable")
                at.text_input[0].set_value("new member")
                next(b for b in at.button if b.label=="產生專屬密碼並開通七天").click().run()
                self.assertTrue(any("one-time-demo" in c.value for c in at.code))
                issued = next(c.value for c in at.code if "one-time-demo" in c.value)
                self.assertIn("APP 網址：https://vidali-sports.streamlit.app/\n", issued)
                self.assertIn("會員編號：VD-000000000002", issued)
                repo.issue.assert_called_once_with("new member")
                repo.manage.return_value = {"ok": True, "password": "reset-demo"}
                at.selectbox[1].set_value("重新產生密碼")
                at.checkbox[0].check()
                next(b for b in at.button if b.label == "確認執行").click().run()
                reset = next(c.value for c in at.code if "reset-demo" in c.value)
                self.assertIn("APP 網址：https://vidali-sports.streamlit.app/\n", reset)
                self.assertNotIn("?", reset.splitlines()[0])
                next(b for b in at.button if b.label=="已交付，關閉密碼顯示").click().run()
                self.assertFalse(at.code)
