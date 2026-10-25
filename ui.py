"""Read-only member presentation for saved probability snapshots."""
from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo
from time_utils import parse_time, filter_snapshot_window, session_label, SESSION_POLICY
import unicodedata


TZ_TAIPEI = ZoneInfo("Asia/Taipei")

# Display-only dictionary.  Provider names remain untouched in saved events so
# market matching and model inputs keep using the canonical API names.
TEAM_NAMES = {
    # 英超
    "Arsenal": "阿森納", "Aston Villa": "阿斯頓維拉", "Bournemouth": "伯恩茅斯",
    "Brentford": "賓福特", "Brighton": "布萊頓", "Brighton & Hove Albion": "布萊頓",
    "Burnley": "般尼", "Chelsea": "切爾西", "Crystal Palace": "水晶宮",
    "Everton": "愛華頓", "Fulham": "富咸", "Leeds": "列斯聯", "Leeds United": "列斯聯",
    "Liverpool": "利物浦", "Manchester City": "曼城", "Manchester United": "曼聯",
    "Newcastle": "紐卡素", "Newcastle United": "紐卡素", "Nottingham Forest": "諾定咸森林",
    "Sunderland": "新特蘭", "Tottenham": "熱刺", "Tottenham Hotspur": "熱刺",
    "West Ham": "韋斯咸", "West Ham United": "韋斯咸", "Wolves": "狼隊", "Wolverhampton": "狼隊",
    "Wolverhampton Wanderers": "狼隊", "Coventry": "高雲地利", "Coventry City": "高雲地利",
    # 西甲
    "Alaves": "阿拉維斯", "Deportivo Alavés": "阿拉維斯", "Athletic Club": "畢爾包競技",
    "Athletic Bilbao": "畢爾包競技", "Atlético Madrid": "馬德里競技", "Atletico Madrid": "馬德里競技",
    "Barcelona": "巴塞隆納", "Celta Vigo": "切爾達", "Celta de Vigo": "切爾達",
    "Elche": "艾爾切", "Espanyol": "愛斯賓奴", "Getafe": "赫塔費", "Girona": "赫羅納",
    "Levante": "利雲特", "Mallorca": "馬略卡", "Osasuna": "奧薩蘇納",
    "Rayo Vallecano": "巴列卡諾", "Real Betis": "皇家貝迪斯", "Real Madrid": "皇家馬德里",
    "Real Oviedo": "皇家奧維耶多", "Real Sociedad": "皇家蘇斯達", "Sevilla": "西維爾",
    "Valencia": "華倫西亞", "Villarreal": "維拉利爾",
    # 義甲
    "AC Milan": "AC米蘭", "Milan": "AC米蘭", "Atalanta": "亞特蘭大", "Bologna": "博洛尼亞",
    "Cagliari": "卡利亞里", "Como": "科木", "Cremonese": "克雷莫納", "Fiorentina": "費倫天拿",
    "Genoa": "熱拿亞", "Hellas Verona": "維羅納", "Inter": "國際米蘭", "Inter Milan": "國際米蘭",
    "Juventus": "祖雲達斯", "Lazio": "拉素", "Lecce": "萊切", "Monza": "蒙札", "Napoli": "拿玻里",
    "Parma": "帕爾馬", "Pisa": "比薩", "Roma": "羅馬", "AS Roma": "羅馬", "Sassuolo": "薩斯索羅",
    "Torino": "拖連奴", "Udinese": "烏甸尼斯", "Venezia": "威尼斯",
    # 德甲
    "Augsburg": "奧格斯堡", "Bayern Munich": "拜仁慕尼黑", "Bayer Leverkusen": "利華古遜",
    "Borussia Dortmund": "多蒙特", "Borussia M'gladbach": "慕遜加柏", "Borussia Monchengladbach": "慕遜加柏",
    "Eintracht Frankfurt": "法蘭克福", "FC Cologne": "科隆", "FC Köln": "科隆", "Freiburg": "弗賴堡",
    "Hamburger SV": "漢堡", "Heidenheim": "海登咸", "Hoffenheim": "賀芬咸", "Mainz": "緬恩斯",
    "Mainz 05": "緬恩斯", "RB Leipzig": "RB萊比錫", "St. Pauli": "聖保利", "St Pauli": "聖保利",
    "Stuttgart": "史特加", "Union Berlin": "柏林聯", "Werder Bremen": "雲達不萊梅",
    "Wolfsburg": "沃爾夫斯堡",
    # 法甲
    "Auxerre": "歐塞爾", "Brest": "比斯特", "Le Havre": "勒阿弗爾", "Lens": "朗斯", "Lille": "里爾",
    "Lorient": "羅連安特", "Lyon": "里昂", "Marseille": "馬賽", "Metz": "梅斯", "Monaco": "摩納哥",
    "Nantes": "南特", "Nice": "尼斯", "Paris FC": "巴黎FC", "Paris Saint Germain": "巴黎聖日耳曼",
    "Paris Saint-Germain": "巴黎聖日耳曼", "PSG": "巴黎聖日耳曼", "Rennes": "雷恩",
    "Strasbourg": "史特拉斯堡", "Toulouse": "圖盧茲",
    # 歐冠與歐洲常見隊名
    "Ajax": "阿積士", "Atletico Madrid": "馬德里競技", "Benfica": "賓菲加", "Celtic": "些路迪",
    "Club Brugge": "布魯日", "Crvena Zvezda": "貝爾格萊德紅星", "Dinamo Zagreb": "薩格勒布戴拿模",
    "Fenerbahce": "費倫巴治", "Fenerbahçe": "費倫巴治", "Galatasaray": "加拉塔沙雷",
    "Maccabi Tel Aviv": "特拉維夫馬卡比", "Olympiacos": "奧林比亞高斯", "PSV Eindhoven": "PSV燕豪芬",
    "FC Porto": "波圖", "Porto": "波圖", "Qarabag": "卡拉巴克", "Rangers": "格拉斯哥流浪",
    "Sabah FA": "沙巴FA", "Slavia Praha": "布拉格斯拉維亞", "Slavia Prague": "布拉格斯拉維亞",
    "Sporting CP": "士砵亭", "Sporting Lisbon": "士砵亭", "Shakhtar Donetsk": "薩克達",
    "Sparta Praha": "布拉格斯巴達", "Young Boys": "年青人", "Bodø/Glimt": "博德閃耀",
    # NBA
    "Atlanta Hawks": "亞特蘭大老鷹", "Boston Celtics": "波士頓塞爾提克", "Brooklyn Nets": "布魯克林籃網",
    "Charlotte Hornets": "夏洛特黃蜂", "Chicago Bulls": "芝加哥公牛", "Cleveland Cavaliers": "克里夫蘭騎士",
    "Dallas Mavericks": "達拉斯獨行俠", "Denver Nuggets": "丹佛金塊", "Detroit Pistons": "底特律活塞",
    "Golden State Warriors": "金州勇士", "Houston Rockets": "休士頓火箭", "Indiana Pacers": "印第安納溜馬",
    "LA Clippers": "洛杉磯快艇", "Los Angeles Clippers": "洛杉磯快艇", "Los Angeles Lakers": "洛杉磯湖人",
    "Memphis Grizzlies": "曼菲斯灰熊", "Miami Heat": "邁阿密熱火", "Milwaukee Bucks": "密爾瓦基公鹿",
    "Minnesota Timberwolves": "明尼蘇達灰狼", "New Orleans Pelicans": "紐奧良鵜鶘", "New York Knicks": "紐約尼克",
    "Oklahoma City Thunder": "奧克拉荷馬雷霆", "Orlando Magic": "奧蘭多魔術", "Philadelphia 76ers": "費城七六人",
    "Phoenix Suns": "鳳凰城太陽", "Portland Trail Blazers": "波特蘭拓荒者", "Sacramento Kings": "沙加緬度國王",
    "San Antonio Spurs": "聖安東尼奧馬刺", "Toronto Raptors": "多倫多暴龍", "Utah Jazz": "猶他爵士",
    "Washington Wizards": "華盛頓巫師",
    # MLB
    "Arizona Diamondbacks": "亞利桑那響尾蛇", "Athletics": "運動家", "Boston Red Sox": "波士頓紅襪",
    "Chicago Cubs": "芝加哥小熊", "Chicago White Sox": "芝加哥白襪", "Cincinnati Reds": "辛辛那提紅人",
    "Cleveland Guardians": "克里夫蘭守護者", "Colorado Rockies": "科羅拉多洛磯", "Detroit Tigers": "底特律老虎",
    "Houston Astros": "休士頓太空人", "Kansas City Royals": "堪薩斯市皇家", "Los Angeles Angels": "洛杉磯天使",
    "Los Angeles Dodgers": "洛杉磯道奇", "Miami Marlins": "邁阿密馬林魚", "Milwaukee Brewers": "密爾瓦基釀酒人",
    "Minnesota Twins": "明尼蘇達雙城", "New York Mets": "紐約大都會", "New York Yankees": "紐約洋基",
    "Philadelphia Phillies": "費城費城人", "Pittsburgh Pirates": "匹茲堡海盜", "San Diego Padres": "聖地牙哥教士",
    "San Francisco Giants": "舊金山巨人", "Seattle Mariners": "西雅圖水手", "St. Louis Cardinals": "聖路易紅雀",
    "Tampa Bay Rays": "坦帕灣光芒", "Texas Rangers": "德州遊騎兵", "Toronto Blue Jays": "多倫多藍鳥",
    "Washington Nationals": "華盛頓國民", "Baltimore Orioles": "巴爾的摩金鶯",
    # 歐冠常見名稱補充
    "Slovan Bratislava": "布拉迪斯拉發", "Pafos": "帕福斯", "Kairat Almaty": "阿拉木圖凱拉特",
    "FC Copenhagen": "哥本哈根", "Olympique de Marseille": "馬賽", "Galatasaray SK": "加拉塔沙雷",
    "LA Lakers": "洛杉磯湖人", "Oakland Athletics": "奧克蘭運動家",
    "Atletico de Madrid": "馬德里競技", "Bayern München": "拜仁慕尼黑",
    "Borussia Mönchengladbach": "慕遜加柏", "Paris Saint-Germain FC": "巴黎聖日耳曼",
}


