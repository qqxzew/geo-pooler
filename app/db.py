"""Tiny SQLite job store (stdlib sqlite3, run in a thread so the event loop stays free)."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import Any, Optional

_COLUMNS = (
    "job_id", "status", "identifier_from_purchaser", "input_json", "blockchain_identifier",
    "payment_json", "result_json", "result_hash", "result_submitted", "error", "created_at", "updated_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    identifier_from_purchaser TEXT,
    input_json TEXT NOT NULL,
    blockchain_identifier TEXT,
    payment_json TEXT,
    result_json TEXT,
    result_hash TEXT,
    result_submitted INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


class JobStore:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _create(self, job: dict[str, Any]) -> None:
        now = time.time()
        row = {c: job.get(c) for c in _COLUMNS}
        row.update(result_submitted=job.get("result_submitted", 0), created_at=now, updated_at=now)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO jobs ({','.join(_COLUMNS)}) VALUES ({','.join(':' + k for k in _COLUMNS)})", row
            )

    def _get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(r) if r else None

    def _update(self, job_id: str, fields: dict[str, Any]) -> None:
        bad = set(fields) - set(_COLUMNS)
        if bad:
            raise ValueError(f"unknown columns: {bad}")
        fields = {**fields, "updated_at": time.time()}
        sets = ",".join(f"{k} = :{k}" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE job_id = :job_id", {**fields, "job_id": job_id})

    async def create(self, job: dict[str, Any]) -> None:
        await asyncio.to_thread(self._create, job)

    async def get(self, job_id: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._get, job_id)

    async def update(self, job_id: str, **fields: Any) -> None:
        await asyncio.to_thread(self._update, job_id, fields)
