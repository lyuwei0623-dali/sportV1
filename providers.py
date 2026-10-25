"""External provider adapters for the clean rebuild.

Provider responses are normalised here and never exposed to member pages.  A
failed provider returns a safe diagnostic and never destroys an older snapshot.
"""
from __future__ import annotations

import os
import re
import unicodedata
import json
import math
import hashlib
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from time import sleep, monotonic
from threading import Lock
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from models import Event, Market
from time_utils import taiwan_day_matches, parse_time


HTTP_TIMEOUT = 10
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
API_FOOTBALL_URL = "https://v3.football.api-sports.io"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_TEAM_STATS_URL = "https://statsapi.mlb.com/api/v1/teams/{team_id}/stats"
MLB_PERSON_STATS_URL = "https://statsapi.mlb.com/api/v1/people/{person_id}/stats"
# ESPN's web host serves the same public NBA schedule and standings JSON but
# does not reject the cloud runtime that blocks the legacy site.api host.
NBA_SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
NBA_STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings"

ESPN_SOCCER_SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
ESPN_SOCCER_STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/soccer/{league}/standings"
ESPN_SOCCER_LEAGUES: dict[str, str] = {
    "英超": "eng.1", "西甲": "esp.1", "義甲": "ita.1",
    "德甲": "ger.1", "法甲": "fra.1", "歐冠": "uefa.champions",
}

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


_RESPONSE_CACHE = {}
_CACHE_LOCK = Lock()


def _get(url: str, *, headers=None, params=None):
    """Short in-process cache to reduce duplicate quota use; never cache errors."""
    identity = json.dumps([url,headers or {},params or {}],sort_keys=True).encode()
    key = hashlib.sha256(identity).hexdigest()
    now = monotonic()
    with _CACHE_LOCK:
        cached = _RESPONSE_CACHE.get(key)
        if cached and cached[0]>now:
            return deepcopy(cached[1])
    value = _get_uncached(url,headers=headers,params=params)
    if not (isinstance(value,dict) and value.get("errors")):
        with _CACHE_LOCK:
            if len(_RESPONSE_CACHE)>=256:
                _RESPONSE_CACHE.clear()
            _RESPONSE_CACHE[key] = (monotonic()+60,deepcopy(value))
    return value


