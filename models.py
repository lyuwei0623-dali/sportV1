"""Stable data contracts for 維大力體育APP Restart.

The contracts are deliberately independent of Streamlit and provider payloads.
Only a saved ``DailySnapshot`` is ever shown to a member.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Sport = Literal["mlb", "nba", "football"]


@dataclass(frozen=True)
class Event:
    event_id: str
    sport: Sport
    kickoff: str
    home: str
    away: str
    league: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Market:
    """Saved external lines from one bookmaker.

    Prices are deliberately not kept in the clean rebuild. They are neither
    shown to members nor used to manufacture a recommendation. The market is
    only the real handicap / total line against which the team-data model is
    evaluated.
    """

    bookmaker: str
    updated_at: str
    spread_line: float | None = None
    total_line: float | None = None
    prices: dict[str, float] = field(default_factory=dict)
    market_updated_at: dict[str, str] = field(default_factory=dict)
    provider_event_id: str = ""


@dataclass(frozen=True)
class Projection:
    home_expected: float
    away_expected: float
    home_win: float
    draw: float | None
    away_win: float
    score: str
    score_probability: float
    source_quality: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    market_type: Literal["moneyline", "spread", "total"]
    state: Literal["model", "market_line", "model_reference", "unavailable"]
    selection: str
    model_probability: float | None
    market_line: float | None
    reason: str
    side: str = ""
    decimal_price: float | None = None
    expected_return: float | None = None
    settlement_probabilities: dict[str, float] = field(default_factory=dict)
    actionable: bool = False
    decision: str = "PASS｜模型尚未完成樣本外校準"


@dataclass(frozen=True)
class EventAnalysis:
    event: Event
    projection: Projection | None
    market: Market | None
    recommendations: tuple[Recommendation, ...]
    warnings: tuple[str, ...] = ()
    model_inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DailySnapshot:
    sport: Sport
    date_key: str
    generated_at: str
    events: tuple[EventAnalysis, ...]
    source_summary: tuple[str, ...]
    version: str = "audit-v3.1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
