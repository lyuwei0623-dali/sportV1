from copy import deepcopy
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch
import unittest

from streamlit.testing.v1 import AppTest
from ui import render_snapshot, _member_recommendation
from time_utils import SESSION_POLICY


def snapshot(sport='nba'):
    recs = []
    for kind, state, selection, probability in (
        ('moneyline', 'model', '主隊勝', .654),
        ('spread', 'market_line', '客隊 +4.5', .563),
        ('total', 'market_line', '小分 220.5', .572),
    ):
        recs.append(dict(market_type=kind, state=state, selection=selection,
                         model_probability=probability, decimal_price=1.95, expected_return=.1,
                         decision='PASS｜模型尚未完成樣本外校準', actionable=False,
                         settlement_probabilities={'won': .5, 'half_won': .1, 'push': .1, 'half_lost': .1, 'lost': .2}))
    return {'sport': sport, 'date_key': '2030-10-19', 'date_policy': SESSION_POLICY,
            'version': 'audit-v3.3', 'generated_at': '2030-10-19T01:00:00Z',
            'events': [{'event': {'event_id': 'demo', 'home': 'Boston Celtics', 'away': 'New York Knicks',
                                  'kickoff': '2030-10-20T00:30:00Z', 'league': '測試聯賽'},
                        'projection': {'home_expected': 117.3, 'away_expected': 102, 'home_win': .654,
                                       'away_win': .346, 'draw': .1, 'score': '2:1', 'score_probability': .12,
                                       'notes': ['模型診斷文字']},
                        'market': {'bookmaker': '測試盤源', 'updated_at': '2030-10-19T01:00:00Z'},
                        'recommendations': recs, 'warnings': ['診斷內容保留後台'],
                        'settlement': {'status': 'final', 'home_score': 118, 'away_score': 103,
                                       'picks': {'moneyline': {'outcome': 'won', 'evaluation_eligible': True},
                                                 'spread': {'outcome': 'lost', 'evaluation_eligible': True},
                                                 'total': {'outcome': 'half_lost', 'evaluation_eligible': True}}}}]}


class Table(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.headers, self.rows, self.current = [], [], None
        self.cell, self.kind = '', None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.current = []
        if tag in ('th', 'td'):
            self.kind, self.cell = tag, ''

    def handle_data(self, data):
        if self.kind:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in ('th', 'td'):
            if self.kind == 'th':
                self.headers.append(self.cell)
            else:
                self.current.append(self.cell)
            self.kind = None
        if tag == 'tr' and self.current:
            self.rows.append(self.current)


def rendered(payload, member_view):
    st = MagicMock()
    render_snapshot(st, payload, '測試快照', member_view=member_view)
    return st, st.markdown.call_args.args[0]


class MemberCompactTests(unittest.TestCase):
    def test_core_columns_identical_for_all_sports_and_risk_column_removed(self):
        for sport in ('mlb', 'nba', 'football'):
            with self.subTest(sport=sport):
                payload = snapshot(sport)
                before = deepcopy(payload)
                _, full = rendered(payload, False)
                st, compact = rendered(payload, True)
                old, new = Table(full), Table(compact)
                self.assertEqual(new.headers, old.headers[:-1])
                first_columns = 6 if sport == 'football' else 5
                self.assertEqual(new.rows[-1][:first_columns], old.rows[-1][:first_columns])
                self.assertEqual(len(new.rows[-1]), len(new.headers))
                self.assertEqual(len(new.headers), 9 if sport == 'football' else 8)
                if sport == 'football':
                    self.assertIn('colspan="9"', compact)
                for hidden in ('保存的實際盤口線', '未驗證', 'PASS', '模型資料／風險', '估計 EV', '模型診斷文字', '測試盤源'):
                    self.assertNotIn(hidden, compact)
                for kept in ('主隊勝', '客隊 +4.5', '小分 220.5', '65.4%', '56.3%', '57.2%', 'settled-won', 'settled-lost'):
                    self.assertIn(kept, compact)
                self.assertIn('模型資料／風險', full)
                self.assertIn('PASS｜模型尚未完成樣本外校準', full)
                self.assertIn('保存的實際盤口線', full)
                self.assertEqual(payload, before)
                self.assertTrue(any('尚未完成樣本外驗證' in str(c) for c in st.caption.call_args_list))
                st.expander.assert_called_once_with('機率與結算說明')

    def test_reference_and_missing_data_stay_explicit_without_invented_percent(self):
        rec = snapshot()['events'][0]['recommendations'][1]
        rec['state'] = 'model_reference'
        html = _member_recommendation(rec)
        self.assertIn('模型參考線（非實盤）', html)
        self.assertIn('客隊 +4.5', html)
        self.assertIn('56.3%', html)
        for value in ({}, {**rec, 'model_probability': None}, {**rec, 'state': 'unavailable'}):
            html = _member_recommendation(value)
            self.assertIn('資料不足', html)
            self.assertNotIn('%', html)

    def test_all_settlement_labels_colors_and_exclusions_preserved(self):
        rec = snapshot()['events'][0]['recommendations'][1]
        for outcome, label, css in (('won', '全贏', 'won'), ('half_won', '半贏', 'won'),
                                     ('lost', '全輸', 'lost'), ('half_lost', '半輸', 'lost'), ('push', '走盤', 'push')):
            with self.subTest(outcome=outcome):
                html = _member_recommendation(rec, {'outcome': outcome, 'evaluation_eligible': True})
                self.assertIn(label, html)
                self.assertIn('settled-' + css, html)
        self.assertIn('不計績效', _member_recommendation(rec, {'outcome': 'won', 'evaluation_eligible': False}))

    def test_partial_calibration_and_unavailable_rows_are_not_misrepresented(self):
        payload = snapshot()
        payload['release_type'] = 'manual_calibrated'
        payload['events'][0]['manual_calibration'] = {'home_spread': -4.5, 'total': 220.5}
        missing = deepcopy(payload['events'][0])
        missing['event']['event_id'] = 'missing'
        missing.pop('manual_calibration')
        missing['projection'] = None
        missing['recommendations'] = []
        payload['events'].append(missing)
        st, html = rendered(payload, True)
        self.assertIn('已人工校正', html)
        self.assertIn('未人工校正', html)
        self.assertIn('資料不足', html)
        self.assertTrue(any('1／2 場' in str(c) for c in st.caption.call_args_list))
        self.assertTrue(all(len(r) == 8 for r in Table(html).rows))

    def test_selection_is_html_escaped(self):
        rec = snapshot()['events'][0]['recommendations'][0]
        rec['selection'] = '<script>unsafe</script>'
        html = _member_recommendation(rec)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_member_page_and_admin_member_preview_use_compact_renderer(self):
        store = MagicMock()
        store.get_member_snapshot.return_value = (snapshot(), '測試')
        with patch('storage.create_store', return_value=(store, 'test')):
            app = AppTest.from_string('''
from app import _member_page
from storage import create_store
store, _ = create_store()
_member_page(store)
''').run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any('snapshot member-table' in m.value for m in app.markdown))
            self.assertFalse(any('模型資料／風險' in m.value for m in app.markdown))
            self.assertTrue(any('尚未完成樣本外驗證' in c.value for c in app.caption))
