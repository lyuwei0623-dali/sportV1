from copy import deepcopy
from dataclasses import asdict
import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest
from forecasting import score_projection
from ui import render_snapshot


SCRIPT = '''
import streamlit as st
from storage import create_store
from app import _manual_calibration_panel
store, _ = create_store()
_manual_calibration_panel(st, store, store.get_automatic_snapshot('football', '2030-10-19'),
                          'football', '2030-10-19', '歐洲足球')
'''


def snapshot():
    events = []
    for key in ('one', 'missing', 'two'):
        events.append({'event': {'event_id': key, 'home': 'Arsenal', 'away': 'Chelsea',
                                 'kickoff': '2030-10-19T12:00:00Z'},
                       'capture_schema': 3,
                       'projection': None if key == 'missing' else asdict(score_projection(1.8, .8, 'test')),
                       'market': None, 'recommendations': [], 'warnings': []})
    return {'sport': 'football', 'date_key': '2030-10-19', 'generated_at': '2030-10-19T00:00:00Z',
            'events': events, 'source_summary': []}


class PartialManualUITests(unittest.TestCase):
    def test_missing_model_and_half_filled_game_do_not_block_complete_game(self):
        original = snapshot()
        store = MagicMock()
        saved = []
        store.get_automatic_snapshot.return_value = original
        store.get_member_snapshot.side_effect = lambda *args: (saved[-1] if saved else original, '測試')
        store.save_manual_published.side_effect = lambda sport, day, payload: saved.append(deepcopy(payload))
        with patch('storage.create_store', return_value=(store, 'test')):
            at = AppTest.from_string(SCRIPT).run()
            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.text_input), 4)
            at.text_input[0].set_value('-.5')
            at.text_input[1].set_value('2.5')
            at.text_input[2].set_value('-1.5')  # Incomplete second game is skipped.
            at.button[0].click().run()
            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[-1]['events'][1:], original['events'][1:])
            first = deepcopy(saved[-1]['events'][0])
            self.assertTrue(any('已校正並發布 1 場' in x.value for x in at.success))
            at.text_input[3].set_value('3.5')
            at.button[0].click().run()
            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[-1]['events'][0], first)
            self.assertEqual(saved[-1]['events'][2]['market']['total_line'], 3.5)
            self.assertIsNone(original['events'][0]['market'])
            rendered = MagicMock()
            render_snapshot(rendered, saved[-1], '人工校正正式推薦')
            self.assertTrue(any('人工校正涵蓋 2／3 場' in str(c) for c in rendered.caption.call_args_list))
            self.assertIn('未人工校正・保留原資料', rendered.markdown.call_args.args[0])

    def test_empty_incomplete_and_invalid_inputs_do_not_write(self):
        store = MagicMock()
        store.get_automatic_snapshot.return_value = snapshot()
        store.get_member_snapshot.return_value = (snapshot(), 'test')
        with patch('storage.create_store', return_value=(store, 'test')):
            at = AppTest.from_string(SCRIPT).run()
            at.button[0].click().run()
            store.save_manual_published.assert_not_called()
            at.text_input[0].set_value('-.5')
            at.button[0].click().run()
            store.save_manual_published.assert_not_called()
            at.text_input[1].set_value('invalid')
            at.button[0].click().run()
            self.assertTrue(at.error)
            self.assertEqual(len(at.exception), 0)
            store.save_manual_published.assert_not_called()

    def test_started_game_has_no_inputs_and_does_not_block_upcoming_game(self):
        original = snapshot()
        original['events'][0]['event']['kickoff'] = '2030-10-19T04:30:00Z'
        store = MagicMock()
        store.get_automatic_snapshot.return_value = original
        store.get_member_snapshot.return_value = (original, 'test')
        with patch('storage.create_store', return_value=(store, 'test')), patch('app.utc_now_iso', return_value='2030-10-19T05:00:00Z'), patch('service.utc_now_iso', return_value='2030-10-19T05:00:00Z'):
            at = AppTest.from_string(SCRIPT).run()
            self.assertEqual(len(at.text_input), 2)
            at.text_input[0].set_value('-.5')
            at.text_input[1].set_value('2.5')
            at.button[0].click().run()
            self.assertEqual(len(at.exception), 0)
            store.save_manual_published.assert_called_once()
            result = store.save_manual_published.call_args.args[2]
            self.assertEqual(result['events'][0], original['events'][0])