def _get_uncached(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> Any:
    """Fetch JSON with a short retry for temporary source failures.

    Invalid credentials and malformed requests fail immediately; only the
    transient failures that previously produced a one-off empty snapshot are
    retried. A source is never allowed to overwrite an older saved snapshot
    with partial or invented data.
    """

    suffix = ("?" + urlencode(params)) if params else ""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            request_headers = {"User-Agent": "Mozilla/5.0 (compatible; VidaliSports/1.0; +https://vidali-sports.streamlit.app)", "Accept": "application/json"}
            request_headers.update(headers or {})
            request = Request(url + suffix, headers=request_headers)
            with urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Retry quota / gateway errors, but never conceal a 400 / 401 / 403
            # configuration problem by wasting time on the same request.
            if exc.code not in {500, 502, 503, 504}:
                host = urlparse(url).netloc or "資料來源"
                raise ProviderError(f"{host} 回傳 HTTP {exc.code}；請檢查該來源設定") from exc
            last_error = exc
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
        if attempt < 1:
            sleep(.35 * (attempt + 1))
    raise ProviderError("資料來源暫時無法取得；稍後再試即可，既有快照不會被覆蓋") from last_error


def team_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"\b(fc|cf|afc|sc|calcio|club)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _same_team(left: str, right: str) -> bool:
    a, b = team_key(left), team_key(right)
    aliases = {
        "laclippers": "losangelesclippers", "oaklandathletics": "athletics",
        "atleticomadrid": "atleticomadrid", "atleticodemadrid": "atleticomadrid",
        "intermilan": "internazionale", "inter": "internazionale", "milan": "acmilan",
        "psg": "parissaintgermain", "parissg": "parissaintgermain",
        "bayernmunchen": "bayernmunich", "bayernmunich": "bayernmunich",
        "borussiamgladbach": "borussiamonchengladbach", "borussiamonchengladbach": "borussiamonchengladbach",
        "athleticclub": "athleticbilbao", "athletic": "athleticbilbao",
        "tottenham": "tottenhamhotspur", "wolves": "wolverhamptonwanderers",
        "wolverhampton": "wolverhamptonwanderers", "newcastle": "newcastleunited",
        "brighton": "brightonhovealbion", "westham": "westhamunited",
        "sportingcp": "sportinglisbon", "slaviapraha": "slaviaprague",
    }
    return bool(a and b and aliases.get(a,a) == aliases.get(b,b))


def _mlb_schedule(day: date) -> Any:
    return _get(MLB_SCHEDULE_URL, params={
        "sportId": 1, "startDate": (day-timedelta(days=1)).isoformat(), "endDate": day.isoformat(),
        "hydrate": "probablePitcher,linescore,venue",
    })


def _nba_scoreboard(day: date) -> Any:
    # This web endpoint rejects date ranges (HTTP400); fetch overlapping US
    # dates separately and deduplicate by ID before the Taiwan-day filter.
    events = {}
    for source_day in (day-timedelta(days=1),day):
        payload = _get(NBA_SCOREBOARD_URL,params={"dates":source_day.strftime("%Y%m%d"),"limit":100})
        for item in payload.get("events",[]):
            if item.get("id"):
                events[str(item["id"])] = item
    return {"events":list(events.values())}


def fetch_mlb_events(day: date) -> list[Event]:
    payload = _mlb_schedule(day)
    events: list[Event] = []
    for block in payload.get("dates", []):
        for game in block.get("games", []):
            if not taiwan_day_matches(game.get("gameDate"), day) or not game.get("gamePk"):
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
                    "source": "mlb", "status": (game.get("status",{}).get("detailedState") if any(
                        term in str(game.get("status",{}).get("detailedState","")).lower()
                        for term in ("postpon","cancel","suspend")) else game.get("status", {}).get("abstractGameState", "Unknown")),
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
        number = float(value)
        return number if math.isfinite(number) else None
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
                "games": games,
                "runs_per_game": runs_per_game or 0.0,
                "ops": _number(hitting.get("ops")),
                "era": _number(pitching.get("era")),
                "whip": _number(pitching.get("whip")),
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
                "era": _number(stats.get("era")),
                "whip": _number(stats.get("whip")),
                "innings": _innings(stats.get("inningsPitched")),
            }
        except ProviderError:
            continue
    return profiles


def _innings(raw: Any) -> float:
    try:
        whole, _, outs = str(raw).partition(".")
        if outs not in ("", "0", "1", "2"):
            return 0.0
        return int(whole) + int(outs or 0)/3
    except (TypeError, ValueError):
        return 0.0


def fetch_nba_events(day: date) -> list[Event]:
    """Read NBA schedule from ESPN's public scoreboard endpoint."""

    payload = _nba_scoreboard(day)
    events: list[Event] = []
    for item in payload.get("events", []):
        status = ((item.get("status") or {}).get("type") or {}).get("name", "")
        if not taiwan_day_matches(item.get("date"), day) or not item.get("id"):
            continue
        competition = (item.get("competitions") or [{}])[0]
        competitors = competition.get("competitors") or []
        home = next((row for row in competitors if row.get("homeAway") == "home"), {})
        away = next((row for row in competitors if row.get("homeAway") == "away"), {})
        home_team, away_team = home.get("team") or {}, away.get("team") or {}
        if not home_team.get("displayName") or not away_team.get("displayName"):
            continue
        events.append(Event(
            event_id=f"nba:{item.get('id')}", sport="nba", kickoff=str(item.get("date") or ""),
            home=str(home_team["displayName"]), away=str(away_team["displayName"]), league="NBA",
            metadata={"source": "espn", "status": status, "home_team_id": home_team.get("id"), "away_team_id": away_team.get("id")},
        ))
    return events


