"""Masumi (MIP-003) agent endpoints + client for the Masumi payment service (Cardano Preprod)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import canonicaljson
import httpx
from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from pydantic import ValidationError

from .config import Settings
from .db import JobStore
from .gis_engine import GisEngine, build_site
from .models import AuditReport, LandCheckInput
from .scoring import build_report

log = logging.getLogger("land-audit")

PAID_STATES = {"FundsLocked", "ResultSubmitted", "WithdrawAuthorized", "Withdrawn"}
DEAD_STATES = {"FundsOrDatumInvalid", "RefundRequested", "RefundAuthorized", "RefundWithdrawn", "Disputed", "DisputedWithdrawn"}


# ------------------------------------------------------------------ MIP-004 hashing


def _hash_payload(payload: str, identifier_from_purchaser: str) -> str:
    return hashlib.sha256(f"{identifier_from_purchaser};{payload}".encode("utf-8")).hexdigest()


def input_hash(input_data: dict[str, Any], identifier_from_purchaser: str) -> str:
    """MIP-004: sha256("<identifier>;<JCS(input)>")."""
    return _hash_payload(canonicaljson.encode_canonical_json(input_data).decode("utf-8"), identifier_from_purchaser)


def output_hash(result: str, identifier_from_purchaser: str) -> str:
    """MIP-004: sha256("<identifier>;<json-escaped result string>") — what the buyer verifies on-chain."""
    return _hash_payload(json.dumps(result, ensure_ascii=False)[1:-1], identifier_from_purchaser)


def canonical_report(report: AuditReport) -> str:
    """Stable string form of the report: sorted keys, no whitespace."""
    return json.dumps(report.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------ payment-service client


def _unix(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return v
    return v


class PaymentClient:
    def __init__(self, client: httpx.AsyncClient, s: Settings):
        self.http, self.s = client, s
        self.base = s.masumi_payment_service_url.rstrip("/") + "/api/v1/payment"
        self.headers = {"token": s.masumi_api_key, "Content-Type": "application/json"}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post(f"{self.base}{path}", json=payload, headers=self.headers, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"payment service {path or '/'} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    async def create(self, identifier: str, in_hash: str, metadata: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"  # noqa: E731
        payload: dict[str, Any] = {
            "agentIdentifier": self.s.agent_identifier,
            "network": self.s.masumi_network,
            "paymentType": "Web3CardanoV1",
            "payByTime": fmt(now + timedelta(hours=12)),
            "submitResultTime": fmt(now + timedelta(hours=24)),
            "identifierFromPurchaser": identifier,
            "inputHash": in_hash,
            "metadata": metadata,
        }
        if self.s.masumi_payment_source_type:
            payload["paymentSourceType"] = self.s.masumi_payment_source_type
            payload.pop("paymentType")
        if self.s.masumi_supported_payment_source_index:
            payload["supportedPaymentSourceIndex"] = int(self.s.masumi_supported_payment_source_index)
        if self.s.masumi_requested_funds_lovelace:
            payload["RequestedFunds"] = [{"amount": self.s.masumi_requested_funds_lovelace, "unit": ""}]
        return (await self._post("/", payload))["data"]

    async def state(self, blockchain_identifier: str) -> Optional[str]:
        body = await self._post(
            "/resolve-blockchain-identifier",
            {"network": self.s.masumi_network, "blockchainIdentifier": blockchain_identifier, "includeHistory": "false"},
        )
        return (body.get("data") or {}).get("onChainState")

    async def submit_result(self, blockchain_identifier: str, result_hash: str) -> None:
        await self._post(
            "/submit-result",
            {"network": self.s.masumi_network, "blockchainIdentifier": blockchain_identifier, "submitResultHash": result_hash},
        )


# ------------------------------------------------------------------ job service


class JobService:
    def __init__(self, store: JobStore, engine: GisEngine, payment: PaymentClient, settings: Settings):
        self.store, self.engine, self.payment, self.s = store, engine, payment, settings
        self._tasks: dict[str, asyncio.Task] = {}

    async def run_pipeline(self, job_id: str) -> None:
        """Run the GIS audit for a stored job, persist the report and (when paid) submit its hash."""
        job = await self.store.get(job_id)
        if job is None:
            return
        try:
            await self.store.update(job_id, status="running")
            inp = LandCheckInput.model_validate_json(job["input_json"])
            site = build_site(inp, self.s)
            gis = await self.engine.run(site)
            result = canonical_report(build_report(inp, site, gis))
            rhash = output_hash(result, job["identifier_from_purchaser"])
            await self.store.update(job_id, status="completed", result_json=result, result_hash=rhash, error=None)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            await self.store.update(job_id, status="failed", error=f"{type(e).__name__}: {e}"[:500])
            return
        await self.submit_hash(job_id)

    async def submit_hash(self, job_id: str) -> None:
        job = await self.store.get(job_id)
        if not job or self.s.bypass_payment or job["result_submitted"] or not job["blockchain_identifier"]:
            return
        try:
            await self.payment.submit_result(job["blockchain_identifier"], job["result_hash"])
            await self.store.update(job_id, result_submitted=1)
        except Exception as e:  # noqa: BLE001 - retried on the next /status poll
            log.warning("submit-result for %s failed: %s", job_id, e)
            await self.store.update(job_id, error=f"submit-result pending: {e}"[:500])

    def spawn(self, job_id: str) -> None:
        t = self._tasks.get(job_id)
        if t is None or t.done():
            self._tasks[job_id] = asyncio.create_task(self.run_pipeline(job_id))


def _service(request: Request) -> JobService:
    return request.app.state.jobs


# ------------------------------------------------------------------ router


def _view(job: dict[str, Any], extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": job["job_id"], "job_id": job["job_id"], "status": job["status"]}
    if job["result_json"]:
        out["result"] = job["result_json"]  # MIP-003: result is a string
        out["report"] = json.loads(job["result_json"])  # convenience: parsed
        out["result_hash"] = job["result_hash"]
        out["result_hash_submitted"] = bool(job["result_submitted"])
    if job["error"]:
        out["error"] = job["error"]
    out.update(extra or {})
    return out


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/availability")
    async def availability(request: Request):
        s = _service(request).s
        return {"status": "available", "type": "masumi-agent", "network": s.masumi_network,
                "message": "Renewable-energy land due diligence for Germany (wind & solar PV)"}

    @router.get("/input_schema")
    async def input_schema():
        schema = LandCheckInput.model_json_schema()
        schema["examples"] = [{"lat": 52.0717, "lon": 12.6381, "radius_m": 300, "project_type": "hybrid"}]
        num = lambda i, n, d, lo, hi: {  # noqa: E731
            "id": i, "type": "number", "name": n, "data": {"description": d},
            "validations": [{"validation": "min", "value": str(lo)}, {"validation": "max", "value": str(hi)}],
        }
        schema["input_data"] = [  # MIP-003 form description
            num("lat", "Latitude", "WGS84, Germany 47.2..55.1", 47.2, 55.1),
            num("lon", "Longitude", "WGS84, Germany 5.8..15.1", 5.8, 15.1),
            {**num("radius_m", "Radius (m)", "Analysed radius around the point, default 300", 1, 5000),
             "validations": [{"validation": "optional", "value": "true"}]},
            {"id": "project_type", "type": "option", "name": "Project type",
             "data": {"values": ["wind", "solar", "hybrid"], "default": "hybrid"}},
        ]
        return schema

    @router.post("/start_job")
    async def start_job(request: Request, body: dict[str, Any] = Body(...)):
        svc = _service(request)
        s = svc.s
        # Accept MIP-003 {identifier_from_purchaser, input_data:{...}} or a flat body.
        data = body.get("input_data") if isinstance(body.get("input_data"), dict) else {
            k: v for k, v in body.items() if k != "identifier_from_purchaser"
        }
        identifier = body.get("identifier_from_purchaser")
        try:
            inp = LandCheckInput.model_validate(data)
            build_site(inp, s)  # full geometry validation up front -> 422 instead of failed job
        except (ValidationError, ValueError) as e:
            msg = e.errors(include_url=False, include_context=False) if isinstance(e, ValidationError) else str(e)
            raise HTTPException(422, detail=msg)

        job_id = str(uuid.uuid4())
        if not identifier:
            if not s.bypass_payment:
                raise HTTPException(422, detail="identifier_from_purchaser is required (14-26 chars)")
            identifier = job_id.replace("-", "")[:20]
        if not s.bypass_payment and not (14 <= len(identifier) <= 26):
            raise HTTPException(422, detail="identifier_from_purchaser must be 14-26 characters")
        # The payment service rejects anything that is not a hex string; fail fast with a clear 422.
        if not s.bypass_payment and not all(c in "0123456789abcdefABCDEF" for c in identifier):
            raise HTTPException(422, detail="identifier_from_purchaser must be a hex string")

        job = {"job_id": job_id, "status": "awaiting_payment", "identifier_from_purchaser": identifier,
               "input_json": inp.model_dump_json()}

        if s.bypass_payment:
            await svc.store.create({**job, "status": "running"})
            await svc.run_pipeline(job_id)  # immediate, per spec
            done = await svc.store.get(job_id)
            return _view(done, {"identifierFromPurchaser": identifier, "payment": "bypassed (BYPASS_PAYMENT=true)"})

        if not (s.agent_identifier and s.masumi_api_key):
            raise HTTPException(503, detail="AGENT_IDENTIFIER and MASUMI_API_KEY must be set when BYPASS_PAYMENT=false")
        try:
            in_hash = input_hash(inp.model_dump(mode="json"), identifier)
            pay = await svc.payment.create(identifier, in_hash, metadata=f"land-audit job {job_id}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, detail=f"payment service error: {e}")
        await svc.store.create({**job, "blockchain_identifier": pay["blockchainIdentifier"],
                                "payment_json": json.dumps(pay)})
        return {
            "id": job_id, "job_id": job_id, "status": "awaiting_payment",
            "blockchainIdentifier": pay["blockchainIdentifier"],
            "payByTime": _unix(pay.get("payByTime")), "submitResultTime": _unix(pay.get("submitResultTime")),
            "unlockTime": _unix(pay.get("unlockTime")),
            "externalDisputeUnlockTime": _unix(pay.get("externalDisputeUnlockTime")),
            "agentIdentifier": s.agent_identifier,
            "sellerVKey": (pay.get("SmartContractWallet") or {}).get("walletVkey") or pay.get("sellerVKey"),
            "identifierFromPurchaser": identifier, "input_hash": in_hash,
            "price": {"amount": s.price_tada, "unit": "tADA"},
        }

    @router.post("/internal/audit")
    async def internal_audit(request: Request, body: dict[str, Any] = Body(...), x_api_key: str = Header(default="")):
        """Unpaid audit for the platform's own agents (Agent 2). Same report as a paid job; nothing goes on-chain."""
        svc = _service(request)
        if not svc.s.internal_api_key:
            raise HTTPException(503, detail="set INTERNAL_API_KEY to enable the internal audit endpoint")
        if not hmac.compare_digest(x_api_key, svc.s.internal_api_key):
            raise HTTPException(401, detail="invalid API key")
        try:
            inp = LandCheckInput.model_validate(body)
            site = build_site(inp, svc.s)
        except (ValidationError, ValueError) as e:
            msg = e.errors(include_url=False, include_context=False) if isinstance(e, ValidationError) else str(e)
            raise HTTPException(422, detail=msg)
        gis = await svc.engine.run(site)
        return {"status": "completed", "report": json.loads(canonical_report(build_report(inp, site, gis)))}

    @router.get("/status")
    async def status(request: Request, job_id: str = Query(...)):
        svc = _service(request)
        job = await svc.store.get(job_id)
        if job is None:
            raise HTTPException(404, detail="unknown job_id")

        if job["status"] == "awaiting_payment":
            try:
                state = await svc.payment.state(job["blockchain_identifier"])
            except Exception as e:  # noqa: BLE001
                return _view(job, {"payment_state": None, "warning": f"payment status unavailable: {e}"[:300]})
            if state in DEAD_STATES:
                await svc.store.update(job_id, status="failed", error=f"payment state {state}")
                return _view(await svc.store.get(job_id), {"payment_state": state})
            if state in PAID_STATES:
                await svc.store.update(job_id, status="running")
                svc.spawn(job_id)
                return _view(await svc.store.get(job_id), {"payment_state": state})
            return _view(job, {"payment_state": state or "NotYetOnChain"})

        if job["status"] == "running":
            svc.spawn(job_id)  # no-op while the task is alive; restarts it if the worker was restarted mid-run
        if job["status"] == "completed" and not job["result_submitted"] and not svc.s.bypass_payment:
            await svc.submit_hash(job_id)
            job = await svc.store.get(job_id)
        return _view(job)

    return router
