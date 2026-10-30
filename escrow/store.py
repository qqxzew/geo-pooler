"""SQLite store for Agent 3: developer profiles and one JSON document (+ PDFs) per deal."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS developers (
    developer_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deals (
    match_id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL,
    nda_pdf BLOB, signed_pdf BLOB, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deal_parties (
    match_id TEXT NOT NULL, developer_id TEXT NOT NULL, PRIMARY KEY (match_id, developer_id));
CREATE INDEX IF NOT EXISTS deal_parties_dev ON deal_parties (developer_id);
"""


class EscrowStore:
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

    # developers
    def _put_dev(self, dev: str, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO developers VALUES (?,?,?) ON CONFLICT(developer_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                      (dev, json.dumps(data), time.time()))

    def _get_dev(self, dev: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT data FROM developers WHERE developer_id=?", (dev,)).fetchone()
        return json.loads(r["data"]) if r else None

    # deals
    def _save_deal(self, match_id: str, status: str, data: dict, nda_pdf: Optional[bytes], signed_pdf: Optional[bytes]) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO deals (match_id,status,data,nda_pdf,signed_pdf,created_at,updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(match_id) DO UPDATE SET status=excluded.status, data=excluded.data, nda_pdf=excluded.nda_pdf,
                   signed_pdf=excluded.signed_pdf, updated_at=excluded.updated_at""",
                (match_id, status, json.dumps(data), nda_pdf, signed_pdf, now, now))
            for dev in data.get("developers", []):
                c.execute("INSERT OR IGNORE INTO deal_parties VALUES (?,?)", (match_id, dev))

    def _load_deal(self, match_id: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM deals WHERE match_id=?", (match_id,)).fetchone()
        if not r:
            return None
        return {"match_id": r["match_id"], "status": r["status"], "data": json.loads(r["data"]),
                "nda_pdf": r["nda_pdf"], "signed_pdf": r["signed_pdf"]}

    def _deals_of(self, dev: str) -> list[str]:
        with self._conn() as c:
            return [r["match_id"] for r in c.execute("SELECT match_id FROM deal_parties WHERE developer_id=?", (dev,))]

    async def put_developer(self, dev: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._put_dev, dev, data)

    async def get_developer(self, dev: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._get_dev, dev)

    async def save_deal(self, deal: dict[str, Any]) -> None:
        await asyncio.to_thread(self._save_deal, deal["match_id"], deal["status"], deal["data"], deal.get("nda_pdf"), deal.get("signed_pdf"))

    async def load_deal(self, match_id: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._load_deal, match_id)

    async def deals_of(self, dev: str) -> list[str]:
        return await asyncio.to_thread(self._deals_of, dev)
