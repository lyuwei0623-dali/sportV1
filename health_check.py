"""Public-provider smoke probe; no credentials, no snapshot writes, no wagers.

Output deliberately omits URLs, exception text, source payloads and user data.
HTTP-only app reachability is *not* evidence that login/database/model works.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.request import Request,urlopen
from providers import fetch_mlb_events,fetch_nba_events,fetch_espn_football_events
from time_utils import TAIPEI,session_day_matches,session_label,SESSION_POLICY
from service import fetch_session_events
from diagnostics import VERSION

def probe(name,fetch,day):
    try:
        rows=fetch_session_events(fetch,day)
        ids=[e.event_id for e in rows]
        valid=len(ids)==len(set(ids)) and all(session_day_matches(e.kickoff,day) for e in rows)
        return {"source":name,"status":"ok" if valid else "invalid_schema_or_date","events":len(rows),
                "coverage":"未與第二來源交叉驗證"}
    except Exception:
        return {"source":name,"status":"unavailable","coverage":"來源不可用，不等於沒有賽事"}

def app_probe():
    try:
        req=Request("https://vidali-sports.streamlit.app/_stcore/health",headers={"User-Agent":"VidaliHealth/3.0"})
        with urlopen(req,timeout=12) as r:
            body=r.read(2048).decode("utf-8",errors="replace").strip().lower()
            return {"source":"app","status":"http_only_unverified" if r.status==200 else "unavailable",
                    "streamlit_health":body=="ok", "scope":"HTTP檢查不代表會員登入／Supabase正常"}
    except Exception:
        return {"source":"app","status":"unavailable","scope":"網路／休眠／存取權限需另查"}

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="health-report.json")
    args=parser.parse_args();day=datetime.now(TAIPEI).date()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(probe,name,fn,day) for name,fn in
                 [("mlb",fetch_mlb_events),("nba",fetch_nba_events),("football",fetch_espn_football_events)]]
        checks=[f.result() for f in futures]
    checks.append(app_probe())
    report={"version":VERSION,"date_taipei":day.isoformat(),"date_policy":SESSION_POLICY,"query_window":session_label(day),"checks":checks,
            "not_checked":["登入憑證","Supabase寫入","付費盤口API","傷停完整性","模型準確度"]}
    text=json.dumps(report,ensure_ascii=False,indent=2)
    Path(args.output).write_text(text,encoding="utf-8")
    print(text)
    return int(any(row["status"] in {"unavailable","invalid_schema_or_date"} for row in checks))

if __name__=="__main__": raise SystemExit(main())
