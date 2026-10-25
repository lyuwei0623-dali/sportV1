from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,date,timedelta,timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auth import verify_password,credential_version
from diagnostics import record
from forecasting import (score_matrix,score_projection,baseball_projection,probability_recommendations,
                         basketball_projection,asian_distribution)
from models import Event,EventAnalysis,DailySnapshot,Market
from providers import (fetch_mlb_events,fetch_nba_events,fetch_nba_team_profiles,_nba_standings,
                       _same_team,market_for_event,_market_from_book,_innings,_number,fetch_final_results,ProviderError)
from service import settle_snapshot,save_snapshot,apply_manual_calibration
from settlement_math import settle,profit
from storage import LocalSnapshotStore
from time_utils import TAIPEI

NOW=datetime.now(timezone.utc)
FUTURE=(NOW+timedelta(days=2)).isoformat()

def event(sport="football"):
    return Event("one",sport,FUTURE,"Home FC","Away FC","英超" if sport=="football" else sport.upper())

def book():
    return {"title":"test","key":"pinnacle","last_update":NOW.isoformat(),"markets":[
        {"key":"spreads","outcomes":[{"name":"Home FC","point":-.5,"price":1.9},{"name":"Away FC","point":.5,"price":2.0}]},
        {"key":"totals","outcomes":[{"name":"Over","point":2.5,"price":1.95},{"name":"Under","point":2.5,"price":1.95}]},
        {"key":"h2h","outcomes":[{"name":"Home FC","price":1.8},{"name":"Away FC","price":4.1},{"name":"Draw","price":3.4}]},
    ]}

def fixture_snapshot(sport="football",selection="客隊 +0.5",line=-.5):
    return {"sport":sport,"version":"audit-v3.0","events":[{
        "event":asdict(event(sport)),"captured_at":NOW.isoformat(),"capture_schema":3,
        "recommendations":[{"market_type":"moneyline","state":"model","selection":"主隊勝","model_probability":.6,"decimal_price":1.9},
                           {"market_type":"spread","state":"market_line","selection":selection,"market_line":line,"decimal_price":1.9},
                           {"market_type":"total","state":"market_line","selection":"小分 2.25","market_line":2.25,"decimal_price":1.9}]}]}

class AuditMath(unittest.TestCase):
    def test_probability_mass_and_high_score_tail(self):
        m=score_matrix(7.2,7.0,maximum=12)
        self.assertAlmostEqual(sum(m.values()),1,places=12)
        self.assertTrue(any(h>12 for h,a in m))
        self.assertAlmostEqual(sum(h*p for (h,a),p in m.items()),7.2,places=8)

    def test_invalid_rates_and_zero_rate(self):
        for x in [float('nan'),float('inf'),-1]:
            with self.assertRaises(ValueError): score_matrix(x,1)
        self.assertEqual(score_matrix(0,0)[(0,0)],1)

    def test_most_likely_draw_not_suppressed(self):
        self.assertEqual(score_projection(1.1,1.1,"test").score,"1:1")

    def test_quarter_outcomes(self):
        self.assertEqual(settle(0,.25),"half_won")
        self.assertEqual(settle(0,-.25),"half_lost")
        self.assertEqual(settle(1,-1),"push")
        self.assertAlmostEqual(profit("half_won",1.8),.4)
        self.assertEqual(profit("half_lost",1.8),-.5)
        with self.assertRaises(ValueError): settle(0,.3)

    def test_distribution_ev_half_stakes(self):
        m={(1,1):1.0}
        d=asian_distribution(m,side="home",line=.25,market_type="spread")
        self.assertEqual(d["half_won"],1)
        self.assertAlmostEqual(sum(d[k]*profit(k,1.8) for k in d),.4)

    def test_nba_integer_line_push_mass(self):
        p=basketball_projection(110,110,"test")
        picks=probability_recommendations(p,Market("test","now",spread_line=2,total_line=220),sport="nba")
        self.assertGreater(picks[1].settlement_probabilities["push"],0)
        self.assertGreater(picks[2].settlement_probabilities["push"],0)
        for r in picks:
            self.assertAlmostEqual(sum(r.settlement_probabilities.values()),1,places=8)

    def test_mlb_moneyline_and_zero_spread_agree(self):
        p=baseball_projection(5,4,"test")
        picks=probability_recommendations(p,Market("test","now",spread_line=0),sport="mlb")
        self.assertAlmostEqual(picks[0].model_probability,picks[1].model_probability)

    def test_high_probability_can_have_negative_ev_and_still_pass(self):
        p=score_projection(2,.7,"test")
        r=probability_recommendations(p,Market("test","now",prices={"moneyline:home":1.05}),sport="football")[0]
        self.assertGreater(r.model_probability,.5)
        self.assertLess(r.expected_return,0)
        self.assertFalse(r.actionable)

