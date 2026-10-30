"""SQLite store + matching for Agent 2.

Candidate search uses a bounding-box prefilter in SQL and exact polygon distance (shapely, EPSG:25832
metres) in Python. Everything for one registration runs in a single IMMEDIATE transaction, so two
simultaneous registrations cannot miss each other. Swap this module for PostGIS (`ST_DWithin`) when
the pool outgrows a few thousand parcels.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from shapely import wkt

from .config import MatcherSettings
from .teaser import build_teaser

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parcels (
    id TEXT PRIMARY KEY,
    developer_id TEXT NOT NULL,         -- owner account (developer now; farmer accounts later)
    owner_type TEXT NOT NULL DEFAULT 'developer',  -- 'developer' | 'farmer' (farmer land comes later)
    label TEXT,
    project_type TEXT NOT NULL,
    assessed_technology TEXT NOT NULL,
    verdict TEXT NOT NULL,
    score INTEGER NOT NULL,
    area_ha REAL NOT NULL,
    bundesland TEXT,
    municipality TEXT,
    alkis_id TEXT,
    geom_wkt TEXT NOT NULL,          -- EPSG:25832
    geom_geojson TEXT NOT NULL,      -- EPSG:4326, disclosed only after NDA (Agent 3)
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    report_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS parcels_dev ON parcels (developer_id, status);
CREATE INDEX IF NOT EXISTS parcels_bbox ON parcels (status, minx, maxx, miny, maxy);
CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    parcel_a TEXT NOT NULL,
    parcel_b TEXT NOT NULL,
    distance_m REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    UNIQUE (parcel_a, parcel_b)
);
CREATE TABLE IF NOT EXISTS teasers (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL,
    recipient_developer_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    read_at REAL
);
CREATE INDEX IF NOT EXISTS teasers_rcpt ON teasers (recipient_developer_id, read_at);
"""


class QuotaExceeded(Exception):
    pass


class DuplicateParcel(Exception):
    def __init__(self, parcel_id: str):
        super().__init__(parcel_id)
        self.parcel_id = parcel_id


class MatcherStore:
    def __init__(self, path: str, settings: MatcherSettings):
        self.path, self.s = path, settings
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, timeout=15)
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ register + match

    def _register(self, p: dict[str, Any]) -> dict[str, Any]:
        poly = wkt.loads(p["geom_wkt"])
        minx, miny, maxx, maxy = poly.bounds
        r = self.s.match_radius_m
        with self._tx() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM parcels WHERE developer_id=? AND status='active'", (p["developer_id"],)
            ).fetchone()[0]
            if n >= self.s.max_parcels_per_developer:
                raise QuotaExceeded(f"limit of {self.s.max_parcels_per_developer} active parcels reached")

            cands = c.execute(
                "SELECT * FROM parcels WHERE status='active' AND owner_type='developer' AND maxx>=? AND minx<=? AND maxy>=? AND miny<=?",
                (minx - r, maxx + r, miny - r, maxy + r),
            ).fetchall()
            found: list[tuple[sqlite3.Row, float]] = []
            for row in cands:
                other = wkt.loads(row["geom_wkt"])
                if row["developer_id"] == p["developer_id"]:
                    inter = poly.intersection(other).area
                    if inter / min(poly.area, other.area) >= self.s.duplicate_overlap_ratio:
                        raise DuplicateParcel(row["id"])
                    continue  # own parcels are never matched with each other
                d = poly.distance(other)
                if d <= r:
                    found.append((row, d))

            now = time.time()
            c.execute(
                """INSERT INTO parcels (id, developer_id, owner_type, label, project_type, assessed_technology, verdict, score,
                   area_ha, bundesland, municipality, alkis_id, geom_wkt, geom_geojson, minx, miny, maxx, maxy,
                   report_json, created_at) VALUES (:id,:developer_id,:owner_type,:label,:project_type,:assessed_technology,
                   :verdict,:score,:area_ha,:bundesland,:municipality,:alkis_id,:geom_wkt,:geom_geojson,
                   :minx,:miny,:maxx,:maxy,:report_json,:created_at)""",
                {"owner_type": "developer", **p, "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy, "created_at": now},
            )
            own_teasers = []
            for row, d in sorted(found, key=lambda t: t[1]):
                other = dict(row)
                match_id = uuid.uuid4().hex
                a, b = sorted((p["id"], other["id"]))
                c.execute(
                    "INSERT INTO matches (id, parcel_a, parcel_b, distance_m, created_at) VALUES (?,?,?,?,?)",
                    (match_id, a, b, round(d, 1), now),
                )
                to_new = build_teaser(match_id, own=p, other=other, distance_m=d)
                to_old = build_teaser(match_id, own=other, other=p, distance_m=d)
                for dev, t in ((p["developer_id"], to_new), (other["developer_id"], to_old)):
                    c.execute(
                        "INSERT INTO teasers (id, match_id, recipient_developer_id, payload_json, created_at) VALUES (?,?,?,?,?)",
                        (t["teaser_id"], match_id, dev, json.dumps(t), now),
                    )
                own_teasers.append(to_new)
        return {"parcel_id": p["id"], "teasers": own_teasers}

    # ------------------------------------------------------------------ reads / withdraw

    def _teasers(self, developer_id: str, unread_only: bool, mark_read: bool) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute(
                """SELECT t.* FROM teasers t JOIN matches m ON m.id = t.match_id
                   WHERE t.recipient_developer_id=? AND m.status='open' {}
                   ORDER BY t.created_at""".format("AND t.read_at IS NULL" if unread_only else ""),
                (developer_id,),
            ).fetchall()
            if mark_read and rows:
                c.executemany("UPDATE teasers SET read_at=? WHERE id=? AND read_at IS NULL",
                              [(time.time(), r["id"]) for r in rows])
        return [json.loads(r["payload_json"]) for r in rows]

    def _parcels(self, developer_id: str) -> list[dict[str, Any]]:
        with self._tx() as c:
            rows = c.execute(
                "SELECT id,label,project_type,assessed_technology,verdict,score,area_ha,bundesland,status,created_at "
                "FROM parcels WHERE developer_id=? ORDER BY created_at", (developer_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _withdraw(self, parcel_id: str, developer_id: str) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE parcels SET status='withdrawn' WHERE id=? AND developer_id=? AND status='active'",
                (parcel_id, developer_id),
            )
            if cur.rowcount == 0:
                return False
            c.execute("UPDATE matches SET status='closed' WHERE parcel_a=? OR parcel_b=?", (parcel_id, parcel_id))
        return True

    def _get_parcel(self, parcel_id: str, developer_id: str) -> Optional[dict[str, Any]]:
        with self._tx() as c:
            r = c.execute("SELECT * FROM parcels WHERE id=? AND developer_id=?", (parcel_id, developer_id)).fetchone()
        return dict(r) if r else None

    # async wrappers
    async def register(self, parcel: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._register, parcel)

    async def teasers(self, developer_id: str, unread_only: bool = False, mark_read: bool = True):
        return await asyncio.to_thread(self._teasers, developer_id, unread_only, mark_read)

    async def parcels(self, developer_id: str):
        return await asyncio.to_thread(self._parcels, developer_id)

    async def withdraw(self, parcel_id: str, developer_id: str) -> bool:
        return await asyncio.to_thread(self._withdraw, parcel_id, developer_id)

    async def get_parcel(self, parcel_id: str, developer_id: str):
        return await asyncio.to_thread(self._get_parcel, parcel_id, developer_id)
