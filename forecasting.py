"""Experimental, uncalibrated model signals and price-aware estimated EV.

No bookmaker-equivalence claim. Actionable recommendations remain disabled
until an independently reviewed out-of-time calibration is implemented.
"""
from __future__ import annotations
import math
from models import Market, Projection, Recommendation
from settlement_math import OUTCOMES, components, settle, profit

MAX_GOALS = 8

def poisson(value, rate):
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("無效得分均值")
    if value < 0:
        return 0.0
    if rate == 0:
        return float(value == 0)
    return math.exp(-rate + value * math.log(rate) - math.lgamma(value + 1))

def score_matrix(home_rate, away_rate, maximum=MAX_GOALS):
    for rate in (home_rate, away_rate):
        if not math.isfinite(rate) or not 0 <= rate <= 50:
            raise ValueError("得分均值超出模型支援範圍")
    maximum = max(maximum, int(max(home_rate, away_rate) + 12 * math.sqrt(max(home_rate, away_rate) + 1) + 12))
    hp = [poisson(n, home_rate) for n in range(maximum + 1)]
    ap = [poisson(n, away_rate) for n in range(maximum + 1)]
    raw = {(h, a): hp[h] * ap[a] for h in range(maximum + 1) for a in range(maximum + 1)}
    total = sum(raw.values())
    return {key: value / total for key, value in raw.items()}

def outcome_probabilities(matrix):
    return tuple(sum(p for (h, a), p in matrix.items() if check(h, a))
                 for check in (lambda h,a:h>a, lambda h,a:h==a, lambda h,a:h<a))

def _main_score(matrix, home_win=None, away_win=None):
    return max(matrix.items(), key=lambda item: item[1])

def score_projection(home_rate, away_rate, quality):
    home_rate, away_rate = round(home_rate, 2), round(away_rate, 2)
    matrix = score_matrix(home_rate, away_rate)
    home, draw, away = outcome_probabilities(matrix)
    score, probability = _main_score(matrix)
    return Projection(home_rate, away_rate, home, draw, away, f"{score[0]}:{score[1]}", probability, quality,
                      ("獨立卜瓦松模型；未完成樣本外校準", "足球按正規 90 分鐘含補時，不含延長／PK"))

def baseball_matrix(home_rate, away_rate):
    # Explicit approximate extra-inning assumption, consistent across markets.
    raw = score_matrix(home_rate, away_rate, maximum=12)
    result = {}
    for (h, a), p in raw.items():
        outcomes = [((h+1,a),p/2), ((h,a+1),p/2)] if h == a else [((h,a),p)]
        for score, weight in outcomes:
            result[score] = result.get(score, 0) + weight
    return result

def baseball_projection(home_rate, away_rate, quality):
    home_rate, away_rate = round(home_rate, 2), round(away_rate, 2)
    matrix = baseball_matrix(home_rate, away_rate)
    home, _, away = outcome_probabilities(matrix)
    score, probability = _main_score(matrix)
    return Projection(home_rate, away_rate, home, None, away, f"{score[0]}:{score[1]}", probability, quality,
                      ("未完成樣本外校準", "延長賽採對半一分決勝近似，非完整延長賽模型"))

def _normal_cdf(value):
    return .5 * (1 + math.erf(value / math.sqrt(2)))

def normal_integer_distribution(mean, sd, *, no_zero=False):
    if not math.isfinite(mean) or not math.isfinite(sd) or sd <= 0:
        raise ValueError("無效常態分布參數")
    low, high = math.floor(mean-10*sd), math.ceil(mean+10*sd)
    values = {n: _normal_cdf((n+.5-mean)/sd)-_normal_cdf((n-.5-mean)/sd) for n in range(low,high+1)}
    if no_zero:
        tie = values.pop(0, 0)
        values[1] = values.get(1,0) + tie/2
        values[-1] = values.get(-1,0) + tie/2
    mass = sum(values.values())
    return {k:v/mass for k,v in values.items()}