def _nba_standings(season: int) -> dict[int, dict[str, float]]:
    payload = _get(NBA_STANDINGS_URL, params={"season": season, "seasontype": 2, "limit": 100})
    returned_season = (payload.get("season") or {}).get("year")
    if returned_season is not None and int(returned_season) != season:
        raise ProviderError("NBA 來源回傳的賽季不符，已停止使用錯季統計")
    result: dict[int, dict[str, float]] = {}
    for group in payload.get("children", []):
        entries = ((group.get("standings") or {}).get("entries") or [])
        for entry in entries:
            team = entry.get("team") or {}
            team_id = _number(team.get("id"))
            values = {str(stat.get("name")): _number(stat.get("value")) for stat in entry.get("stats", [])}
            wins, losses = values.get("wins") or 0.0, values.get("losses") or 0.0
            games = wins + losses
            points_for = values.get("avgPointsFor")
            points_against = values.get("avgPointsAgainst")
            if points_for is None and values.get("pointsFor") is not None and games:
                points_for = values["pointsFor"] / games
            if points_against is None and values.get("pointsAgainst") is not None and games:
                points_against = values["pointsAgainst"] / games
            if team_id is None or not games or points_for is None or points_against is None:
                continue
            result[int(team_id)] = {"games": games, "for": points_for, "against": points_against,
                                    "win_pct": wins / games, "season": season, "previous_season": False}
    return result


def fetch_nba_team_profiles(events: Iterable[Event], day: date) -> dict[int, dict[str, float]]:
    """Use current NBA standings; opening week safely falls back to last season."""

    season = day.year + 1 if day.month >= 7 else day.year
    current = _nba_standings(season)
    needed = {int(event.metadata[key]) for event in events for key in ("home_team_id", "away_team_id")
              if event.metadata.get(key)}
    if any(current.get(team_id, {}).get("games", 0) < 3 for team_id in needed):
        previous = _nba_standings(season - 1)
        for team_id in needed:
            if current.get(team_id, {}).get("games", 0) < 3 and team_id in previous:
                current[team_id] = {**previous[team_id], "previous_season": True}
    return {team_id: current[team_id] for team_id in needed if team_id in current}


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
        # API-Football returns useful non-secret details for quota, plan and
        # invalid-request failures.  Keeping that detail in the administrator
        # message makes the problem actionable, without ever exposing the key.
        raise ProviderError("API-Football 無法提供資料，請檢查帳號狀態、方案與額度")
    return payload.get("response", [])


def _espn_football_payload(url: str, *, league: str, params: dict[str, Any]) -> Any:
    return _get(url.format(league=league), params=params)


def fetch_espn_football_events(day: date) -> list[Event]:
    """Keyless fallback for the supported European competitions."""

    from zoneinfo import ZoneInfo

    taipei = ZoneInfo("Asia/Taipei")
    events: list[Event] = []
    for league_name, espn_league in ESPN_SOCCER_LEAGUES.items():
        seen: set[str] = set()
        for source_day in (day - timedelta(days=1), day):
            payload = _espn_football_payload(
                ESPN_SOCCER_SCOREBOARD_URL, league=espn_league,
                params={"dates": source_day.strftime("%Y%m%d"), "limit": 100},
            )
            for item in payload.get("events", []):
                event_id = str(item.get("id") or "")
                if not event_id or event_id in seen:
                    continue
                seen.add(event_id)
                competition = (item.get("competitions") or [{}])[0]
                competitors = competition.get("competitors") or []
                home = next((row for row in competitors if row.get("homeAway") == "home"), {})
                away = next((row for row in competitors if row.get("homeAway") == "away"), {})
                home_team, away_team = home.get("team") or {}, away.get("team") or {}
                kickoff = str(item.get("date") or "")
                try:
                    local_day = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(taipei).date()
                except ValueError:
                    continue
                status = ((item.get("status") or {}).get("type") or {}).get("name", "")
                if local_day != day:
                    continue
                if not home_team.get("displayName") or not away_team.get("displayName"):
                    continue
                events.append(Event(
                    event_id=f"football:espn:{espn_league}:{event_id}", sport="football", kickoff=kickoff,
                    home=str(home_team["displayName"]), away=str(away_team["displayName"]), league=league_name,
                    metadata={"source": "espn", "status": status, "season": football_season(day), "league_id": FOOTBALL_LEAGUES[league_name][1],
                              "espn_league": espn_league, "home_team_id": home_team.get("id"),
                              "away_team_id": away_team.get("id"), "fixture_id": event_id},
                ))
    return events

