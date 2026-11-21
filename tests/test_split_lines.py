"""Football Asian notation uses two equal stakes, never numeric division."""
from copy import deepcopy
from dataclasses import asdict
import unittest

from forecasting import score_projection
from providers import ProviderError
from service import apply_manual_calibration
from settlement_math import components, parse_manual_line, settle


class SplitLineTests(unittest.TestCase):
    def test_common_signed_zero_and_fullwidth_notation(self):
        for raw, expected in [('0/0.5', .25), ('-0/0.5', -.25),
                              ('-0.5/1', -.75), ('-1/1.5', -1.25),
                              ('-1/-1.5', -1.25), ('+0/+0.5', .25),
                              ('2/2.5', 2.25), ('2.5/3', 2.75),
                              (' －0 ／ 0.5 ', -.25), ('−0.5/1', -.75),
                              ('+1.25', 1.25), ('0', 0)]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_manual_line(raw, '盤口', allow_split=True), expected)

    def test_bad_or_ambiguous_inputs_fail_closed(self):
        for raw in ('', 'abc', '1/2/3', '/.5', '1/', '0/1', '2/3',
                    '2.25/2.75', '-.5/+.5', '-0/+0.5', '+0/-0.5',
                    '0.5/-1', 'nan', 'inf', '-inf', '1e309', '1e308',
                    '9' * 309 + '/0', '.3', '2/2', '2//2.5'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_manual_line(raw, '盤口', allow_split=True)

    def test_split_opt_in_and_quarter_settlement(self):
        with self.assertRaises(ValueError):
            parse_manual_line('-0/0.5', '盤口')
        spread = parse_manual_line('-0/0.5', '盤口', allow_split=True)
        total = parse_manual_line('2/2.5', '盤口', allow_split=True)
        self.assertEqual(components(spread), (-.5, 0))
        self.assertEqual(components(total), (2, 2.5))
        self.assertEqual(settle(0, spread), 'half_lost')
        self.assertEqual(settle(0, -spread), 'half_won')
        self.assertEqual(settle(2, -total), 'half_lost')  # Over 2/2.5, two goals
        self.assertEqual(settle(-2, total), 'half_won')  # Under 2/2.5

    def test_backend_split_equals_decimal_preserves_pass_and_original(self):
        original = {'sport': 'football', 'date_key': '2030-11-20',
                    'events': [{'event': {'event_id': 'test', 'home': 'Home', 'away': 'Away',
                                         'kickoff': '2030-11-21T04:00:00+08:00'},
                                'capture_schema': 3, 'projection': asdict(score_projection(1.8, .8, 'test')),
                                'market': None, 'recommendations': [], 'warnings': []}]}
        before = deepcopy(original)
        kwargs = {'calibrated_at': '2030-11-20T05:00:00Z'}
        split = apply_manual_calibration(original, {'test': ('-0/0.5', '2/2.5')}, **kwargs)
        decimal = apply_manual_calibration(original, {'test': (-.25, 2.25)}, **kwargs)
        self.assertEqual(split, decimal)
        self.assertEqual(original, before)
        recs = split['events'][0]['recommendations']
        self.assertTrue(all(r['decision'].startswith('PASS') for r in recs))
        self.assertEqual([r['state'] for r in recs], ['model', 'market_line', 'market_line'])
        for sport in ('nba', 'mlb'):
            original['sport'] = sport
            with self.subTest(sport=sport), self.assertRaises(ProviderError):
                apply_manual_calibration(original, {'test': ('-0/0.5', '2/2.5')}, **kwargs)
        original['sport'] = 'football'
        with self.assertRaises(ProviderError):
            apply_manual_calibration(original, {'test': ('-0/0.5', '-2/2.5')}, **kwargs)
