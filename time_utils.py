"""One time convention for providers, markets, publication and settlement."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")

def parse_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None

def taiwan_day_matches(value, day):
    stamp = parse_time(value)
    return stamp is not None and stamp.astimezone(TAIPEI).date() == day

def pregame(kickoff, observed_at):
    start, observed = parse_time(kickoff), parse_time(observed_at)
    return start is not None and observed is not None and observed < start
