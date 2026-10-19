"""Read-only member presentation for saved probability snapshots."""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo


TZ_TAIPEI = ZoneInfo("Asia/Taipei")

TEAM_NAMES = {
    "Manchester City": "曼城", "Manchester United": "曼聯", "Liverpool": "利物浦", "Arsenal": "阿森納",
    "Chelsea": "切爾西", "Tottenham": "熱刺", "Newcastle": "紐卡索", "Aston Villa": "阿斯頓維拉",
    "Real Madrid": "皇家馬德里", "Barcelona": "巴塞隆納", "Atlético Madrid": "馬德里競技",
    "Athletic Club": "畢爾包競技", "Sevilla": "塞維利亞", "Valencia": "瓦倫西亞",
    "Bayern Munich": "拜仁慕尼黑", "Borussia Dortmund": "多特蒙德", "Bayer Leverkusen": "勒沃庫森",
    "Juventus": "尤文圖斯", "Inter": "國際米蘭", "AC Milan": "AC米蘭", "Napoli": "拿坡里",
    "Paris Saint Germain": "巴黎聖日耳曼", "Paris Saint-Germain": "巴黎聖日耳曼", "Marseille": "馬賽",
}


def display_team(name: str) -> str:
    return TEAM_NAMES.get(name, name)


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


def _recommendation(value: dict[str, Any]) -> str:
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
        detail = reason or "尚無足夠資料"
    elif state == "model_reference":
        detail = f"{metric} {_pct(probability)}<br>非實際可下注盤口"
    else:
        detail = f"{metric} {_pct(probability)}"
    return (
        f'<div class="pick {escape(state)}"><b>{escape(label)}</b><br>{escape(title)}'
        f'<br><small>{detail}</small></div>'
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
    .pick {border-radius:7px;padding:7px;min-width:138px}.pick small{color:#40566d}.pick.model,.pick.market_line{background:#eaf3ff;color:#174d7d}.pick.model_reference{background:#f4f1ff;color:#59408a}.pick.unavailable{background:#f1f5f9;color:#64748b}
    .warning {color:#795313;font-size:12px;max-width:230px}.member-note{color:#52677d;font-size:13px;margin:6px 0 13px}
    </style>
    """


def render_snapshot(st: Any, snapshot: dict[str, Any], release_label: str) -> None:
    sport = snapshot.get("sport", "")
    events = snapshot.get("events", [])
    if not events:
        st.info("這個日期沒有已保存賽事。")
        return
    if sport == "football":
        title = "歐洲足球｜當日賽事分析"
        headers = ["開賽（台灣）", "對戰", "預估進球", "主預測比分", "賽果機率", "勝率最高", "讓分過盤率", "大小過盤率", "模型資料／風險"]
    else:
        title = "MLB｜當日賽事分析"
        headers = ["開賽（台灣）", "對戰／先發", "模型預估得分", "賽果機率", "勝率最高", "讓分過盤率", "大小過盤率", "模型資料／風險"]
    html = [_style(), f'<div class="snapshot-title">{escape(title)}｜{escape(release_label)}</div>', '<div class="snapshot-wrap"><table class="snapshot"><thead><tr>']
    html.extend(f"<th>{escape(header)}</th>" for header in headers)
    html.append("</tr></thead><tbody>")
    current_league = None
    for item in events:
        event = item.get("event", {})
        projection = item.get("projection")
        recs = {entry.get("market_type"): entry for entry in item.get("recommendations", [])}
        if sport == "football" and event.get("league") != current_league:
            current_league = event.get("league")
            html.append(f'<tr class="league-row"><td colspan="9">{escape(str(current_league))}</td></tr>')
        matchup = f"{display_team(str(event.get('away', '客隊')))}（客）<br>vs<br>{display_team(str(event.get('home', '主隊')))}（主）"
        warnings = "<br>".join(escape(str(entry)) for entry in item.get("warnings", [])) or "—"
        if projection:
            expected = f"主 {float(projection['home_expected']):.2f}<br>客 {float(projection['away_expected']):.2f}"
            if sport == "football":
                outcome = f"主 {_pct(projection.get('home_win'))}<br>和 {_pct(projection.get('draw'))}<br>客 {_pct(projection.get('away_win'))}"
                score = f"{escape(str(projection.get('score','—')))}<br><small>命中機率 {_pct(projection.get('score_probability'))}</small>"
                cells = [_time(event.get("kickoff", "")), matchup, expected, score, outcome,
                         _recommendation(recs.get("moneyline", {})), _recommendation(recs.get("spread", {})),
                         _recommendation(recs.get("total", {})), f'<span class="warning">{warnings}</span>']
            else:
                outcome = f"主勝 {_pct(projection.get('home_win'))}<br>客勝 {_pct(projection.get('away_win'))}"
                pitchers = event.get("metadata", {})
                matchup += f"<br><small>客投：{escape(str(pitchers.get('away_pitcher','尚未確認')))}<br>主投：{escape(str(pitchers.get('home_pitcher','尚未確認')))}</small>"
                cells = [_time(event.get("kickoff", "")), matchup, expected, outcome,
                         _recommendation(recs.get("moneyline", {})), _recommendation(recs.get("spread", {})),
                         _recommendation(recs.get("total", {})), f'<span class="warning">{warnings}</span>']
        else:
            unavailable = _recommendation(recs.get("moneyline", {}))
            if sport == "football":
                cells = [_time(event.get("kickoff", "")), matchup, "—", "—", "—", unavailable,
                         _recommendation(recs.get("spread", {})), _recommendation(recs.get("total", {})),
                         f'<span class="warning">{warnings}</span>']
            else:
                cells = [_time(event.get("kickoff", "")), matchup, "—", "—", unavailable,
                         _recommendation(recs.get("spread", {})), _recommendation(recs.get("total", {})),
                         f'<span class="warning">{warnings}</span>']
        html.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    html.append("</tbody></table></div>")
    st.caption("所有百分比都由保存的球隊、球員／先發與傷停資料計算。讓分與大小優先採保存的實際線；「模型基準線」不是可下注盤。機率不保證結果。")
    st.markdown("".join(html), unsafe_allow_html=True)
