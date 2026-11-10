"""Small, durable snapshot store for the clean Streamlit rebuild.

Production uses one normal Supabase table through its server-side REST API.
The local SQLite implementation exists only for development, never as a silent
fallback in Streamlit production.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class StorageError(RuntimeError):
    def __init__(self, message, *, code="STORAGE_UNKNOWN"):
        super().__init__(message)
        self.code = code


STORAGE_CODES = frozenset({"STORAGE_CONFIG", "STORAGE_AUTH", "STORAGE_ACCESS",
    "STORAGE_SCHEMA", "STORAGE_CONFLICT", "STORAGE_RATE_LIMIT", "STORAGE_UPSTREAM",
    "STORAGE_TIMEOUT", "STORAGE_NETWORK", "STORAGE_RESPONSE", "STORAGE_HTTP", "STORAGE_UNKNOWN"})


def _http_failure(status):
    # Do not read response bodies, headers, URLs or exception messages.
    code, message = {
        401: ("STORAGE_AUTH", "資料庫驗證失敗；請管理員核對專案與伺服器金鑰是否相符。"),
        403: ("STORAGE_ACCESS", "資料庫存取遭拒；需由管理員核對權限，不會自動放寬權限。"),
        404: ("STORAGE_SCHEMA", "資料庫端點或資料表未找到；請核對專案網址及資料表設定。"),
        409: ("STORAGE_CONFLICT", "資料庫寫入衝突；請重新讀取後再試，不會自動重送寫入。"),
        429: ("STORAGE_RATE_LIMIT", "資料庫請求受到限流；請稍後再試。"),
    }.get(status, ("STORAGE_UPSTREAM", "資料庫服務暫時失敗；請核對專案狀態後再試。") if status >= 500
          else ("STORAGE_HTTP", "資料庫請求遭拒；請管理員核對端點與資料表結構。"))
    return StorageError(message, code=code)


_UNSET = object()

def _check_revision(current, expected):
    if expected is not _UNSET and (current or {}).get("revision_id") != expected:
        raise StorageError("另一個管理操作已更新快照；本次沒有覆蓋資料，請重新整理後再試")


class SnapshotStore(Protocol):
    def save_automatic(self, sport: str, date_key: str, payload: dict[str, Any], *, expected_revision=_UNSET) -> None: ...
    def publish_automatic(self, sport: str, date_key: str) -> None: ...
    def save_manual_published(self, sport: str, date_key: str, payload: dict[str, Any]) -> None: ...
    def get_member_snapshot(self, sport: str, date_key: str) -> tuple[dict[str, Any] | None, str]: ...
    def get_automatic_snapshot(self, sport: str, date_key: str) -> dict[str, Any] | None: ...
    def save_settled_snapshots(self, sport: str, date_key: str, automatic: dict[str, Any], published: dict[str, Any] | None) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _published_label(payload: dict[str, Any]) -> str:
    """Keep the member-facing release state explicit and truthful."""

    if payload.get("release_type") == "manual_calibrated":
        return "人工校正正式推薦"
    return "管理員確認的自動推薦"


class LocalSnapshotStore:
    """For local development only; clearly labelled by the UI."""

    def __init__(self, path: str = "data/rebuild.sqlite3") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                sport TEXT NOT NULL, date_key TEXT NOT NULL,
                automatic_payload TEXT, automatic_at TEXT,
                published_payload TEXT, published_at TEXT,
                PRIMARY KEY (sport, date_key))""")

    def _connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_automatic(self, sport: str, date_key: str, payload: dict[str, Any], *, expected_revision=_UNSET) -> None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT automatic_payload FROM snapshots WHERE sport=? AND date_key=?",(sport,date_key)).fetchone()
            _check_revision(json.loads(current[0]) if current and current[0] else None,expected_revision)
            conn.execute("""INSERT INTO snapshots(sport,date_key,automatic_payload,automatic_at)
                VALUES(?,?,?,?) ON CONFLICT(sport,date_key) DO UPDATE SET
                automatic_payload=excluded.automatic_payload, automatic_at=excluded.automatic_at""",
                (sport, date_key, json.dumps(payload, ensure_ascii=False), _now()))

    def publish_automatic(self, sport: str, date_key: str) -> None:
        with self._connection() as conn:
            row = conn.execute("SELECT automatic_payload FROM snapshots WHERE sport=? AND date_key=?", (sport, date_key)).fetchone()
            if not row or not row["automatic_payload"]:
                raise StorageError("尚無可發布的自動快照")
            conn.execute("UPDATE snapshots SET published_payload=?, published_at=? WHERE sport=? AND date_key=?",
                         (row["automatic_payload"], _now(), sport, date_key))

    def save_manual_published(self, sport: str, date_key: str, payload: dict[str, Any]) -> None:
        """Persist a manual release without replacing the automatic baseline."""

        with self._connection() as conn:
            row = conn.execute("SELECT automatic_payload FROM snapshots WHERE sport=? AND date_key=?", (sport, date_key)).fetchone()
            if not row or not row["automatic_payload"]:
                raise StorageError("請先建立完整自動快照，才能發布人工校正版本")
            conn.execute("UPDATE snapshots SET published_payload=?, published_at=? WHERE sport=? AND date_key=?",
                         (json.dumps(payload, ensure_ascii=False), _now(), sport, date_key))

    def save_settled_snapshots(self, sport: str, date_key: str, automatic: dict[str, Any], published: dict[str, Any] | None) -> None:
        with self._connection() as conn:
            conn.execute("""UPDATE snapshots SET automatic_payload=?, automatic_at=?,
                published_payload=COALESCE(?, published_payload), published_at=CASE WHEN ? IS NULL THEN published_at ELSE ? END
                WHERE sport=? AND date_key=?""",
                (json.dumps(automatic, ensure_ascii=False), _now(),
                 json.dumps(published, ensure_ascii=False) if published is not None else None,
                 "updated" if published is not None else None, _now(), sport, date_key))

    def get_member_snapshot(self, sport: str, date_key: str) -> tuple[dict[str, Any] | None, str]:
        with self._connection() as conn:
            row = conn.execute("SELECT automatic_payload,published_payload FROM snapshots WHERE sport=? AND date_key=?", (sport, date_key)).fetchone()
        if not row:
            return None, "尚未建立快照"
        if row["published_payload"]:
            payload = json.loads(row["published_payload"])
            return payload, _published_label(payload)
        if row["automatic_payload"]:
            return json.loads(row["automatic_payload"]), "自動更新推薦｜尚未人工校正"
        return None, "尚未建立快照"

    def get_automatic_snapshot(self, sport: str, date_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT automatic_payload FROM snapshots WHERE sport=? AND date_key=?", (sport, date_key)).fetchone()
        return json.loads(row["automatic_payload"]) if row and row["automatic_payload"] else None


class SupabaseSnapshotStore:
    TABLE = "vd_daily_snapshots"

    def __init__(self, url: str, service_key: str) -> None:
        try:
            parts = urlsplit(url)
            valid = (parts.scheme == "https" and bool(parts.hostname)
                     and parts.hostname != "supabase.com" and not parts.hostname.endswith(".supabase.com")
                     and not parts.username and not parts.password and not parts.query
                     and not parts.fragment and parts.path in ("", "/"))
            _ = parts.port
        except ValueError:
            valid = False
        if not valid or not service_key.strip() or service_key.startswith("sb_publishable_"):
            raise StorageError("資料庫設定無效；請使用專案 API HTTPS 根網址（不是 dashboard 網址）與伺服器金鑰。", code="STORAGE_CONFIG")
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": service_key,
            "Content-Type": "application/json",
        }
        # Current Supabase secret keys (sb_secret_...) are opaque API keys and
        # must be sent on `apikey` only.  Legacy service_role values are JWTs
        # and retain the older Bearer header for backward compatibility.
        if not service_key.startswith("sb_secret_"):
            self.headers["Authorization"] = f"Bearer {service_key}"

    def _request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                 body: Any | None = None, headers: dict[str, str] | None = None) -> Any:
        try:
            suffix = ("?" + urlencode(params)) if params else ""
            data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            request = Request(self.base + path + suffix, data=data,
                              headers={**self.headers, **(headers or {})}, method=method)
            with urlopen(request, timeout=18) as response:
                content = response.read().decode("utf-8")
                return json.loads(content) if content else None
        except HTTPError as exc:
            raise _http_failure(exc.code) from None
        except TimeoutError:
            raise StorageError("資料庫連線逾時；請稍後再試並核對專案狀態。", code="STORAGE_TIMEOUT") from None
        except URLError as exc:
            code = "STORAGE_TIMEOUT" if isinstance(exc.reason, TimeoutError) else "STORAGE_NETWORK"
            raise StorageError("資料庫網路連線未完成；請核對網路與專案狀態。", code=code) from None
        except OSError:
            raise StorageError("資料庫網路連線未完成；請核對網路與專案狀態。", code="STORAGE_NETWORK") from None
        except ValueError:
            raise StorageError("資料庫回應格式不符預期；請核對 API 端點。", code="STORAGE_RESPONSE") from None

    def check_read_access(self) -> None:
        """Validate endpoint/schema read access without fetching snapshot contents.

        A successful empty query does NOT prove write permission or RLS visibility.
        Never insert, update or delete a health-check row.
        """
        rows = self._request("GET", f"/{self.TABLE}", params={
            "select": "sport,date_key,automatic_payload,automatic_at,published_payload,published_at", "limit": "0"})
        if not isinstance(rows, list) or rows:
            raise StorageError("資料庫唯讀檢查回應不符預期。", code="STORAGE_RESPONSE")

    def _row(self, sport: str, date_key: str) -> dict[str, Any] | None:
        rows = self._request("GET", f"/{self.TABLE}", params={
            "sport": f"eq.{sport}", "date_key": f"eq.{date_key}", "select": "*",
        })
        return rows[0] if rows else None

    def save_automatic(self, sport: str, date_key: str, payload: dict[str, Any], *, expected_revision=_UNSET) -> None:
        record = {"sport": sport, "date_key": date_key, "automatic_payload": payload, "automatic_at": _now()}
        row = self._row(sport,date_key)
        _check_revision((row or {}).get("automatic_payload"),expected_revision)
        if row:
            timestamp = row.get("automatic_at")
            rows = self._request("PATCH",f"/{self.TABLE}",params={"sport":f"eq.{sport}","date_key":f"eq.{date_key}",
                                 "automatic_at":f"eq.{timestamp}" if timestamp else "is.null"},
                                 body={"automatic_payload":payload,"automatic_at":record["automatic_at"]},
                                 headers={"Prefer":"return=representation"})
            if not rows:
                raise StorageError("快照在儲存期間已變更；本次未覆蓋，請重新整理後再試")
        else:
            # No upsert: a simultaneous insert must fail, not overwrite.
            self._request("POST",f"/{self.TABLE}",body=record,headers={"Prefer":"return=minimal"})

    def publish_automatic(self, sport: str, date_key: str) -> None:
        row = self._row(sport, date_key)
        if not row or not row.get("automatic_payload"):
            raise StorageError("尚無可發布的自動快照")
        self._request("PATCH", f"/{self.TABLE}", params={"sport": f"eq.{sport}", "date_key": f"eq.{date_key}"},
                      body={"published_payload": row["automatic_payload"], "published_at": _now()})

    def save_manual_published(self, sport: str, date_key: str, payload: dict[str, Any]) -> None:
        """Save a manually calibrated member release while retaining auto data."""

        row = self._row(sport, date_key)
        if not row or not row.get("automatic_payload"):
            raise StorageError("請先建立完整自動快照，才能發布人工校正版本")
        self._request("PATCH", f"/{self.TABLE}", params={"sport": f"eq.{sport}", "date_key": f"eq.{date_key}"},
                      body={"published_payload": payload, "published_at": _now()})

    def save_settled_snapshots(self, sport: str, date_key: str, automatic: dict[str, Any], published: dict[str, Any] | None) -> None:
        body: dict[str, Any] = {"automatic_payload": automatic, "automatic_at": _now()}
        if published is not None:
            body["published_payload"] = published
            body["published_at"] = _now()
        self._request("PATCH", f"/{self.TABLE}", params={"sport": f"eq.{sport}", "date_key": f"eq.{date_key}"},
                      body=body)

    def get_member_snapshot(self, sport: str, date_key: str) -> tuple[dict[str, Any] | None, str]:
        row = self._row(sport, date_key)
        if not row:
            return None, "尚未建立快照"
        if row.get("published_payload"):
            payload = row["published_payload"]
            return payload, _published_label(payload)
        if row.get("automatic_payload"):
            return row["automatic_payload"], "自動更新推薦｜尚未人工校正"
        return None, "尚未建立快照"

    def get_automatic_snapshot(self, sport: str, date_key: str) -> dict[str, Any] | None:
        row = self._row(sport, date_key)
        return row.get("automatic_payload") if row else None


def create_store() -> tuple[SnapshotStore, str]:
    """Return store and a safe status label for the admin UI."""

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )
    if url and key:
        return SupabaseSnapshotStore(url, key), "Supabase 已設定（連線及寫入尚未驗證）"
    if os.environ.get("REQUIRE_SUPABASE", "1") == "1":
        raise StorageError("尚未完成 Supabase 設定：請在 Streamlit Secrets 填入 SUPABASE_URL 與 SUPABASE_SECRET_KEY", code="STORAGE_CONFIG")
    return LocalSnapshotStore(), "本機測試模式（部署後不保存）"

