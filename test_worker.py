"""Live end-to-end test: three real locations in Germany, full JSON reports printed.

    python test_worker.py                      # in-process (BYPASS_PAYMENT forced on, temp DB)
    python test_worker.py --url http://localhost:8000   # against a running worker

Needs internet access: it calls the real BKG / OSM / BfN(EEA) / PVGIS / Open-Meteo / ALKIS endpoints.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

LOCATIONS = [
    {
        "name": "Wind site — Brandenburg, open land inside an existing wind-farm cluster (Fläming)",
        "payload": {"lat": 52.0746, "lon": 12.7329, "radius_m": 300, "project_type": "wind"},
        "expect": "wind-friendly: strong wind, wind farm nearby, no protected-area veto",
    },
    {
        "name": "Solar site — Bavaria, strip beside Autobahn A9 (Manching / Oberstimm)",
        "payload": {"lat": 48.7286, "lon": 11.4770, "radius_m": 250, "project_type": "solar"},
        "expect": "EEG highway corridor = true",
    },
    {
        "name": "Forbidden site — inside Nationalpark Berchtesgaden",
        "payload": {"lat": 47.5500, "lon": 12.9000, "radius_m": 200, "project_type": "hybrid"},
        "expect": "environmental veto -> NO_GO",
    },
]


async def run(base_url: str | None) -> int:
    if base_url:
        client = httpx.AsyncClient(base_url=base_url, timeout=240)
        lifespan = None
    else:
        sys.path.insert(0, str(Path(__file__).parent))
        from app.config import Settings
        from app.main import create_app

        tmp = tempfile.mkdtemp(prefix="land-audit-test-")
        app = create_app(Settings(bypass_payment=True, database_path=f"{tmp}/jobs.sqlite3"))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=240)

    failures = 0
    try:
        print("availability:", (await client.get("/availability")).json())
        for loc in LOCATIONS:
            print("\n" + "=" * 100 + f"\n{loc['name']}\nexpect: {loc['expect']}\n" + "=" * 100)
            t0 = time.time()
            r = await client.post("/start_job", json=loc["payload"])
            if r.status_code != 200:
                print("start_job FAILED:", r.status_code, r.text)
                failures += 1
                continue
            job = r.json()
            if job["status"] != "completed":  # only when payment is enabled
                job = (await client.get("/status", params={"job_id": job["job_id"]})).json()
            print(f"job {job['job_id']} -> {job['status']} in {time.time() - t0:.1f}s")
            report = job.get("report")
            if not report:
                print("no report:", job)
                failures += 1
                continue
            print(json.dumps(report, indent=2, ensure_ascii=False))
            print(f"\n>>> VERDICT {report['overall_verdict']}  SCORE {report['score']}/100  hash={job['result_hash'][:16]}…")
            if "unavailable" in " ".join(report["data_quality"].values()):
                print("    (some data sources were unavailable — see data_quality)")
    finally:
        await client.aclose()
        if lifespan:
            await lifespan.__aexit__(None, None, None)
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="base URL of a running worker (default: run in-process)")
    sys.exit(1 if asyncio.run(run(ap.parse_args().url)) else 0)
