"""Regression: selected evening plus next morning, not two full calendar days."""
from copy import deepcopy
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
        ("start", "2026-10-25T12:00:00+08:00"),
        ("evening", "2026-10-25T20:00:00+08:00"),
        ("overnight", "2026-10-25T18:00:00Z"),
        ("last", "2026-10-26T11:59:59+08:00"),
        ("end", "2026-10-26T12:00:00+08:00"),
        ("next-evening", "2026-10-26T20:00:00+08:00"),
    ]]


def payload(rows=None):
    return {"sport": "nba", "date_key": DAY.isoformat(), "generated_at": "2026-10-25T02:00:00Z",
            "events": [{"event": asdict(e), "projection": None, "recommendations": []}
                       for e in (rows if rows is not None else slate())]}


class SessionWindowTests(unittest.TestCase):
    def test_noon_boundaries_utc_and_naive_values(self):
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

    def test_fetch_two_civil_dates_filters_sorts_and_deduplicates(self):
        fetch = Mock(return_value=list(reversed(slate())) + [slate()[3]])
        rows = fetch_session_events(fetch, DAY)
        self.assertEqual([e.event_id for e in rows], ["start", "evening", "overnight", "last"])
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [DAY, DAY+timedelta(days=1)])

    def test_conflicting_identity_and_partial_fetch_are_not_published(self):
        first = slate()[2]
        with self.assertRaises(ProviderError):
            fetch_session_events(Mock(side_effect=[[first], [replace(first, home="Other")]]), DAY)
        with self.assertRaises(ProviderError):
            fetch_session_events(Mock(side_effect=[[first], ProviderError("unavailable")]), DAY)

    def test_all_builders_use_identical_window(self):
        for sport, builder, target in (("nba", build_nba_snapshot, "fetch_nba_events"),
                                       ("mlb", build_mlb_snapshot, "fetch_mlb_events"),
                                       ("football", build_football_snapshot, "fetch_football_events")):
            with self.subTest(sport=sport), patch("service."+target, return_value=slate(sport)), patch("service._upcoming", return_value=False):
                snapshot = builder(DAY)
                self.assertEqual([i.event.event_id for i in snapshot.events], ["start", "evening", "overnight", "last"])

    def test_football_fallback_uses_same_window(self):
        with patch("service.fetch_football_events", side_effect=ProviderError("unavailable")), patch("service.fetch_espn_football_events", return_value=slate("football")), patch("service._upcoming", return_value=False):
            self.assertEqual([i.event.event_id for i in build_football_snapshot(DAY).events], ["start", "evening", "overnight", "last"])

    def test_read_filter_preserves_original_archives_and_hides_stale_aggregate(self):
        original = payload()
        original["events"].append(deepcopy(original["events"][2]))
        original["settlement_summary"] = {"moneyline": {"won": 9}}
        unchanged = deepcopy(original)
        view = filter_snapshot_window(original)
        self.assertEqual(original, unchanged)
        self.assertEqual([i["event"]["event_id"] for i in view["events"]], ["start", "evening", "overnight", "last"])
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
            self.assertEqual([i["event"]["event_id"] for i in saved["events"]], ["evening", "overnight"])
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
        with patch("service.fetch_final_results", side_effect=[{"evening": final}, {"overnight": final, "next-evening": final}]) as get:
            results = fetch_session_results("nba", DAY)
        self.assertEqual([c.args[1] for c in get.call_args_list], [DAY, DAY+timedelta(days=1)])
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
                    at.radio[0].set_value("管理員")
                    at.text_input[0].set_value("test-admin-only")
                    at.button[0].click().run()
                    next(r for r in at.radio if r.label == "管理項目").set_value(label)
                    at.date_input[0].set_value(future_day).run()
                    self.assertEqual(len(at.exception), 0)
                    self.assertEqual([i.label for i in at.text_input], [chinese+" 主隊讓分", "大小總分"])
                    self.assertTrue(any("2030/10/26 12:00" in c.value for c in at.caption))
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
                    self.assertNotIn("10/26 20:00", table)
                    self.assertEqual(store.get_automatic_snapshot(sport, future_day.isoformat()), stored_before)


if __name__ == "__main__":
    unittest.main()
