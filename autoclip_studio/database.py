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
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clips (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, path TEXT NOT NULL,
                    title TEXT NOT NULL, hook TEXT NOT NULL, description TEXT NOT NULL,
                    score INTEGER NOT NULL, duration REAL NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                );
            """)

    def create_project(self, source: str, options: dict[str, Any]) -> str:
        project_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO projects VALUES (?, ?, 'queued', 0, ?, '', ?, ?)",
                (project_id, source, json.dumps(options), now, now),
            )
        return project_id

    def update_project(self, project_id: str, *, status: str | None = None,
                       progress: int | None = None, error: str | None = None) -> None:
        changes: list[str] = ["updated_at = ?"]
        values: list[Any] = [datetime.now(timezone.utc).isoformat()]
        for column, value in (("status", status), ("progress", progress), ("error", error)):
            if value is not None:
                changes.append(f"{column} = ?")
                values.append(value)
        values.append(project_id)
        with self._lock, closing(self._connect()) as db, db:
            db.execute(f"UPDATE projects SET {', '.join(changes)} WHERE id = ?", values)

    def add_clip(self, project_id: str, path: str, title: str, hook: str,
                 description: str, score: int, duration: float) -> str:
        clip_id = uuid.uuid4().hex
        with self._lock, closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO clips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clip_id, project_id, path, title, hook, description, score, duration,
                 datetime.now(timezone.utc).isoformat()),
            )
        return clip_id

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
