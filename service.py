"""Snapshot orchestration for the probability-first rebuild.

Only this module calls providers and models. Member pages consume a previously
saved snapshot; they never fetch live data or recalculate probabilities.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date
from math import isfinite
from typing import Any, Mapping

from forecasting import baseball_projection, basketball_projection, probability_recommendations, score_projection
from models import DailySnapshot, Event, EventAnalysis, Market, Projection, Recommendation, utc_now_iso
from providers import (
    FOOTBALL_LEAGUES,
    ProviderError,
    fetch_football_events,
    fetch_football_injury_counts,
    fetch_football_standings,
    fetch_football_team_profiles,
    fetch_mlb_events,
    fetch_mlb_pitcher_profiles,
    fetch_mlb_team_profiles,
    fetch_nba_events,
    fetch_nba_team_profiles,
    fetch_odds,
    market_for_event,
)
from storage import SnapshotStore


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _record_pct(record: Any) -> float | None:
    try:
        wins, losses = float(record.get("wins", 0)), float(record.get("losses", 0))
        return wins / (wins + losses) if wins + losses else None
    except (AttributeError, TypeError, ValueError):
        return None


def _unavailable_recommendations(reason: str) -> tuple[Recommendation, ...]:
    return tuple(Recommendation(kind, "unavailable", "資料不足", None, None, reason)
                 for kind in ("moneyline", "spread", "total"))


def _mlb_projection(event: Event, team_profiles: dict[int, dict[str, float]],
                    pitcher_profiles: dict[int, dict[str, float]]) -> Projection | None:
    """Use team offence/defence plus the announced probable starter.

    Every adjustment below is tied to a saved baseball input: runs per game,
    OPS, team ERA/WHIP, starter ERA/WHIP and season record.  It does not use
    bookmaker prices or a generic league score.
    """

    home_id, away_id = event.metadata.get("home_team_id"), event.metadata.get("away_team_id")
    home_team = team_profiles.get(int(home_id)) if home_id else None
    away_team = team_profiles.get(int(away_id)) if away_id else None
    home_pct = _record_pct(event.metadata.get("home_record"))
    away_pct = _record_pct(event.metadata.get("away_record"))
    notes: list[str] = []
    if home_team and away_team and home_team.get("runs_per_game") and away_team.get("runs_per_game"):
        # OPS keeps two teams with the same raw run average from becoming
        # indistinguishable. WHIP complements ERA as a baserunner indicator.
        home_offence = home_team["runs_per_game"] * _clamp(1 + ((home_team.get("ops") or .720) - .720) * .75, .89, 1.12)
        away_offence = away_team["runs_per_game"] * _clamp(1 + ((away_team.get("ops") or .720) - .720) * .75, .89, 1.12)
        away_staff = (away_team.get("era") or 4.25) + ((away_team.get("whip") or 1.30) - 1.30) * .65
        home_staff = (home_team.get("era") or 4.25) + ((home_team.get("whip") or 1.30) - 1.30) * .65
        home_rate = .62 * home_offence + .38 * away_staff + .14
        away_rate = .62 * away_offence + .38 * home_staff
        notes.append("團隊得分／OPS／ERA／WHIP")
        home_starter = pitcher_profiles.get(int(event.metadata["home_pitcher_id"])) if event.metadata.get("home_pitcher_id") else None
        away_starter = pitcher_profiles.get(int(event.metadata["away_pitcher_id"])) if event.metadata.get("away_pitcher_id") else None
        if away_starter and away_starter.get("innings", 0) >= 15 and away_starter.get("era", 0) > 0:
            home_rate *= _clamp(
                1 + (away_starter["era"] - 4.20) * .045 + ((away_starter.get("whip") or 1.30) - 1.30) * .12,
                .80, 1.22,
            )
            notes.append("客隊先發投手 ERA／WHIP")
        else:
            notes.append("客隊先發待確認")
        if home_starter and home_starter.get("innings", 0) >= 15 and home_starter.get("era", 0) > 0:
            away_rate *= _clamp(
                1 + (home_starter["era"] - 4.20) * .045 + ((home_starter.get("whip") or 1.30) - 1.30) * .12,
                .80, 1.22,
            )
            notes.append("主隊先發投手 ERA／WHIP")
        else:
            notes.append("主隊先發待確認")
        if home_pct is not None and away_pct is not None:
            # A deliberately light form adjustment: team performance matters,
            # but cannot overwhelm actual offence / pitching statistics.
            home_rate *= _clamp(1 + (home_pct - .50) * .08, .94, 1.06)
            away_rate *= _clamp(1 + (away_pct - .50) * .08, .94, 1.06)
            notes.append("賽季勝率")
        return baseball_projection(_clamp(home_rate, 2.3, 7.2), _clamp(away_rate, 2.2, 7.0),
                                   "MLB 團隊攻守＋先發投手資料｜" + "、".join(notes))
    if home_pct is None or away_pct is None:
        return None
    difference = home_pct - away_pct
    home_rate = _clamp(4.50 + difference * 1.30 + .16, 2.6, 6.8)
    away_rate = _clamp(4.32 - difference * 1.10, 2.4, 6.6)
    return baseball_projection(home_rate, away_rate, "MLB 賽季戰績資料（詳細球隊／先發資料暫缺）")


def _football_projection(event: Event, standings: dict[int, dict[int, dict[str, float]]],
                         team_profiles: dict[tuple[int, int, int, str], dict[str, float]],
                         injuries: dict[str, dict[str, int]]) -> Projection | None:
    """Build a football score model from home/away team stats and availability."""

    league_id, season = int(event.metadata.get("league_id") or 0), int(event.metadata.get("season") or 0)
    home_id, away_id = int(event.metadata.get("home_team_id") or 0), int(event.metadata.get("away_team_id") or 0)
    home_profile = team_profiles.get((league_id, season, home_id, "home"))
    away_profile = team_profiles.get((league_id, season, away_id, "away"))
    league_table = standings.get(league_id, {})
    if home_profile and away_profile and home_profile.get("played", 0) >= 3 and away_profile.get("played", 0) >= 3:
        team_rows = list(league_table.values())
        league_rate = (sum(row["for"] for row in team_rows) / sum(row["played"] for row in team_rows)
                       if team_rows and sum(row["played"] for row in team_rows) else 1.35)
        league_rate = _clamp(league_rate, .8, 2.2)
        home_rate = home_profile["for_average"] * _clamp(away_profile["against_average"] / league_rate, .60, 1.60) * 1.05
        away_rate = away_profile["for_average"] * _clamp(home_profile["against_average"] / league_rate, .60, 1.60) * .95
        # Form, clean-sheet and failed-to-score data are real team inputs. The
        # coefficients are deliberately small so they refine, not replace,
        # the stronger home/away scoring record.
        home_rate *= _clamp(1 + (home_profile.get("form_ratio", .5) - .5) * .12
                            - home_profile.get("failed_to_score_rate", 0) * .06
                            - away_profile.get("clean_sheet_rate", 0) * .05, .84, 1.16)
        away_rate *= _clamp(1 + (away_profile.get("form_ratio", .5) - .5) * .12
                            - away_profile.get("failed_to_score_rate", 0) * .06
                            - home_profile.get("clean_sheet_rate", 0) * .05, .84, 1.16)
        availability = injuries.get(event.event_id)
        notes = ["主客場進失球", "近期狀態／零封／失球率", "聯賽攻守強度"]
        if availability:
            # The source gives a verified count but not a universally reliable
            # player-value rating, so apply only a small, transparent penalty.
            home_rate *= _clamp(1 - .018 * availability.get("home", 0), .86, 1.0)
            away_rate *= _clamp(1 - .018 * availability.get("away", 0), .86, 1.0)
            notes.append(f"傷停名單 主{availability.get('home', 0)}／客{availability.get('away', 0)}")
        return score_projection(_clamp(home_rate, .25, 3.7), _clamp(away_rate, .20, 3.3),
                                "API-Football 團隊資料｜" + "、".join(notes))
    # Fallback is differentiated standings data—not a fixed league prior and
    # never a market-derived probability.
    home, away = league_table.get(home_id), league_table.get(away_id)
    if home and away:
        team_rows = list(league_table.values())
        league_rate = sum(row["for"] for row in team_rows) / sum(row["played"] for row in team_rows)
        home_attack = (home["for"] / home["played"]) / league_rate
        home_defence = (home["against"] / home["played"]) / league_rate
        away_attack = (away["for"] / away["played"]) / league_rate
        away_defence = (away["against"] / away["played"]) / league_rate
        return score_projection(_clamp(league_rate * home_attack * away_defence * 1.08, .25, 3.7),
                                _clamp(league_rate * away_attack * home_defence * .92, .20, 3.3),
                                "API-Football 聯賽團隊攻守資料（主客細項暫缺）")
    return None


def _nba_projection(event: Event, team_profiles: dict[int, dict[str, float]]) -> Projection | None:
    """NBA score model from team scoring, defence and season win rate."""

    home_id, away_id = event.metadata.get("home_team_id"), event.metadata.get("away_team_id")
    home = team_profiles.get(int(home_id)) if home_id else None
    away = team_profiles.get(int(away_id)) if away_id else None
    if not home or not away or min(home.get("games", 0), away.get("games", 0)) < 3:
        return None
    home_rate = .58 * home["for"] + .42 * away["against"] + 1.8
    away_rate = .58 * away["for"] + .42 * home["against"] - .4
    home_rate *= _clamp(1 + (home.get("win_pct", .5) - .5) * .10, .94, 1.06)
    away_rate *= _clamp(1 + (away.get("win_pct", .5) - .5) * .10, .94, 1.06)
    return basketball_projection(_clamp(home_rate, 92, 132), _clamp(away_rate, 90, 130),
                                 "NBA 團隊得失分＋賽季勝率資料")


def build_mlb_snapshot(day: date) -> DailySnapshot:
    events = fetch_mlb_events(day)
    if not events:
        raise ProviderError("當日沒有可建立的 MLB 賽事；未覆蓋既有快照")
    team_profiles = fetch_mlb_team_profiles(events, day)
    pitcher_profiles = fetch_mlb_pitcher_profiles(events, day)
    notes = ["MLB Stats API 賽程、球隊打擊／投球、預定先發"]
    try:
        odds = fetch_odds("baseball_mlb")
        notes.append("The Odds API 讓分／大小線")
    except ProviderError:
        odds = []
        notes.append("讓分／大小線暫不可用")
    analyses: list[EventAnalysis] = []
    for event in events:
        market = market_for_event(event, odds)
        projection = _mlb_projection(event, team_profiles, pitcher_profiles)
        if projection is None:
            analyses.append(EventAnalysis(event, None, market, _unavailable_recommendations("缺少足夠球隊資料，拒絕產生虛構機率"),
                                          ("球隊或先發資料不足",)))
            continue
        recommendations = probability_recommendations(projection, market, sport="mlb")
        warnings = [f"模型資料：{projection.source_quality}", "正式先發、打線、牛棚使用與天氣仍可能在開賽前改變"]
        if market is None or market.spread_line is None or market.total_line is None:
            warnings.append("缺少的讓分／大小線會顯示模型基準線，非實際可下注盤口")
        analyses.append(EventAnalysis(event, projection, market, recommendations, tuple(warnings)))
    return DailySnapshot("mlb", day.isoformat(), utc_now_iso(), tuple(analyses), tuple(notes))


def build_nba_snapshot(day: date) -> DailySnapshot:
    events = fetch_nba_events(day)
    if not events:
        raise ProviderError("當日沒有可建立的 NBA 賽事；未覆蓋既有快照")
    team_profiles = fetch_nba_team_profiles(events, day)
    notes = ["ESPN NBA 賽程、球隊得失分與賽季勝率"]
    try:
        odds = fetch_odds("basketball_nba", regions="us")
        notes.append("The Odds API 讓分／大小線")
    except ProviderError:
        odds = []
        notes.append("讓分／大小線暫不可用")
    analyses: list[EventAnalysis] = []
    for event in events:
        market = market_for_event(event, odds)
        projection = _nba_projection(event, team_profiles)
        if projection is None:
            analyses.append(EventAnalysis(event, None, market, _unavailable_recommendations(
                "NBA 球隊得失分資料不足；拒絕產生虛構機率"), ("傷停與最終輪換仍待開賽前確認",)))
            continue
        recommendations = probability_recommendations(projection, market, sport="nba")
        warnings = [f"模型資料：{projection.source_quality}", "傷停與最終輪換仍以開賽前公告為準"]
        if market is None or market.spread_line is None or market.total_line is None:
            warnings.append("缺少的讓分／大小線會顯示模型基準線，非實際可下注盤口")
        analyses.append(EventAnalysis(event, projection, market, recommendations, tuple(warnings)))
    return DailySnapshot("nba", day.isoformat(), utc_now_iso(), tuple(analyses), tuple(notes))


def build_football_snapshot(day: date) -> DailySnapshot:
    events = fetch_football_events(day)
    if not events:
        raise ProviderError("當日沒有可建立的歐洲足球賽事；未覆蓋既有快照")
    notes = ["API-Football 賽程"]
    try:
        standings = fetch_football_standings(day)
        team_profiles = fetch_football_team_profiles(events)
        notes.append("API-Football 團隊主客場攻守資料")
    except ProviderError:
        standings, team_profiles = {}, {}
        notes.append("團隊統計暫不可用")
    injuries = fetch_football_injury_counts(events)
    if injuries:
        notes.append("API-Football 傷停名單")
    odds_by_league: dict[str, list[dict[str, Any]]] = {}
    for league in {event.league for event in events}:
        sport_key = FOOTBALL_LEAGUES[league][0]
        try:
            odds_by_league[league] = fetch_odds(sport_key)
        except ProviderError:
            odds_by_league[league] = []
            notes.append(f"{league} 讓分／大小線暫不可用")
    analyses: list[EventAnalysis] = []
    for event in events:
        market = market_for_event(event, odds_by_league[event.league])
        projection = _football_projection(event, standings, team_profiles, injuries)
        if projection is None:
            analyses.append(EventAnalysis(event, None, market, _unavailable_recommendations(
                "球隊攻守與傷停資料不足；拒絕使用固定聯賽底板"),
                ("傷停／先發尚未確認",)))
            continue
        recommendations = probability_recommendations(projection, market, sport="football")
        warnings = [f"模型資料：{projection.source_quality}", "傷停、停賽與先發仍以開賽前最終公告為準"]
        injury = injuries.get(event.event_id)
        if injury:
            warnings.append(f"目前傷停名單：主隊 {injury.get('home', 0)} 位／客隊 {injury.get('away', 0)} 位")
        if market is None or market.spread_line is None or market.total_line is None:
            warnings.append("缺少的讓分／大小線會顯示模型基準線，非實際可下注盤口")
        analyses.append(EventAnalysis(event, projection, market, recommendations, tuple(warnings)))
    return DailySnapshot("football", day.isoformat(), utc_now_iso(), tuple(analyses), tuple(dict.fromkeys(notes)))


def save_snapshot(store: SnapshotStore, snapshot: DailySnapshot) -> None:
    """Save only after an entire non-empty slate has been assembled."""

    if not snapshot.events:
        raise ProviderError("空賽程不可覆蓋已保存快照")
    store.save_automatic(snapshot.sport, snapshot.date_key, snapshot.as_dict())


def _projection_from_saved_payload(payload: Mapping[str, Any]) -> Projection:
    """Restore the small, stable projection contract from a saved snapshot.

    Manual calibration deliberately does *not* fetch a provider or rebuild the
    team model.  It reuses the already saved pre-match model and recalculates
    the two probabilities that depend on a real handicap / total line.
    """

    try:
        draw = payload.get("draw")
        return Projection(
            home_expected=float(payload["home_expected"]),
            away_expected=float(payload["away_expected"]),
            home_win=float(payload["home_win"]),
            draw=None if draw is None else float(draw),
            away_win=float(payload["away_win"]),
            score=str(payload["score"]),
            score_probability=float(payload["score_probability"]),
            source_quality=str(payload["source_quality"]),
            notes=tuple(str(note) for note in payload.get("notes", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError("已保存快照缺少可用模型資料，不能用人工盤口製造機率") from exc


def _manual_line(value: Any, label: str) -> float:
    """Validate a numeric line before it can be labelled as an actual line."""

    try:
        line = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"{label}必須是數字") from exc
    if not isfinite(line):
        raise ProviderError(f"{label}必須是有限數字")
    return line


def apply_manual_calibration(
    automatic_snapshot: Mapping[str, Any],
    lines_by_event: Mapping[str, tuple[float, float]],
    *,
    calibrated_at: str | None = None,
) -> dict[str, Any]:
    """Create a separately published, manually calibrated snapshot.

    ``lines_by_event`` contains the home team's handicap then the game total.
    Every event must be calibrated.  This prevents a partly corrected slate
    from being presented as a complete "人工校正正式推薦" release.

    The automatic snapshot is never mutated.  A later automatic refresh can
    therefore be saved without replacing a manual member release.
    """

    sport = str(automatic_snapshot.get("sport", ""))
    if sport not in {"mlb", "nba", "football"}:
        raise ProviderError("人工校正只支援 MLB、NBA 或歐洲足球快照")
    events = automatic_snapshot.get("events")
    if not isinstance(events, list) or not events:
        raise ProviderError("尚無可人工校正的完整自動快照")

    expected_ids: list[str] = []
    for item in events:
        if not isinstance(item, Mapping):
            raise ProviderError("已保存快照格式不完整，請重新建立自動快照")
        event = item.get("event")
        if not isinstance(event, Mapping) or not event.get("event_id"):
            raise ProviderError("已保存快照缺少賽事識別，請重新建立自動快照")
        if not isinstance(item.get("projection"), Mapping):
            raise ProviderError("有賽事缺少球隊模型資料，不能靠人工盤口產生推薦")
        expected_ids.append(str(event["event_id"]))
    if set(lines_by_event) != set(expected_ids):
        raise ProviderError("每一場都必須填入主隊讓分與大小總分後才能發布人工校正版本")

    when = calibrated_at or utc_now_iso()
    updated = deepcopy(dict(automatic_snapshot))
    updated_events = updated["events"]
    for item in updated_events:
        event = item["event"]
        event_id = str(event["event_id"])
        raw_spread, raw_total = lines_by_event[event_id]
        spread = _manual_line(raw_spread, f"{event.get('home', '主隊')}讓分")
        total = _manual_line(raw_total, f"{event.get('away', '客隊')} vs {event.get('home', '主隊')}大小總分")
        if total <= 0:
            raise ProviderError("大小總分必須大於 0")
        projection = _projection_from_saved_payload(item["projection"])
        market = Market("管理員人工校正盤口", when, spread_line=spread, total_line=total)
        item["market"] = asdict(market)
        item["recommendations"] = [
            asdict(recommendation)
            for recommendation in probability_recommendations(projection, market, sport=sport)
        ]
        warnings = [
            str(warning) for warning in item.get("warnings", ())
            if "缺少的讓分／大小線" not in str(warning)
        ]
        warnings.append(f"人工校正盤口：主隊讓分 {spread:+g}｜大小 {total:g}")
        item["warnings"] = warnings
        item["manual_calibration"] = {
            "home_spread": spread,
            "total": total,
            "calibrated_at": when,
        }

    summary = [str(item) for item in updated.get("source_summary", ())]
    if "管理員人工校正讓分／大小線" not in summary:
        summary.append("管理員人工校正讓分／大小線")
    updated["source_summary"] = summary
    updated["manual_calibrated_at"] = when
    updated["release_type"] = "manual_calibrated"
    updated["version"] = "restart-v2.1-manual-calibration"
    return updated
