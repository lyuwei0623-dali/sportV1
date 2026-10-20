from unittest import TestCase
from unittest.mock import patch

from providers import _recent_fixture_profile


class FootballRecentFixtureFallbackTests(TestCase):
    def test_completed_matches_become_real_home_and_away_profiles(self):
        matches = [
            {"fixture": {"status": {"short": "FT"}}, "teams": {"home": {"id": 7}, "away": {"id": 9}}, "goals": {"home": 2, "away": 0}},
            {"fixture": {"status": {"short": "FT"}}, "teams": {"home": {"id": 8}, "away": {"id": 7}}, "goals": {"home": 1, "away": 1}},
        ]
        with patch("providers._api_football", return_value=matches):
            profiles = _recent_fixture_profile(7)
        self.assertEqual(profiles["home"]["for_average"], 2)
        self.assertEqual(profiles["home"]["clean_sheet_rate"], 1)
        self.assertEqual(profiles["away"]["against_average"], 1)
        self.assertEqual(profiles["away"]["form_ratio"], 1 / 3)
