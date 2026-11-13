"""Durable daily publication; no Streamlit session or public payload logging."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json

from providers import NoEventsError, ProviderError
from service import build_mlb_snapshot, build_nba_snapshot, build_football_snapshot, save_snapshot
from storage import create_store, SupabaseSnapshotStore, StorageError, STORAGE_CODES
from time_utils import TAIPEI

BUILDERS = {"mlb": build_mlb_snapshot, "nba": build_nba_snapshot,
            "football": build_football_snapshot}


def current_session_day(now: datetime):
    """A delayed run after midnight still targets the preceding noon's slate."""
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    return (now.astimezone(TAIPEI) - timedelta(hours=12)).date()


def run_one(store, sport, day, *, builder=None):
    # Never invoke publish_automatic: that operation replaces the manual release.
    existing, _ = store.get_member_snapshot(sport, day.isoformat())
    if existing is not None:
        return {"status": "existing_preserved", "events": len(existing.get("events", []))}
    try:
        snapshot = (builder or BUILDERS[sport])(day)
    except NoEventsError:
        return {"status": "no_events", "events": 0}
    if snapshot.sport != sport or snapshot.date_key != day.isoformat():
        raise ProviderError("快照運動／日期不符，停止保存")
    # Recheck inside save_snapshot, then use the store's compare-and-swap to
    # reject a simultaneous administrator / other scheduler write.
    save_snapshot(store, snapshot, only_if_absent=True)
    # Saving automatic makes it member-visible when no manual release exists.
    return {"status": "saved", "events": len(snapshot.events),
            "models": sum(item.projection is not None for item in snapshot.events),
            "generated_at": snapshot.generated_at}


def main(argv=None):
    parser = argparse.ArgumentParser(description="台灣中午賽前快照（僅新增，不覆蓋）")
    parser.add_argument("--sport", required=True, choices=tuple(BUILDERS))
    args = parser.parse_args(argv)
    day = current_session_day(datetime.now(timezone.utc))
    report = {"sport": args.sport, "date_key": day.isoformat()}
    try:
        store, _ = create_store()
        if not isinstance(store, SupabaseSnapshotStore):
            raise StorageError("排程必須使用永久資料庫", code="STORAGE_CONFIG")
        store.check_read_access()
        report.update(run_one(store, args.sport, day))
    except StorageError as exc:
        report.update(status="failed", code=exc.code if exc.code in STORAGE_CODES else "STORAGE_UNKNOWN")
    except ProviderError:
        report.update(status="failed", code="PROVIDER_UNAVAILABLE")
    except Exception:
        # Never print exception text, tracebacks, secrets or original payloads.
        report.update(status="failed", code="UNEXPECTED_ERROR")
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