def _display_key(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).casefold()
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


_TEAM_DISPLAY_INDEX = {_display_key(name): label for name, label in TEAM_NAMES.items()}


def display_team(name: str) -> str:
    # Presentation only: never replace canonical provider names or event IDs.
    return TEAM_NAMES.get(name, _TEAM_DISPLAY_INDEX.get(_display_key(name), name))


def _time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(TZ_TAIPEI).strftime("%m/%d %H:%M")
    except (TypeError, ValueError):
        return "時間未提供"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _recommendation(value: dict[str, Any], settlement: dict[str, Any] | None = None) -> str:
    """Render a direct probability card, never a price-based recommendation."""

    state = str(value.get("state", "unavailable"))
    title = str(value.get("selection", "資料不足"))
    probability = value.get("model_probability")
    market_type = value.get("market_type")
    reason = str(value.get("reason", ""))
    metric = "勝率" if market_type == "moneyline" else "過盤機率"
    label = {
        "model": "勝率較高",
        "market_line": "保存的實際盤口線",
        "model_reference": "模型基準線",
        "unavailable": "資料不足",
    }.get(state, "資料不足")
    if probability is None:
        detail = escape(reason or "尚無足夠資料")
    elif state == "model_reference":
        detail = f"{metric} {_pct(probability)}<br>非實盤，不計入績效"
    else:
        detail = f"{metric} {_pct(probability)}"
    outcome = (settlement or {}).get("outcome")
    distribution = value.get("settlement_probabilities") or {}
    if distribution and market_type!="moneyline":
        detail += f"<br>全贏 {_pct(distribution.get('won'))}／半贏 {_pct(distribution.get('half_won'))}<br>走 {_pct(distribution.get('push'))}／半輸 {_pct(distribution.get('half_lost'))}／全輸 {_pct(distribution.get('lost'))}"
    price, ev = value.get("decimal_price"), value.get("expected_return")
    if price is not None:
        detail += f"<br>賠率 {float(price):.3f}"
    if ev is not None:
        detail += f"<br>估計 EV {float(ev):+.1%}（未驗證）"
    decision = value.get("decision","PASS｜舊版模型未驗證")
    detail += f"<br>{escape(str(decision))}"
    outcome_label = {"won":"✓ 全贏", "half_won":"✓ 半贏", "lost":"✕ 全輸", "half_lost":"✕ 半輸", "push":"— 走盤"}.get(outcome, "")
    if outcome and not (settlement or {}).get("evaluation_eligible",False):
        outcome_label += "（舊版／時間不明，不計績效）"
    css_outcome = {"half_won":"won","half_lost":"lost"}.get(outcome,outcome)
    outcome_class = f" settled-{css_outcome}" if css_outcome in {"won","lost","push"} else ""
    return (
        f'<div class="pick {escape(state)}{outcome_class}"><b>{escape(label)}</b><br>{escape(title)}'
        f'<br><small>{detail}</small>{f"<br><b>{outcome_label}</b>" if outcome_label else ""}</div>'
    )


