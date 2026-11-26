from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import DB_PATH, ensure_directories


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        ensure_directories()
        self.path = path
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0, options TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    queue_order INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS clips (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, path TEXT NOT NULL,
                    title TEXT NOT NULL, hook TEXT NOT NULL, description TEXT NOT NULL,
                    score INTEGER NOT NULL, duration REAL NOT NULL, created_at TEXT NOT NULL,
                    thumbnail_path TEXT NOT NULL DEFAULT '',
                    thumbnail_raw_path TEXT NOT NULL DEFAULT '',
                    thumbnail_timestamp REAL,
                    thumbnail_cache_key TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                );
            """)
            proj_existing = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
            for col, defn in (("queue_order", "INTEGER NOT NULL DEFAULT 0"), ("stage", "TEXT NOT NULL DEFAULT ''")):
                if col not in proj_existing:
                    db.execute(f"ALTER TABLE projects ADD COLUMN {col} {defn}")
            existing = {row["name"] for row in db.execute("PRAGMA table_info(clips)")}
            migrations = (
                ("thumbnail_path", "TEXT NOT NULL DEFAULT ''"),
                ("thumbnail_raw_path", "TEXT NOT NULL DEFAULT ''"),
                ("thumbnail_timestamp", "REAL"),
                ("thumbnail_cache_key", "TEXT NOT NULL DEFAULT ''"),
            )
            for column, definition in migrations:
                if column not in existing:
                    db.execute(f"ALTER TABLE clips ADD COLUMN {column} {definition}")

    def create_project(self, source: str, options: dict[str, Any]) -> str:
        project_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db, db:
            next_order = int(db.execute("SELECT COALESCE(MAX(queue_order), 0) + 1 FROM projects").fetchone()[0])
            db.execute(
                "INSERT INTO projects (id, source, status, progress, options, error, created_at, updated_at, queue_order, stage) "
                "VALUES (?, ?, 'queued', 0, ?, '', ?, ?, ?, '')",
                (project_id, source, json.dumps(options), now, now, next_order),
            )
        return project_id

    def update_project(self, project_id: str, *, status: str | None = None,
                       progress: int | None = None, error: str | None = None,
                       stage: str | None = None) -> None:
        changes: list[str] = ["updated_at = ?"]
        values: list[Any] = [datetime.now(timezone.utc).isoformat()]
        for column, value in (("status", status), ("progress", progress), ("error", error), ("stage", stage)):
            if value is not None:
                changes.append(f"{column} = ?")
                values.append(value)
        values.append(project_id)
        with self._lock, closing(self._connect()) as db, db:
            db.execute(f"UPDATE projects SET {', '.join(changes)} WHERE id = ?", values)

    def reorder_queued_project(self, project_id: str, direction: int) -> bool:
        """Move a queued project up (direction < 0) or down (direction > 0) in queue_order."""
        with self._lock, closing(self._connect()) as db, db:
            rows = list(db.execute(
                "SELECT id, queue_order FROM projects WHERE status = 'queued' ORDER BY queue_order ASC, created_at ASC"
            ))
            idx = next((i for i, r in enumerate(rows) if r["id"] == project_id), -1)
            swap_idx = idx - 1 if direction < 0 else idx + 1
            if idx < 0 or not (0 <= swap_idx < len(rows)):
                return False
            a_id, a_ord = rows[idx]["id"], int(rows[idx]["queue_order"])
            b_id, b_ord = rows[swap_idx]["id"], int(rows[swap_idx]["queue_order"])
            if a_ord == b_ord:
                a_ord, b_ord = idx + 1, swap_idx + 1
            db.execute("UPDATE projects SET queue_order = ? WHERE id = ?", (b_ord, a_id))
            db.execute("UPDATE projects SET queue_order = ? WHERE id = ?", (a_ord, b_id))
            return True

    def delete_project_history(self, project_id: str) -> bool:
        """Remove a non-active project row from history without deleting video or clip files."""
        with self._lock, closing(self._connect()) as db, db:
            return db.execute(
                "DELETE FROM projects WHERE id = ? AND status != 'processing'", (project_id,)
            ).rowcount == 1

    def next_queued_project(self) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM projects WHERE status = 'queued' ORDER BY queue_order ASC, created_at ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def claim_analysis_retry(self, project_id: str, options: dict[str, Any]) -> bool:
        with self._lock, closing(self._connect()) as db, db:
            return db.execute(
                "UPDATE projects SET status = 'processing', updated_at = ?, error = '', options = ? "
                "WHERE id = ? AND status = 'analysis_failed'",
                (datetime.now(timezone.utc).isoformat(), json.dumps(options), project_id),
            ).rowcount == 1

    def add_clip(self, project_id: str, path: str, title: str, hook: str,
                 description: str, score: int, duration: float, *,
                 thumbnail_path: str = "", thumbnail_raw_path: str = "",
                 thumbnail_timestamp: float | None = None,
                 thumbnail_cache_key: str = "") -> str:
        clip_id = uuid.uuid4().hex
        with self._lock, closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO clips (id, project_id, path, title, hook, description, score, duration, created_at, "
                "thumbnail_path, thumbnail_raw_path, thumbnail_timestamp, thumbnail_cache_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clip_id, project_id, path, title, hook, description, score, duration,
                 datetime.now(timezone.utc).isoformat(), thumbnail_path, thumbnail_raw_path,
                 thumbnail_timestamp, thumbnail_cache_key),
            )
        return clip_id

    def update_clip_thumbnail(self, clip_id: str, thumbnail_path: str, thumbnail_raw_path: str,
                              thumbnail_timestamp: float | None, thumbnail_cache_key: str) -> None:
        with self._lock, closing(self._connect()) as db, db:
            db.execute(
                "UPDATE clips SET thumbnail_path = ?, thumbnail_raw_path = ?, "
                "thumbnail_timestamp = ?, thumbnail_cache_key = ? WHERE id = ?",
                (thumbnail_path, thumbnail_raw_path, thumbnail_timestamp, thumbnail_cache_key, clip_id),
            )

    def rename_clip(self, clip_id: str, title: str) -> bool:
        cleaned = title.strip()
        if not cleaned:
            return False
        with self._lock, closing(self._connect()) as db, db:
            return db.execute("UPDATE clips SET title = ? WHERE id = ?", (cleaned, clip_id)).rowcount == 1

    def delete_clip(self, clip_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as db, db:
            row = db.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
            return dict(row)

    def projects(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,)
            )]

    def clips(self, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", (limit,)
            )]

    def project_count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT count(*) FROM projects").fetchone()[0])

    def clip_count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT count(*) FROM clips").fetchone()[0])
