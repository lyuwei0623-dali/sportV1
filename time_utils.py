"""One time convention for providers, markets, publication and settlement."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
SESSION_POLICY = "taipei-next-calendar-day-v2"

def parse_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None

def taiwan_day_matches(value, day):
    stamp = parse_time(value)
    return stamp is not None and stamp.astimezone(TAIPEI).date() == day

def session_window(day):
    """Selecting D queries D+1 in Taipei, midnight inclusive to midnight exclusive."""
    start = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TAIPEI)
    return start, start + timedelta(days=1)

def session_day_matches(value, day):
    stamp = parse_time(value)
    start, end = session_window(day)
    return stamp is not None and start <= stamp < end

def session_label(day):
    start, end = session_window(day)
    return f"查詢日 {day:%Y/%m/%d}｜台灣賽事時間 {start:%Y/%m/%d} 00:00～23:59（至 {end:%Y/%m/%d} 00:00 前）"

def filter_snapshot_window(snapshot):
    """Read-only projection and safe migration of legacy mixed-date payloads.

    Excluded records remain in an archive within the copied payload, never
    deleted from storage. Do not mark an old slate as newly fetched/complete.
    """
    result = deepcopy(dict(snapshot))
    try:
        day = date.fromisoformat(str(result.get("date_key", "")))
    except ValueError:
        day = None
    kept, removed, seen = [], [], set()
    active = list(result.get("events", []))
    candidates = list(active)
    archived = list(result.get("window_archived_events", []))
    restore_archive = result.get("date_policy") != SESSION_POLICY and bool(archived)
    if restore_archive:
        # Old noon-window archives may contain the next evening, now in range.
        # Active records win duplicates; no prediction is recomputed.
        candidates.extend(archived)
    for item in candidates:
        event = item.get("event", {})
        event_id = event.get("event_id")
        if (day is not None and event_id and event_id not in seen
                and session_day_matches(event.get("kickoff"), day)):
            kept.append(item)
            seen.add(event_id)
        else:
            removed.append(item)
    result["events"] = sorted(kept, key=lambda item: parse_time(item["event"]["kickoff"]))
    if removed or restore_archive:
        result["window_archived_events"] = removed if restore_archive else [*archived, *removed]
    if kept != active:
        # The old aggregate includes excluded games; never present it as this slate's accuracy.
        result.pop("settlement_summary", None)
        result["window_summary_needs_refresh"] = True
    return result

def pregame(kickoff, observed_at):
    start, observed = parse_time(kickoff), parse_time(observed_at)
    return start is not None and observed is not None and observed < start

