"""維大力體育APP Restart.

Admin actions build and save snapshots.  Member pages only read those saved
snapshots and never call a provider or run a model.
"""
from __future__ import annotations

import hmac
import math
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from providers import ProviderError, fetch_final_results
from service import apply_manual_calibration, build_football_snapshot, build_mlb_snapshot, build_nba_snapshot, save_snapshot, settle_snapshot
from storage import StorageError, create_store
from ui import render_snapshot
from auth import verify_password, credential_version
from diagnostics import record, snapshot_health, VERSION


APP_NAME = "維大力體育"
TZ_TAIPEI = ZoneInfo("Asia/Taipei")
SECRET_NAMES = (
    "APP_ADMIN_PASSWORD", "APP_MEMBER_PASSWORD", "SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY",
    "API_FOOTBALL_KEY", "API_FOOTBALL_INCLUDE_INJURIES", "THE_ODDS_API_KEY", "ODDS_API_KEY", "ODDS_REGIONS", "REQUIRE_SUPABASE",
)


def _load_secrets() -> None:
    """Make Streamlit Secrets available to provider/storage modules.

    Values are never printed or sent to the browser by this application.
    """

    for name in SECRET_NAMES:
        try:
            value = st.secrets.get(name)
        except FileNotFoundError:
            value = None
        if value not in (None, ""):
            os.environ[name] = str(value)


def _brand() -> None:
    logo, heading = st.columns((1, 8), vertical_alignment="center")
    with logo:
        image = Path(__file__).with_name("logo.png")
        if image.is_file():
            st.image(str(image), width=68)
    with heading:
        st.title(APP_NAME)
        st.caption("保存後才展示｜勝率與過盤率｜會員唯讀快照")
    st.divider()


def _login() -> str:
    admin_password = os.environ.get("APP_ADMIN_PASSWORD", "")
    member_password = os.environ.get("APP_MEMBER_PASSWORD", "")
    if not admin_password or not member_password:
        _brand()
        st.error("APP 尚未完成登入設定；請管理員在 Streamlit Secrets 設定 APP_ADMIN_PASSWORD 與 APP_MEMBER_PASSWORD。")
        st.stop()
    version = credential_version(admin_password,member_password)
    if st.session_state.get("credential_version") != version:
        st.session_state.pop("role",None)
    if "role" in st.session_state:
        return st.session_state["role"]
    _brand()
    login_type = st.radio("登入身分", ["會員", "管理員"], horizontal=True)
    password = st.text_input("登入密碼", type="password")
    if st.button("登入", type="primary"):
        if time.time() < st.session_state.get("login_retry_after",0):
            st.error("嘗試次數過多，請稍候一分鐘再試。")
            st.stop()
        expected = admin_password if login_type == "管理員" else member_password
        if verify_password(password, expected):
            st.session_state["role"] = "admin" if login_type == "管理員" else "member"
            st.session_state["credential_version"] = version
            st.session_state["login_failures"] = 0
            st.rerun()
        failures = st.session_state.get("login_failures",0)+1
        st.session_state["login_failures"] = failures
        if failures>=5:
            st.session_state["login_retry_after"] = time.time()+60
        st.error("密碼不正確")
    st.stop()


def _taiwan_day():
    return datetime.now(TZ_TAIPEI).date()


def _parse_manual_line(raw: str, label: str) -> float:
    """Accept normal and full-width plus/minus signs, but never a blank line."""

    cleaned = raw.strip().replace("＋", "+").replace("－", "-").replace("−", "-")
    if not cleaned:
        raise ValueError(f"請填入{label}")
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{label}必須是數字，例如 -0.5、+0.5 或 2.5") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label}必須是有限數字")
    return value


def _manual_defaults(snapshot: dict | None) -> dict[str, tuple[float, float]]:
    """Reuse the active manual lines when an admin is revising a release."""

    if not snapshot or snapshot.get("release_type") != "manual_calibrated":
        return {}
    values: dict[str, tuple[float, float]] = {}
    for item in snapshot.get("events", []):
        event, market = item.get("event", {}), item.get("market", {})
        event_id = event.get("event_id")
        if not event_id or not isinstance(market, dict):
            continue
        try:
            values[str(event_id)] = (float(market["spread_line"]), float(market["total_line"]))
        except (KeyError, TypeError, ValueError):
            continue
    return values


