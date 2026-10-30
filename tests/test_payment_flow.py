"""Offline check of the BYPASS_PAYMENT=false orchestration against a fake Masumi payment service.

Only the payment service and the GIS engine are stubbed here (to test job/payment state handling
without a wallet or network). The real GIS checks are exercised by ../test_worker.py.

    python tests/test_payment_flow.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402
from app.gis_engine import GisResult  # noqa: E402
from app.main import create_app  # noqa: E402
from app.masumi import PaymentClient, output_hash  # noqa: E402

AGENT_ID = "a" * 60
BCID = "block-ident-123"
calls: list[tuple[str, dict]] = []
state = {"onChainState": None}


def fake_service(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    path = request.url.path
    calls.append((path, body))
    assert request.headers["token"] == "key", "missing/incorrect token header"
    if path == "/api/v1/payment/":
        assert body["agentIdentifier"] == AGENT_ID and len(body["inputHash"]) == 64
        return httpx.Response(200, json={"status": "success", "data": {
            "blockchainIdentifier": BCID, "payByTime": "2026-10-31T10:00:00.000Z",
            "submitResultTime": "2026-10-31T22:00:00.000Z", "unlockTime": "2026-11-01T10:00:00.000Z",
            "externalDisputeUnlockTime": "2026-11-01T22:00:00.000Z",
            "SmartContractWallet": {"walletVkey": "vkey123"}}})
    if path == "/api/v1/payment/resolve-blockchain-identifier":
        return httpx.Response(200, json={"data": dict(state)})
    if path == "/api/v1/payment/submit-result":
        return httpx.Response(200, json={"status": "success"})
    return httpx.Response(404)


class StubEngine:
    async def run(self, site):
        return GisResult(errors={k: "stubbed" for k in ("admin", "osm", "protected", "solar", "wind", "slope")})


async def main():
    s = Settings(bypass_payment=False, agent_identifier=AGENT_ID, masumi_api_key="key", internal_api_key="ikey",
                 database_path=f"{tempfile.mkdtemp()}/jobs.sqlite3")
    app = create_app(s)
    async with app.router.lifespan_context(app):
        svc = app.state.jobs
        svc.engine = StubEngine()
        svc.payment = PaymentClient(httpx.AsyncClient(transport=httpx.MockTransport(fake_service)), s)
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        r = await c.post("/start_job", json={"identifier_from_purchaser": "b0e7e7d00000000001",
                                             "input_data": {"lat": 52.1, "lon": 13.1}})
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["status"] == "awaiting_payment" and job["blockchainIdentifier"] == BCID, job
        assert isinstance(job["payByTime"], int) and job["sellerVKey"] == "vkey123", job

        st = (await c.get("/status", params={"job_id": job["job_id"]})).json()
        assert st["status"] == "awaiting_payment", st            # not on chain yet
        state["onChainState"] = "FundsLocked"
        st = (await c.get("/status", params={"job_id": job["job_id"]})).json()
        assert st["status"] == "running", st
        await svc._tasks[job["job_id"]]                           # let the background pipeline finish
        st = (await c.get("/status", params={"job_id": job["job_id"]})).json()
        assert st["status"] == "completed" and st["result_hash_submitted"], st

        submitted = [b for p, b in calls if p.endswith("/submit-result")]
        assert len(submitted) == 1
        assert submitted[0]["submitResultHash"] == output_hash(st["result"], "b0e7e7d00000000001")
        assert submitted[0]["blockchainIdentifier"] == BCID
        print("payment flow OK:", st["status"], "hash", st["result_hash"][:16] + "…")

        bad = await c.post("/start_job", json={"lat": 10, "lon": 13})
        assert bad.status_code == 422, bad.text                   # outside Germany
        bad = await c.post("/start_job", json={"identifier_from_purchaser": "buyer-id-0000001",
                                               "input_data": {"lat": 52.1, "lon": 13.1}})
        assert bad.status_code == 422 and "hex" in bad.text, bad.text  # payment service only accepts hex
        print("validation OK")

        # Agent 2's unpaid path: works in paid mode with the key, never touches the payment service.
        n = len(calls)
        geo = {"lat": 52.1, "lon": 13.1}
        r = await c.post("/internal/audit", json=geo, headers={"X-API-Key": "ikey"})
        assert r.status_code == 200 and r.json()["status"] == "completed" and "overall_verdict" in r.json()["report"], r.text
        assert len(calls) == n, "internal audit must not call the payment service"
        assert (await c.post("/internal/audit", json=geo, headers={"X-API-Key": "wrong"})).status_code == 401
        assert (await c.post("/internal/audit", json=geo)).status_code == 401
        assert (await c.post("/internal/audit", json={"lat": 10, "lon": 13}, headers={"X-API-Key": "ikey"})).status_code == 422
        print("internal audit OK")
        await c.aclose()


asyncio.run(main())
