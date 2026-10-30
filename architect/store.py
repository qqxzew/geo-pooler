"""SQLite store for Agent 4: one pool plan (JSON) + its two PDFs per match."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    match_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL,
    agreement_pdf BLOB, grid_pdf BLOB, created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""


class ArchitectStore:
    def __init__(self, path: str):
        self.path = path
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path)
        try:
            c.executescript(_SCHEMA)
        finally:
            c.close()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    def _save(self, match_id: str, version: int, data: dict, agreement: bytes, grid: bytes) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO pools (match_id,version,data,agreement_pdf,grid_pdf,created_at,updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(match_id) DO UPDATE SET version=excluded.version, data=excluded.data,
                   agreement_pdf=excluded.agreement_pdf, grid_pdf=excluded.grid_pdf, updated_at=excluded.updated_at""",
                (match_id, version, json.dumps(data), agreement, grid, now, now))

    def _load(self, match_id: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM pools WHERE match_id=?", (match_id,)).fetchone()
        if not r:
            return None
        return {"match_id": r["match_id"], "version": r["version"], "data": json.loads(r["data"]),
                "agreement_pdf": r["agreement_pdf"], "grid_pdf": r["grid_pdf"]}

    async def save(self, match_id: str, version: int, data: dict[str, Any], agreement: bytes, grid: bytes) -> None:
        await asyncio.to_thread(self._save, match_id, version, data, agreement, grid)

    async def load(self, match_id: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._load, match_id)
