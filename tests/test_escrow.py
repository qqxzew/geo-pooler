"""Offline end-to-end test of Agent 3 (Escrow & NDA) on top of the real Agent 2.

Stubbed: Agent 1 (validator) and the Masumi payment service (only in the paid-mode test).
Real: matcher, escrow state machine, German NDA PDF generation/validation, hashing.

    python tests/test_escrow.py
"""
import asyncio
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path

import httpx
from pyproj import Transformer
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings as AgentSettings  # noqa: E402
from app.masumi import PaymentClient, output_hash  # noqa: E402
from escrow.config import EscrowSettings  # noqa: E402
from escrow.main import create_app as create_escrow  # noqa: E402
from escrow.service import DealService  # noqa: E402
from escrow.store import EscrowStore  # noqa: E402
from matcher.config import MatcherSettings  # noqa: E402
from matcher.main import create_app as create_matcher  # noqa: E402

TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)
TO_WGS = Transformer.from_crs(25832, 4326, always_xy=True)
x0, y0 = TO_UTM.transform(12.7329, 52.0746)
KEY = "k" * 20


def at(dx, dy=0.0):
    lon, lat = TO_WGS.transform(x0 + dx, y0 + dy)
    return {"lat": round(lat, 6), "lon": round(lon, 6), "radius_m": 300}


def fake_validator(request):
    report = {"overall_verdict": "GO", "score": 85, "assessed_technology": "wind", "warnings": [],
              "location_info": {"bundesland": "Brandenburg", "municipality": "Testgemeinde", "alkis_parcel_id": "12-1536-005-17"}}
    return httpx.Response(200, json={"job_id": "j", "status": "completed", "report": report})


def profile(name, rep, mail):
    return {"company_name": name, "street": "Hauptstraße 1", "postal_code": "14467", "city": "Potsdam", "representative": rep,
            "commercial_register": "Amtsgericht Potsdam, HRB 12345", "email": mail, "phone": "+49 331 123456"}


async def make_match(mc, dev_a, dev_b, dy):
    for dev, dx in ((dev_a, 0), (dev_b, 1400)):
        r = await mc.post("/register_parcel", json={"developer_id": dev, "project_type": "wind", **at(dx, dy)},
                          headers={"X-API-Key": KEY})
        assert r.status_code == 200 and r.json()["status"] == "registered", r.text
    t = (await mc.get("/teasers", params={"developer_id": dev_a}, headers={"X-API-Key": KEY})).json()["teasers"][0]
    return t["match_id"]


def pdf_text(b):
    r = PdfReader(io.BytesIO(b))
    return len(r.pages), " ".join(" ".join(p.extract_text() for p in r.pages).split())