def basketball_projection(home_points, away_points, quality):
    home_points, away_points = round(home_points,1), round(away_points,1)
    diff = normal_integer_distribution(home_points-away_points,14.5,no_zero=True)
    home = sum(p for n,p in diff.items() if n>0)
    return Projection(home_points,away_points,home,None,1-home,f"{round(home_points)}:{round(away_points)}",0,quality,
                      ("分差 SD=14.5／總分 SD=18.5 為未校準假設", "未建模現役名單、傷停、休息天數與輪換"))

def _asian_components(line):
    return components(line)

def asian_distribution(matrix, *, side, line, market_type):
    result = dict.fromkeys(OUTCOMES, 0.0)
    for (h,a), weight in matrix.items():
        if market_type == "spread":
            outcome = settle(h-a if side=="home" else a-h, line)
        else:
            outcome = settle(h+a, -line) if side=="over" else settle(-h-a, line)
        result[outcome] += weight
    return result

def asian_cover_probability(matrix, *, side, line, market_type):
    d = asian_distribution(matrix,side=side,line=line,market_type=market_type)
    return d["won"]+d["half_won"]

def _pick(kind, state, side, line, dist, market, reason):
    labels = {"home":"主隊", "away":"客隊", "draw":"和局", "over":"大分", "under":"小分"}
    selection = labels[side]+("勝" if kind=="moneyline" and side!="draw" else "")
    if kind != "moneyline":
        shown = -line if kind=="spread" and side=="away" else line
        selection += f" {shown:+g}" if kind=="spread" else f" {shown:g}"
    price = (market.prices.get(f"{kind}:{side}") if market and state != "model_reference" else None)
    ev = sum(dist[k]*profit(k,price) for k in OUTCOMES) if price is not None else None
    return Recommendation(kind,state,selection,dist["won"]+dist["half_won"],line,reason,
                          side,price,ev,dist,False,
                          "PASS｜無實際盤口" if state=="model_reference" else
                          "PASS｜缺少賠率，無法評估價值" if price is None else "PASS｜模型尚未完成樣本外校準")

def probability_recommendations(projection, market, *, sport):
    winner = "home" if projection.home_win >= projection.away_win else "away"
    win = projection.home_win if winner=="home" else projection.away_win
    dist = dict.fromkeys(OUTCOMES,0.0)
    dist.update(won=win,lost=1-win)
    picks = [_pick("moneyline","model",winner,None,dist,market,"模型估計，不代表實測命中率；足球和局另列")]
    matrix = None if sport=="nba" else (baseball_matrix(projection.home_expected,projection.away_expected) if sport=="mlb" else score_matrix(projection.home_expected,projection.away_expected))
    for kind in ("spread","total"):
        actual = getattr(market,f"{kind}_line",None)
        reference = (-(projection.home_expected-projection.away_expected) if kind=="spread" else projection.home_expected+projection.away_expected)
        line = actual if actual is not None else round(reference*2)/2
        state = "market_line" if actual is not None else "model_reference"
        reason = "保存的實際盤口線" if actual is not None else "模型參考線，非可下注盤，不計入實盤績效"
        candidates=[]
        for side in (("home","away") if kind=="spread" else ("over","under")):
            side_line = -line if kind=="spread" and side=="away" else line
            if sport=="nba":
                values = normal_integer_distribution(projection.home_expected-projection.away_expected,14.5,no_zero=True) if kind=="spread" else normal_integer_distribution(projection.home_expected+projection.away_expected,18.5)
                d = dict.fromkeys(OUTCOMES,0.0)
                for n,p in values.items():
                    outcome = settle(n if side=="home" else -n,side_line) if kind=="spread" else (settle(n,-line) if side=="over" else settle(-n,line))
                    d[outcome] += p
            else:
                d = asian_distribution(matrix,side=side,line=side_line,market_type=kind)
            candidates.append(_pick(kind,state,side,line,d,market,reason))
        price_complete = all(c.expected_return is not None for c in candidates)
        picks.append(max(candidates,key=lambda c:c.expected_return if price_complete else c.model_probability))
    return tuple(picks)

def basketball_probability_recommendations(projection, market):
    return probability_recommendations(projection,market,sport="nba")