class AuditSettlement(unittest.TestCase):
    def test_away_spread_direction_and_soccer_draw(self):
        s=fixture_snapshot()
        result={"one":{"status":"final","home_score":0,"away_score":1}}
        new,n=settle_snapshot(s,results=result)
        self.assertEqual(new["events"][0]["settlement"]["picks"]["spread"]["outcome"],"won")
        self.assertNotIn("settlement",s["events"][0])
        draw,_=settle_snapshot(s,results={"one":{"status":"final","home_score":1,"away_score":1}})
        picks=draw["events"][0]["settlement"]["picks"]
        self.assertEqual(picks["moneyline"]["outcome"],"lost")
        self.assertEqual(picks["total"]["outcome"],"half_won")

    def test_partial_second_fetch_keeps_summary(self):
        s,_=settle_snapshot(fixture_snapshot(),results={"one":{"status":"final","home_score":1,"away_score":1}})
        again,_=settle_snapshot(s,results={})
        self.assertEqual(s["settlement_summary"],again["settlement_summary"])

    def test_legacy_and_postgame_not_counted(self):
        for mutate in (lambda s:s["events"][0].pop("capture_schema"),lambda s:s["events"][0].update(captured_at=FUTURE)):
            s=fixture_snapshot();mutate(s)
            new,_=settle_snapshot(s,results={"one":{"status":"final","home_score":1,"away_score":1}})
            self.assertEqual(new["settlement_summary"]["moneyline"]["lost"],0)
            self.assertEqual(new["settlement_summary"]["moneyline"]["excluded"],1)

    def test_reference_is_not_counted(self):
        s=fixture_snapshot();s["events"][0]["recommendations"][1]["state"]="model_reference"
        new,_=settle_snapshot(s,results={"one":{"status":"final","home_score":1,"away_score":1}})
        self.assertNotIn("spread",new["events"][0]["settlement"]["picks"])