def _style() -> str:
    return """
    <style>
    .snapshot-title {padding:14px 17px; border-radius:12px 12px 0 0; background:linear-gradient(105deg,#071c35,#185989); color:white; font-weight:800; font-size:17px}
    .snapshot-wrap {overflow-x:auto; border:1px solid #d9e2ec; border-radius:0 0 12px 12px; margin-bottom:18px}
    .snapshot {border-collapse:collapse; width:100%; min-width:1100px; font-size:13px}
    .snapshot th {background:#edf4fa; color:#123a5d; padding:10px 8px; border-bottom:1px solid #d5e0ea; white-space:nowrap}
    .snapshot td {vertical-align:top; text-align:center; padding:10px 8px; border-bottom:1px solid #e6edf3; line-height:1.5}
    .snapshot tr:nth-child(even){background:#fbfdff}.snapshot .league-row td{text-align:left;background:#173f64;color:#fff;font-weight:700;padding:8px 10px}
    .pick {border-radius:7px;padding:7px;min-width:138px}.pick small{color:#40566d}.pick.model,.pick.market_line{background:#eaf3ff;color:#174d7d}.pick.model_reference{background:#f4f1ff;color:#59408a}.pick.unavailable{background:#f1f5f9;color:#64748b}.pick.settled-won{background:#dcfce7!important;color:#166534!important;border:1px solid #86efac}.pick.settled-lost{background:#fee2e2!important;color:#991b1b!important;border:1px solid #fca5a5}.pick.settled-push{background:#fef3c7!important;color:#92400e!important;border:1px solid #fcd34d}.settlement-summary{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 14px}.settlement-stat{background:#f1f5f9;border-radius:8px;padding:7px 10px;color:#334155;font-size:13px}
    .warning {color:#795313;font-size:12px;max-width:230px}.member-note{color:#52677d;font-size:13px;margin:6px 0 13px}
    </style>
    """