async def main():
    tmp = tempfile.mkdtemp()
    matcher = create_matcher(MatcherSettings(matcher_database_path=f"{tmp}/m.sqlite3", matcher_api_key=KEY),
                             transport=httpx.MockTransport(fake_validator))
    async with matcher.router.lifespan_context(matcher):
        mc = httpx.AsyncClient(transport=httpx.ASGITransport(app=matcher), base_url="http://matcher")
        transport = httpx.ASGITransport(app=matcher)

        # ------------------------------------------------------------ happy path (bypass payment)
        s = EscrowSettings(escrow_database_path=f"{tmp}/e.sqlite3", matcher_url="http://matcher", matcher_api_key=KEY,
                           escrow_bypass_payment=True)
        escrow = create_escrow(s, transport=transport)
        async with escrow.router.lifespan_context(escrow):
            ec = httpx.AsyncClient(transport=httpx.ASGITransport(app=escrow), base_url="http://escrow")
            mid = await make_match(mc, "dev1", "dev2", 0)

            r = await ec.post(f"/deals/{mid}/consent", json={"developer_id": "dev1", "decision": "accept"})
            assert r.status_code == 422, "consent must require a profile"
            assert (await ec.put("/developers/dev1/profile", json=profile("Windkraft Nord GmbH & Co. KG", "Anna Müller, Geschäftsführerin", "anna@wn.de"))).status_code == 200
            assert (await ec.put("/developers/dev2/profile", json=profile("Solarpark Süd GmbH", "Jörg Schäfer, Prokurist", "joerg@ss.de"))).status_code == 200
            assert (await ec.put("/developers/dev2/profile", json={**profile("X", "Y", "bad"), "postal_code": "1"})).status_code == 422

            v = (await ec.post(f"/deals/{mid}/consent", json={"developer_id": "dev1", "decision": "accept"})).json()
            assert v["status"] == "pending_consent" and not v["counterpart"]["consented"], v
            assert (await ec.post(f"/deals/{mid}/consent", json={"developer_id": "dev3", "decision": "accept"})).status_code == 403
            v = (await ec.post(f"/deals/{mid}/consent", json={"developer_id": "dev2", "decision": "accept"})).json()
            assert v["status"] == "awaiting_signatures" and v["nda"]["language"] == "de", v
            assert (await ec.get(f"/deals/{mid}/reveal", params={"developer_id": "dev1"})).status_code == 403

            r = await ec.get(f"/deals/{mid}/nda.pdf", params={"developer_id": "dev1"})
            assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
            assert r.content.startswith(b"%PDF-") and r.content.rstrip().endswith(b"%%EOF")
            assert hashlib.sha256(r.content).hexdigest() == v["nda"]["document_sha256"]
            pages, text = pdf_text(r.content)
            for must in ("Gegenseitige Geheimhaltungs- und Nichtumgehungsvereinbarung", "Windkraft Nord GmbH & Co. KG",
                         "Solarpark Süd GmbH", "§ 4 Nichtumgehung", "§ 5 Vertragsstrafe", "GeschGehG", "DSGVO",
                         "§ 126b BGB", "Hauptstraße 1", "Jörg Schäfer"):
                assert must in text, must
            assert pages >= 4
            assert "anna@wn.de" in text and "+49 331" not in text, "phone must not be printed in the NDA"
            sha = v["nda"]["document_sha256"]

            def sign(dev, h=None):
                return ec.post(f"/deals/{mid}/sign", json={
                    "developer_id": dev, "signer_name": "Anna Müller" if dev == "dev1" else "Jörg Schäfer",
                    "signer_role": "Geschäftsführerin" if dev == "dev1" else "Prokurist",
                    "document_sha256": h or sha, "accept": True})

            assert (await sign("dev1", "0" * 64)).status_code == 409
            r = await ec.post(f"/deals/{mid}/sign", json={"developer_id": "dev1", "signer_name": "A B C", "signer_role": "GF",
                                                          "document_sha256": sha, "accept": False})
            assert r.status_code == 422
            v = (await sign("dev1")).json()
            assert v["status"] == "awaiting_signatures" and v["you"]["signed"] and not v["counterpart"]["signed"]
            assert (await ec.get(f"/deals/{mid}/reveal", params={"developer_id": "dev1"})).status_code == 403
            v = (await sign("dev2")).json()
            assert v["status"] == "revealed" and v["nda"]["signed_document_sha256"], v

            rv = (await ec.get(f"/deals/{mid}/reveal", params={"developer_id": "dev1"})).json()
            assert rv["counterpart"]["company"]["email"] == "joerg@ss.de" and rv["counterpart"]["company"]["phone"].startswith("+49")
            assert rv["counterpart"]["parcel"]["geometry"]["type"] == "Polygon"
            assert rv["counterpart"]["parcel"]["municipality"] == "Testgemeinde"
            assert rv["counterpart"]["parcel"]["alkis_parcel_id"] == "12-1536-005-17"
            rv2 = (await ec.get(f"/deals/{mid}/reveal", params={"developer_id": "dev2"})).json()
            assert rv2["counterpart"]["company"]["company_name"].startswith("Windkraft")
            assert (await ec.get(f"/deals/{mid}/reveal", params={"developer_id": "dev3"})).status_code == 403

            r = await ec.get(f"/deals/{mid}/nda.pdf", params={"developer_id": "dev2"})
            assert hashlib.sha256(r.content).hexdigest() == v["nda"]["signed_document_sha256"]
            pages, text = pdf_text(r.content)
            assert pages >= 5 and "Signaturprotokoll" in text and "Elektronisch bestätigt" in text
            assert sha in text.replace("\n", "")
            print("happy path OK:", pages, "pages, signed")

            # decline
            mid2 = await make_match(mc, "dev4", "dev5", 5000)
            for d in ("dev4", "dev5"):
                await ec.put(f"/developers/{d}/profile", json=profile(f"Firma {d} GmbH", "Max Muster, GF", f"{d}@x.de"))
            r = await ec.post(f"/deals/{mid2}/consent", json={"developer_id": "dev4", "decision": "decline"})
            assert r.json()["status"] == "declined"
            r = await ec.post(f"/deals/{mid2}/consent", json={"developer_id": "dev5", "decision": "accept"})
            assert r.status_code == 409
            assert (await ec.get(f"/deals/{mid2}/nda.pdf", params={"developer_id": "dev5"})).status_code == 409
            print("decline OK")

        # ------------------------------------------------------------ expiry
        s_exp = EscrowSettings(escrow_database_path=f"{tmp}/e2.sqlite3", matcher_url="http://matcher", matcher_api_key=KEY)
        escrow2 = create_escrow(s_exp, transport=transport)
        async with escrow2.router.lifespan_context(escrow2):
            ec2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=escrow2), base_url="http://escrow")
            mid3 = await make_match(mc, "dev6", "dev7", 9000)
            await ec2.put("/developers/dev6/profile", json=profile("Firma 6 GmbH", "Max Muster, GF", "a@b.de"))
            r = await ec2.post(f"/deals/{mid3}/consent", json={"developer_id": "dev6", "decision": "accept"})
            assert r.status_code == 200
            deal = await escrow2.state.deals.store.load_deal(mid3)
            deal["data"]["deadline"] = 1.0  # simulate that the consent window has passed
            await escrow2.state.deals.store.save_deal(deal)
            assert (await ec2.get(f"/deals/{mid3}", params={"developer_id": "dev6"})).json()["status"] == "expired"
            print("expiry OK")

        # ------------------------------------------------------------ paid mode against a fake Masumi payment service
        calls, state = [], {}

        def fake_masumi(request):
            body = json.loads(request.content)
            calls.append((request.url.path, body))
            if request.url.path.endswith("/payment/"):
                n = sum(1 for p, _ in calls if p.endswith("/payment/"))
                return httpx.Response(200, json={"data": {
                    "blockchainIdentifier": f"bc-{n}", "payByTime": "2026-11-01T10:00:00.000Z",
                    "submitResultTime": "2026-11-02T10:00:00.000Z", "unlockTime": "2026-11-03T10:00:00.000Z"}})
            if request.url.path.endswith("resolve-blockchain-identifier"):
                return httpx.Response(200, json={"data": {"onChainState": state.get(body["blockchainIdentifier"])}})
            return httpx.Response(200, json={"status": "success"})

        s_paid = EscrowSettings(escrow_database_path=f"{tmp}/e3.sqlite3", matcher_url="http://matcher", matcher_api_key=KEY,
                                escrow_bypass_payment=False, escrow_agent_identifier="e" * 60, masumi_api_key="m" * 20)
        pay = PaymentClient(httpx.AsyncClient(transport=httpx.MockTransport(fake_masumi)),
                            AgentSettings(masumi_payment_service_url="http://masumi", masumi_api_key="m" * 20,
                                          agent_identifier="e" * 60))
        svc = DealService(EscrowStore(s_paid.escrow_database_path), httpx.AsyncClient(transport=transport), s_paid, payment=pay)
        escrow3 = create_escrow(s_paid, service=svc)
        async with escrow3.router.lifespan_context(escrow3):
            ec3 = httpx.AsyncClient(transport=httpx.ASGITransport(app=escrow3), base_url="http://escrow")
            mid4 = await make_match(mc, "dev8", "dev9", 13000)
            for d in ("dev8", "dev9"):
                await ec3.put(f"/developers/{d}/profile", json=profile(f"Firma {d} GmbH", "Max Muster, GF", f"{d}@x.de"))
            await ec3.post(f"/deals/{mid4}/consent", json={"developer_id": "dev8", "decision": "accept"})
            v = (await ec3.post(f"/deals/{mid4}/consent", json={"developer_id": "dev9", "decision": "accept"})).json()
            assert v["status"] == "awaiting_deposits" and v["you"]["payment"]["blockchainIdentifier"].startswith("bc-"), v
            assert len(v["you"]["payment"]["identifier_from_purchaser"]) == 20
            bc = {}
            for d in ("dev8", "dev9"):
                bc[d] = (await ec3.get(f"/deals/{mid4}", params={"developer_id": d})).json()["you"]["payment"]
            state[bc["dev8"]["blockchainIdentifier"]] = "FundsLocked"
            v = (await ec3.get(f"/deals/{mid4}", params={"developer_id": "dev8"})).json()
            assert v["status"] == "awaiting_deposits" and v["you"]["deposit_locked"] and not v["counterpart"]["deposit_locked"], v
            state[bc["dev9"]["blockchainIdentifier"]] = "FundsLocked"
            v = (await ec3.get(f"/deals/{mid4}", params={"developer_id": "dev9"})).json()
            assert v["status"] == "awaiting_signatures", v
            assert not [c for c in calls if c[0].endswith("submit-result")], "no funds may be released before signing"
            sha = v["nda"]["document_sha256"]
            for d in ("dev8", "dev9"):
                r = await ec3.post(f"/deals/{mid4}/sign", json={"developer_id": d, "signer_name": "Max Muster", "signer_role": "GF",
                                                               "document_sha256": sha, "accept": True})
            v = r.json()
            assert v["status"] == "revealed", v
            subs = [b for p, b in calls if p.endswith("submit-result")]
            assert len(subs) == 2, subs
            for b in subs:
                dev = next(d for d in bc if bc[d]["blockchainIdentifier"] == b["blockchainIdentifier"])
                assert len(b["submitResultHash"]) == 64 and bc[dev]["identifier_from_purchaser"]
            # hash = MIP-004 over the canonical result string
            deal = await svc.store.load_deal(mid4)
            d = deal["data"]
            result = json.dumps({"match_id": mid4, "signed_nda_sha256": d["signed_sha256"],
                                 "revealed_at": __import__("datetime").datetime.fromtimestamp(d["revealed_at"], __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                                sort_keys=True, separators=(",", ":"))
            for b in subs:
                dev = next(x for x in bc if bc[x]["blockchainIdentifier"] == b["blockchainIdentifier"])
                assert b["submitResultHash"] == output_hash(result, bc[dev]["identifier_from_purchaser"])
            print("paid mode OK: funds released only after both signatures; result hashes verified")
    print("escrow OK")


asyncio.run(main())
