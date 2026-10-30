"""FastAPI entrypoint: `uvicorn app.main:app`."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .config import Settings, get_settings
from .db import JobStore
from .gis_engine import GisEngine
from .masumi import JobService, PaymentClient, build_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(s.http_timeout_s, connect=10.0),
            headers={"User-Agent": s.user_agent},
            limits=httpx.Limits(max_connections=40),
        ) as client:
            app.state.jobs = JobService(JobStore(s.database_path), GisEngine(client, s), PaymentClient(client, s), s)
            logging.getLogger("land-audit").info(
                "ready — network=%s bypass_payment=%s", s.masumi_network, s.bypass_payment
            )
            yield

    app = FastAPI(title="Land Audit Worker (Masumi agent)", version="0.1.0", lifespan=lifespan)
    app.include_router(build_router())

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


app = create_app()
