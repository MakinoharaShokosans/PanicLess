"""逐调用执行日志。副作用开始前记 running，返回后立即持久化原始结果。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_runtime.core import ToolResult


class ToolJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS tool_calls (
                task_id TEXT NOT NULL, turn INTEGER NOT NULL, call_id TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, trust TEXT, updated_at REAL NOT NULL,
                PRIMARY KEY(task_id,turn,call_id))""")
            with conn:
                yield conn
        finally:
            conn.close()

    def load(self, task_id: str, turn: int, call_id: str) -> tuple[str, ToolResult | None, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status,result,trust FROM tool_calls "
                "WHERE task_id=? AND turn=? AND call_id=?",
                (task_id, turn, call_id),
            ).fetchone()
        if row is None:
            return "queued", None, "untrusted"
        return (
            row[0],
            ToolResult.model_validate_json(row[1]) if row[1] else None,
            row[2] or "untrusted",
        )

    def has_batch(self, task_id: str, turn: int) -> bool:
        if not self.path.exists():
            return False
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM tool_calls WHERE task_id=? AND turn=? LIMIT 1", (task_id, turn)
                ).fetchone()
                is not None
            )

    def enqueue(self, task_id: str, turn: int, call_ids: Sequence[str]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO tool_calls VALUES (?,?,?,'queued',NULL,NULL,?)",
                [(task_id, turn, call_id, time.time()) for call_id in call_ids],
            )

    def start(self, task_id: str, turn: int, call_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tool_calls VALUES (?,?,?,'running',NULL,NULL,?) "
                "ON CONFLICT(task_id,turn,call_id) DO UPDATE SET "
                "status='running',updated_at=excluded.updated_at",
                (task_id, turn, call_id, time.time()),
            )

    def finish(self, task_id: str, turn: int, call_id: str, result: ToolResult, trust: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tool_calls SET status='completed',result=?,trust=?,updated_at=? "
                "WHERE task_id=? AND turn=? AND call_id=?",
                (result.model_dump_json(), trust, time.time(), task_id, turn, call_id),
            )

    def entries(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM tool_calls WHERE task_id=? ORDER BY turn,updated_at", (task_id,)
            ).fetchall()
        return [
            {**dict(row), "result": json.loads(row["result"]) if row["result"] else None}
            for row in rows
        ]

    def delete(self, task_id: str) -> None:
        if self.path.exists():
            with self._connect() as conn:
                conn.execute("DELETE FROM tool_calls WHERE task_id=?", (task_id,))
