import io
import json
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import daily_snapshots as daily
from forecasting import basketball_projection, probability_recommendations
from models import DailySnapshot, Event, EventAnalysis
from providers import NoEventsError, ProviderError
from service import apply_manual_calibration, save_snapshot, build_mlb_snapshot, build_nba_snapshot, build_football_snapshot
from storage import LocalSnapshotStore, StorageError, SupabaseSnapshotStore


class DailySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocalSnapshotStore(str(Path(self.tmp.name) / 'test.sqlite'))
        self.day = date(2030, 11, 13)
        projection = basketball_projection(115, 110, 'test inputs')
        event = Event('nba:test', 'nba', '2030-11-13T12:00:00Z', 'Home', 'Away', 'NBA')
        self.snapshot = DailySnapshot('nba', self.day.isoformat(), '2030-11-13T04:01:00Z',
            (EventAnalysis(event, projection, None, probability_recommendations(projection, None, sport='nba')),), ())

    def test_noon_boundary_and_delayed_run(self):
        for stamp, expected in [('2030-11-13T03:59:59+00:00', '2030-11-12'),
                                ('2030-11-13T04:00:00+00:00', '2030-11-13'),
                                ('2030-11-13T20:00:00+00:00', '2030-11-13')]:
            self.assertEqual(daily.current_session_day(datetime.fromisoformat(stamp)).isoformat(), expected)
        with self.assertRaises(ValueError):
            daily.current_session_day(datetime(2030, 11, 13))

    def test_saved_visible_then_manual_overrides_and_rerun_preserves(self):
        builder = Mock(return_value=self.snapshot)
        self.assertEqual(daily.run_one(self.store, 'nba', self.day, builder=builder)['status'], 'saved')
        automatic, _ = self.store.get_member_snapshot('nba', self.day.isoformat())
        self.assertEqual(automatic['events'][0]['event']['event_id'], 'nba:test')
        self.assertTrue(all(r['decision'].startswith('PASS') for r in automatic['events'][0]['recommendations']))
        manual = apply_manual_calibration(automatic, {'nba:test': (-3.5, 222.5)},
                                         calibrated_at='2030-11-13T05:00:00Z')
        self.store.save_manual_published('nba', self.day.isoformat(), manual)
        self.assertEqual(daily.run_one(self.store, 'nba', self.day, builder=builder)['status'], 'existing_preserved')
        builder.assert_called_once_with(self.day)
        shown, label = self.store.get_member_snapshot('nba', self.day.isoformat())
        self.assertEqual(shown, manual)
        self.assertIn('人工校正', label)
        self.assertEqual(self.store.get_automatic_snapshot('nba', self.day.isoformat()), automatic)

    def test_no_events_does_not_write_or_claim_failure(self):
        report = daily.run_one(self.store, 'nba', self.day, builder=Mock(side_effect=NoEventsError('none')))
        self.assertEqual(report, {'status': 'no_events', 'events': 0})
        self.assertIsNone(self.store.get_automatic_snapshot('nba', self.day.isoformat()))

    def test_each_empty_schedule_has_typed_result(self):
        for builder in (build_mlb_snapshot, build_nba_snapshot, build_football_snapshot):
            with patch('service.fetch_session_events', return_value=[]), self.assertRaises(NoEventsError):
                builder(self.day)

    def test_source_failure_and_mismatched_snapshot_do_not_write(self):
        for builder in (Mock(side_effect=ProviderError('private source error')),
                        Mock(return_value=DailySnapshot('mlb', self.day.isoformat(), '', (), ()))):
            with self.assertRaises(ProviderError):
                daily.run_one(self.store, 'nba', self.day, builder=builder)
            self.assertIsNone(self.store.get_automatic_snapshot('nba', self.day.isoformat()))

    def test_admin_write_during_fetch_is_preserved(self):
        def competing_builder(day):
            save_snapshot(self.store, self.snapshot)
            return self.snapshot
        with self.assertRaises(StorageError):
            daily.run_one(self.store, 'nba', self.day, builder=competing_builder)
        self.assertEqual(len(self.store.get_automatic_snapshot('nba', self.day.isoformat())['events'][0]['prediction_history']), 1)

    def test_insert_race_compare_and_swap_rejects_overwrite(self):
        original = self.store.save_automatic
        def concurrent_save(sport, day, payload, **kwargs):
            original(sport, day, {**payload, 'revision_id': 'concurrent-winner'})
            original(sport, day, payload, **kwargs)
        with patch.object(self.store, 'save_automatic', side_effect=concurrent_save), self.assertRaises(StorageError):
            daily.run_one(self.store, 'nba', self.day, builder=Mock(return_value=self.snapshot))
        self.assertEqual(self.store.get_automatic_snapshot('nba', self.day.isoformat())['revision_id'], 'concurrent-winner')

    def test_cli_rejects_local_storage_before_fetch(self):
        with patch('daily_snapshots.create_store', return_value=(self.store, 'local')), patch('daily_snapshots.run_one') as runner, patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(daily.main(['--sport', 'nba']), 1)
            runner.assert_not_called()
            self.assertEqual(json.loads(out.getvalue())['code'], 'STORAGE_CONFIG')

    def test_cli_sanitizes_errors_and_reports_nonzero(self):
        store = Mock(spec=SupabaseSnapshotStore)
        for error in (RuntimeError('sensitive-test-key'), ProviderError('sensitive-test-key'),
                      StorageError('sensitive-test-key', code='sensitive-test-key')):
            with patch('daily_snapshots.create_store', return_value=(store, 'test')), patch('daily_snapshots.run_one', side_effect=error), patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(daily.main(['--sport', 'football']), 1)
                self.assertNotIn('sensitive-test-key', out.getvalue())

    def test_cli_success_checks_database_before_building(self):
        store = Mock(spec=SupabaseSnapshotStore)
        def run(*args):
            store.check_read_access.assert_called_once()
            return {'status': 'saved', 'events': 2}
        with patch('daily_snapshots.create_store', return_value=(store, 'test')), patch('daily_snapshots.run_one', side_effect=run), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(daily.main(['--sport', 'nba']), 0)

    def test_workflow_schedule_and_no_pr_write_trigger(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/daily-snapshots.yml').read_text()
        self.assertIn("cron: '0 4 * * *'", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn('sport: [mlb, nba, football]', text)
        self.assertNotIn('pull_request:', text)
        self.assertNotIn('push:', text)
        self.assertIn('cancel-in-progress: false', text)
