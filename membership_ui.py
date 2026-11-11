"""Traditional Chinese login and admin UI. All DB calls stay server-side."""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from auth import credential_version, verify_password
from device_binding import browser_device
from membership import MemberError, MemberRepository
from storage import create_store, StorageError

MEMBER_APP_URL = "https://vidali-sports.streamlit.app/"


def taipei_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d %H:%M") if value else "—"


def repository():
    store, _ = create_store()
    return MemberRepository(store)


def clear_member(st):
    for key in ("role", "member_session", "member_info", "_browser_device"):
        st.session_state.pop(key, None)


def login(st, brand):
    admin = os.environ.get("APP_ADMIN_PASSWORD", "")
    if not admin:
        brand()
        st.error("APP 尚未完成管理員登入設定；請設定 APP_ADMIN_PASSWORD。")
        st.stop()
    version = credential_version(admin, "independent-members-v1")
    if st.session_state.get("credential_version") != version:
        clear_member(st)
        st.session_state.pop("issued_credentials", None)
    if st.session_state.get("role") == "admin":
        return "admin"
    if st.session_state.get("role") == "member":
        device = browser_device()
        if not device:
            st.info("正在確認綁定瀏覽器；請允許網站儲存資料。")
            if st.button("返回登入"):
                clear_member(st)
                st.rerun()
            st.stop()
        try:
            info = repository().validate(st.session_state.get("member_session"), device)
        except (MemberError, StorageError):
            st.error("無法確認會員資格，暫停顯示會員內容；請稍後重新整理。")
            if st.button("返回登入"):
                clear_member(st)
                st.rerun()
            st.stop()
        if info:
            st.session_state["_browser_device"] = device
            st.session_state["member_info"] = info
            return "member"
        clear_member(st)
        st.warning("會員資格已到期、停用或登入已失效，請重新登入或聯絡管理員。")
    brand()
    kind = st.radio("登入身分", ["會員", "管理員"], horizontal=True)
    login_id, device = "", None
    if kind == "會員":
        st.caption("使用管理員提供的會員編號及專屬密碼；首次登入綁定此瀏覽器。舊共用密碼不再適用。")
        login_id = st.text_input("會員編號", placeholder="VD-…")
        device = browser_device()
        if not device:
            st.info("請允許瀏覽器網站儲存；準備完成後即可登入。")
    password = st.text_input("登入密碼", type="password")
    if st.button("登入", type="primary", disabled=kind == "會員" and not device):
        if time.time() < st.session_state.get("login_retry_after", 0):
            st.error("嘗試次數過多，請稍候一分鐘再試。")
            st.stop()
        try:
            if kind == "管理員":
                if not verify_password(password, admin):
                    raise MemberError("密碼不正確")
                st.session_state["role"] = "admin"
            else:
                session = repository().login(login_id, password, device)
                st.session_state["member_session"] = session
                st.session_state["_browser_device"] = device
                st.session_state["role"] = "member"
            st.session_state["credential_version"] = version
            st.session_state["login_failures"] = 0
            st.rerun()
        except (MemberError, StorageError) as exc:
            failures = st.session_state.get("login_failures", 0) + 1
            st.session_state["login_failures"] = failures
            if failures >= 5:
                st.session_state["login_retry_after"] = time.time() + 60
            st.error(str(exc))
    st.stop()


def require_active_member(st, repo):
    try:
        info = repo.validate(st.session_state.get("member_session"), st.session_state.get("_browser_device"))
    except (MemberError, StorageError):
        st.error("會員資格驗證暫時無法完成；未顯示會員內容，請稍後再試。")
        st.stop()
    if not info:
        clear_member(st)
        st.rerun()
    st.caption(f"會員：{info['label']}｜有效至 {taipei_time(info['expires_at'])}（台灣時間）")


def member_management(st, repo):
    # Defense in depth: never trust a selected tab as authorization.
    if st.session_state.get("role") != "admin":
        st.error("僅管理員可操作會員管理。")
        st.stop()
    st.subheader("會員管理")
    st.caption("開通起算七天；到期自動停止存取。裝置綁定指瀏覽器，不是不可複製的硬體鎖。")
    page = int(st.number_input("會員清單頁碼", min_value=1, value=1, step=1))
    data = repo.overview(page - 1)
    columns = st.columns(4)
    for column, label, key in zip(columns, ["累計開通", "目前會員", "有效會員", "已到期"], ["total_issued", "current", "active", "expired"]):
        column.metric(label, data[key])
    st.caption(f"停用：{data['disabled']}｜已刪除：{data['deleted']}")
    notice = st.session_state.pop("member_notice", None)
    if notice:
        st.success(notice)
    credentials = st.session_state.get("issued_credentials")
    if credentials:
        st.warning("請私下交付給該會員。密碼不提供事後查詢；關閉後只能重新產生。勿貼至公開錯誤單。")
        st.code(f"APP 網址：{MEMBER_APP_URL}\n會員編號：{credentials['login_id']}\n專屬密碼：{credentials['password']}", language=None)
        st.caption("所有會員使用同一個 APP 網址；開啟後選擇會員，以各自的編號及密碼登入。網址不含密碼或登入憑證。")
        if credentials.get("expires_at"):
            st.caption("到期時間：" + taipei_time(credentials["expires_at"]))
        if st.button("已交付，關閉密碼顯示"):
            st.session_state.pop("issued_credentials", None)
            st.rerun()
    with st.form("new_member", clear_on_submit=True):
        label = st.text_input("會員稱呼／內部備註", max_chars=80)
        add = st.form_submit_button("產生專屬密碼並開通七天")
    if add:
        st.session_state["issued_credentials"] = repo.issue(label)
        st.session_state["member_notice"] = "會員已建立，請交付編號與專屬密碼。"
        st.rerun()
    rows = data["members"]
    st.dataframe([{"編號": m["login_id"], "稱呼": m["label"], "狀態": m["state"],
                   "到期（台灣）": taipei_time(m["expires_at"]), "綁定": "已綁定" if m["bound"] else "未綁定",
                   "最近登入（台灣）": taipei_time(m["last_login_at"])} for m in rows], hide_index=True)
    if not rows:
        st.info("本頁沒有會員；可新增會員或返回前一頁。")
        return
    mapping = {m["id"]: m for m in rows}
    selected = st.selectbox("選擇管理的會員", list(mapping), format_func=lambda x: mapping[x]["login_id"] + "｜" + mapping[x]["label"])
    actions = {"開通／延長七天": "renew", "停用會員": "disable", "解除裝置綁定": "reset_device",
               "重新產生密碼": "reset_password", "刪除會員": "delete"}
    with st.form("manage_member"):
        action = st.selectbox("操作", list(actions))
        st.caption("延期：未到期從原到期日加七天，已到期從現在起算。刪除會移除稱呼、密碼與綁定，僅留匿名化開通計數紀錄，不能復原。")
        confirmed = st.checkbox("我確認要對上方選定會員執行此操作")
        submit = st.form_submit_button("確認執行")
    if submit:
        if not confirmed:
            st.error("請先勾選確認。")
            return
        result = repo.manage(selected, actions[action])
        st.session_state.pop("issued_credentials", None)
        if result.get("password"):
            st.session_state["issued_credentials"] = {"login_id": mapping[selected]["login_id"], "password": result["password"]}
        st.session_state["member_notice"] = f"{mapping[selected]['login_id']}：{action}已完成。"
        st.rerun()
