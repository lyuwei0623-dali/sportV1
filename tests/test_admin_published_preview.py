from copy import deepcopy
from dataclasses import asdict
from datetime import date
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from forecasting import score_projection, basketball_projection, baseball_projection, probability_recommendations
from service import apply_manual_calibration
from storage import LocalSnapshotStore, StorageError
from time_utils import SESSION_POLICY


SCRIPT = '''
from app import _admin_page
from storage import create_store
store, label = create_store()
_admin_page(store, label)
'''
DAY = '2030-10-19'
NOW = '2030-10-19T05:00:00Z'


def automatic(sport):
    projection = (basketball_projection(116, 112, 'test inputs') if sport == 'nba' else
                  baseball_projection(4.5, 3.8, 'test inputs') if sport == 'mlb' else
                  score_projection(1.8, .8, 'test inputs'))
    item = {
        'event': {'event_id': 'one', 'home': 'Arsenal', 'away': 'Chelsea',
                  'kickoff': '2030-10-19T12:00:00Z'},
        'capture_schema': 3, 'captured_at': NOW,
        'projection': asdict(projection), 'market': None,
        'recommendations': [asdict(r) for r in probability_recommendations(projection, None, sport=sport)],
        'warnings': ['缺少的讓分／大小線會顯示模型基準線，非實際可下注盤口'],
    }
    return json.loads(json.dumps({'sport': sport, 'date_key': DAY, 'date_policy': SESSION_POLICY,
            'generated_at': NOW, 'events': [item], 'source_summary': []}))


def tables(app):
    return [m.value for m in app.markdown if 'snapshot-wrap' in m.value and '<table' in m.value]


class AdminPublishedPreviewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = LocalSnapshotStore(str(Path(folder.name) / 'test.db'))
        for target, value in (
            ('storage.create_store', (self.store, 'isolated test')),
            ('app._taiwan_day', date.fromisoformat(DAY)),
            ('app.utc_now_iso', NOW), ('service.utc_now_iso', NOW),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open(self, sport):
        app = AppTest.from_string(SCRIPT).run()
        label = {'mlb': 'MLB', 'nba': 'NBA', 'football': '歐洲足球'}[sport]
        app.radio(key='admin_sport').set_value(label).run()
        self.assertFalse(app.exception)
        return app

    def submit(self, app):
        app.text_input[0].set_value('-0.5')
        total = {'MLB': '8.5', 'NBA': '220.5', '歐洲足球': '2.5'}[app.radio(key='admin_sport').value]
        app.text_input[1].set_value(total)
        next(b for b in app.button if b.label == '儲存人工校正並發布會員版').click().run()
        self.assertFalse(app.exception)

    def test_submit_rerun_and_new_session_preview_actual_lines_all_sports(self):
        for sport in ('mlb', 'nba', 'football'):
            with self.subTest(sport=sport):
                original = automatic(sport)
                self.store.save_automatic(sport, DAY, original)
                app = self.open(sport)
                self.assertIn('模型基準線', tables(app)[0])
                self.submit(app)
                published, _ = self.store.get_member_snapshot(sport, DAY)
                self.assertEqual([r['state'] for r in published['events'][0]['recommendations']],
                                 ['model', 'market_line', 'market_line'])
                for shown in (app, self.open(sport)):
                    self.assertEqual(len(tables(shown)), 1)
                    self.assertIn('已人工校正', tables(shown)[0])
                    self.assertIn('保存的實際盤口線', tables(shown)[0])
                    self.assertNotIn('模型基準線', tables(shown)[0])
                    self.assertTrue(any('最近人工校正發布（台灣）：10/19 13:00' in c.value for c in shown.caption))
                self.assertEqual(self.store.get_automatic_snapshot(sport, DAY), original)
                self.assertTrue(all(r['decision'].startswith('PASS') for r in published['events'][0]['recommendations']))

    def test_partial_release_retains_uncorrected_reference_row(self):
        original = automatic('football')
        second = deepcopy(original['events'][0])
        second['event']['event_id'] = 'two'
        original['events'].append(second)
        self.store.save_automatic('football', DAY, original)
        app = self.open('football')
        self.submit(app)
        html = tables(app)[0]
        self.assertIn('已人工校正', html)
        self.assertIn('未人工校正・保留原資料', html)
        self.assertIn('保存的實際盤口線', html)
        self.assertEqual(html.count('<b>模型基準線</b>'), 2)
        visible, _ = self.store.get_member_snapshot('football', DAY)
        self.assertEqual(visible['events'][1], original['events'][1])

    def test_baseline_is_explicit_opt_in_and_refresh_keeps_published_priority(self):
        original = automatic('nba')
        self.store.save_automatic('nba', DAY, original)
        manual = apply_manual_calibration(original, {'one': (-.5, 2.5)}, calibrated_at=NOW)
        self.store.save_manual_published('nba', DAY, manual)
        newer = deepcopy(original)
        newer['generated_at'] = '2030-10-19T06:00:00Z'
        self.store.save_automatic('nba', DAY, newer)
        app = self.open('nba')
        self.assertNotIn('模型基準線', tables(app)[0])
        app.checkbox(key='baseline_nba_' + DAY).check().run()
        self.assertEqual(len(tables(app)), 2)
        self.assertNotIn('模型基準線', tables(app)[0])
        self.assertIn('模型基準線', tables(app)[1])
        self.assertEqual(self.store.get_member_snapshot('nba', DAY)[0], manual)

    def test_failed_save_keeps_reference_and_never_claims_success(self):
        self.store.save_automatic('football', DAY, automatic('football'))
        app = self.open('football')
        with patch.object(self.store, 'save_manual_published', side_effect=StorageError('測試儲存失敗')):
            self.submit(app)
        self.assertTrue(app.error)
        self.assertFalse(app.success)
        self.assertIn('模型基準線', tables(app)[0])
        self.assertNotEqual(self.store.get_member_snapshot('football', DAY)[0].get('release_type'), 'manual_calibrated')

    def test_finished_reference_cannot_be_relabelled_as_manual(self):
        original = automatic('nba')
        original['events'][0]['event']['kickoff'] = '2030-10-19T04:30:00Z'
        original['events'][0]['settlement'] = {'status': 'final', 'home_score': 116, 'away_score': 94}
        self.store.save_automatic('nba', DAY, original)
        app = self.open('nba')
        self.assertFalse(app.text_input)
        self.assertIn('模型基準線', tables(app)[0])
        self.assertEqual(self.store.get_automatic_snapshot('nba', DAY), original)
