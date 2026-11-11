from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from pathlib import Path
import tempfile
import json
import unittest

from forecasting import score_projection
from providers import ProviderError
from service import apply_manual_calibration, manual_calibration_base
from storage import LocalSnapshotStore


class ManualCalibrationTest(unittest.TestCase):
    def _snapshot(self) -> dict:
        projection = score_projection(1.8, .8, "test team inputs")
        return {
            "sport": "football",
            "date_key": "2026-10-19",
            "generated_at": "2026-10-19T00:00:00+00:00",
            "events": [{
                "event": {"event_id": "football:one", "home": "Home", "away": "Away", "kickoff":"2026-10-19T12:00:00Z"},
                "capture_schema":3,
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

    def test_empty_manual_input_cannot_publish(self):
        with self.assertRaises(ProviderError):
            apply_manual_calibration(self._snapshot(), {})

    def test_partial_release_preserves_missing_model_and_unfilled_games(self):
        original = self._snapshot()
        missing = deepcopy(original['events'][0])
        missing['event']['event_id'] = 'football:missing'
        missing['projection'] = None
        untouched = deepcopy(original['events'][0])
        untouched['event']['event_id'] = 'football:unfilled'
        original['events'].extend([missing, untouched])
        before = deepcopy(original)
        result = apply_manual_calibration(original, {'football:one': (-.5, 2.5)},
                                          calibrated_at='2026-10-19T01:00:00Z')
        self.assertEqual(original, before)
        self.assertEqual(result['events'][1:], before['events'][1:])
        self.assertEqual(result['events'][0]['market']['spread_line'], -.5)

    def test_unselected_started_game_is_preserved(self):
        original = self._snapshot()
        started = deepcopy(original['events'][0])
        started['event'].update(event_id='football:started', kickoff='2026-10-19T04:30:00Z')
        started['settlement'] = {'status': 'final', 'home_score': 2, 'away_score': 1}
        original['events'].append(started)
        result = apply_manual_calibration(original, {'football:one': (-.5, 2.5)},
                                          calibrated_at='2026-10-19T05:00:00Z')
        self.assertEqual(next(i for i in result['events'] if i['event']['event_id'] == 'football:started'), started)

    def test_selected_started_or_missing_model_still_rejected(self):
        for change in ('started', 'missing', 'legacy'):
            original = self._snapshot()
            if change == 'started':
                original['events'][0]['event']['kickoff'] = '2026-10-19T04:30:00Z'
            elif change == 'missing':
                original['events'][0]['projection'] = None
            else:
                original['events'][0]['capture_schema'] = 1
            with self.subTest(change=change), self.assertRaises(ProviderError):
                apply_manual_calibration(original, {'football:one': (-.5, 2.5)},
                                         calibrated_at='2026-10-19T05:00:00Z')

    def test_repeat_partial_release_retains_prior_manual_game_and_adds_new_game(self):
        original = self._snapshot()
        first = apply_manual_calibration(original, {'football:one': (-.5, 2.5)},
                                         calibrated_at='2026-10-19T01:00:00Z')
        fresh = self._snapshot()
        new = deepcopy(fresh['events'][0])
        new['event']['event_id'] = 'football:two'
        fresh['events'] = [new]  # Refresh omits a previously published game.
        before = deepcopy(first)
        base = manual_calibration_base(fresh, first)
        result = apply_manual_calibration(base, {'football:two': (-1.5, 3.5)},
                                          calibrated_at='2026-10-19T02:00:00Z')
        self.assertEqual(result['events'][0], first['events'][0])
        self.assertEqual(result['events'][1]['market']['total_line'], 3.5)
        self.assertEqual(first, before)
        self.assertIsNone(fresh['events'][0]['market'])

    def test_partial_release_supported_for_all_sports_and_keeps_pass(self):
        for sport in ('mlb', 'nba', 'football'):
            original = self._snapshot()
            original['sport'] = sport
            skipped = deepcopy(original['events'][0])
            skipped['event']['event_id'] = 'skipped'
            skipped['projection'] = None
            original['events'].append(skipped)
            with self.subTest(sport=sport):
                result = apply_manual_calibration(original, {'football:one': (-.5, 2.5)},
                                                  calibrated_at='2026-10-19T01:00:00Z')
                self.assertEqual(result['events'][1], skipped)
                self.assertTrue(all(r['decision'].startswith('PASS') for r in result['events'][0]['recommendations']))

    def test_unknown_id_bad_lines_and_mismatched_release_are_rejected(self):
        for lines in ({'unknown': (-.5, 2.5)}, {'football:one': (float('nan'), 2.5)},
                      {'football:one': (-.5, 0)}, {'football:one': (-.3, 2.5)}):
            with self.subTest(lines=lines), self.assertRaises((ProviderError, ValueError)):
                apply_manual_calibration(self._snapshot(), lines, calibrated_at='2026-10-19T01:00:00Z')
        manual = apply_manual_calibration(self._snapshot(), {'football:one': (-.5, 2.5)},
                                         calibrated_at='2026-10-19T01:00:00Z')
        changed = self._snapshot()
        changed['events'][0]['event']['home'] = 'different'
        with self.assertRaises(ProviderError):
            manual_calibration_base(changed, manual)
        changed = self._snapshot()
        changed['date_key'] = '2026-10-20'
        with self.assertRaises(ProviderError):
            manual_calibration_base(changed, manual)

    def test_partial_release_storage_round_trip_does_not_replace_automatic(self):
        original = self._snapshot()
        second = deepcopy(original['events'][0])
        second['event']['event_id'] = 'football:two'
        original['events'].append(second)
        with tempfile.TemporaryDirectory() as folder:
            store = LocalSnapshotStore(str(Path(folder) / 'test.db'))
            store.save_automatic('football', original['date_key'], original)
            first = apply_manual_calibration(original, {'football:one': (-.5, 2.5)},
                                             calibrated_at='2026-10-19T01:00:00Z')
            store.save_manual_published('football', original['date_key'], first)
            published, _ = store.get_member_snapshot('football', original['date_key'])
            revised = apply_manual_calibration(manual_calibration_base(original, published),
                {'football:two': (-1.5, 3.5)}, calibrated_at='2026-10-19T02:00:00Z')
            store.save_manual_published('football', original['date_key'], revised)
            visible, _ = store.get_member_snapshot('football', original['date_key'])
            self.assertEqual(visible, revised)
            self.assertEqual(visible['events'][0], published['events'][0])
            self.assertEqual(store.get_automatic_snapshot('football', original['date_key']), json.loads(json.dumps(original)))
if __name__ == "__main__":
    unittest.main()