def fetch_espn_football_standings(day: date) -> dict[int, dict[int, dict[str, float]]]:
    """Return current-season team scoring rows from ESPN, keyed like API-Football."""

    season = football_season(day)
    result: dict[int, dict[int, dict[str, float]]] = {}
    for league_name, espn_league in ESPN_SOCCER_LEAGUES.items():
        try:
            payload = _espn_football_payload(
                ESPN_SOCCER_STANDINGS_URL, league=espn_league,
                params={"season": season, "seasontype": 1, "limit": 100},
            )
        except ProviderError:
            continue
        table: dict[int, dict[str, float]] = {}
        for group in [payload, *payload.get("children", [])]:
            for entry in ((group.get("standings") or {}).get("entries") or []):
                team_id = _number((entry.get("team") or {}).get("id"))
                values = {str(stat.get("name")): _number(stat.get("value")) for stat in entry.get("stats", [])}
                played = values.get("gamesPlayed") or 0.0
                if team_id is None or not played:
                    continue
                table[int(team_id)] = {
                    "played": played, "for": values.get("pointsFor") or 0.0,
                    "against": values.get("pointsAgainst") or 0.0,
                }
        if table:
            result[FOOTBALL_LEAGUES[league_name][1]] = table
    return result


def fetch_football_events(day: date) -> list[Event]:
    """Get fixtures for one Taiwan calendar day, then filter locally.

    API-Football's ``date`` parameter is a UTC date while the administrator
    date picker and member table are explicitly Taiwan time.  A European
    evening match on UTC 10/20 is already 10/21 in Taiwan.  Fetch the two UTC
    dates which can overlap the chosen Taiwan day, then retain only kickoffs
    whose Asia/Taipei date exactly matches ``day``.  This avoids both the old
    six-call design and cross-day snapshots.
    """

    from zoneinfo import ZoneInfo

    fixtures: list[dict[str, Any]] = []
    for utc_day in (day - timedelta(days=1), day):
        try:
            response = _api_football("fixtures", {"date": utc_day.isoformat()})
        except ProviderError as exc:
            raise ProviderError(
                f"無法抓取 {day.isoformat()}（台灣）的歐洲足球賽程：{exc}；既有快照沒有被覆蓋"
            ) from exc
        fixtures.extend(item for item in response if isinstance(item, dict))

    supported = {league_id: league_name for league_name, (_, league_id) in FOOTBALL_LEAGUES.items()}
    events: list[Event] = []
    seen_fixture_ids: set[str] = set()
    taipei = ZoneInfo("Asia/Taipei")
    for item in fixtures:
        league = item.get("league", {})
        league_id = league.get("id")
        try:
            league_id = int(league_id)
        except (TypeError, ValueError):
            continue
        league_name = supported.get(league_id)
        if not league_name:
            continue
        fixture = item.get("fixture", {})
        fixture_id = str(fixture.get("id") or "")
        kickoff = str(fixture.get("date") or "")
        try:
            kickoff_local = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(taipei)
        except ValueError:
            continue
        if kickoff_local.date() != day or not fixture_id or fixture_id in seen_fixture_ids:
            continue
        status = fixture.get("status", {}).get("short")
        teams = item.get("teams", {})
        home, away = teams.get("home", {}), teams.get("away", {})
        if not home.get("name") or not away.get("name"):
            continue
        seen_fixture_ids.add(fixture_id)
        events.append(Event(
            event_id=f"football:{fixture_id}", sport="football", kickoff=kickoff,
            home=home["name"], away=away["name"], league=league_name,
            metadata={"source": "api-football", "status": status, "league_id": league_id, "season": football_season(day),
                      "home_team_id": home.get("id"), "away_team_id": away.get("id"),
                      "fixture_id": fixture.get("id")},
        ))
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