def _manual_calibration_panel(st, store, automatic: dict, sport: str, day: str, sport_label: str) -> None:
    """Admin-only form for a complete manual handicap / total recalculation."""

    active_snapshot, active_label = store.get_member_snapshot(sport, day)
    defaults = _manual_defaults(active_snapshot)
    st.divider()
    st.subheader("人工校正盤口並發布")
    st.caption(f"會員目前讀取：{active_label}")
    st.info(
        "每場都填「主隊讓分」與「大小總分」後，系統才會以這些實際線重新計算讓分／大小過盤率，"
        "並保存為人工校正正式推薦。主隊讓分填負數，例如 -0.5；主隊受讓填正數，例如 +0.5。"
    )
    st.caption("獨贏勝率由已保存的球隊／球員模型決定，不需要也不使用賠率或 EV。")

    raw_lines: dict[str, tuple[str, str]] = {}
    missing_model: list[str] = []
    with st.form(f"manual_calibration_{sport}_{day}", clear_on_submit=False):
        for index, item in enumerate(automatic.get("events", []), start=1):
            event = item.get("event", {})
            event_id = str(event.get("event_id", ""))
            home, away = str(event.get("home", "主隊")), str(event.get("away", "客隊"))
            st.markdown(f"**{index}. {away}（客） vs {home}（主）**")
            if not event_id or not item.get("projection"):
                missing_model.append(f"{away} vs {home}")
                st.error("這場自動快照缺少球隊模型資料；不能只靠人工盤口產生機率。請先重新建立完整自動快照。")
                continue
            market = item.get("market") or {}
            automatic_spread, automatic_total = market.get("spread_line"), market.get("total_line")
            if automatic_spread is not None or automatic_total is not None:
                spread_text = "—" if automatic_spread is None else f"{float(automatic_spread):+g}"
                total_text = "—" if automatic_total is None else f"{float(automatic_total):g}"
                st.caption(f"自動保存線（僅供比對）：主隊讓分 {spread_text}｜大小 {total_text}")
            previous = defaults.get(event_id)
            left, right = st.columns(2)
            with left:
                spread_raw = st.text_input(
                    f"{home} 主隊讓分",
                    value="" if previous is None else f"{previous[0]:+g}",
                    placeholder="例如 -0.5 或 +0.5",
                    key=f"manual_spread_{sport}_{day}_{event_id}",
                )
            with right:
                total_raw = st.text_input(
                    "大小總分",
                    value="" if previous is None else f"{previous[1]:g}",
                    placeholder="例如 2.5",
                    key=f"manual_total_{sport}_{day}_{event_id}",
                )
            raw_lines[event_id] = (spread_raw, total_raw)
        submitted = st.form_submit_button("儲存人工校正並發布會員版", type="primary")

    if not submitted:
        return
    if missing_model:
        st.error("以下賽事沒有可用模型資料，請先重新建立自動快照：" + "、".join(missing_model))
        return
    try:
        parsed_lines = {
            event_id: (
                _parse_manual_line(raw[0], "主隊讓分"),
                _parse_manual_line(raw[1], "大小總分"),
            )
            for event_id, raw in raw_lines.items()
        }
        manual_snapshot = apply_manual_calibration(automatic, parsed_lines)
        store.save_manual_published(sport, day, manual_snapshot)
    except ValueError as exc:
        st.error(str(exc))
        return
    except (ProviderError, StorageError) as exc:
        st.error(str(exc))
        return
    st.success("人工校正盤口已重算並發布。會員現在會優先看到「人工校正正式推薦」。")
    st.rerun()


def _member_page(store) -> None:
    _brand()
    sport_label = st.radio("賽事類別", ["MLB", "NBA", "歐洲足球"], horizontal=True)
    sport = {"MLB": "mlb", "NBA": "nba", "歐洲足球": "football"}[sport_label]
    selected = st.date_input("賽事日期（台灣）", _taiwan_day())
    snapshot, label = store.get_member_snapshot(sport, selected.isoformat())
    if not snapshot:
        st.info("這個日期尚未保存賽前分析。管理員建立自動快照後，會員即可直接查看。")
        return
    st.caption(f"目前顯示：{label}｜會員頁面只讀取已保存內容，不抓取即時盤口、不重算模型。")
    render_snapshot(st, snapshot, label)


