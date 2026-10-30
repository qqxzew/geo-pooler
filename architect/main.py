"""Agent 4 (Pool Architect): `uvicorn architect.main:app --port 8003`.

Takes the two revealed parcels of a signed deal (from Agent 3), computes the joint project and each firm's share,
and renders a draft Flächenpoolvertrag and a Netzanschlussbegehren as PDF.
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .config import ArchitectSettings, get_architect_settings
from .model import Assumptions, Method
from .service import PoolError, PoolService
from .store import ArchitectStore

log = logging.getLogger("architect")
DEV_ID = Field(min_length=3, max_length=64, pattern=r"^[\w.@:-]+$")


class PoolRequest(BaseModel):
    developer_id: str = DEV_ID
    method: Method = Field("shapley", description="shapley (default) | area | revenue — how the shares are derived")
    assumptions: Assumptions = Field(default_factory=Assumptions, description="override any screening assumption")


def create_app(settings: Optional[ArchitectSettings] = None, transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    s = settings or get_architect_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(transport=transport) as client:
            app.state.pools = PoolService(ArchitectStore(s.architect_database_path), client, s)
            if not s.architect_api_key:
                log.warning("ARCHITECT_API_KEY is empty: authentication disabled (dev only)")
            if not s.escrow_api_key:
                log.error("ESCROW_API_KEY is empty: Agent 3 will refuse the package request")
            yield

    app = FastAPI(title="Pool Architect (Agent 4)", version="0.1.0", lifespan=lifespan)

    async def auth(x_api_key: str = Header(default="")) -> None:
        if s.architect_api_key and not hmac.compare_digest(x_api_key, s.architect_api_key):
            raise HTTPException(401, detail="invalid API key")

    def svc(request: Request) -> PoolService:
        return request.app.state.pools

    async def guard(coro):
        try:
            return await coro
        except PoolError as e:
            raise HTTPException(e.status, detail=e.message)

    def pdf(data: tuple[bytes, str]) -> Response:
        return Response(data[0], media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{data[1]}"'})

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/pools/{match_id}", dependencies=[Depends(auth)])
    async def create_pool(request: Request, match_id: str, body: PoolRequest):
        """(Re)compute the pool plan for a revealed deal; each call creates a new version of plan and documents."""
        return await guard(svc(request).create(match_id, body.developer_id, body.method, body.assumptions))

    @app.get("/pools/{match_id}", dependencies=[Depends(auth)])
    async def get_pool(request: Request, match_id: str, developer_id: str = Query(...)):
        return await guard(svc(request).get(match_id, developer_id))

    @app.get("/pools/{match_id}/agreement.pdf", dependencies=[Depends(auth)])
    async def agreement(request: Request, match_id: str, developer_id: str = Query(...)):
        return pdf(await guard(svc(request).agreement_pdf(match_id, developer_id)))

    @app.get("/pools/{match_id}/grid_application.pdf", dependencies=[Depends(auth)])
    async def grid_application(request: Request, match_id: str, developer_id: str = Query(...)):
        return pdf(await guard(svc(request).grid_pdf(match_id, developer_id)))

    return app


app = create_app()
