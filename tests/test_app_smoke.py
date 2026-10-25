import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from storage import LocalSnapshotStore
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
