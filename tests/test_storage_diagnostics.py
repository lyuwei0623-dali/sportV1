import json
import os
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

from diagnostics import record
from storage import SupabaseSnapshotStore, StorageError, create_store


class StorageDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseSnapshotStore("https://example.supabase.co", "sb_secret_test")

    def test_http_categories_are_safe_and_do_not_read_error_body(self):
        cases = {401: "STORAGE_AUTH", 403: "STORAGE_ACCESS", 404: "STORAGE_SCHEMA",
                 409: "STORAGE_CONFLICT", 429: "STORAGE_RATE_LIMIT", 503: "STORAGE_UPSTREAM", 400: "STORAGE_HTTP"}
        for status, code in cases.items():
            with self.subTest(status=status):
                body = MagicMock()
                error = HTTPError("https://private.invalid?key=DO_NOT_LEAK", status, "DO_NOT_LEAK", {}, body)
                with patch("storage.urlopen", side_effect=error) as call:
                    with self.assertRaises(StorageError) as caught:
                        self.store._request("GET", "/vd_daily_snapshots")
                    call.assert_called_once()
                self.assertEqual(caught.exception.code, code)
                body.read.assert_not_called()
                with self.assertLogs("vidali.audit", level="WARNING") as logs:
                    report = record("storage", error=caught.exception)
                self.assertEqual(report["code"], code)
                self.assertNotIn("DO_NOT_LEAK", str(caught.exception) + json.dumps(report) + str(logs.output))

    def test_network_timeout_and_invalid_json_are_distinct(self):
        for error, code in [(TimeoutError("DO_NOT_LEAK"), "STORAGE_TIMEOUT"),
                            (URLError(TimeoutError()), "STORAGE_TIMEOUT"),
                            (URLError("DO_NOT_LEAK"), "STORAGE_NETWORK"),
                            (OSError("DO_NOT_LEAK"), "STORAGE_NETWORK")]:
            with patch("storage.urlopen", side_effect=error):
                with self.assertRaises(StorageError) as caught:
                    self.store._request("GET", "/test")
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("DO_NOT_LEAK", str(caught.exception))
        with patch("storage.urlopen") as opening:
            opening.return_value.__enter__.return_value.read.return_value = b"not-json DO_NOT_LEAK"
            with self.assertRaises(StorageError) as caught:
                self.store._request("GET", "/test")
            self.assertEqual(caught.exception.code, "STORAGE_RESPONSE")

    def test_configuration_rejected_before_network(self):
        urls = ["https://supabase.com/dashboard/project/example", "http://example.supabase.co",
                "https://user:pass@example.supabase.co", "https://example.supabase.co?key=secret",
                "https://example.supabase.co/rest/v1", "https://[invalid"]
        with patch("storage.urlopen") as opening:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(StorageError) as caught:
                    SupabaseSnapshotStore(url, "sb_secret_test")
                self.assertEqual(caught.exception.code, "STORAGE_CONFIG")
            with self.assertRaises(StorageError):
                SupabaseSnapshotStore("https://example.supabase.co", "sb_publishable_test")
            opening.assert_not_called()

    def test_zero_row_probe_is_read_only_and_contains_no_snapshot_values(self):
        with patch("storage.urlopen") as opening:
            opening.return_value.__enter__.return_value.read.return_value = b"[]"
            self.store.check_read_access()
            request = opening.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIn("limit=0", request.full_url)
            self.assertIsNone(request.data)
            self.assertNotIn("Authorization", request.headers)
        for value in (None, {}, [{"sport": "nba"}]):
            with patch.object(self.store, "_request", return_value=value), self.assertRaises(StorageError):
                self.store.check_read_access()

    def test_configured_is_not_claimed_connected(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SECRET_KEY": "sb_secret_test"}):
            with patch("storage.urlopen") as opening:
                _, label = create_store()
                self.assertIn("尚未驗證", label)
                opening.assert_not_called()

    def test_unknown_diagnostic_code_is_not_exported(self):
        report = record("storage", error=StorageError("private", code="DO_NOT_LEAK"))
        self.assertEqual(report["code"], "STORAGE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