def _admin_page(store, storage_label: str) -> None:
    _brand()
    st.caption(f"儲存狀態：{storage_label}")
    sport_label = st.radio("管理項目", ["MLB", "NBA", "歐洲足球"], horizontal=True, key="admin_sport")
    sport = {"MLB": "mlb", "NBA": "nba", "歐洲足球": "football"}[sport_label]
    selected = st.date_input("建立哪一天的賽前快照", _taiwan_day(), key="admin_day")
    day = selected.isoformat()
    st.info("後台抓取賽程、球隊資料與盤口／賠率。已開賽的預測鎖定；缺線不當實盤。模型目前未完成樣本外校準，顯示估計機率與 PASS，不宣稱已驗證優勢。")
    if st.button(f"建立並保存 {sport_label} 自動快照", type="primary"):
        try:
            with st.spinner("正在建立快照…"):
                builders = {"mlb": build_mlb_snapshot, "nba": build_nba_snapshot, "football": build_football_snapshot}
                snapshot = builders[sport](selected)
                save_snapshot(store, snapshot)
                st.session_state["last_diagnostic"] = record("snapshot",sport=sport,event_count=len(snapshot.events))
            st.success(f"已保存 {len(snapshot.events)} 場 {sport_label} 賽事。自動快照現在已可供會員查詢。")
        except ProviderError as exc:
            st.session_state["last_diagnostic"] = record("snapshot",sport=sport,error=exc)
            st.error(str(exc))
        except StorageError as exc:
            st.session_state["last_diagnostic"] = record("storage",sport=sport,error=exc)
            st.error(str(exc))
        except Exception as exc:
            st.session_state["last_diagnostic"] = record("snapshot",sport=sport,error=exc)
            st.error("本次建立未完成；既有快照沒有被覆蓋。請稍後再試或檢查資料來源設定。")
    automatic = store.get_automatic_snapshot(sport, day)
    if automatic:
        with st.expander("資料健康檢查與追蹤"):
            for label,value in snapshot_health(automatic).items():
                st.write(f"{label}：{value}")
            st.caption("會員仍讀保存的快照；不是背景即時更新。新增版本保留首筆及最近29次紀錄，已開賽預測不覆蓋。")
        st.divider()
        st.subheader("已保存快照預覽")
        render_snapshot(st, automatic, "自動快照（會員可查詢）")
        st.caption("不做人工校正時，自動快照已可直接提供會員查詢；之後建立新的自動快照也不會覆蓋已發布的人工校正版。")
        _manual_calibration_panel(st, store, automatic, sport, day, sport_label)
        st.divider()
        st.subheader("賽後回查與命中率")
        st.caption("僅更新已保存快照的賽果與結算，不會重算或改寫原本的賽前預測。延期／未完成賽事不納入命中率。")
        if st.button(f"回查 {sport_label} 最終賽果", key=f"settle_{sport}_{day}"):
            try:
                with st.spinner("正在核對最終賽果…"):
                    results = fetch_final_results(sport, selected)
                    settled_auto, auto_count = settle_snapshot(automatic, results=results)
                    member_snapshot, member_label = store.get_member_snapshot(sport, day)
                    published = None
                    published_count = 0
                    if member_snapshot and member_label in {"人工校正正式推薦", "管理員確認的自動推薦"}:
                        published, published_count = settle_snapshot(member_snapshot, results=results)
                    if not auto_count and not published_count:
                        st.info("目前沒有已結束且可核對的賽事；快照沒有變更。")
                    else:
                        store.save_settled_snapshots(sport, day, settled_auto, published)
                        st.session_state["last_diagnostic"] = record("settlement",sport=sport,event_count=max(auto_count,published_count))
                        st.success(f"已回查 {max(auto_count, published_count)} 場已結束賽事；會員端現在可查看綠／紅結算與命中率。")
                        st.rerun()
            except (ProviderError, StorageError) as exc:
                st.session_state["last_diagnostic"] = record("settlement",sport=sport,error=exc)
                st.error(str(exc))
            except Exception as exc:
                st.session_state["last_diagnostic"] = record("settlement",sport=sport,error=exc)
                st.error("本次回查未完成；既有快照沒有被覆蓋。請稍後再試。")
    with st.expander("資料來源與安全規則"):
        st.write("MLB：MLB Stats API（球隊打擊／投球、預定先發）＋ The Odds API（讓分／大小線）。")
        st.write("NBA：ESPN（賽程、球隊得失分與賽季勝率）＋ The Odds API（讓分／大小線）。")
        st.write("足球：API-Football（主客場攻守、近期狀態、傷停）＋ ESPN 公開資料備援（賽程、賽果、積分／進失球）＋ The Odds API（讓分／大小線）。")
        st.write("機率為未校準模型估計。價格僅用於計算估計期望報酬，不是調高機率的工具；機率高不等於值得下注。")
    diagnostic = st.session_state.get("last_diagnostic")
    if diagnostic:
        st.caption(f"追蹤碼：{diagnostic['id']}｜{diagnostic['status']}｜{VERSION}")
        safe_text = "\n".join(f"{k}: {v}" for k,v in diagnostic.items())
        st.download_button("下載安全診斷摘要",safe_text,file_name="vidali-diagnostic.txt",mime="text/plain")
    st.link_button("錯誤回報／修正進度", "https://github.com/lyuwei0623-dali/sportV1/issues")


def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="⚾", layout="wide")
    _load_secrets()
    role = _login()
    try:
        store, storage_label = create_store()
        if role == "admin":
            page = st.radio("功能", ["管理快照", "會員畫面預覽"], horizontal=True)
            if page == "管理快照":
                _admin_page(store, storage_label)
            else:
                _member_page(store)
        else:
            _member_page(store)
    except StorageError as exc:
        record("storage",error=exc)
        st.error(str(exc))
        st.caption("為保護已保存內容，APP 不會自動改用可能在重新部署後遺失的暫存資料庫。")
    except Exception as exc:
        report = record("render",error=exc)
        st.error(f"頁面暫時無法完成，已保留既有資料。追蹤碼：{report['id']}")
    if st.session_state.get("role") and st.button("登出"):
        st.session_state.clear()
        st.rerun()


if __name__ == "__main__":
    main()
