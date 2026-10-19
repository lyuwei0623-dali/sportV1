"""Transparent, probability-first forecasting for the clean rebuild.

The member-facing questions are deliberately simple:

* Which *team* has the higher win probability?
* At the saved handicap, which side has the higher cover probability?
* At the saved total, is over or under more likely to pass?

Bookmaker prices and price-derived value scores do not enter this module. When
a real handicap or total line is temporarily unavailable, a clearly-labelled model reference line
keeps the automatic snapshot complete without pretending that it is a live
betting line.
"""
from __future__ import annotations

import math

from models import Market, Projection, Recommendation


MAX_GOALS = 8


def poisson(value: int, rate: float) -> float:
    if value < 0 or rate <= 0:
        return 0.0
    return math.exp(-rate) * rate**value / math.factorial(value)


def score_matrix(home_rate: float, away_rate: float, maximum: int = MAX_GOALS) -> dict[tuple[int, int], float]:
    """Return a truncated, re-normalised independent Poisson score grid."""

    raw = {(home, away): poisson(home, home_rate) * poisson(away, away_rate)
           for home in range(maximum + 1) for away in range(maximum + 1)}
    total = sum(raw.values())
    return {key: value / total for key, value in raw.items()} if total else raw


def outcome_probabilities(matrix: dict[tuple[int, int], float]) -> tuple[float, float, float]:
    home = sum(probability for (left, right), probability in matrix.items() if left > right)
    draw = sum(probability for (left, right), probability in matrix.items() if left == right)
    away = sum(probability for (left, right), probability in matrix.items() if left < right)
    return home, draw, away


def _main_score(matrix: dict[tuple[int, int], float], home_win: float, away_win: float) -> tuple[tuple[int, int], float]:
    """Pick a score within the higher-win-probability team's scenario.

    This prevents a generic draw such as 1:1 from becoming the displayed main
    prediction merely because it is the largest *unconditional* individual
    score cell. The shown percentage stays unconditional and is therefore
    honest about correct-score uncertainty.
    """

    if home_win >= away_win:
        candidates = {score: probability for score, probability in matrix.items() if score[0] > score[1]}
    else:
        candidates = {score: probability for score, probability in matrix.items() if score[1] > score[0]}
    return max(candidates.items(), key=lambda item: item[1]) if candidates else max(matrix.items(), key=lambda item: item[1])


def score_projection(home_rate: float, away_rate: float, quality: str) -> Projection:
    """Build a football projection from team / player inputs only."""

    matrix = score_matrix(home_rate, away_rate)
    home, draw, away = outcome_probabilities(matrix)
    score, probability = _main_score(matrix, home, away)
    return Projection(
        home_expected=round(home_rate, 2), away_expected=round(away_rate, 2),
        home_win=home, draw=draw, away_win=away,
        score=f"{score[0]}:{score[1]}", score_probability=probability,
        source_quality=quality,
    )


def baseball_projection(home_rate: float, away_rate: float, quality: str) -> Projection:
    """Build an MLB win projection; terminal tie mass is split evenly."""

    matrix = score_matrix(home_rate, away_rate, maximum=12)
    home, draw, away = outcome_probabilities(matrix)
    home += draw / 2
    away += draw / 2
    score, probability = _main_score(matrix, home, away)
    return Projection(round(home_rate, 2), round(away_rate, 2), home, None, away,
                      f"{score[0]}:{score[1]}", probability, quality)


def _asian_components(line: float) -> tuple[float, ...]:
    """Split quarter Asian lines into half-stakes; leave other lines intact."""

    scaled = round(line * 4)
    if abs(scaled) % 2:
        return math.floor(line * 2) / 2, math.ceil(line * 2) / 2
    return (line,)


def asian_cover_probability(matrix: dict[tuple[int, int], float], *, side: str, line: float,
                            market_type: str) -> float:
    """Probability of a net-positive settlement; pushes are not counted."""

    probability = 0.0
    components = _asian_components(line)
    for score, weight in matrix.items():
        if market_type == "spread":
            difference = score[0] - score[1]
            base = difference if side == "home" else -difference
            settled = sum(1 if base + part > 0 else -1 if base + part < 0 else 0 for part in components) / len(components)
        else:
            goals = score[0] + score[1]
            # For totals, the line is subtracted for over and subtracted from
            # the line for under.  (Using the same sign as a spread would
            # incorrectly make every positive total an "over".)
            settled = sum(
                1 if ((goals - part) if side == "over" else (part - goals)) > 0
                else -1 if ((goals - part) if side == "over" else (part - goals)) < 0
                else 0
                for part in components
            ) / len(components)
        if settled > 0:
            probability += weight
    return probability


def _round_to_step(value: float, step: float) -> float:
    return round(value / step) * step


def _model_spread_line(projection: Projection, sport: str) -> float:
    """Return a transparent reference handicap when no real line is saved."""

    step = 0.5 if sport == "mlb" else 0.25
    line = -_round_to_step(projection.home_expected - projection.away_expected, step)
    return 0.0 if abs(line) < .001 else line


def _model_total_line(projection: Projection, sport: str) -> float:
    step = 0.5 if sport == "mlb" else 0.25
    return _round_to_step(projection.home_expected + projection.away_expected, step)


def _line_source(actual_line: float | None, fallback_line: float, *, kind: str) -> tuple[str, float, str]:
    if actual_line is not None:
        label = "保存的實際讓分線" if kind == "spread" else "保存的實際大小線"
        return "market_line", actual_line, label
    label = "尚無實際讓分線，使用模型基準線（非可下注盤）" if kind == "spread" else "尚無實際大小線，使用模型基準線（非可下注盤）"
    return "model_reference", fallback_line, label


def probability_recommendations(projection: Projection, market: Market | None, *, sport: str) -> tuple[Recommendation, ...]:
    """Return one plain-language probability statement per required category."""

    matrix = score_matrix(projection.home_expected, projection.away_expected,
                          maximum=12 if sport == "mlb" else MAX_GOALS)
    labels = {"home": "主隊", "away": "客隊", "over": "大分", "under": "小分"}

    # The direct member question is which TEAM is more likely to win. A draw
    # remains visible separately for football, but is never substituted for a
    # team answer in the win-probability card.
    win_probability, winner = max((projection.home_win, "home"), (projection.away_win, "away"))
    moneyline = Recommendation(
        "moneyline", "model", f"{labels[winner]}勝", win_probability, None,
        "以球隊、球員／先發與可用傷停資料計算；足球和局機率另列",
    )

    spread_state, spread_line, spread_reason = _line_source(
        market.spread_line if market else None, _model_spread_line(projection, sport), kind="spread"
    )
    spread_candidates = []
    for side in ("home", "away"):
        side_line = spread_line if side == "home" else -spread_line
        spread_candidates.append((
            asian_cover_probability(matrix, side=side, line=side_line, market_type="spread"), side, side_line,
        ))
    spread_probability, spread_side, displayed_spread_line = max(spread_candidates)
    spread = Recommendation(
        "spread", spread_state, f"{labels[spread_side]} {displayed_spread_line:+g}",
        spread_probability, spread_line, spread_reason,
    )

    total_state, total_line, total_reason = _line_source(
        market.total_line if market else None, _model_total_line(projection, sport), kind="total"
    )
    total_candidates = [
        (asian_cover_probability(matrix, side=side, line=total_line, market_type="total"), side)
        for side in ("over", "under")
    ]
    total_probability, total_side = max(total_candidates)
    total = Recommendation(
        "total", total_state, f"{labels[total_side]} {total_line:g}",
        total_probability, total_line, total_reason,
    )
    return moneyline, spread, total
