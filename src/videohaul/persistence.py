from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import DATABASE_PATH, ensure_directories


class Store:
    def __init__(self, path: Path | str = DATABASE_PATH, session_id: str = ""):
        ensure_directories()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.session_id = str(session_id or uuid4().hex)
        self.session_started_at = time.time()
        self._connection = sqlite3.connect(self.path, timeout=15.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._diagnostic_writes_since_prune = 0
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is None:
            return
        with self._lock:
            if self._connection is None:
                return
            self._connection.close()
            self._connection = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs(
                    job_id TEXT PRIMARY KEY,
                    position INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_position_idx ON jobs(position);
                CREATE TABLE IF NOT EXISTS diagnostics(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    level TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS diagnostics_at_idx ON diagnostics(at,id);
                CREATE TABLE IF NOT EXISTS history(
                    history_id TEXT PRIMARY KEY,
                    completed_at REAL NOT NULL,
                    canonical_identity TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_completed_idx ON history(completed_at DESC,history_id);
                CREATE INDEX IF NOT EXISTS history_identity_idx ON history(canonical_identity);
                """
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(diagnostics)").fetchall()}
            if "session" not in columns:
                connection.execute("ALTER TABLE diagnostics ADD COLUMN session TEXT NOT NULL DEFAULT ''")
            connection.execute("CREATE INDEX IF NOT EXISTS diagnostics_session_idx ON diagnostics(session,id)")
            connection.commit()

    def get_json(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def set_json(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )
            connection.commit()


    def add_diagnostic(self, entry: dict, max_entries: int = 20000) -> None:
        encoded = json.dumps(entry.get("payload") or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO diagnostics(at,level,kind,message,payload,session) VALUES(?,?,?,?,?,?)",
                (float(entry.get("at") or 0.0), str(entry.get("level") or "info"), str(entry.get("kind") or "event"), str(entry.get("message") or ""), encoded, self.session_id),
            )
            limit = max(100, int(max_entries))
            self._diagnostic_writes_since_prune += 1
            if self._diagnostic_writes_since_prune >= 256:
                connection.execute(
                    "DELETE FROM diagnostics WHERE id <= COALESCE((SELECT id FROM diagnostics ORDER BY id DESC LIMIT 1 OFFSET ?),0)",
                    (limit,),
                )
                self._diagnostic_writes_since_prune = 0
            connection.commit()

    def load_diagnostics(self, limit: int = 1000, session_id: str | None = None) -> list[dict]:
        safe_limit = max(1, min(20000, int(limit)))
        with self._lock, self._connect() as connection:
            if session_id is None:
                rows = connection.execute(
                    "SELECT id,at,level,kind,message,payload,session FROM diagnostics ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,at,level,kind,message,payload,session FROM diagnostics WHERE session=? ORDER BY id DESC LIMIT ?",
                    (str(session_id), safe_limit),
                ).fetchall()
            values = []
            for row in reversed(rows):
                try:
                    payload = json.loads(row["payload"])
                except Exception:
                    payload = {}
                values.append({"id": int(row["id"]), "at": float(row["at"]), "level": str(row["level"]), "kind": str(row["kind"]), "message": str(row["message"]), "payload": payload, "session": str(row["session"] or "")})
            return values

    def diagnostic_session_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM diagnostics WHERE session<>?", (self.session_id,)).fetchone()
            return int(row["total"] if row else 0)

    def clear_diagnostics(self, session_id: str | None = None) -> None:
        with self._lock, self._connect() as connection:
            if session_id is None:
                connection.execute("DELETE FROM diagnostics")
            else:
                connection.execute("DELETE FROM diagnostics WHERE session=?", (str(session_id),))
            connection.commit()

    def load_jobs(self) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT payload FROM jobs ORDER BY position, job_id").fetchall()
            result = []
            for row in rows:
                try:
                    result.append(json.loads(row["payload"]))
                except Exception:
                    continue
            return result

    def save_jobs(self, jobs: list[dict]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM jobs")
            connection.executemany(
                "INSERT INTO jobs(job_id,position,payload) VALUES(?,?,?)",
                [
                    (
                        str(job["job_id"]),
                        int(index),
                        json.dumps(job, ensure_ascii=False, separators=(",", ":")),
                    )
                    for index, job in enumerate(jobs)
                ],
            )
            connection.commit()

    def save_job(self, job: dict, position: int) -> None:
        encoded = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs(job_id,position,payload) VALUES(?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET position=excluded.position,payload=excluded.payload",
                (str(job["job_id"]), int(position), encoded),
            )
            connection.commit()

    def delete_job(self, job_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE job_id=?", (str(job_id),))
            connection.commit()
    def add_history(self, entry: dict) -> None:
        encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO history(history_id,completed_at,canonical_identity,source_url,payload) VALUES(?,?,?,?,?)",
                (str(entry["history_id"]), float(entry.get("completed_at") or 0.0), str(entry.get("canonical_identity") or ""), str(entry.get("source_url") or ""), encoded),
            )
            connection.commit()

    def load_history(self, limit: int = 1000) -> list[dict]:
        safe_limit = max(1, min(10000, int(limit)))
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT history_id,payload FROM history ORDER BY completed_at DESC,history_id DESC LIMIT ?", (safe_limit,)).fetchall()
            result = []
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    if isinstance(payload, dict):
                        payload.setdefault("history_id", str(row["history_id"]))
                        result.append(payload)
                except Exception:
                    continue
            return result

    def find_history_identity(self, canonical_identity: str) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT history_id,payload FROM history WHERE canonical_identity=? ORDER BY completed_at DESC", (str(canonical_identity),)).fetchall()
            result = []
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    if isinstance(payload, dict):
                        payload.setdefault("history_id", str(row["history_id"]))
                        result.append(payload)
                except Exception:
                    continue
            return result

    def delete_history(self, history_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM history WHERE history_id=?", (str(history_id),))
            connection.commit()
            return cursor.rowcount > 0

    def clear_history(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM history")
            connection.commit()

