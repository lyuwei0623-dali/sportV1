"""Snapshot orchestration for the probability-first rebuild.

Only this module calls providers and models. Member pages consume a previously
saved snapshot; they never fetch live data or recalculate probabilities.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timezone
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
    fetch_espn_football_events,
    fetch_espn_football_standings,
    fetch_mlb_events,
    fetch_mlb_pitcher_profiles,
    fetch_mlb_team_profiles,
    fetch_nba_events,
    fetch_nba_team_profiles,
    fetch_odds,
    fetch_final_results,
    market_for_event,
)
from storage import SnapshotStore
from time_utils import parse_time, pregame, taiwan_day_matches
from settlement_math import OUTCOMES, settle, profit, components


def _upcoming(event: Event) -> bool:
    return pregame(event.kickoff, utc_now_iso()) and event.metadata.get("status") in (
        None, "NS", "Scheduled", "Preview", "Pre-Game", "Warmup", "STATUS_SCHEDULED")


def _schedule_only(event: Event) -> EventAnalysis:
    return EventAnalysis(event,None,None,_unavailable_recommendations(
        "已開賽、已結束、延期或時間未確認；不使用事後資料補造賽前預測"),
        ("賽事保留供查詢，僅既有賽前快照可納入回查",))


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
    if (home_team and away_team and home_team.get("runs_per_game") and away_team.get("runs_per_game")
            and min(home_team.get("games",10),away_team.get("games",10))>=10
            and all(team.get(key) is not None for team in (home_team,away_team) for key in ("ops","era","whip"))):
        # OPS keeps two teams with the same raw run average from becoming
        # indistinguishable. WHIP complements ERA as a baserunner indicator.
        home_offence = home_team["runs_per_game"] * _clamp(1 + (home_team["ops"] - .720) * .75, .89, 1.12)
        away_offence = away_team["runs_per_game"] * _clamp(1 + (away_team["ops"] - .720) * .75, .89, 1.12)
        away_staff = away_team["era"] + (away_team["whip"] - 1.30) * .65
        home_staff = home_team["era"] + (home_team["whip"] - 1.30) * .65
        home_rate = .62 * home_offence + .38 * away_staff + .14
        away_rate = .62 * away_offence + .38 * home_staff
        notes.append("團隊得分／OPS／ERA／WHIP")
        home_starter = pitcher_profiles.get(int(event.metadata["home_pitcher_id"])) if event.metadata.get("home_pitcher_id") else None
        away_starter = pitcher_profiles.get(int(event.metadata["away_pitcher_id"])) if event.metadata.get("away_pitcher_id") else None
        if away_starter and away_starter.get("innings", 0) >= 15 and away_starter.get("era") is not None:
            home_rate *= _clamp(
                1 + (away_starter["era"] - 4.20) * .045 + ((away_starter.get("whip") or 1.30) - 1.30) * .12,
                .80, 1.22,
            )
            notes.append("客隊先發投手 ERA／WHIP")
        else:
            notes.append("客隊先發待確認")
        if home_starter and home_starter.get("innings", 0) >= 15 and home_starter.get("era") is not None:
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
    # A win/loss record cannot identify a scoring distribution. Do not
    # manufacture run means from a fixed 4.50/4.32 baseline when stats are absent.
    return None


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
        if not team_rows or sum(row["played"] for row in team_rows)<=0 or sum(row["for"] for row in team_rows)<=0:
            return None
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
            notes.append(f"傷停名單 主{availability.get('home', 0)}／客{availability.get('away', 0)}（無球員影響權重，不以人數直接扣分）")
        return score_projection(_clamp(home_rate, .25, 3.7), _clamp(away_rate, .20, 3.3),
                                "API-Football 團隊資料｜" + "、".join(notes))
    # Fallback is differentiated standings data—not a fixed league prior and
    # never a market-derived probability.
    home, away = league_table.get(home_id), league_table.get(away_id)
    if home and away and min(home.get("played",0),away.get("played",0)) >= 3:
        team_rows = [row for row in league_table.values() if row.get("played",0)>0]
        league_rate = sum(row["for"] for row in team_rows) / sum(row["played"] for row in team_rows)
        if league_rate <= 0:
            return None
        home_attack = (home["for"] / home["played"]) / league_rate
        home_defence = (home["against"] / home["played"]) / league_rate
        away_attack = (away["for"] / away["played"]) / league_rate
        away_defence = (away["against"] / away["played"]) / league_rate
        return score_projection(_clamp(league_rate * home_attack * away_defence * 1.08, .25, 3.7),
                                _clamp(league_rate * away_attack * home_defence * .92, .20, 3.3),
                                ("ESPN" if event.metadata.get("source")=="espn" else "API-Football") + " 聯賽團隊攻守資料（主客細項暫缺）")
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
    source = "NBA 團隊得失分＋賽季勝率資料"
    if home.get("previous_season") or away.get("previous_season"):
        source += "｜開季樣本不足：使用上一季，未調整轉隊／新秀／教練變動"
    return basketball_projection(_clamp(home_rate, 92, 132), _clamp(away_rate, 90, 130), source)


def build_mlb_snapshot(day: date) -> DailySnapshot:
    events = fetch_mlb_events(day)
    if not events:
        raise ProviderError("當日沒有可建立的 MLB 賽事；未覆蓋既有快照")
    active = [e for e in events if _upcoming(e)]
    team_profiles = fetch_mlb_team_profiles(active, day) if active else {}
    pitcher_profiles = fetch_mlb_pitcher_profiles(active, day) if active else {}
    notes = ["MLB Stats API 賽程、球隊打擊／投球、預定先發"]
    try:
        odds = fetch_odds("baseball_mlb") if active else []
        notes.append("The Odds API 讓分／大小線")
    except ProviderError:
        odds = []
        notes.append("讓分／大小線暫不可用")
    analyses: list[EventAnalysis] = []
    for event in events:
        if not _upcoming(event):
            analyses.append(_schedule_only(event))
            continue
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
        inputs = {"teams": {str(k):v for k,v in team_profiles.items() if k in (event.metadata.get("home_team_id"),event.metadata.get("away_team_id"))},
                  "pitchers": {str(k):v for k,v in pitcher_profiles.items() if k in (event.metadata.get("home_pitcher_id"),event.metadata.get("away_pitcher_id"))},
                  "retrieved_at": utc_now_iso()}
        analyses.append(EventAnalysis(event, projection, market, recommendations, tuple(warnings), inputs))
    return DailySnapshot("mlb", day.isoformat(), utc_now_iso(), tuple(analyses), tuple(notes))


def build_nba_snapshot(day: date) -> DailySnapshot:
    events = fetch_nba_events(day)
    if not events:
        raise ProviderError("當日沒有可建立的 NBA 賽事；未覆蓋既有快照")
    active = [e for e in events if _upcoming(e)]
    team_profiles = fetch_nba_team_profiles(active, day) if active else {}
    notes = ["ESPN NBA 賽程、球隊得失分與賽季勝率"]
    try:
        odds = fetch_odds("basketball_nba", regions="us") if active else []
        notes.append("The Odds API 讓分／大小線")
    except ProviderError:
        odds = []
        notes.append("讓分／大小線暫不可用")
    analyses: list[EventAnalysis] = []
    for event in events:
        if not _upcoming(event):
            analyses.append(_schedule_only(event))
            continue
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
        inputs = {"teams": {str(k):v for k,v in team_profiles.items() if str(k) in (str(event.metadata.get("home_team_id")),str(event.metadata.get("away_team_id")))}, "retrieved_at": utc_now_iso()}
        analyses.append(EventAnalysis(event, projection, market, recommendations, tuple(warnings), inputs))
    return DailySnapshot("nba", day.isoformat(), utc_now_iso(), tuple(analyses), tuple(notes))


def build_football_snapshot(day: date) -> DailySnapshot:
    try:
        events = fetch_football_events(day)
        fallback = False
        if not events:
            events = fetch_espn_football_events(day)
            fallback = True
    except ProviderError:
        events = fetch_espn_football_events(day)
        fallback = True
    if not events:
        raise ProviderError("當日沒有可建立的歐洲足球賽事；未覆蓋既有快照")
    active = [e for e in events if _upcoming(e)]
    if not active:
        return DailySnapshot("football",day.isoformat(),utc_now_iso(),tuple(_schedule_only(e) for e in events),("僅顯示賽程，未建立事後預測",))
    if fallback:
        notes = ["ESPN 公開賽程與聯賽積分（API-Football 暫不可用）"]
        try:
            standings = fetch_espn_football_standings(day)
        except ProviderError:
            standings = {}
        team_profiles, injuries = {}, {}
        notes.append("ESPN 聯賽進失球資料（傷停／主客細項暫不可用）")
    else:
        notes = ["API-Football 賽程"]
        try:
            standings = fetch_football_standings(day)
            team_profiles = fetch_football_team_profiles(active)
            notes.append("API-Football 團隊主客場攻守資料")
        except ProviderError:
            standings, team_profiles = {}, {}
            notes.append("團隊統計暫不可用")
        injuries = fetch_football_injury_counts(active)
        if injuries:
            notes.append("API-Football 傷停名單")
    odds_by_league: dict[str, list[dict[str, Any]]] = {}
    for league in {event.league for event in active}:
        sport_key = FOOTBALL_LEAGUES[league][0]
        try:
            odds_by_league[league] = fetch_odds(sport_key)
        except ProviderError:
            odds_by_league[league] = []
            notes.append(f"{league} 讓分／大小線暫不可用")
    analyses: list[EventAnalysis] = []
    for event in events:
        if not _upcoming(event):
            analyses.append(_schedule_only(event))
            continue
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
        inputs = {"standings": standings.get(int(event.metadata.get("league_id",0)),{}),
                  "team_profiles": {str(k):v for k,v in team_profiles.items() if k[2] in (event.metadata.get("home_team_id"),event.metadata.get("away_team_id"))},
                  "injuries": injuries.get(event.event_id), "retrieved_at": utc_now_iso()}
        analyses.append(EventAnalysis(event, projection, market, recommendations, tuple(warnings), inputs))
    return DailySnapshot("football", day.isoformat(), utc_now_iso(), tuple(analyses), tuple(dict.fromkeys(notes)))



def _settlement_state(values: list[float]) -> str:
    net = sum(values) / len(values)
    if net > 0:
        return "won"
    if net < 0:
        return "lost"
    return "push"


def _asian_settlement(value: float, line: float) -> str:
    return settle(value,line)


def settle_snapshot(snapshot: Mapping[str, Any], *, results: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], int]:
    """Attach final scores and transparent pick outcomes without changing predictions."""

    updated = deepcopy(dict(snapshot))
    settled_events = 0
    summary = {kind: {**dict.fromkeys(OUTCOMES,0), "excluded":0, "priced_count":0, "net_units":0.0,
                      "brier_sum":0.0, "brier_count":0, "actionable_count":0}
               for kind in ("moneyline","spread","total")}
    for item in updated.get("events", []):
        event = item.get("event", {})
        result = results.get(str(event.get("event_id", ""))) or item.get("settlement")
        if not result or result.get("status") != "final":
            continue
        try:
            raw_home, raw_away = float(result["home_score"]), float(result["away_score"])
            if not all(isfinite(n) and n >= 0 and n.is_integer() for n in (raw_home,raw_away)):
                continue
            home, away = int(raw_home),int(raw_away)
        except (KeyError, TypeError, ValueError):
            continue
        picks: dict[str, dict[str, Any]] = {}
        for rec in item.get("recommendations", []):
            kind, state, selection = rec.get("market_type"), rec.get("state"), str(rec.get("selection", ""))
            if kind not in summary or state in {"unavailable", "model_reference"}:
                continue
            if result.get("unsupported_rules"):
                continue
            side = rec.get("side") or ("home" if selection.startswith("主隊") else "away" if selection.startswith("客隊") else "over" if selection.startswith("大分") else "under" if selection.startswith("小分") else "draw" if selection.startswith("和局") else "")
            if kind == "moneyline":
                if side not in {"home","away","draw"}:
                    continue
                if home==away and updated.get("sport") != "football":
                    continue  # incomplete/invalid NBA or MLB final score
                actual = "home" if home>away else "away" if away>home else "draw"
                outcome = "won" if side==actual else "lost"
            elif kind == "spread":
                line = rec.get("market_line")
                if line is None:
                    continue
                if side not in {"home","away"}:
                    continue
                outcome = _asian_settlement(home-away if side=="home" else away-home,
                                            float(line) if side=="home" else -float(line))
            else:
                line = rec.get("market_line")
                if line is None:
                    continue
                score_total = home + away
                outcome = (_asian_settlement(score_total, -float(line))
                           if side=="over"
                           else _asian_settlement(-score_total, float(line)))
            eligible = item.get("capture_schema") == 3 and pregame(event.get("kickoff"),item.get("captured_at"))
            price = rec.get("decimal_price")
            net = profit(outcome,float(price)) if price is not None else None
            picks[kind] = {"outcome": outcome, "source": state, "evaluation_eligible":eligible, "net_units":net}
            row = summary[kind]
            if eligible:
                row[outcome] += 1
                row["actionable_count"] += int(rec.get("actionable") is True)
                if net is not None:
                    row["priced_count"] += 1
                    row["net_units"] += net
                if kind=="moneyline" and rec.get("model_probability") is not None:
                    row["brier_sum"] += (float(rec["model_probability"])-int(outcome=="won"))**2
                    row["brier_count"] += 1
            else:
                row["excluded"] += 1
        item["settlement"] = {
            "status": "final", "home_score": home, "away_score": away, "picks": picks,
            "unsupported_rules": bool(result.get("unsupported_rules")),
        }
        settled_events += 1
    updated["settlement_summary"] = summary
    updated["settled_at"] = utc_now_iso()
    updated["settlement_version"] = "audit-v3.0"
    return updated, settled_events


def save_snapshot(store: SnapshotStore, snapshot: DailySnapshot) -> None:
    """Save only after an entire non-empty slate has been assembled."""

    if not snapshot.events:
        raise ProviderError("空賽程不可覆蓋已保存快照")
    # JSON representation has list semantics, matching durable storage.
    import json
    import uuid
    payload = json.loads(json.dumps(snapshot.as_dict(),ensure_ascii=False,allow_nan=False))
    prior = store.get_automatic_snapshot(snapshot.sport,snapshot.date_key) or {}
    payload["revision_id"] = uuid.uuid4().hex
    previous = {i["event"]["event_id"]: i for i in prior.get("events",[])}
    ids = set()
    for index,item in enumerate(payload["events"]):
        event = item["event"]
        event_id = event["event_id"]
        if event_id in ids or not taiwan_day_matches(event["kickoff"],date.fromisoformat(snapshot.date_key)):
            raise ProviderError("賽程日期或識別重複，已取消保存以保護既有快照")
        ids.add(event_id)
        old = previous.get(event_id)
        if old and (not pregame(old["event"].get("kickoff"),snapshot.generated_at)
                    or (old.get("projection") and not item.get("projection"))):
            payload["events"][index] = deepcopy(old)
            continue
        if item.get("projection") and not pregame(event["kickoff"],snapshot.generated_at):
            raise ProviderError("計算期間已開賽，已取消發布以避免事後預測")
        item["captured_at"] = snapshot.generated_at
        item["capture_schema"] = 3
        item["model_version"] = snapshot.version
        record = {k:deepcopy(item.get(k)) for k in ("captured_at","market","projection","recommendations","model_version")}
        history = deepcopy(old.get("prediction_history",[])) if old else []
        if not history and old:
            history = [{"captured_at":old.get("captured_at",prior.get("generated_at")),"market":old.get("market"),"legacy":True}]
        history.append(record)
        # Bounded payload: retain first observation plus latest 29 revisions.
        item["prediction_history"] = history if len(history)<=30 else [history[0],*history[-29:]]
        item["first_observed_at"] = old.get("first_observed_at",old.get("captured_at",prior.get("generated_at"))) if old else snapshot.generated_at
    for event_id,item in previous.items():
        if event_id not in ids:
            payload["events"].append(deepcopy(item))
    store.save_automatic(snapshot.sport, snapshot.date_key, payload,expected_revision=prior.get("revision_id"))


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
        if not pregame(event.get("kickoff"),when):
            raise ProviderError("已開賽或缺少開賽時間，不能修改賽前盤口")
        if item.get("capture_schema") != 3:
            raise ProviderError("舊版快照缺少可稽核紀錄；請先建立新版賽前快照")
        event_id = str(event["event_id"])
        raw_spread, raw_total = lines_by_event[event_id]
        spread = _manual_line(raw_spread, f"{event.get('home', '主隊')}讓分")
        total = _manual_line(raw_total, f"{event.get('away', '客隊')} vs {event.get('home', '主隊')}大小總分")
        components(spread)
        components(total)
        if total <= 0:
            raise ProviderError("大小總分必須大於 0")
        projection = _projection_from_saved_payload(item["projection"])
        market = Market("管理員人工校正盤口", when, spread_line=spread, total_line=total)
        item["market"] = asdict(market)
        item.pop("settlement",None)
        item["captured_at"] = when
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
    updated.pop("settlement_summary",None)
    updated.pop("settled_at",None)
    updated["version"] = "audit-v3.0-manual"
    return updated