class AuditProviders(unittest.TestCase):
    def test_strict_names(self):
        self.assertFalse(_same_team("Inter","Inter Miami"))
        self.assertFalse(_same_team("Manchester","Manchester City"))
        self.assertTrue(_same_team("LA Clippers","Los Angeles Clippers"))
        self.assertTrue(_same_team("Málaga","Malaga"))

    def test_matching_requires_time_and_no_ambiguity(self):
        e=event()
        offer={"id":"q","home_team":e.home,"away_team":e.away,"commence_time":e.kickoff,"bookmakers":[book()]}
        self.assertIsNotNone(market_for_event(e,[offer]))
        wrong=deepcopy(offer);wrong["commence_time"]=(NOW+timedelta(days=3)).isoformat()
        self.assertIsNone(market_for_event(e,[wrong]))
        self.assertIsNone(market_for_event(e,[offer,deepcopy(offer)]))
        del offer["commence_time"]
        self.assertIsNone(market_for_event(e,[offer]))

    def test_invalid_or_stale_market(self):
        b=book();b["last_update"]=(NOW-timedelta(hours=2)).isoformat()
        self.assertIsNone(_market_from_book(b,event()))
        b=book();b["markets"][0]["outcomes"][0]["point"]=float('nan')
        self.assertIsNone(_market_from_book(b,event()).spread_line)
        b=book();b["markets"][1]["outcomes"][1]["point"]=3.5
        self.assertIsNone(_market_from_book(b,event()).total_line)

    def test_market_level_update_overrides_book_time(self):
        b=book();b["markets"][0]["last_update"]=(NOW-timedelta(hours=2)).isoformat()
        m=_market_from_book(b,event())
        self.assertIsNone(m.spread_line)
        self.assertEqual(m.total_line,2.5)

    def test_innings_and_nonfinite_numbers(self):
        self.assertAlmostEqual(_innings("15.2"),15+2/3)
        self.assertIsNone(_number("NaN"))

    def test_nba_season_is_end_year(self):
        e=Event("nba:1","nba",FUTURE,"H","A","NBA",{"home_team_id":1,"away_team_id":2})
        with patch("providers._nba_standings",return_value={}) as get:
            fetch_nba_team_profiles([e],date(2026,10,24))
        self.assertEqual([c.args[0] for c in get.call_args_list],[2027,2026])

    def test_nba_totals_not_guessed_from_magnitude(self):
        payload={"season":{"year":2027},"children":[{"standings":{"entries":[{"team":{"id":1},"stats":[
            {"name":"wins","value":1},{"name":"losses","value":1},{"name":"pointsFor","value":198},{"name":"pointsAgainst","value":200}]}]}}]}
        with patch("providers._get",return_value=payload):
            r=_nba_standings(2027)
        self.assertEqual(r[1]["for"],99)
        self.assertEqual(r[1]["against"],100)

    def test_mlb_taiwan_date_retains_finals(self):
        games=[{"gamePk":n,"gameDate":t,"status":{"abstractGameState":"Final"},"teams":{
            "home":{"team":{"id":1,"name":"Home"}},"away":{"team":{"id":2,"name":"Away"}}}}
            for n,t in [(1,"2026-10-23T16:00:00Z"),(2,"2026-10-24T15:59:59Z"),(3,"2026-10-24T16:00:00Z")]]
        with patch("providers._get",return_value={"dates":[{"games":games}]}) as get:
            rows=fetch_mlb_events(date(2026,10,24))
        self.assertEqual([e.event_id for e in rows],["mlb:1","mlb:2"])
        self.assertEqual(get.call_args.kwargs["params"]["startDate"],"2026-10-23")

    def test_soccer_regulation_score_not_extra_time(self):
        data=[{"fixture":{"id":77,"date":"2026-10-23T20:00:00Z","status":{"short":"AET"}},
               "score":{"fulltime":{"home":1,"away":1}},"goals":{"home":2,"away":1}}]
        with patch("providers._api_football",return_value=data),patch("providers._espn_football_payload",return_value={"events":[]}):
            r=fetch_final_results("football",date(2026,10,24))
        self.assertEqual(r["football:77"]["home_score"],1)

class AuditStorage(unittest.TestCase):
    def test_history_and_frozen_predictions(self):
        with tempfile.TemporaryDirectory() as root:
            store=LocalSnapshotStore(str(Path(root)/"db"))
            e=event();p=score_projection(2,1,"test")
            a=EventAnalysis(e,p,None,probability_recommendations(p,None,sport="football"))
            day=datetime.fromisoformat(FUTURE).astimezone(TAIPEI).date().isoformat()
            snap=DailySnapshot("football",day,NOW.isoformat(),(a,),())
            save_snapshot(store,snap);save_snapshot(store,snap)
            stored=store.get_automatic_snapshot("football",day)
            self.assertEqual(len(stored["events"][0]["prediction_history"]),2)
            late=DailySnapshot("football",day,(NOW+timedelta(days=3)).isoformat(),(EventAnalysis(e,None,None,()),),())
            save_snapshot(store,late)
            self.assertEqual(store.get_automatic_snapshot("football",day)["events"][0],stored["events"][0])

    def test_settle_without_published_keeps_published_at(self):
        with tempfile.TemporaryDirectory() as root:
            store=LocalSnapshotStore(str(Path(root)/"db"));store.save_automatic("nba","2026-10-24",{"events":[]})
            store.publish_automatic("nba","2026-10-24")
            with store._connection() as conn:
                conn.execute("UPDATE snapshots SET published_at='unchanged'")
            store.save_settled_snapshots("nba","2026-10-24",{"events":[]},None)
            with store._connection() as conn:
                self.assertEqual(conn.execute("SELECT published_at FROM snapshots").fetchone()[0],"unchanged")

    def test_credentials_and_safe_diagnostics(self):
        self.assertTrue(verify_password("測試密碼","測試密碼"))
        self.assertFalse(verify_password("",""))
        self.assertNotEqual(credential_version("one","two"),credential_version("new","two"))
        with self.assertLogs("vidali.audit") as logs:
            report=record("snapshot",sport="nba",error=ValueError("private key and request URL"))
        self.assertNotIn("private key",str(logs.output)+str(report))

if __name__=="__main__": unittest.main()
