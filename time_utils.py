"""One time convention for providers, markets, publication and settlement."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
SESSION_POLICY = "taipei-noon-to-noon-v1"

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
    """One non-overlapping sports slate: local noon to next noon, end excluded."""
    start = datetime.combine(day, time(12), tzinfo=TAIPEI)
    return start, start + timedelta(days=1)

def session_day_matches(value, day):
    stamp = parse_time(value)
    start, end = session_window(day)
    return stamp is not None and start <= stamp < end

def session_label(day):
    start, end = session_window(day)
    return f"台灣時間 {start:%Y/%m/%d %H:%M} ～ {end:%Y/%m/%d %H:%M} 前（當晚＋隔日凌晨／上午）"

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
    for item in result.get("events", []):
        event = item.get("event", {})
        event_id = event.get("event_id")
        if (day is not None and event_id and event_id not in seen
                and session_day_matches(event.get("kickoff"), day)):
            kept.append(item)
            seen.add(event_id)
        else:
            removed.append(item)
    result["events"] = sorted(kept, key=lambda item: parse_time(item["event"]["kickoff"]))
    if removed:
        result["window_archived_events"] = [*result.get("window_archived_events", []), *removed]
        # The old aggregate includes excluded games; never present it as this slate's accuracy.
        result.pop("settlement_summary", None)
        result["window_summary_needs_refresh"] = True
    return result

def pregame(kickoff, observed_at):
    start, observed = parse_time(kickoff), parse_time(observed_at)
    return start is not None and observed is not None and observed < start
