from datetime import date
from unittest import TestCase
from unittest.mock import patch

from forecasting import basketball_projection, probability_recommendations
from providers import fetch_nba_events, fetch_nba_team_profiles


class NbaTests(TestCase):
    def test_nba_schedule_and_previous_season_profile_fallback(self):
        scoreboard = {"events": [{
            "id": "401", "date": "2026-10-21T00:00:00Z", "status": {"type": {"name": "STATUS_SCHEDULED"}},
            "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"id": "1", "displayName": "Home Team"}},
                {"homeAway": "away", "team": {"id": "2", "displayName": "Away Team"}},
            ]}],
        }]}
        current = {"children": [{"standings": {"entries": []}}]}
        previous = {"children": [{"standings": {"entries": [
            {"team": {"id": "1"}, "stats": [{"name": "wins", "value": 50}, {"name": "losses", "value": 32}, {"name": "avgPointsFor", "value": 116}, {"name": "avgPointsAgainst", "value": 112}]},
            {"team": {"id": "2"}, "stats": [{"name": "wins", "value": 40}, {"name": "losses", "value": 42}, {"name": "avgPointsFor", "value": 111}, {"name": "avgPointsAgainst", "value": 114}]},
        ]}}]}
        with patch("providers._get", side_effect=[scoreboard, current, previous]):
            events = fetch_nba_events(date(2026, 10, 21))
            profiles = fetch_nba_team_profiles(events, date(2026, 10, 21))
        self.assertEqual(events[0].home, "Home Team")
        self.assertEqual(profiles[1]["for"], 116)
        self.assertGreater(profiles[1]["win_pct"], profiles[2]["win_pct"])

    def test_nba_probability_cards_use_basketball_distribution(self):
        projection = basketball_projection(118, 108, "test")
        recommendations = {item.market_type: item for item in probability_recommendations(projection, None, sport="nba")}
        self.assertGreater(recommendations["moneyline"].model_probability, .5)
        self.assertEqual(recommendations["spread"].state, "model_reference")
        self.assertTrue(recommendations["total"].selection.startswith(("大分", "小分")))
