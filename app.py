"""維大力體育APP Restart.

Admin actions build and save snapshots.  Member pages only read those saved
snapshots and never call a provider or run a model.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from providers import ProviderError
from service import build_football_snapshot, build_mlb_snapshot, save_snapshot
from storage import StorageError, create_store
from ui import render_snapshot


APP_NAME = "維大力體育APP"
TZ_TAIPEI = ZoneInfo("Asia/Taipei")
SECRET_NAMES = (
    "APP_ADMIN_PASSWORD", "APP_MEMBER_PASSWORD", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
    "API_FOOTBALL_KEY", "API_FOOTBALL_INCLUDE_INJURIES", "THE_ODDS_API_KEY", "ODDS_REGIONS", "REQUIRE_SUPABASE",
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
    _brand()
    admin_password = os.environ.get("APP_ADMIN_PASSWORD", "")
    member_password = os.environ.get("APP_MEMBER_PASSWORD", "")
    if not admin_password or not member_password:
        st.error("APP 尚未完成登入設定；請管理員在 Streamlit Secrets 設定 APP_ADMIN_PASSWORD 與 APP_MEMBER_PASSWORD。")
        st.stop()
    if "role" in st.session_state:
        return st.session_state["role"]
    login_type = st.radio("登入身分", ["會員", "管理員"], horizontal=True)
    password = st.text_input("登入密碼", type="password")
    if st.button("登入", type="primary"):
        expected = admin_password if login_type == "管理員" else member_password
        if hmac.compare_digest(password, expected):
            st.session_state["role"] = "admin" if login_type == "管理員" else "member"
            st.rerun()
        st.error("密碼不正確")
    st.stop()


def _taiwan_day():
    return datetime.now(TZ_TAIPEI).date()


def _member_page(store) -> None:
    _brand()
    sport_label = st.radio("賽事類別", ["MLB", "歐洲足球"], horizontal=True)
    sport = "mlb" if sport_label == "MLB" else "football"
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
    sport_label = st.radio("管理項目", ["MLB", "歐洲足球"], horizontal=True, key="admin_sport")
    sport = "mlb" if sport_label == "MLB" else "football"
    selected = st.date_input("建立哪一天的賽前快照", _taiwan_day(), key="admin_day")
    day = selected.isoformat()
    st.info("按下後由後台自動抓取賽程、球隊／球員資料與讓分／大小線；不需要手動輸入盤口。勝率永遠由模型資料產生；缺少實際讓分或大小線時，會明確顯示模型基準線，不會留白。")
    if st.button(f"建立並保存 {sport_label} 自動快照", type="primary"):
        try:
            with st.spinner("正在建立快照…"):
                snapshot = build_mlb_snapshot(selected) if sport == "mlb" else build_football_snapshot(selected)
                save_snapshot(store, snapshot)
            st.success(f"已保存 {len(snapshot.events)} 場 {sport_label} 賽事。自動快照現在已可供會員查詢。")
        except ProviderError as exc:
            st.error(str(exc))
        except StorageError as exc:
            st.error(str(exc))
        except Exception:
            st.error("本次建立未完成；既有快照沒有被覆蓋。請稍後再試或檢查資料來源設定。")
    automatic = store.get_automatic_snapshot(sport, day)
    if automatic:
        st.divider()
        st.subheader("已保存快照預覽")
        render_snapshot(st, automatic, "自動快照（會員可查詢）")
        if st.button("將這份自動快照標記為管理員確認版"):
            try:
                store.publish_automatic(sport, day)
                st.success("已標記為管理員確認版。會員看到的賽事內容不會被即時資料回頭改寫。")
            except StorageError as exc:
                st.error(str(exc))
    with st.expander("資料來源與安全規則"):
        st.write("MLB：MLB Stats API（球隊打擊／投球、預定先發）＋ The Odds API（讓分／大小線）。")
        st.write("足球：API-Football（賽程、主客場攻守、近期狀態、傷停）＋ The Odds API（讓分／大小線）。")
        st.write("機率由球隊／球員資料計算；市場只提供實際讓分與大小線。缺線時會標記模型基準線，絕不偽裝成實際可下注盤。")


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
        st.error(str(exc))
        st.caption("為保護已保存內容，APP 不會自動改用可能在重新部署後遺失的暫存資料庫。")
    if st.session_state.get("role") and st.button("登出"):
        st.session_state.clear()
        st.rerun()


if __name__ == "__main__":
    main()
