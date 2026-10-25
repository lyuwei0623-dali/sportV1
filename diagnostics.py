"""Allowlisted diagnostics: no raw exception, request, key, password or payload."""
import json
import logging
import uuid
from datetime import datetime, timezone
from time_utils import parse_time

VERSION = "audit-v3.0"

def record(stage, *, sport="", error=None, event_count=None):
    report = {"id":uuid.uuid4().hex[:12], "version":VERSION,
              "time":datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "stage":stage if stage in {"snapshot","settlement","storage","render","publication"} else "unknown",
              "sport":sport if sport in {"mlb","nba","football"} else "",
              "status":"failed" if error is not None else "ok"}
    if event_count is not None:
        report["event_count"] = int(event_count)
    # Never str(error), repr(error) or traceback: upstream exceptions contain URLs/keys.
    logging.getLogger("vidali.audit").warning(json.dumps(report,ensure_ascii=False))
    return report

def snapshot_health(snapshot):
    events = snapshot.get("events",[])
    now = datetime.now(timezone.utc)
    models = sum(bool(i.get("projection")) for i in events)
    lines = sum(bool(i.get("market")) for i in events)
    missing_prices = sum(r.get("decimal_price") is None for i in events for r in i.get("recommendations",[]) if r.get("state")!="unavailable")
    stale = 0
    for item in events:
        stamp = parse_time((item.get("market") or {}).get("updated_at"))
        if item.get("market") and (stamp is None or (now-stamp).total_seconds()>1800):
            stale += 1
    return {"賽事數":len(events), "有模型估計":models, "有保存盤口":lines,
            "盤口逾30分鐘或時間不明":stale, "缺賠率的分析項目":missing_prices,
            "模型驗證":"未完成樣本外校準；PASS", "版本":snapshot.get("version","舊版")}
