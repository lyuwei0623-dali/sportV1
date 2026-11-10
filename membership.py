"""Server-only membership; no plaintext passwords or browser tokens in the DB."""
import hashlib
import hmac
import re
import secrets

from storage import StorageError, SupabaseSnapshotStore


class MemberError(RuntimeError):
    pass


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return f"scrypt1${salt}${digest.hex()}"


def password_matches(password, encoded):
    try:
        version, salt, digest = encoded.split("$")
        if version != "scrypt1" or not re.fullmatch(r"[0-9a-f]{32}", salt) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError, AttributeError):
        return False


def token_hash(token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise MemberError("裝置識別尚未就緒；請使用一般瀏覽模式並允許網站儲存資料。")
    return hashlib.sha256(token.encode()).hexdigest()


class MemberRepository:
    def __init__(self, store):
        if not isinstance(store, SupabaseSnapshotStore):
            raise MemberError("獨立會員需要 Supabase；不支援暫存資料庫登入。")
        self.store = store

    def rpc(self, name, **params):
        try:
            return self.store._request("POST", "/rpc/" + name, body=params)
        except StorageError:
            raise MemberError("會員資料庫暫時無法使用；請管理員確認專案已恢復及會員安裝檔已執行。") from None

    def issue(self, label):
        label = label.strip()
        if not label or len(label) > 80:
            raise MemberError("請填入1～80字的會員稱呼。")
        login_id = "VD-" + secrets.token_hex(6).upper()
        password = secrets.token_urlsafe(18)
        result = self.rpc("vd_member_issue", p_login_id=login_id, p_label=label, p_password_hash=password_hash(password))
        if not isinstance(result, dict) or not result.get("id"):
            raise MemberError("會員建立結果無法確認；請先查會員清單，避免重複新增。")
        return {**result, "login_id": login_id, "password": password}

    def overview(self, page=0):
        return self.rpc("vd_member_overview", p_offset=max(0, int(page)) * 50)

    def login(self, login_id, password, device):
        device_hash = token_hash(device)
        login_id = login_id.strip().upper()
        if not re.fullmatch(r"VD-[0-9A-F]{12}", login_id) or not isinstance(password, str) or len(password) > 128:
            raise MemberError("會員編號或密碼不正確。")
        credential = self.rpc("vd_member_credential", p_login_id=login_id)
        # Unknown accounts still run one KDF. Never return the password hash to UI.
        encoded = (credential or {}).get("password_hash") or password_hash("dummy-not-a-real-account", "0" * 32)
        valid = password_matches(password, encoded)
        session = secrets.token_hex(32)
        result = self.rpc("vd_member_attempt", p_login_id=login_id, p_expected_hash=encoded,
                          p_password_ok=valid, p_device_hash=device_hash, p_session_hash=token_hash(session))
        if not result or not result.get("ok"):
            messages = {"expired": "會員已到期，請聯絡管理員開通延期。",
                        "device": "此會員已綁定其他瀏覽器；換裝置請聯絡管理員解除綁定。",
                        "locked": "登入嘗試過多，請五分鐘後再試。"}
            raise MemberError(messages.get((result or {}).get("reason"), "會員編號或密碼不正確，或會員已停用。"))
        return {"id": result["id"], "session": session}

    def validate(self, session, device):
        if not isinstance(session, dict) or not session.get("id"):
            return None
        result = self.rpc("vd_member_session", p_id=session["id"], p_device_hash=token_hash(device),
                          p_session_hash=token_hash(session.get("session")))
        return result if result and result.get("ok") else None

    def manage(self, member_id, action):
        if action not in {"renew", "disable", "delete", "reset_device", "reset_password"}:
            raise MemberError("不支援此會員操作。")
        password = secrets.token_urlsafe(18) if action == "reset_password" else None
        result = self.rpc("vd_member_manage", p_id=member_id, p_action=action,
                          p_password_hash=password_hash(password) if password else None)
        if not result or not result.get("ok"):
            raise MemberError("會員不存在或已刪除；請重新整理清單。")
        return {**result, "password": password} if password else result

    def logout(self, session):
        if session:
            self.rpc("vd_member_logout", p_id=session["id"], p_session_hash=token_hash(session["session"]))
