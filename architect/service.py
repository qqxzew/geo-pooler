"""Agent 4 service: fetch the revealed deal from Agent 3, compute the pool plan, render both German documents."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .config import ArchitectSettings
from .docs import DocParams, build_grid_application_pdf, build_pool_agreement_pdf
from .model import Assumptions, Method, compute_plan, parcel_from_package
from .store import ArchitectStore


class PoolError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class PoolService:
    def __init__(self, store: ArchitectStore, client: httpx.AsyncClient, s: ArchitectSettings):
        self.store, self.http, self.s = store, client, s

    async def _package(self, match_id: str) -> dict[str, Any]:
        try:
            r = await self.http.get(f"{self.s.escrow_url}/internal/deals/{match_id}/package",
                                    headers={"X-API-Key": self.s.escrow_api_key}, timeout=20)
        except httpx.HTTPError as e:
            raise PoolError(502, f"escrow agent unreachable: {e}")
        if r.status_code in (404, 409):
            raise PoolError(r.status_code, r.json().get("detail", "deal not available"))
        if r.status_code != 200:
            raise PoolError(502, f"escrow agent answered {r.status_code}")
        return r.json()

    @staticmethod
    def _check_member(package: dict[str, Any], dev: str) -> None:
        if dev not in {p["developer_id"] for p in package["parties"]}:
            raise PoolError(403, "you are not a party of this deal")

    @staticmethod
    def _grid_hint(package: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        best: Optional[tuple[float, Optional[str], Optional[str]]] = None
        for p in package["parties"]:
            g = p["parcel"]["report"].get("grid_access", {})
            d = g.get("nearest_substation_m")
            if d is not None and (best is None or d < best[0]):
                best = (d, g.get("substation_operator"), g.get("substation_name"))
        return (best[1], best[2]) if best else (None, None)

    async def create(self, match_id: str, dev: str, method: Method, assumptions: Assumptions) -> dict[str, Any]:
        package = await self._package(match_id)
        self._check_member(package, dev)
        if len(package["parties"]) != 2:
            raise PoolError(500, "package must contain exactly two parties")
        try:
            a = parcel_from_package("A", package["parties"][0])
            b = parcel_from_package("B", package["parties"][1])
            plan = await asyncio.to_thread(compute_plan, a, b, assumptions, method)
        except (KeyError, ValueError) as e:
            raise PoolError(422, f"cannot build a plan from the revealed data: {e}")

        prev = await self.store.load(match_id)
        version = (prev["version"] + 1) if prev else 1
        now = datetime.now(timezone.utc).replace(microsecond=0)
        params = DocParams(
            pool_id=f"POOL-{match_id[:8].upper()}-V{version}", match_id=match_id, created_at=now,
            platform_name=self.s.platform_name, nda_sha256=package["signed_nda"]["document_sha256"],
            nda_id=package["signed_nda"]["nda_id"], jurisdiction_city=self.s.pool_jurisdiction_city,
            exclusivity_months=self.s.pool_exclusivity_months, term_years=self.s.pool_term_years)
        operator, substation = self._grid_hint(package)
        agreement = await asyncio.to_thread(build_pool_agreement_pdf, plan, package, params)
        grid = await asyncio.to_thread(build_grid_application_pdf, plan, package, params, operator, substation)
        record = {
            "match_id": match_id, "version": version, "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "requested_by": dev, "plan": plan, "grid_operator": operator, "substation": substation,
            "documents": {
                "flaechenpoolvertrag": {"template": "POOL-DE-1.0", "sha256": hashlib.sha256(agreement).hexdigest()},
                "netzanschlussbegehren": {"template": "NAB-DE-1.0", "sha256": hashlib.sha256(grid).hexdigest()},
            },
            "based_on_nda_sha256": package["signed_nda"]["document_sha256"],
        }
        await self.store.save(match_id, version, record, agreement, grid)
        return self._view(record, match_id)

    @staticmethod
    def _view(rec: dict[str, Any], match_id: str) -> dict[str, Any]:
        out = {k: v for k, v in rec.items() if k != "documents"}
        out["documents"] = {
            "flaechenpoolvertrag": {**rec["documents"]["flaechenpoolvertrag"], "download": f"/pools/{match_id}/agreement.pdf"},
            "netzanschlussbegehren": {**rec["documents"]["netzanschlussbegehren"], "download": f"/pools/{match_id}/grid_application.pdf"},
        }
        return out

    async def _load(self, match_id: str, dev: str) -> dict[str, Any]:
        pool = await self.store.load(match_id)
        if pool is None:
            raise PoolError(404, "no pool plan yet for this match; POST /pools/{match_id} first")
        package = await self._package(match_id)  # membership is checked against the live deal
        self._check_member(package, dev)
        return pool

    async def get(self, match_id: str, dev: str) -> dict[str, Any]:
        pool = await self._load(match_id, dev)
        return self._view(pool["data"], match_id)

    async def agreement_pdf(self, match_id: str, dev: str) -> tuple[bytes, str]:
        pool = await self._load(match_id, dev)
        return pool["agreement_pdf"], f"Flaechenpoolvertrag_{match_id[:8]}_v{pool['version']}_entwurf.pdf"

    async def grid_pdf(self, match_id: str, dev: str) -> tuple[bytes, str]:
        pool = await self._load(match_id, dev)
        return pool["grid_pdf"], f"Netzanschlussbegehren_{match_id[:8]}_v{pool['version']}_entwurf.pdf"
