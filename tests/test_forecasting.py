from __future__ import annotations

import unittest

from forecasting import probability_recommendations, score_projection
from models import Market


class ForecastingTest(unittest.TestCase):
    def test_different_team_strengths_do_not_use_fixed_score_or_xg(self):
        strong = score_projection(2.25, .65, "test")
        balanced = score_projection(1.15, 1.10, "test")
        self.assertNotEqual((strong.home_expected, strong.away_expected), (balanced.home_expected, balanced.away_expected))
        self.assertGreater(strong.home_win, balanced.home_win)

    def test_missing_actual_lines_still_returns_three_plain_probability_cards(self):
        projection = score_projection(1.8, .9, "test")
        picks = probability_recommendations(projection, None, sport="football")
        self.assertEqual([pick.market_type for pick in picks], ["moneyline", "spread", "total"])
        self.assertEqual([pick.state for pick in picks], ["model", "model_reference", "model_reference"])
        self.assertTrue(all(pick.model_probability is not None for pick in picks))
        expected_fields = {"market_type", "state", "selection", "model_probability", "market_line", "reason"}
        self.assertTrue(all(expected_fields <= set(pick.__dict__) for pick in picks))
        self.assertTrue(all(not pick.actionable and pick.expected_return is None for pick in picks))

    def test_actual_lines_replace_reference_lines_without_using_prices(self):
        projection = score_projection(2.0, .7, "test")
        market = Market("test", "now", spread_line=-.5, total_line=2.5)
        picks = probability_recommendations(projection, market, sport="football")
        self.assertEqual([pick.state for pick in picks], ["model", "market_line", "market_line"])
        self.assertEqual(picks[1].market_line, -.5)
        self.assertEqual(picks[2].market_line, 2.5)

    def test_total_probability_is_calculated_against_the_total_line(self):
        projection = score_projection(1.1, .9, "test")
        total = probability_recommendations(projection, Market("test", "now", total_line=2.5), sport="football")[2]
        self.assertGreater(total.model_probability, 0)
        self.assertLess(total.model_probability, 1)

    def test_win_card_always_answers_with_a_team_not_a_draw(self):
        projection = score_projection(1.05, 1.02, "test")
        moneyline = probability_recommendations(projection, None, sport="football")[0]
        self.assertIn(moneyline.selection, {"主隊勝", "客隊勝"})
        self.assertNotIn("和局", moneyline.selection)


if __name__ == "__main__":
    unittest.main()
