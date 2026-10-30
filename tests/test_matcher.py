"""Offline test of Agent 2 (Matcher) with a stubbed validator (Agent 1).

The validator is stubbed only to keep this deterministic and fast; geometry, distances, bands,
anonymity and the SQLite store are the real code.

    python tests/test_matcher.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from matcher.config import MatcherSettings  # noqa: E402
from matcher.main import create_app  # noqa: E402

TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)
TO_WGS = Transformer.from_crs(25832, 4326, always_xy=True)
x0, y0 = TO_UTM.transform(12.7329, 52.0746)  # Brandenburg
R = 300.0


def at(dx_m: float, dy_m: float = 0.0):
    lon, lat = TO_WGS.transform(x0 + dx_m, y0 + dy_m)
    return {"lat": round(lat, 6), "lon": round(lon, 6), "radius_m": R}


state = {"verdict": "CONDITIONAL_GO", "tech": "solar"}


def fake_validator(request: httpx.Request) -> httpx.Response:
    assert request.url.path in ("/start_job", "/internal/audit")
    if request.url.path == "/internal/audit":
        assert request.headers.get("X-API-Key") == "ikey", "internal audit needs the validator key"
    report = {
        "overall_verdict": state["verdict"], "score": 77 if state["verdict"] != "NO_GO" else 10,
        "assessed_technology": state["tech"], "warnings": [],
        "location_info": {"bundesland": "Brandenburg", "municipality": "Testgemeinde", "alkis_parcel_id": "12-0000-001-1"},
        "environmental_veto": {"has_veto": state["verdict"] == "NO_GO",
                               "intersecting_protected_zones": ["Naturschutzgebiet: Test"] if state["verdict"] == "NO_GO" else []},
        "technology_assessment": {state["tech"]: {"blockers": ["x"] if state["verdict"] == "NO_GO" else []}},
    }
    return httpx.Response(200, json={"job_id": "j1", "status": "completed", "report": report})


async def main():
    s = MatcherSettings(matcher_database_path=f"{tempfile.mkdtemp()}/m.sqlite3", max_parcels_per_developer=3, matcher_api_key="")
    app = create_app(s)
    async with app.router.lifespan_context(app):
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(fake_validator))
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m")

        async def reg(dev, tech, **geo):
            state["tech"] = tech
            return await c.post("/register_parcel", json={"developer_id": dev, **geo})

        # A (dev1, solar)
        r = await reg("dev1", "solar", **at(0)); a = r.json()
        assert r.status_code == 200 and a["status"] == "registered" and a["new_matches"] == 0, r.text

        # B (dev2, wind): 800 m gap to A -> match
        r = await reg("dev2", "wind", **at(2 * R + 800)); b = r.json()
        assert b["new_matches"] == 1, b
        t = b["teasers"][0]
        assert t["distance_band"] == "500-1000 m" and t["neighbour"]["technology"] == "solar", t
        assert "complementary" in t["synergy"], t
        dev1 = (await c.get("/teasers", params={"developer_id": "dev1"})).json()
        assert dev1["count"] == 1 and dev1["teasers"][0]["neighbour"]["technology"] == "wind", dev1

        # anonymity: nothing identifying in either teaser
        blob = json.dumps([t, dev1["teasers"]])
        for forbidden in ("Testgemeinde", "dev1", "dev2", "12-0000", "12.7", "52.0", str(a["parcel_id"]), str(b["parcel_id"])):
            assert forbidden not in blob.replace(t["your_parcel_id"], "").replace(dev1["teasers"][0]["your_parcel_id"], ""), forbidden
        again = (await c.get("/teasers", params={"developer_id": "dev1", "unread_only": True})).json()
        assert again["count"] == 0, "teaser should be marked read"

        # C (dev1) near A on the far side: same developer as A -> never matched with A; too far from B
        r = await reg("dev1", "solar", **at(-(2 * R + 700))); assert r.json()["new_matches"] == 0, r.text
        # D (dev3) far away
        r = await reg("dev3", "wind", **at(25_000)); assert r.json()["new_matches"] == 0, r.text
        # duplicate by the same developer
        r = await reg("dev2", "wind", **at(2 * R + 800)); assert r.status_code == 409, r.text
        # NO_GO is rejected and not stored
        state["verdict"] = "NO_GO"
        r = await reg("dev4", "wind", **at(0, 8000)); assert r.json()["status"] == "rejected" and r.json()["reasons"], r.text
        assert (await c.get("/parcels", params={"developer_id": "dev4"})).json()["parcels"] == []
        state["verdict"] = "CONDITIONAL_GO"
        # quota (limit 3 for dev1: A, C + one more, then 429)
        assert (await reg("dev1", "solar", **at(0, 30_000))).status_code == 200
        assert (await reg("dev1", "solar", **at(0, 40_000))).status_code == 429
        # polygon input works
        poly = {"type": "Polygon", "coordinates": [[[12.9, 52.2], [12.905, 52.2], [12.905, 52.204], [12.9, 52.204], [12.9, 52.2]]]}
        r = await reg("dev5", "solar", polygon_geojson=poly); assert r.json()["status"] == "registered", r.text
        # tiny parcel rejected
        r = await reg("dev6", "solar", lat=52.5, lon=13.0, radius_m=10); assert r.json()["status"] == "rejected", r.text

        # withdraw closes the match
        assert (await c.delete(f"/parcels/{b['parcel_id']}", params={"developer_id": "dev1"})).status_code == 404
        assert (await c.delete(f"/parcels/{b['parcel_id']}", params={"developer_id": "dev2"})).status_code == 200
        assert (await c.get("/teasers", params={"developer_id": "dev1"})).json()["count"] == 0
        print("matcher OK")

    # auth
    s2 = MatcherSettings(matcher_database_path=f"{tempfile.mkdtemp()}/m2.sqlite3", matcher_api_key="secret")
    app2 = create_app(s2)
    async with app2.router.lifespan_context(app2):
        c2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://m")
        assert (await c2.get("/teasers", params={"developer_id": "x"})).status_code == 401
        assert (await c2.get("/teasers", params={"developer_id": "x"}, headers={"X-API-Key": "secret"})).status_code == 200
        print("auth OK")

    # paid Agent 1: the matcher must use the keyed /internal/audit, not the public /start_job
    s3 = MatcherSettings(matcher_database_path=f"{tempfile.mkdtemp()}/m3.sqlite3", matcher_api_key="", validator_api_key="ikey")
    app3 = create_app(s3)
    async with app3.router.lifespan_context(app3):
        seen: list[str] = []
        app3.state.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: (seen.append(req.url.path), fake_validator(req))[1]))
        c3 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app3), base_url="http://m")
        state.update(verdict="CONDITIONAL_GO", tech="solar")
        r = await c3.post("/register_parcel", json={"developer_id": "dev9", **at(0)})
        assert r.status_code == 200 and r.json()["status"] == "registered", r.text
        assert seen == ["/internal/audit"], seen
        print("internal validator OK")


asyncio.run(main())