def _recent_fixture_profile(team_id: int) -> dict[str, dict[str, float]]:
    """Build home/away profiles from a team's recent completed fixtures.

    UEFA competitions can have too few matches for ``teams/statistics`` early
    in a season.  This remains API-Football-only: it derives the same attack,
    defence and form inputs from real completed matches, never a fixed prior.
    """

    response = _api_football("fixtures", {"team": team_id, "last": 8})
    buckets: dict[str, list[tuple[int, int, str]]] = {"home": [], "away": []}
    for item in response:
        fixture = item.get("fixture", {})
        if fixture.get("status", {}).get("short") not in {"FT", "AET", "PEN"}:
            continue
        teams, goals = item.get("teams", {}), item.get("goals", {})
        home_id, away_id = (teams.get("home") or {}).get("id"), (teams.get("away") or {}).get("id")
        home_goals, away_goals = _number(goals.get("home")), _number(goals.get("away"))
        if home_goals is None or away_goals is None:
            continue
        if home_id == team_id:
            buckets["home"].append((int(home_goals), int(away_goals), "W" if home_goals > away_goals else "D" if home_goals == away_goals else "L"))
        elif away_id == team_id:
            buckets["away"].append((int(away_goals), int(home_goals), "W" if away_goals > home_goals else "D" if away_goals == home_goals else "L"))

    profiles: dict[str, dict[str, float]] = {}
    for context, matches in buckets.items():
        played = len(matches)
        if not played:
            continue
        scored = sum(row[0] for row in matches)
        conceded = sum(row[1] for row in matches)
        points = sum({"W": 3, "D": 1}.get(row[2], 0) for row in matches)
        clean = sum(1 for _, against, _ in matches if against == 0)
        failed = sum(1 for scored, _, _ in matches if scored == 0)
        profiles[context] = {
            "played": float(played), "for_average": scored / played, "against_average": conceded / played,
            "clean_sheets": float(clean), "failed_to_score": float(failed),
            "clean_sheet_rate": clean / played, "failed_to_score_rate": failed / played,
            "form_ratio": points / (3 * played),
        }
    return profiles


def _domestic_team_profiles(team_id: int, season: int) -> tuple[dict[str, float], dict[str, float]]:
    """Use the team's domestic-league split when a UEFA competition is too small.

    Champions League samples are often fewer than three home/away matches.
    The API's own domestic league statistics provide a real current-season
    alternative; no fixed league prior or bookmaker probability is used.
    """

    competitions = _api_football("leagues", {"team": team_id, "season": season})
    candidates: list[int] = []
    for item in competitions:
        league = item.get("league") or {}
        country = item.get("country") or {}
        league_id = _number(league.get("id"))
        if (str(league.get("type", "")).lower() == "league"
                and str(country.get("name", "")).lower() not in {"world", "europe"}
                and league_id is not None):
            candidates.append(int(league_id))
    for league_id in candidates:
        response = _api_football("teams/statistics", {"league": league_id, "season": season, "team": team_id})
        raw = response if isinstance(response, Mapping) else (response[0] if response else {})
        home, away = _team_stat_profile(raw, "home"), _team_stat_profile(raw, "away")
        if home.get("played", 0) >= 3 and away.get("played", 0) >= 3:
            return home, away
    return {}, {}


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
                    home_profile, away_profile = _team_stat_profile(raw, "home"), _team_stat_profile(raw, "away")
                except ProviderError:
                    home_profile, away_profile = {}, {}
                if (league_id == 2
                        and (home_profile.get("played", 0) < 3 or away_profile.get("played", 0) < 3)):
                    try:
                        domestic_home, domestic_away = _domestic_team_profiles(team_id, season)
                    except ProviderError:
                        domestic_home, domestic_away = {}, {}
                    if home_profile.get("played", 0) < 3 and domestic_home.get("played", 0) >= 3:
                        home_profile = domestic_home
                    if away_profile.get("played", 0) < 3 and domestic_away.get("played", 0) >= 3:
                        away_profile = domestic_away
                if home_profile.get("played", 0) < 3 or away_profile.get("played", 0) < 3:
                    try:
                        recent = _recent_fixture_profile(team_id)
                    except ProviderError:
                        recent = {}
                    if home_profile.get("played", 0) < 3 and recent.get("home", {}).get("played", 0) >= 3:
                        home_profile = recent["home"]
                    if away_profile.get("played", 0) < 3 and recent.get("away", {}).get("played", 0) >= 3:
                        away_profile = recent["away"]
                if home_profile:
                    profiles[(league_id, season, team_id, "home")] = home_profile
                if away_profile:
                    profiles[(league_id, season, team_id, "away")] = away_profile
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



