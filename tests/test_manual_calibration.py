from __future__ import annotations

from dataclasses import asdict
import unittest

from forecasting import score_projection
from providers import ProviderError
from service import apply_manual_calibration


class ManualCalibrationTest(unittest.TestCase):
    def _snapshot(self) -> dict:
        projection = score_projection(1.8, .8, "test team inputs")
        return {
            "sport": "football",
            "date_key": "2026-10-19",
            "generated_at": "2026-10-19T00:00:00+00:00",
            "events": [{
                "event": {"event_id": "football:one", "home": "Home", "away": "Away"},
                "projection": asdict(projection),
                "market": None,
                "recommendations": [],
                "warnings": ["缺少的讓分／大小線會顯示模型基準線，非實際可下注盤口"],
            }],
            "source_summary": ["test source"],
        }

    def test_manual_lines_recalculate_and_mark_member_release(self):
        automatic = self._snapshot()
        manual = apply_manual_calibration(
            automatic, {"football:one": (-.5, 2.5)}, calibrated_at="2026-10-19T01:00:00+00:00"
        )
        self.assertEqual(automatic["events"][0]["market"], None)
        self.assertEqual(manual["release_type"], "manual_calibrated")
        self.assertEqual(manual["events"][0]["market"]["spread_line"], -.5)
        self.assertEqual(manual["events"][0]["market"]["total_line"], 2.5)
        self.assertEqual(
            [item["state"] for item in manual["events"][0]["recommendations"]],
            ["model", "market_line", "market_line"],
        )
        self.assertTrue(any("人工校正盤口" in warning for warning in manual["events"][0]["warnings"]))

    def test_partial_manual_input_cannot_be_published_as_complete_release(self):
        with self.assertRaises(ProviderError):
            apply_manual_calibration(self._snapshot(), {})


if __name__ == "__main__":
    unittest.main()
