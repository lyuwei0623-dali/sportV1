"""Small, durable snapshot store for the clean Streamlit rebuild.

Production uses one normal Supabase table through its server-side REST API.
The local SQLite implementation exists only for development, never as a silent
fallback in Streamlit production.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class StorageError(RuntimeError):
    pass


class SnapshotStore(Protocol):
    def save_automatic(self, sport: str, date_key: str, payload: dict[str, Any]) -> None: ...
    def publish_automatic(self, sport: str, date_key: str) -> None: ...
    def save_manual_published(self, sport: str, date_key: str, payload: dict[str, Any]) -> None: ...
    def get_member_snapshot(self, sport: str, date_key: str) -> tuple[dict[str, Any] | None, str]: ...
    def get_automatic_snapshot(self, sport: str, date_key: str) -> dict[str, Any] | None: ...


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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

    def save_automatic(self, sport: str, date_key: str, payload: dict[str, Any]) -> None:
        with self._connection() as conn:
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
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            raise StorageError("無法連線至永久資料庫；請檢查 Supabase Secrets 與專案狀態") from exc

    def _row(self, sport: str, date_key: str) -> dict[str, Any] | None:
        rows = self._request("GET", f"/{self.TABLE}", params={
            "sport": f"eq.{sport}", "date_key": f"eq.{date_key}", "select": "*",
        })
        return rows[0] if rows else None

    def save_automatic(self, sport: str, date_key: str, payload: dict[str, Any]) -> None:
        record = {"sport": sport, "date_key": date_key, "automatic_payload": payload, "automatic_at": _now()}
        self._request("POST", f"/{self.TABLE}", params={"on_conflict": "sport,date_key"}, body=record,
                      headers={"Prefer": "resolution=merge-duplicates,return=minimal"})

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
        return SupabaseSnapshotStore(url, key), "Supabase 永久保存"
    if os.environ.get("REQUIRE_SUPABASE", "1") == "1":
        raise StorageError("尚未完成 Supabase 設定：請在 Streamlit Secrets 填入 SUPABASE_URL 與 SUPABASE_SECRET_KEY")
    return LocalSnapshotStore(), "本機測試模式（部署後不保存）"