def fetch_final_results(sport: str, day: date) -> dict[str, dict[str, Any]]:
    """Return only completed official results for events in a saved snapshot."""

    results: dict[str, dict[str, Any]] = {}
    if sport == "mlb":
        payload = _mlb_schedule(day)
        for block in payload.get("dates", []):
            for game in block.get("games", []):
                if game.get("status", {}).get("abstractGameState") != "Final" or not taiwan_day_matches(game.get("gameDate"), day):
                    continue
                teams = game.get("teams", {})
                home, away = teams.get("home", {}), teams.get("away", {})
                if home.get("score") is None or away.get("score") is None:
                    continue
                results[f"mlb:{game.get('gamePk')}"] = {
                    "status": "final", "home_score": int(home["score"]), "away_score": int(away["score"]),
                    "unsupported_rules": int((game.get("linescore") or {}).get("currentInning") or 0)<9,
                }
        return results
    if sport == "nba":
        payload = _nba_scoreboard(day)
        for item in payload.get("events", []):
            status = ((item.get("status") or {}).get("type") or {}).get("name", "")
            if status != "STATUS_FINAL" or not taiwan_day_matches(item.get("date"), day):
                continue
            competitors = ((item.get("competitions") or [{}])[0].get("competitors") or [])
            home = next((row for row in competitors if row.get("homeAway") == "home"), {})
            away = next((row for row in competitors if row.get("homeAway") == "away"), {})
            if home.get("score") is None or away.get("score") is None:
                continue
            results[f"nba:{item.get('id')}"] = {
                "status": "final", "home_score": int(home["score"]), "away_score": int(away["score"]),
            }
        return results
    if sport == "football":
        # Exact API-Football IDs remain preferred when that service is available.
        try:
            for utc_day in (day - timedelta(days=1), day):
                for item in _api_football("fixtures", {"date": utc_day.isoformat()}):
                    fixture = item.get("fixture", {})
                    if fixture.get("status", {}).get("short") not in {"FT", "AET", "PEN"}:
                        continue
                    if not taiwan_day_matches(fixture.get("date"), day):
                        continue
                    # fulltime is the 90-minute score; goals may include ET/PK.
                    goals = (item.get("score") or {}).get("fulltime") or {}
                    if not goals and fixture.get("status", {}).get("short") == "FT":
                        goals = item.get("goals", {})
                    if goals.get("home") is None or goals.get("away") is None:
                        continue
                    results[f"football:{fixture.get('id')}"] = {
                        "status": "final", "home_score": int(goals["home"]), "away_score": int(goals["away"]),
                    }
        except ProviderError:
            pass
        # ESPN IDs settle snapshots created through the keyless fallback.
        for league_name, espn_league in ESPN_SOCCER_LEAGUES.items():
            for source_day in (day - timedelta(days=1), day):
                payload = _espn_football_payload(
                    ESPN_SOCCER_SCOREBOARD_URL, league=espn_league,
                    params={"dates": source_day.strftime("%Y%m%d"), "limit": 100},
                )
                for item in payload.get("events", []):
                    status = ((item.get("status") or {}).get("type") or {}).get("name", "")
                    competitors = ((item.get("competitions") or [{}])[0].get("competitors") or [])
                    home = next((row for row in competitors if row.get("homeAway") == "home"), {})
                    away = next((row for row in competitors if row.get("homeAway") == "away"), {})
                    if (status != "STATUS_FINAL" or competition_period(item) != 2
                            or not taiwan_day_matches(item.get("date"), day)
                            or home.get("score") is None or away.get("score") is None):
                        continue
                    results[f"football:espn:{espn_league}:{item.get('id')}"] = {
                        "status": "final", "home_score": int(home["score"]), "away_score": int(away["score"]),
                    }
        return results
    raise ProviderError("不支援的賽事種類")