def render_snapshot(st: Any, snapshot: dict[str, Any], release_label: str) -> None:
    snapshot = filter_snapshot_window(snapshot)
    try:
        st.caption(session_label(date.fromisoformat(str(snapshot.get("date_key", "")))))
    except ValueError:
        st.error("快照缺少有效賽事日期，已停止顯示；請重新建立快照。")
        return
    if snapshot.get("date_policy") != SESSION_POLICY:
        st.info("這份舊快照已依新賽事日範圍篩選，但未補抓隔日賽程。管理員請重新建立此賽事日的自動快照；人工發布版需重新校正發布。")
    if snapshot.get("window_archived_events"):
        st.caption(f"已隱藏 {len(snapshot['window_archived_events'])} 筆範圍外／重複舊紀錄；原始內容保留於封存，不列入本次賽事。")
    if snapshot.get("window_summary_needs_refresh"):
        st.info("舊命中率包含已排除場次，暫不顯示；管理員執行賽後回查即可更新本賽事日統計。")
    sport = snapshot.get("sport", "")
    events = snapshot.get("events", [])
    if not events:
        st.info("這個賽事日範圍內沒有已保存賽事，請管理員重新建立快照；不代表來源沒有比賽。")
        return
    st.warning("目前為未校準模型分析，不是已驗證的下注推薦。機率高不等於值得下注；請勿將模型百分比當成實測準確率。")
    stamp = parse_time(snapshot.get("generated_at"))
    st.caption(f"快照時間（台灣）：{_time(snapshot.get('generated_at',''))}｜版本：{snapshot.get('version','舊版')}｜盤口不是即時畫面")
    if stamp is None or (datetime.now(timezone.utc)-stamp).total_seconds()>1800:
        st.warning("這份快照已超過30分鐘或時間不明，盤口／先發可能已變動；歷史資料僅供回查。")
    if snapshot.get("version","").startswith("restart"):
        st.info("這是稽核修正前的舊版預測。已保留原始內容；請重新執行賽後回查以修正結算，舊版不納入新版績效。")
    if sport == "football":
        title = "歐洲足球｜當日賽事分析"
        headers = ["開賽（台灣）", "對戰", "預估進球", "主預測比分", "賽果機率", "最終比分", "勝率最高", "讓分過盤率", "大小過盤率", "模型資料／風險"]
    elif sport == "nba":
        title = "NBA｜當日賽事分析"
        headers = ["開賽（台灣）", "對戰", "模型預估得分", "賽果機率", "最終比分", "勝率最高", "讓分過盤率", "大小過盤率", "模型資料／風險"]
    else:
        title = "MLB｜當日賽事分析"
        headers = ["開賽（台灣）", "對戰／先發", "模型預估得分", "賽果機率", "最終比分", "勝率最高", "讓分過盤率", "大小過盤率", "模型資料／風險"]
    html = [_style(), f'<div class="snapshot-title">{escape(title)}｜{escape(release_label)}</div>', '<div class="snapshot-wrap"><table class="snapshot"><thead><tr>']
    html.extend(f"<th>{escape(header)}</th>" for header in headers)
    html.append("</tr></thead><tbody>")
    summary = snapshot.get("settlement_summary") or {} if snapshot.get("settlement_version")=="audit-v3.0" else {}
    if summary:
        stats = []
        for kind, label in (("moneyline", "獨贏"), ("spread", "讓分"), ("total", "大小")):
            row = summary.get(kind, {})
            won,lost,push,half_won,half_lost = (int(row.get(k,0)) for k in ("won","lost","push","half_won","half_lost"))
            decided = won+lost+half_won+half_lost
            rate = f"{(won+half_won)/decided:.1%}" if decided else "—"
            text = f"{label}（模型觀察，非實盤）：全贏{won}／半贏{half_won}／走{push}／半輸{half_lost}／全輸{lost}｜正收益比例 {rate}｜樣本 {decided}"
            if row.get("priced_count",0):
                text += f"｜模擬 ROI {float(row.get('net_units',0))/row['priced_count']:+.1%}（有賠率{row['priced_count']}筆）"
            if row.get("brier_count",0):
                text += f"｜Brier {row['brier_sum']/row['brier_count']:.3f}"
            text += f"｜排除{row.get('excluded',0)}筆"
            stats.append(f'<div class="settlement-stat">{escape(text)}</div>')
        html.insert(1, '<div class="settlement-summary">' + "".join(stats) + '</div>')
    current_league = None
    for item in events:
        event = item.get("event", {})
        projection = item.get("projection")
        recs = {entry.get("market_type"): entry for entry in item.get("recommendations", [])}
        settled = item.get("settlement") or {}
        pick_results = settled.get("picks") or {}
        final_score = (f"主 {settled.get('home_score')}<br>客 {settled.get('away_score')}"
                       if settled.get("status") == "final" else "尚未結束")
        if sport == "football" and event.get("league") != current_league:
            current_league = event.get("league")
            html.append(f'<tr class="league-row"><td colspan="10">{escape(str(current_league))}</td></tr>')
        matchup = f"{escape(display_team(str(event.get('away', '客隊'))))}（客）<br>vs<br>{escape(display_team(str(event.get('home', '主隊'))))}（主）"
        warnings = "<br>".join(escape(str(entry)) for entry in item.get("warnings", [])) or "—"
        market = item.get("market") or {}
        if market:
            warnings += f"<br>盤源：{escape(str(market.get('bookmaker','未提供')))}<br>盤口時間：{_time(market.get('updated_at',''))}"
        if projection:
            warnings += "<br>" + "<br>".join(escape(str(n)) for n in projection.get("notes",[]))
        history = item.get("prediction_history",[])
        if len(history)>1:
            first_market = history[0].get("market") or {}
            warnings += f"<br>首次保存讓分／大小：{escape(str(first_market.get('spread_line','—')))}／{escape(str(first_market.get('total_line','—')))}<br>保留修訂：{len(history)} 筆"
        if projection:
            expected = f"主 {float(projection['home_expected']):.2f}<br>客 {float(projection['away_expected']):.2f}"
            if sport == "football":
                outcome = f"主 {_pct(projection.get('home_win'))}<br>和 {_pct(projection.get('draw'))}<br>客 {_pct(projection.get('away_win'))}"
                score = f"{escape(str(projection.get('score','—')))}<br><small>命中機率 {_pct(projection.get('score_probability'))}</small>"
                cells = [_time(event.get("kickoff", "")), matchup, expected, score, outcome,
                         final_score, _recommendation(recs.get("moneyline", {}), pick_results.get("moneyline")), _recommendation(recs.get("spread", {}), pick_results.get("spread")),
                         _recommendation(recs.get("total", {}), pick_results.get("total")), f'<span class="warning">{warnings}</span>']
            else:
                outcome = f"主勝 {_pct(projection.get('home_win'))}<br>客勝 {_pct(projection.get('away_win'))}"
                if sport == "mlb":
                    pitchers = event.get("metadata", {})
                    matchup += f"<br><small>客投：{escape(str(pitchers.get('away_pitcher','尚未確認')))}<br>主投：{escape(str(pitchers.get('home_pitcher','尚未確認')))}</small>"
                cells = [_time(event.get("kickoff", "")), matchup, expected, outcome,
                         final_score, _recommendation(recs.get("moneyline", {}), pick_results.get("moneyline")), _recommendation(recs.get("spread", {}), pick_results.get("spread")),
                         _recommendation(recs.get("total", {}), pick_results.get("total")), f'<span class="warning">{warnings}</span>']
        else:
            unavailable = _recommendation(recs.get("moneyline", {}))
            if sport == "football":
                cells = [_time(event.get("kickoff", "")), matchup, "—", "—", "—", final_score, unavailable,
                         _recommendation(recs.get("spread", {}), pick_results.get("spread")), _recommendation(recs.get("total", {}), pick_results.get("total")),
                         f'<span class="warning">{warnings}</span>']
            else:
                cells = [_time(event.get("kickoff", "")), matchup, "—", "—", final_score, unavailable,
                         _recommendation(recs.get("spread", {}), pick_results.get("spread")), _recommendation(recs.get("total", {}), pick_results.get("total")),
                         f'<span class="warning">{warnings}</span>']
        html.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    html.append("</tbody></table></div>")
    st.caption("機率使用可取得的球隊統計；球員／傷停／輪換未完整建模。足球結算為90分鐘含補時，不含延長及PK。半贏與半輸分開列；模型參考盤及無賽前時間證據者不計績效。")
    st.markdown("".join(html), unsafe_allow_html=True)
