"""Regression: selecting D queries the next full Taipei civil day only."""
from copy import deepcopy
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from forecasting import basketball_projection
from models import DailySnapshot, Event, EventAnalysis
from providers import ProviderError
from service import (fetch_session_events, fetch_session_results, build_nba_snapshot,
                     build_mlb_snapshot, build_football_snapshot, save_snapshot,
                     settle_snapshot, apply_manual_calibration)
from storage import LocalSnapshotStore
from time_utils import (session_day_matches, session_window, filter_snapshot_window,
                        SESSION_POLICY, parse_time)
from ui import display_team

DAY = date(2026, 10, 25)


def event(key, time, sport="nba"):
    return Event(key, sport, time, "Los Angeles Lakers", "Boston Celtics", sport.upper())


def slate(sport="nba"):
    return [event(key, time, sport) for key, time in [
        ("previous-morning", "2026-10-25T08:00:00+08:00"),
        ("start", "2026-10-26T00:00:00+08:00"),
        ("evening", "2026-10-26T20:00:00+08:00"),
        ("overnight", "2026-10-25T18:00:00Z"),
        ("last", "2026-10-26T23:59:59.999999+08:00"),
        ("end", "2026-10-27T00:00:00+08:00"),
        ("next-evening", "2026-10-27T20:00:00+08:00"),
    ]]


def payload(rows=None):
    return {"sport": "nba", "date_key": DAY.isoformat(), "generated_at": "2026-10-25T02:00:00Z",
            "events": [{"event": asdict(e), "projection": None, "recommendations": []}
                       for e in (rows if rows is not None else slate())]}


