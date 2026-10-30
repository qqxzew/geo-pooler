"""Agent 2 (Matcher): `uvicorn matcher.main:app --port 8001`.

Registers a developer's parcel (after Agent 1 says it is usable), finds other developers' parcels within
1.5 km and drops an anonymous teaser into both sides' inbox. Consent, escrow and NDA are Agent 3.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import Field
from shapely.geometry import mapping

from app.config import get_settings as get_validator_settings
from app.gis_engine import build_site
from app.models import LandCheckInput

from .config import MatcherSettings, get_matcher_settings
from .store import DuplicateParcel, MatcherStore, QuotaExceeded

log = logging.getLogger("matcher")


class RegisterBody(LandCheckInput):
    developer_id: str = Field(min_length=3, max_length=64, pattern=r"^[\w.@:-]+$")
    label: Optional[str] = Field(None, max_length=120)


async def run_validator(client: httpx.AsyncClient, s: MatcherSettings, inp: LandCheckInput) -> dict[str, Any]:
    """Ask Agent 1 for the audit report of this geometry. Returns the report dict."""
    payload = inp.model_dump(mode="json", exclude_none=True)
    r = await client.post(f"{s.validator_url.rstrip('/')}/start_job", json=payload, timeout=s.validator_poll_timeout_s)
    if r.status_code == 422:
        raise HTTPException(422, detail=r.json().get("detail"))
    r.raise_for_status()
    job = r.json()
    deadline = time.monotonic() + s.validator_poll_timeout_s
    while job.get("status") not in ("completed", "failed"):
        if job.get("status") == "awaiting_payment" or time.monotonic() > deadline:
            # Paid validation is the orchestrator's job later; here the validator must run in bypass mode.
            raise HTTPException(504, detail="validator did not complete (is it waiting for payment?)")
        await asyncio.sleep(3)
        job = (await client.get(f"{s.validator_url.rstrip('/')}/status", params={"job_id": job["job_id"]})).json()
    if job["status"] == "failed" or "report" not in job:
        raise HTTPException(502, detail=f"validator failed: {job.get('error')}")
    return job["report"]


def create_app(settings: Optional[MatcherSettings] = None, transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    s = settings or get_matcher_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(transport=transport) as client:
            app.state.client = client
            app.state.store = MatcherStore(s.matcher_database_path, s)
            if not s.matcher_api_key:
                log.warning("MATCHER_API_KEY is empty: authentication disabled (dev only)")
            yield

    app = FastAPI(title="Pool Matcher (Agent 2)", version="0.1.0", lifespan=lifespan)

    async def auth(x_api_key: str = Header(default="")) -> None:
        if s.matcher_api_key and not hmac.compare_digest(x_api_key, s.matcher_api_key):
            raise HTTPException(401, detail="invalid API key")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/register_parcel", dependencies=[Depends(auth)])
    async def register_parcel(request: Request, body: RegisterBody):
        store: MatcherStore = request.app.state.store
        inp = LandCheckInput(**body.model_dump(include=set(LandCheckInput.model_fields)))
        try:
            site = build_site(inp, get_validator_settings().model_copy(update={"max_area_ha": s.max_area_ha}))
        except ValueError as e:
            raise HTTPException(422, detail=str(e))
        if site.area_ha < s.min_area_ha:
            return {"status": "rejected", "verdict": None, "score": None,
                    "reasons": [f"parcel too small ({site.area_ha:.2f} ha < {s.min_area_ha} ha minimum)"]}

        report = await run_validator(request.app.state.client, s, inp)
        if report["overall_verdict"] in s.rejected_verdicts:
            return {"status": "rejected", "verdict": report["overall_verdict"], "score": report["score"],
                    "reasons": report.get("warnings") or ["audit verdict NO_GO"]}

        loc = report["location_info"]
        parcel = {
            "id": uuid.uuid4().hex, "developer_id": body.developer_id, "label": body.label,
            "project_type": inp.project_type, "assessed_technology": report.get("assessed_technology") or inp.project_type,
            "verdict": report["overall_verdict"], "score": report["score"], "area_ha": round(site.area_ha, 2),
            "bundesland": loc.get("bundesland"), "municipality": loc.get("municipality"),
            "alkis_id": loc.get("alkis_parcel_id"), "geom_wkt": site.poly_utm.wkt,
            "geom_geojson": _dumps(mapping(site.poly_wgs)), "report_json": _dumps(report),
        }
        try:
            res = await store.register(parcel)
        except DuplicateParcel as e:
            raise HTTPException(409, detail=f"you already registered this area (parcel {e.parcel_id})")
        except QuotaExceeded as e:
            raise HTTPException(429, detail=str(e))
        return {"status": "registered", "parcel_id": res["parcel_id"],
                "audit": {"verdict": parcel["verdict"], "score": parcel["score"], "technology": parcel["assessed_technology"]},
                "new_matches": len(res["teasers"]), "teasers": res["teasers"]}

    @app.get("/teasers", dependencies=[Depends(auth)])
    async def teasers(request: Request, developer_id: str = Query(...), unread_only: bool = False, mark_read: bool = True):
        items = await request.app.state.store.teasers(developer_id, unread_only, mark_read)
        return {"count": len(items), "teasers": items}

    @app.get("/parcels", dependencies=[Depends(auth)])
    async def parcels(request: Request, developer_id: str = Query(...)):
        return {"parcels": await request.app.state.store.parcels(developer_id)}

    @app.delete("/parcels/{parcel_id}", dependencies=[Depends(auth)])
    async def withdraw(request: Request, parcel_id: str, developer_id: str = Query(...)):
        if not await request.app.state.store.withdraw(parcel_id, developer_id):
            raise HTTPException(404, detail="no active parcel with this id for this developer")
        return {"status": "withdrawn", "parcel_id": parcel_id}

    @app.get("/internal/matches/{match_id}", dependencies=[Depends(auth)])
    async def internal_match(request: Request, match_id: str):
        """Un-anonymised match data for Agent 3 (escrow/NDA). Refuses to run without an API key."""
        if not s.matcher_api_key:
            raise HTTPException(503, detail="set MATCHER_API_KEY to enable the internal match endpoint")
        rec = await request.app.state.store.get_match(match_id)
        if rec is None:
            raise HTTPException(404, detail="unknown match")
        keep = ("id", "developer_id", "label", "assessed_technology", "verdict", "score", "area_ha", "bundesland",
                "municipality", "alkis_id", "status")
        parcels = [{**{k: p[k] for k in keep}, "geometry": json.loads(p["geom_geojson"]),
                    "report": json.loads(p["report_json"])} for p in rec["parcels"]]
        return {"match": {"id": rec["match"]["id"], "status": rec["match"]["status"],
                          "distance_m": rec["match"]["distance_m"]}, "parcels": parcels}

    return app


def _dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)


app = create_app()
