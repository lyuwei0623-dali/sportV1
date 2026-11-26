from datetime import date
import unittest
from unittest.mock import patch

from service import fetch_session_results, settle_snapshot


class FootballFinalLookup(unittest.TestCase):
    def lookup(self, name, period, *, completed=True, kickoff="2026-11-25T20:00Z", score="0", nested=False):
        status = {"type": {"name": name, "completed": completed}, "period": period}
        competition = {"status": status, "competitors": [
            {"homeAway": "away", "score": "2"}, {"homeAway": "home", "score": score}]}
        event = {"id": "77", "date": kickoff, "competitions": [competition]}
        if not nested:
            event["status"] = status
        with patch("providers._api_football", return_value=[]), \
             patch("providers.ESPN_SOCCER_LEAGUES", {"歐冠": "uefa.champions"}), \
             patch("providers._espn_football_payload", return_value={"events": [event]}):
            return fetch_session_results("football", date(2026, 11, 25))

    def test_regulation_final_reaches_saved_id_and_settles_pick(self):
        for name, nested in [("STATUS_FULL_TIME", False), ("STATUS_FULL_TIME", True), ("STATUS_FINAL", False)]:
            with self.subTest(name=name, nested=nested):
                results = self.lookup(name, 2, nested=nested)
                game = {"event_id": "football:espn:uefa.champions:77", "kickoff": "2026-11-25T20:00Z"}
                pick = {"market_type": "moneyline", "state": "model", "selection": "客隊勝"}
                snap = {"sport": "football", "events": [{"event": game, "recommendations": [pick]},
                                                         {"event": {"event_id": "football:77"}}]}
                settled, count = settle_snapshot(snap, results=results)
                self.assertEqual(count, 1)
                final = settled["events"][0]["settlement"]
                self.assertEqual((final["home_score"], final["away_score"]), (0, 2))
                self.assertIn("moneyline", final["picks"])
                self.assertNotIn("settlement", snap["events"][0])

    def test_unsafe_or_unrelated_results_stay_unsettled(self):
        for name, period, kwargs in [
            ("STATUS_END_ET", 4, {}), ("STATUS_FINAL_PEN", 5, {}),
            ("STATUS_FULL_TIME", 4, {}), ("STATUS_IN_PROGRESS", 2, {}),
            ("STATUS_FULL_TIME", 2, {"completed": False}),
            ("STATUS_FULL_TIME", "unknown", {}), ("STATUS_FULL_TIME", 2, {"score": "-"}),
            ("STATUS_FULL_TIME", 2, {"kickoff": "2026-11-26T16:00Z"}),
        ]:
            with self.subTest(name=name, period=period, kwargs=kwargs):
                self.assertEqual(self.lookup(name, period, **kwargs), {})