class SessionWindowTests(unittest.TestCase):
    def test_requested_november_example_and_utc_conversion(self):
        start, end = session_window(date(2026, 11, 20))
        self.assertEqual(start.isoformat(), "2026-11-21T00:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-11-22T00:00:00+08:00")
        self.assertEqual(parse_time(start).isoformat(), "2026-11-20T16:00:00+00:00")
        self.assertTrue(session_day_matches("2026-11-21T15:59:59Z", date(2026, 11, 20)))
        self.assertFalse(session_day_matches("2026-11-21T16:00:00Z", date(2026, 11, 20)))

    def test_old_archive_restores_evening_and_preserves_recalculated_summary(self):
        original = payload([slate()[3]])
        archived_evening = payload([slate()[2]])["events"][0]
        archived_evening["prediction_history"] = [{"marker": "original-pregame"}]
        duplicate = deepcopy(original["events"][0])
        duplicate["projection"] = {"marker": "must-not-replace-active"}
        original["window_archived_events"] = [archived_evening, duplicate, payload([slate()[0]])["events"][0]]
        original["date_policy"] = "taipei-noon-v1"
        original["settlement_summary"] = {"stale": True}
        before = deepcopy(original)
        view = filter_snapshot_window(original)
        self.assertEqual(original, before)
        self.assertEqual(view["events"], [original["events"][0], archived_evening])
        self.assertEqual(len(view["window_archived_events"]), 2)
        self.assertNotIn("settlement_summary", view)
        self.assertEqual(view["date_policy"], "taipei-noon-v1")
        settled, _ = settle_snapshot(view, results={})
        self.assertIn("settlement_summary", settled)
        self.assertEqual(filter_snapshot_window(settled), settled)

    def test_next_midnight_boundaries_utc_and_naive_values(self):
        self.assertEqual([e.event_id for e in slate() if session_day_matches(e.kickoff, DAY)],
                         ["start", "evening", "overnight", "last"])
        for invalid in ("2026-10-26T02:00:00", "", None, "invalid"):
            self.assertFalse(session_day_matches(invalid, DAY))

    def test_adjacent_sessions_never_overlap_at_month_year_and_dst_boundaries(self):
        for day in (DAY, date(2026, 10, 31), date(2026, 12, 31), date(2028, 2, 29)):
            start, end = session_window(day)
            self.assertEqual((end-start).total_seconds(), 86400)
            self.assertTrue(session_day_matches(start.isoformat(), day))
            self.assertFalse(session_day_matches(end.isoformat(), day))
            self.assertTrue(session_day_matches(end.isoformat(), day+timedelta(days=1)))

    def test_fetch_next_civil_date_filters_sorts_and_deduplicates(self):
        fetch = Mock(return_value=list(reversed(slate())) + [slate()[3]])
        rows = fetch_session_events(fetch, DAY)
        self.assertEqual([e.event_id for e in rows], ["start", "overnight", "evening", "last"])
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [DAY+timedelta(days=1)])

    def test_conflicting_identity_and_partial_fetch_are_not_published(self):
        first = slate()[2]
        with self.assertRaises(ProviderError):
            fetch_session_events(Mock(return_value=[first, replace(first, home="Other")]), DAY)
        with self.assertRaises(ProviderError):
            fetch_session_events(Mock(side_effect=ProviderError("unavailable")), DAY)

    def test_all_builders_use_identical_window(self):
        for sport, builder, target in (("nba", build_nba_snapshot, "fetch_nba_events"),
                                       ("mlb", build_mlb_snapshot, "fetch_mlb_events"),
                                       ("football", build_football_snapshot, "fetch_football_events")):
            with self.subTest(sport=sport), patch("service."+target, return_value=slate(sport)), patch("service._upcoming", return_value=False):
                snapshot = builder(DAY)
                self.assertEqual([i.event.event_id for i in snapshot.events], ["start", "overnight", "evening", "last"])

    def test_profile_seasons_use_target_day_at_year_boundary(self):
        day, target = date(2030, 12, 31), date(2031, 1, 1)
        for sport, builder, targets in (
                ('nba', build_nba_snapshot, ['fetch_nba_team_profiles']),
                ('mlb', build_mlb_snapshot, ['fetch_mlb_team_profiles', 'fetch_mlb_pitcher_profiles']),
                ('football', build_football_snapshot, ['fetch_football_standings'])):
            row = Event('test', sport, '2031-01-01T04:00:00+08:00', 'Home', 'Away', '英超' if sport == 'football' else sport.upper())
            with self.subTest(sport=sport), ExitStack() as stack:
                stack.enter_context(patch('service.fetch_session_events', return_value=[row]))
                stack.enter_context(patch('service._upcoming', return_value=True))
                stack.enter_context(patch('service.fetch_odds', return_value=[]))
                stack.enter_context(patch('service.fetch_football_team_profiles', return_value={}))
                stack.enter_context(patch('service.fetch_football_injury_counts', return_value={}))
                sources = [stack.enter_context(patch('service.' + name, return_value={})) for name in targets]
                builder(day)
                for source in sources:
                    self.assertEqual(source.call_args.args[-1], target)
        with patch('service.fetch_football_events', side_effect=ProviderError('test')), patch('service.fetch_espn_football_events', return_value=[row]), patch('service._upcoming', return_value=True), patch('service.fetch_odds', return_value=[]), patch('service.fetch_espn_football_standings', return_value={}) as standings:
            build_football_snapshot(day)
            standings.assert_called_once_with(target)

    def test_football_fallback_uses_same_window(self):
        with patch("service.fetch_football_events", side_effect=ProviderError("unavailable")), patch("service.fetch_espn_football_events", return_value=slate("football")), patch("service._upcoming", return_value=False):
            self.assertEqual([i.event.event_id for i in build_football_snapshot(DAY).events], ["start", "overnight", "evening", "last"])

    def test_read_filter_preserves_original_archives_and_hides_stale_aggregate(self):
        original = payload()
        original["events"].append(deepcopy(original["events"][2]))
        original["settlement_summary"] = {"moneyline": {"won": 9}}
        unchanged = deepcopy(original)
        view = filter_snapshot_window(original)
        self.assertEqual(original, unchanged)
        self.assertEqual([i["event"]["event_id"] for i in view["events"]], ["start", "overnight", "evening", "last"])
        self.assertEqual(len(view["window_archived_events"]), 4)
        self.assertNotIn("settlement_summary", view)
        self.assertTrue(view["window_summary_needs_refresh"])
        self.assertEqual(filter_snapshot_window(view), view)
        self.assertNotIn("date_policy", view)  # Never pretend a legacy fetch is complete.

    def test_invalid_snapshot_date_fails_closed_without_losing_records(self):
        original = payload()
        original["date_key"] = "bad"
        filtered = filter_snapshot_window(original)
        self.assertEqual(filtered["events"], [])
        self.assertEqual(len(filtered["window_archived_events"]), len(original["events"]))

    def test_save_does_not_reintroduce_old_out_of_window_games(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalSnapshotStore(str(Path(root)/"test.db"))
            old = payload([slate()[0], slate()[2], slate()[-1]])
            store.save_automatic("nba", DAY.isoformat(), old)
            new_event = slate()[3]
            snapshot = DailySnapshot("nba", DAY.isoformat(), "2026-10-25T02:00:00Z",
                                     (EventAnalysis(new_event, None, None, ()),), ())
            save_snapshot(store, snapshot)
            saved = store.get_automatic_snapshot("nba", DAY.isoformat())
            self.assertEqual([i["event"]["event_id"] for i in saved["events"]], ["overnight", "evening"])
            self.assertEqual(len(saved["window_archived_events"]), 2)
            self.assertEqual(saved["date_policy"], SESSION_POLICY)
            save_snapshot(store, snapshot)
            again = store.get_automatic_snapshot("nba", DAY.isoformat())
            self.assertEqual(len(again["window_archived_events"]), 2)
            self.assertEqual(len(again["events"]), 2)

    def test_reject_out_of_window_save_without_touching_existing(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalSnapshotStore(str(Path(root)/"test.db"))
            original = payload([slate()[2]])
            store.save_automatic("nba", DAY.isoformat(), original)
            bad = DailySnapshot("nba", DAY.isoformat(), "2026-10-25T02:00:00Z",
                                (EventAnalysis(slate()[-1], None, None, ()),), ())
            with self.assertRaises(ProviderError):
                save_snapshot(store, bad)
            self.assertEqual(store.get_automatic_snapshot("nba", DAY.isoformat()), original)

    def test_result_lookup_includes_next_morning_and_settles_only_selected_ids(self):
        final = {"status": "final", "home_score": 110, "away_score": 100}
        with patch("service.fetch_final_results", return_value={"evening": final, "overnight": final, "next-evening": final}) as get:
            results = fetch_session_results("nba", DAY)
        self.assertEqual([c.args[1] for c in get.call_args_list], [DAY+timedelta(days=1)])
        updated, count = settle_snapshot(filter_snapshot_window(payload()), results=results)
        self.assertEqual(count, 2)
        self.assertNotIn("next-evening", [i["event"]["event_id"] for i in updated["events"]])
        self.assertNotIn("window_summary_needs_refresh", updated)

    def test_manual_calibration_only_requires_visible_window_games(self):
        original = payload([slate()[0], slate()[3]])
        for item in original["events"]:
            item["projection"] = asdict(basketball_projection(110, 105, "test"))
            item["capture_schema"] = 3
        calibrated = apply_manual_calibration(original, {"overnight": (-2.5, 215.5)},
                                              calibrated_at="2026-10-25T03:00:00Z")
        self.assertEqual(len(calibrated["events"]), 1)
        self.assertEqual(calibrated["events"][0]["event"]["home"], "Los Angeles Lakers")
        self.assertEqual(len(calibrated["window_archived_events"]), 1)
        self.assertEqual(len(original["events"]), 2)

    def test_display_names_normalize_case_accents_and_spaces_only_for_presentation(self):
        for source, expected in (("los angeles lakers", "洛杉磯湖人"), (" Boston  Celtics ", "波士頓塞爾提克"),
                                 ("Tampa Bay Rays", "坦帕灣光芒"), ("Milwaukee Brewers", "密爾瓦基釀酒人"),
                                 ("Atlético de Madrid", "馬德里競技"), ("Chelsea", "切爾西")):
            self.assertEqual(display_team(source), expected)
        self.assertEqual(display_team("Unknown Club"), "Unknown Club")


class SessionUiTests(unittest.TestCase):
    def test_admin_form_and_member_preview_share_filter_and_chinese_names(self):
        from streamlit.testing.v1 import AppTest
        future_day = date(2030, 10, 25)
        for sport, label, home, away, chinese in (
                ("nba", "NBA", "Los Angeles Lakers", "Boston Celtics", "洛杉磯湖人"),
                ("mlb", "MLB", "Milwaukee Brewers", "Tampa Bay Rays", "密爾瓦基釀酒人"),
                ("football", "歐洲足球", "Arsenal", "Chelsea", "阿森納")):
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as root:
                store = LocalSnapshotStore(str(Path(root)/"test.db"))
                snap = payload([slate()[0], slate()[3], slate()[-1]])
                snap.update(sport=sport, date_key=future_day.isoformat(), date_policy=SESSION_POLICY)
                for i in snap["events"]:
                    i["event"].update(home=home, away=away, sport=sport)
                    i["event"]["kickoff"] = i["event"]["kickoff"].replace("2026", "2030")
                    i["projection"] = asdict(basketball_projection(110, 105, "test"))
                    i["capture_schema"] = 3
                store.save_automatic(sport, future_day.isoformat(), snap)
                stored_before = store.get_automatic_snapshot(sport, future_day.isoformat())
                with patch.dict(os.environ, {"APP_ADMIN_PASSWORD": "test-admin-only", "APP_MEMBER_PASSWORD": "test-member-only"}), patch("storage.create_store", return_value=(store, "測試")):
                    at = AppTest.from_file(str(Path(__file__).resolve().parents[1]/"app.py")).run()
                    at.radio[0].set_value("管理員").run()
                    at.text_input[0].set_value("test-admin-only")
                    at.button[0].click().run()
                    next(r for r in at.radio if r.label == "管理項目").set_value(label)
                    at.date_input[0].set_value(future_day).run()
                    self.assertEqual(len(at.exception), 0)
                    self.assertEqual([i.label for i in at.text_input], [chinese+" 主隊讓分", "大小總分"])
                    self.assertTrue(any("2030/10/26 00:00～23:59" in c.value for c in at.caption))
                    headings = [m.value for m in at.markdown if m.value.startswith("**1.")]
                    self.assertEqual(len(headings), 1)
                    self.assertIn(chinese, headings[0])
                    self.assertNotIn(home, headings[0])
                    next(r for r in at.radio if r.label == "功能").set_value("會員畫面預覽").run()
                    next(r for r in at.radio if r.label == "賽事類別").set_value(label)
                    at.date_input[0].set_value(future_day).run()
                    self.assertEqual(len(at.exception), 0)
                    table = "".join(m.value for m in at.markdown)
                    self.assertIn("10/26 02:00", table)
                    self.assertNotIn("10/25 08:00", table)
                    self.assertNotIn("10/27 20:00", table)
                    self.assertEqual(store.get_automatic_snapshot(sport, future_day.isoformat()), stored_before)


if __name__ == "__main__":
    unittest.main()
