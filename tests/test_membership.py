import unittest
from unittest.mock import MagicMock, patch

from membership import MemberError, MemberRepository, password_hash, password_matches, token_hash
from storage import SupabaseSnapshotStore, StorageError


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseSnapshotStore("https://example.supabase.co", "sb_secret_test")
        self.repo = MemberRepository(self.store)

    def test_password_is_salted_and_verified(self):
        a, b = password_hash("sample-only"), password_hash("sample-only")
        self.assertNotEqual(a, b)
        self.assertNotIn("sample-only", a)
        self.assertTrue(password_matches("sample-only", a))
        self.assertFalse(password_matches("wrong", a))
        for invalid in (None, "", "scrypt1$zz$aa"):
            self.assertFalse(password_matches("x", invalid))

    def test_device_token_is_validated(self):
        self.assertEqual(len(token_hash("a"*64)), 64)
        for invalid in (None, "", "ip-address", "a"*63):
            with self.assertRaises(MemberError):
                token_hash(invalid)

    def test_create_sends_hash_only_and_returns_one_time_password(self):
        with patch.object(self.repo, "rpc", return_value={"id":"example", "expires_at":"2026-11-17T00:00:00Z"}) as rpc:
            out = self.repo.issue("測試會員")
            args = rpc.call_args.kwargs
            self.assertNotIn(out["password"], str(args))
            self.assertTrue(password_matches(out["password"], args["p_password_hash"]))
            self.assertRegex(out["login_id"], r"^VD-[A-F0-9]{12}$")

    def test_login_sends_expected_hash_and_no_raw_device_or_password(self):
        encoded = password_hash("sample-only")
        with patch.object(self.repo, "rpc", side_effect=[{"password_hash":encoded},{"ok":True,"id":"id"}]) as rpc:
            session = self.repo.login("VD-000000000001", "sample-only", "a"*64)
            args = rpc.call_args.kwargs
            self.assertTrue(args["p_password_ok"])
            self.assertEqual(args["p_expected_hash"],encoded)
            self.assertEqual(args["p_device_hash"], token_hash("a"*64))
            self.assertNotIn("sample-only",str(args))
            self.assertNotIn("password_hash",session)
            self.assertEqual(len(session["session"]),64)

    def test_invalid_password_cannot_get_session(self):
        with patch.object(self.repo,"rpc",side_effect=[{"password_hash":password_hash("correct")},{"ok":False}]) as rpc:
            with self.assertRaises(MemberError):
                self.repo.login("VD-000000000001","wrong","b"*64)
            self.assertFalse(rpc.call_args.kwargs["p_password_ok"])

    def test_no_local_store_fallback(self):
        with self.assertRaises(MemberError):
            MemberRepository(object())

    def test_error_does_not_expose_db_detail(self):
        with patch.object(self.store,"_request",side_effect=StorageError("PRIVATE_DATA")):
            with self.assertRaises(MemberError) as caught:
                self.repo.overview()
        self.assertNotIn("PRIVATE_DATA",str(caught.exception))

    def test_reset_password_never_stores_plaintext(self):
        with patch.object(self.repo,"rpc",return_value={"ok":True}) as rpc:
            result=self.repo.manage("id","reset_password")
            self.assertTrue(password_matches(result["password"],rpc.call_args.kwargs["p_password_hash"]))
        with self.assertRaises(MemberError):
            self.repo.manage("id","grant_admin")

    def test_validate_denied_fails_closed(self):
        with patch.object(self.repo,"rpc",return_value=None):
            self.assertIsNone(self.repo.validate({"id":"id","session":"c"*64},"d"*64))
