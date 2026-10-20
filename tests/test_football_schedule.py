from datetime import date
from unittest import TestCase
from unittest.mock import patch

from providers import ProviderError, fetch_football_events


class FootballScheduleTests(TestCase):
    def test_two_utc_date_requests_and_taiwan_day_filter(self):
        previous_utc_day = [
            {
                "fixture": {"id": 77, "date": "2026-10-19T17:00:00+00:00", "status": {"short": "NS"}},
                "league": {"id": 39},
                "teams": {"home": {"id": 1, "name": "Home FC"}, "away": {"id": 2, "name": "Away FC"}},
            },
        ]
        selected_utc_day = [
            {
                "fixture": {"id": 78, "date": "2026-10-20T20:00:00+00:00", "status": {"short": "NS"}},
                "league": {"id": 2},
                "teams": {"home": {"id": 3, "name": "Next day"}, "away": {"id": 4, "name": "Other 2"}},
            },
        ]
        with patch("providers._api_football", side_effect=[previous_utc_day, selected_utc_day]) as api:
            events = fetch_football_events(date(2026, 10, 20))
        self.assertEqual(api.call_count, 2)
        api.assert_any_call("fixtures", {"date": "2026-10-19"})
        api.assert_any_call("fixtures", {"date": "2026-10-20"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].league, "英超")

    def test_provider_failure_explains_that_existing_snapshot_is_safe(self):
        with patch("providers._api_football", side_effect=ProviderError("API-Football 無法提供資料（quota exceeded）")):
            with self.assertRaisesRegex(ProviderError, "quota exceeded"):
                fetch_football_events(date(2026, 10, 20))