def competition_period(item: dict) -> int:
    # When ESPN cannot prove regulation-only result, leave ET/PK unsettled.
    status = item.get("status") or (item.get("competitions") or [{}])[0].get("status") or {}
    return int(status.get("period") or 0)

def fetch_odds(sport_key: str, *, regions: str | None = None) -> list[dict[str, Any]]:
    """Fetch lines and decimal prices; h2h consumes an extra market quota."""

    key = (os.environ.get("THE_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY") or "").strip()
    if not key:
        raise ProviderError("尚未設定 THE_ODDS_API_KEY")
    return _get(ODDS_URL.format(sport=sport_key), params={
        "apiKey": key, "regions": regions or os.environ.get("ODDS_REGIONS", "eu"),
        "markets": "h2h,spreads,totals", "dateFormat": "iso", "oddsFormat": "decimal",
    })


def _market_from_book(book: dict[str, Any], event: Event) -> Market | None:
    """Only coherent, fresh two-sided lines/prices from one bookmaker."""
    lines, prices, timestamps = {}, {}, {}
    now = datetime.now(timezone.utc)
    for market in book.get("markets", []):
        kind = {"h2h":"moneyline","spreads":"spread","totals":"total"}.get(market.get("key"))
        stamp = parse_time(market.get("last_update") or book.get("last_update"))
        if not kind or stamp is None or not -60 <= (now-stamp).total_seconds() <= 1800:
            continue
        points, quoted = {}, {}
        for outcome in market.get("outcomes", []):
            name = str(outcome.get("name", ""))
            side = (name.lower() if kind=="total" and name.lower() in {"over","under"} else
                    "home" if _same_team(name,event.home) else "away" if _same_team(name,event.away) else
                    "draw" if kind=="moneyline" and name.lower()=="draw" else None)
            if side is None:
                continue
            point, price = _number(outcome.get("point")), _number(outcome.get("price"))
            if point is not None and abs(point*4-round(point*4)) < 1e-7:
                points[side] = point
            if price is not None and price > 1:
                quoted[side] = price
        if kind=="spread":
            if not {"home","away"} <= points.keys() or abs(points["home"]+points["away"]) > 1e-7:
                continue
            lines[kind] = points["home"]
        elif kind=="total":
            if not {"over","under"} <= points.keys() or points["over"] <= 0 or abs(points["over"]-points["under"]) > 1e-7:
                continue
            lines[kind] = points["over"]
        else:
            expected = {"home","away","draw"} if event.sport=="football" else {"home","away"}
            if not expected <= quoted.keys():
                continue
        timestamps[kind] = stamp.isoformat()
        prices.update({f"{kind}:{side}":price for side,price in quoted.items()})
    if not timestamps:
        return None
    return Market(str(book.get("title", "Unknown bookmaker")), min(timestamps.values()),
                  lines.get("spread"),lines.get("total"),prices,timestamps)


def market_for_event(event: Event, odds_events: Iterable[dict[str, Any]]) -> Market | None:
    """Match teams AND kickoff; fail closed on ambiguous/doubleheader matches."""
    from dataclasses import replace
    start = parse_time(event.kickoff)
    if start is None or start <= datetime.now(timezone.utc):
        return None
    matches = []
    for odds_event in odds_events:
        if not (_same_team(str(odds_event.get("home_team", "")), event.home)
                and _same_team(str(odds_event.get("away_team", "")), event.away)):
            continue
        kickoff = parse_time(odds_event.get("commence_time"))
        if kickoff is not None and abs((start-kickoff).total_seconds()) <= 900:
            matches.append(odds_event)
    if len(matches)!=1:
        return None
    choices = []
    for book in matches[0].get("bookmakers", []):
        market = _market_from_book(book,event)
        if market:
            completeness = int(market.spread_line is not None)+int(market.total_line is not None)
            choices.append((completeness, int(book.get("key")=="pinnacle"), market.updated_at, market))
    if not choices:
        return None
    best = max(choices,key=lambda x:x[:3])[-1]
    return replace(best,provider_event_id=str(matches[0].get("id") or ""))
