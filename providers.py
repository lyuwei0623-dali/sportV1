"""External provider adapters for the clean rebuild.

Provider responses are normalised here and never exposed to member pages.  A
failed provider returns a safe diagnostic and never destroys an older snapshot.
"""
from __future__ import annotations

import os
import re
import unicodedata
import json
from datetime import date
from time import sleep
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from models import Event, Market


HTTP_TIMEOUT = 18
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
API_FOOTBALL_URL = "https://v3.football.api-sports.io"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_TEAM_STATS_URL = "https://statsapi.mlb.com/api/v1/teams/{team_id}/stats"
MLB_PERSON_STATS_URL = "https://statsapi.mlb.com/api/v1/people/{person_id}/stats"

FOOTBALL_LEAGUES: dict[str, tuple[str, int]] = {
    "英超": ("soccer_epl", 39),
    "西甲": ("soccer_spain_la_liga", 140),
    "義甲": ("soccer_italy_serie_a", 135),
    "德甲": ("soccer_germany_bundesliga", 78),
    "法甲": ("soccer_france_ligue_one", 61),
    "歐冠": ("soccer_uefa_champs_league", 2),
}


class ProviderError(RuntimeError):
    pass


def _get(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> Any:
    """Fetch JSON with a short retry for temporary source failures.

    Invalid credentials and malformed requests fail immediately; only the
    transient failures that previously produced a one-off empty snapshot are
    retried. A source is never allowed to overwrite an older saved snapshot
    with partial or invented data.
    """

    suffix = ("?" + urlencode(params)) if params else ""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = Request(url + suffix, headers=headers or {})
            with urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Retry quota / gateway errors, but never conceal a 400 / 401 / 403
            # configuration problem by wasting time on the same request.
            if exc.code not in {429, 500, 502, 503, 504}:
                raise ProviderError("資料來源拒絕請求；請檢查金鑰與設定") from exc
            last_error = exc
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
        if attempt < 2:
            sleep(.35 * (attempt + 1))
    raise ProviderError("資料來源暫時無法取得；稍後再試即可，既有快照不會被覆蓋") from last_error


def team_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"\b(fc|cf|afc|sc|calcio|club)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _same_team(left: str, right: str) -> bool:
    a, b = team_key(left), team_key(right)
    return bool(a and b and (a == b or a in b or b in a))


def fetch_mlb_events(day: date) -> list[Event]:
    payload = _get(MLB_SCHEDULE_URL, params={
        "sportId": 1, "date": day.isoformat(),
        "hydrate": "probablePitcher,linescore,venue",
    })
    events: list[Event] = []
    for block in payload.get("dates", []):
        for game in block.get("games", []):
            if game.get("status", {}).get("abstractGameState") == "Final":
                continue
            teams = game.get("teams", {})
            home, away = teams.get("home", {}), teams.get("away", {})
            home_team, away_team = home.get("team", {}), away.get("team", {})
            if not home_team or not away_team:
                continue
            events.append(Event(
                event_id=f"mlb:{game.get('gamePk')}", sport="mlb", kickoff=game.get("gameDate", ""),
                home=home_team.get("name", "主隊"), away=away_team.get("name", "客隊"), league="MLB",
                metadata={
                    "home_record": home.get("leagueRecord", {}), "away_record": away.get("leagueRecord", {}),
                    "home_pitcher": home.get("probablePitcher", {}).get("fullName", "尚未確認"),
                    "away_pitcher": away.get("probablePitcher", {}).get("fullName", "尚未確認"),
                    "home_team_id": home_team.get("id"), "away_team_id": away_team.get("id"),
                    "home_pitcher_id": home.get("probablePitcher", {}).get("id"),
                    "away_pitcher_id": away.get("probablePitcher", {}).get("id"),
                    "venue": game.get("venue", {}).get("name", "未提供"),
                },
            ))
    return events


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mlb_stats(url: str, *, team_or_person: int, season: int, group: str) -> dict[str, Any]:
    payload = _get(url.format(team_id=team_or_person, person_id=team_or_person), params={
        "stats": "season", "group": group, "season": season, "gameType": "R",
    })
    for block in payload.get("stats", []):
        splits = block.get("splits", [])
        if splits:
            return dict(splits[0].get("stat", {}))
    return {}


def fetch_mlb_team_profiles(events: Iterable[Event], day: date) -> dict[int, dict[str, float]]:
    """Fetch season hitting/pitching inputs once per team for a slate."""

    season = day.year
    profiles: dict[int, dict[str, float]] = {}
    ids = {int(event.metadata[key]) for event in events for key in ("home_team_id", "away_team_id")
           if event.metadata.get(key)}
    for team_id in ids:
        try:
            hitting = _mlb_stats(MLB_TEAM_STATS_URL, team_or_person=team_id, season=season, group="hitting")
            pitching = _mlb_stats(MLB_TEAM_STATS_URL, team_or_person=team_id, season=season, group="pitching")
            games = _number(hitting.get("gamesPlayed")) or 0
            runs_per_game = _number(hitting.get("runsPerGame"))
            if runs_per_game is None and games:
                runs_per_game = (_number(hitting.get("runs")) or 0) / games
            profiles[team_id] = {
                "runs_per_game": runs_per_game or 0.0,
                "ops": _number(hitting.get("ops")) or 0.0,
                "era": _number(pitching.get("era")) or 0.0,
                "whip": _number(pitching.get("whip")) or 0.0,
            }
        except ProviderError:
            # A single team profile must not discard a full daily slate.
            continue
    return profiles


def fetch_mlb_pitcher_profiles(events: Iterable[Event], day: date) -> dict[int, dict[str, float]]:
    """Fetch probable-starter season statistics when MLB has named a starter."""

    season = day.year
    profiles: dict[int, dict[str, float]] = {}
    ids = {int(event.metadata[key]) for event in events for key in ("home_pitcher_id", "away_pitcher_id")
           if event.metadata.get(key)}
    for pitcher_id in ids:
        try:
            stats = _mlb_stats(MLB_PERSON_STATS_URL, team_or_person=pitcher_id, season=season, group="pitching")
            profiles[pitcher_id] = {
                "era": _number(stats.get("era")) or 0.0,
                "whip": _number(stats.get("whip")) or 0.0,
                "innings": _number(stats.get("inningsPitched")) or 0.0,
            }
        except ProviderError:
            continue
    return profiles


def football_season(day: date) -> int:
    """European seasons are identified by their starting calendar year."""
    return day.year if day.month >= 7 else day.year - 1


def _api_football_headers() -> dict[str, str]:
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise ProviderError("尚未設定 API_FOOTBALL_KEY")
    return {"x-apisports-key": key}


def _api_football(path: str, params: dict[str, Any]) -> Any:
    payload = _get(f"{API_FOOTBALL_URL}/{path}", headers=_api_football_headers(), params=params)
    errors = payload.get("errors") or {}
    if errors:
        raise ProviderError("API-Football 暫時無法回應；請在後台稍後重試")
    return payload.get("response", [])


def fetch_football_events(day: date) -> list[Event]:
    season = football_season(day)
    events: list[Event] = []
    failed_leagues = 0
    for league_name, (_, league_id) in FOOTBALL_LEAGUES.items():
        try:
            fixtures = _api_football("fixtures", {"league": league_id, "season": season, "date": day.isoformat()})
        except ProviderError:
            # One league outage must not erase another league's saved slate.
            failed_leagues += 1
            continue
        for item in fixtures:
            fixture = item.get("fixture", {})
            status = fixture.get("status", {}).get("short")
            if status in {"FT", "AET", "PEN", "CANC", "PST", "ABD"}:
                continue
            teams = item.get("teams", {})
            home, away = teams.get("home", {}), teams.get("away", {})
            if not home.get("name") or not away.get("name"):
                continue
            events.append(Event(
                event_id=f"football:{fixture.get('id')}", sport="football", kickoff=fixture.get("date", ""),
                home=home["name"], away=away["name"], league=league_name,
                metadata={"league_id": league_id, "season": season,
                          "home_team_id": home.get("id"), "away_team_id": away.get("id"),
                          "fixture_id": fixture.get("id")},
            ))
    if not events and failed_leagues:
        # If even one relevant league failed, an empty response is ambiguous:
        # do not mistake a source outage for a genuine no-fixture day.
        raise ProviderError("部分足球賽程暫時無法取得；為保護會員資料，既有快照沒有被覆蓋")
    return events


def fetch_football_standings(day: date) -> dict[int, dict[int, dict[str, float]]]:
    season = football_season(day)
    result: dict[int, dict[int, dict[str, float]]] = {}
    for _, (_, league_id) in FOOTBALL_LEAGUES.items():
        try:
            response = _api_football("standings", {"league": league_id, "season": season})
        except ProviderError:
            continue
        table: dict[int, dict[str, float]] = {}
        for group in response:
            for row in (group.get("league", {}).get("standings") or [[]])[0]:
                team_id = row.get("team", {}).get("id")
                played = float((row.get("all", {}).get("played") or 0))
                if team_id and played:
                    goals = row.get("all", {}).get("goals", {})
                    table[int(team_id)] = {
                        "played": played,
                        "for": float(goals.get("for") or 0),
                        "against": float(goals.get("against") or 0),
                    }
        if table:
            result[league_id] = table
    return result


def _team_stat_profile(payload: dict[str, Any], context: str) -> dict[str, float]:
    """Extract home/away attack, defence and form from API-Football team stats."""

    fixtures = payload.get("fixtures", {})
    goals = payload.get("goals", {})
    played = _number(fixtures.get("played", {}).get(context)) or 0.0
    for_average = _number(goals.get("for", {}).get("average", {}).get(context)) or 0.0
    against_average = _number(goals.get("against", {}).get("average", {}).get(context)) or 0.0
    form = str(payload.get("form") or "").upper()
    form_points = sum({"W": 3, "D": 1}.get(result, 0) for result in form if result in {"W", "D", "L"})
    form_games = sum(1 for result in form if result in {"W", "D", "L"})
    clean_sheets = _number(payload.get("clean_sheet", {}).get(context)) or 0.0
    failed_to_score = _number(payload.get("failed_to_score", {}).get(context)) or 0.0
    return {
        "played": played,
        "for_average": for_average,
        "against_average": against_average,
        "clean_sheets": clean_sheets,
        "failed_to_score": failed_to_score,
        "clean_sheet_rate": clean_sheets / played if played else 0.0,
        "failed_to_score_rate": failed_to_score / played if played else 0.0,
        "form_ratio": form_points / (3 * form_games) if form_games else 0.5,
    }


def fetch_football_team_profiles(events: Iterable[Event]) -> dict[tuple[int, int, int, str], dict[str, float]]:
    """Fetch team home/away statistics, cached for every unique team/league."""

    profiles: dict[tuple[int, int, int, str], dict[str, float]] = {}
    seen: set[tuple[int, int, int]] = set()
    for event in events:
        league_id, season = int(event.metadata["league_id"]), int(event.metadata["season"])
        for key, context in (("home_team_id", "home"), ("away_team_id", "away")):
            team_id = int(event.metadata[key])
            marker = (league_id, season, team_id)
            if marker not in seen:
                seen.add(marker)
                try:
                    response = _api_football("teams/statistics", {"league": league_id, "season": season, "team": team_id})
                    raw = response if isinstance(response, Mapping) else (response[0] if response else {})
                    profiles[(league_id, season, team_id, "home")] = _team_stat_profile(raw, "home")
                    profiles[(league_id, season, team_id, "away")] = _team_stat_profile(raw, "away")
                except ProviderError:
                    continue
    return profiles


def fetch_football_injury_counts(events: Iterable[Event]) -> dict[str, dict[str, int]]:
    """Count currently listed injuries/suspensions per fixture when available.

    It is deliberately non-fatal: provider-plan limits or unavailable line-ups
    become a risk note, never a fake zero-injury assertion.
    """

    if os.environ.get("API_FOOTBALL_INCLUDE_INJURIES", "1") != "1":
        return {}
    counts: dict[str, dict[str, int]] = {}
    for event in events:
        try:
            response = _api_football("injuries", {"fixture": event.metadata["fixture_id"]})
            home_id, away_id = event.metadata.get("home_team_id"), event.metadata.get("away_team_id")
            split = {"home": 0, "away": 0}
            for item in response:
                team_id = (item.get("team") or {}).get("id")
                if team_id == home_id:
                    split["home"] += 1
                elif team_id == away_id:
                    split["away"] += 1
            counts[event.event_id] = split
        except ProviderError:
            continue
    return counts


def fetch_odds(sport_key: str) -> list[dict[str, Any]]:
    """Fetch lines only.

    The Odds API response contains prices, but this app intentionally ignores
    them.  A line is useful for calculating a model cover probability; a price
    is not part of this product's member promise.
    """

    key = os.environ.get("THE_ODDS_API_KEY", "").strip()
    if not key:
        raise ProviderError("尚未設定 THE_ODDS_API_KEY")
    return _get(ODDS_URL.format(sport=sport_key), params={
        "apiKey": key, "regions": os.environ.get("ODDS_REGIONS", "eu"),
        "markets": "spreads,totals", "dateFormat": "iso",
    })


def _market_from_book(book: dict[str, Any], event: Event) -> Market | None:
    """Keep only a complete pair of line points from a single bookmaker."""

    spread_line: float | None = None
    total_line: float | None = None
    spread_points: dict[str, float] = {}
    total_points: dict[str, float] = {}
    for market in book.get("markets", []):
        outcomes = market.get("outcomes", [])
        if market.get("key") == "spreads":
            for outcome in outcomes:
                name = str(outcome.get("name", ""))
                try: point = float(outcome.get("point"))
                except (TypeError, ValueError): continue
                if _same_team(name, event.home):
                    spread_points["home"] = point
                elif _same_team(name, event.away):
                    spread_points["away"] = point
        elif market.get("key") == "totals":
            for outcome in outcomes:
                try: point = float(outcome.get("point"))
                except (TypeError, ValueError): continue
                if str(outcome.get("name", "")).lower() == "over":
                    total_points["over"] = point
                elif str(outcome.get("name", "")).lower() == "under":
                    total_points["under"] = point
    if {"home", "away"} <= set(spread_points) and abs(spread_points["home"] + spread_points["away"]) < .001:
        spread_line = spread_points["home"]
    if {"over", "under"} <= set(total_points) and abs(total_points["over"] - total_points["under"]) < .001:
        total_line = total_points["over"]
    if spread_line is None and total_line is None:
        return None
    return Market(str(book.get("title", "Unknown bookmaker")), str(book.get("last_update", "")),
                  spread_line, total_line)


def market_for_event(event: Event, odds_events: Iterable[dict[str, Any]]) -> Market | None:
    """Pick one bookmaker with a matched event; never combine multiple books."""

    for odds_event in odds_events:
        if not (_same_team(str(odds_event.get("home_team", "")), event.home)
                and _same_team(str(odds_event.get("away_team", "")), event.away)):
            continue
        books = odds_event.get("bookmakers", [])
        books = sorted(books, key=lambda item: 0 if str(item.get("key", "")).lower() == "pinnacle" else 1)
        for book in books:
            market = _market_from_book(book, event)
            if market:
                return market
    return None
