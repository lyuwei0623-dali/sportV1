from __future__ import annotations

import unittest

from models import Event
from service import _football_projection, _mlb_projection


class TeamAndPlayerInputTest(unittest.TestCase):
    def test_mlb_probable_starter_changes_projection(self):
        event = Event("mlb:1", "mlb", "", "Home", "Away", "MLB", {
            "home_team_id": 1, "away_team_id": 2,
            "home_pitcher_id": 11, "away_pitcher_id": 22,
            "home_record": {"wins": 60, "losses": 40},
            "away_record": {"wins": 50, "losses": 50},
        })
        teams = {
            1: {"runs_per_game": 4.8, "ops": .750, "era": 3.9, "whip": 1.22},
            2: {"runs_per_game": 4.4, "ops": .710, "era": 4.4, "whip": 1.35},
        }
        weak_away_starter = {11: {"era": 3.2, "whip": 1.15, "innings": 80}, 22: {"era": 5.1, "whip": 1.48, "innings": 80}}
        strong_away_starter = {11: {"era": 3.2, "whip": 1.15, "innings": 80}, 22: {"era": 2.7, "whip": 1.02, "innings": 80}}
        weak_projection = _mlb_projection(event, teams, weak_away_starter)
        strong_projection = _mlb_projection(event, teams, strong_away_starter)
        self.assertIsNotNone(weak_projection)
        self.assertIsNotNone(strong_projection)
        self.assertGreater(weak_projection.home_expected, strong_projection.home_expected)

    def test_football_injury_data_changes_projection_without_fixed_baseline(self):
        event = Event("football:1", "football", "", "Home", "Away", "英超", {
            "league_id": 39, "season": 2026, "home_team_id": 1, "away_team_id": 2,
        })
        standings = {39: {
            1: {"played": 8, "for": 16, "against": 8},
            2: {"played": 8, "for": 9, "against": 12},
        }}
        profiles = {
            (39, 2026, 1, "home"): {"played": 4, "for_average": 2.2, "against_average": .8, "form_ratio": .80, "clean_sheet_rate": .5, "failed_to_score_rate": .0},
            (39, 2026, 2, "away"): {"played": 4, "for_average": .9, "against_average": 1.8, "form_ratio": .30, "clean_sheet_rate": .0, "failed_to_score_rate": .5},
        }
        healthy = _football_projection(event, standings, profiles, {})
        injured = _football_projection(event, standings, profiles, {"football:1": {"home": 4, "away": 0}})
        self.assertIsNotNone(healthy)
        self.assertIsNotNone(injured)
        self.assertGreater(healthy.home_expected, injured.home_expected)
        self.assertNotEqual((healthy.home_expected, healthy.away_expected), (1.4, 1.1))


if __name__ == "__main__":
    unittest.main()
