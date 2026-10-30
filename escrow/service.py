r"""Deal state machine for Agent 3.

pending_consent -> awaiting_deposits -> awaiting_signatures -> revealed
                \-> declined / expired / failed

Money: each developer buys the "reveal" service from this agent through the Masumi payment service (two separate
payments). Funds are locked on-chain; only when both sides signed the NDA does the agent submit the result hash
(SHA-256 of the signed-NDA record) which lets Masumi release the funds. If the deal dies before that, the buyers
use Masumi's refund mechanism — this agent never submits a result for a failed deal.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import Settings as AgentSettings
from app.masumi import DEAD_STATES, PAID_STATES, PaymentClient, input_hash, output_hash

from .config import EscrowSettings
from .nda import NdaParams, Party, Signature, build_nda_pdf
from .store import EscrowStore

OPEN = ("pending_consent", "awaiting_deposits", "awaiting_signatures")


class DealError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _party(profile: dict[str, Any]) -> Party:
    return Party(company_name=profile["company_name"], street=profile["street"], postal_code=profile["postal_code"],
                 city=profile["city"], representative=profile["representative"], register=profile.get("commercial_register", ""),
                 email=profile.get("email", ""))


class DealService:
    def __init__(self, store: EscrowStore, client: httpx.AsyncClient, s: EscrowSettings,
                 payment: Optional[PaymentClient] = None):
        self.store, self.http, self.s = store, client, s
        self.payment = payment or PaymentClient(client, AgentSettings(
            masumi_payment_service_url=s.masumi_payment_service_url, masumi_api_key=s.masumi_api_key,
            agent_identifier=s.escrow_agent_identifier, masumi_network=s.masumi_network))
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------ helpers

    async def _fetch_match(self, match_id: str) -> dict[str, Any]:
        try:
            r = await self.http.get(f"{self.s.matcher_url.rstrip('/')}/internal/matches/{match_id}",
                                    headers={"X-API-Key": self.s.matcher_api_key}, timeout=20)
        except httpx.HTTPError as e:
            raise DealError(502, f"matcher unreachable: {type(e).__name__}")
        if r.status_code == 404:
            raise DealError(404, "unknown match")
        if r.status_code != 200:
            raise DealError(502, f"matcher error {r.status_code}")
        return r.json()

    async def _deal(self, match_id: str, dev: str, create: bool = False) -> dict[str, Any]:
        deal = await self.store.load_deal(match_id)
        if deal is None:
            if not create:
                raise DealError(404, "no deal for this match yet — give consent first")
            rec = await self._fetch_match(match_id)
            if rec["match"]["status"] != "open":
                raise DealError(409, "match is closed (a parcel was withdrawn)")
            parcels = {p["developer_id"]: p for p in rec["parcels"]}
            if dev not in parcels or len(parcels) != 2:
                raise DealError(403, "you are not a party of this match")
            devs = sorted(parcels)
            deal = {"match_id": match_id, "status": "pending_consent", "nda_pdf": None, "signed_pdf": None, "data": {
                "match_id": match_id, "developers": devs, "parcels_full": parcels, "consent": {}, "payments": {},
                "signatures": {}, "nda": None, "created_at": _now(), "deadline": _now() + self.s.consent_window_h * 3600}}
        elif dev not in deal["data"]["developers"]:
            raise DealError(403, "you are not a party of this deal")
        return deal

    def _lock(self, match_id: str) -> asyncio.Lock:
        return self._locks[match_id]

    # ------------------------------------------------------------------ profile

    async def put_profile(self, dev: str, profile: dict[str, Any]) -> None:
        await self.store.put_developer(dev, profile)

    # ------------------------------------------------------------------ steps

    async def consent(self, match_id: str, dev: str, decision: str) -> dict[str, Any]:
        async with self._lock(match_id):
            deal = await self._deal(match_id, dev, create=True)
            await self._advance(deal)
            d = deal["data"]
            if deal["status"] != "pending_consent":
                if dev in d["consent"] and d["consent"][dev] == decision:
                    return self._view(deal, dev)  # idempotent
                raise DealError(409, f"deal is '{deal['status']}'; consent no longer possible")
            if decision == "accept" and not await self.store.get_developer(dev):
                raise DealError(422, f"set your company profile first: PUT /developers/{dev}/profile")
            d["consent"][dev] = decision
            if decision == "decline":
                deal["status"] = "declined"
            elif len(d["consent"]) == 2 and all(v == "accept" for v in d["consent"].values()):
                deal["status"] = "awaiting_deposits"
                d["deadline"] = _now() + self.s.deposit_window_h * 3600
                await self._advance(deal)
            await self.store.save_deal(deal)
            return self._view(deal, dev)

    async def status(self, match_id: str, dev: str) -> dict[str, Any]:
        async with self._lock(match_id):
            deal = await self._deal(match_id, dev)
            before = json.dumps(deal["data"], sort_keys=True, default=str), deal["status"]
            await self._advance(deal)
            if (json.dumps(deal["data"], sort_keys=True, default=str), deal["status"]) != before:
                await self.store.save_deal(deal)
            return self._view(deal, dev)

    async def sign(self, match_id: str, dev: str, signer_name: str, signer_role: str, doc_sha256: str,
                   ip: str) -> dict[str, Any]:
        async with self._lock(match_id):
            deal = await self._deal(match_id, dev)
            await self._advance(deal)
            d = deal["data"]
            if deal["status"] != "awaiting_signatures":
                raise DealError(409, f"deal is '{deal['status']}'; nothing to sign")
            if not hmac.compare_digest(doc_sha256.lower(), d["nda"]["sha256"]):
                raise DealError(409, "document_sha256 does not match the NDA you were shown — download it again")
            if dev in d["signatures"]:
                return self._view(deal, dev)
            d["signatures"][dev] = {"name": signer_name, "role": signer_role, "signed_at": _now(), "ip": ip}
            if len(d["signatures"]) == 2:
                self._finalize(deal)
                await self._submit_results(deal)
            await self.store.save_deal(deal)
            return self._view(deal, dev)

    # ------------------------------------------------------------------ transitions

    async def _advance(self, deal: dict[str, Any]) -> None:
        d = deal["data"]
        if deal["status"] in OPEN and _now() > d["deadline"]:
            deal["status"] = "expired"
            return
        if deal["status"] == "awaiting_deposits":
            for dev in d["developers"]:
                if dev not in d["payments"]:
                    await self._create_payment(deal, dev)
                p = d["payments"].get(dev)
                if p and p["state"] not in PAID_STATES and not self.s.escrow_bypass_payment:
                    try:
                        p["state"] = await self.payment.state(p["blockchain_identifier"]) or p["state"]
                    except Exception as e:  # noqa: BLE001 - transient; retried on next poll
                        d["warning"] = f"payment status unavailable: {e}"[:200]
            states = [d["payments"].get(dev, {}).get("state") for dev in d["developers"]]
            if any(s in DEAD_STATES for s in states):
                deal["status"], d["error"] = "failed", "a payment was refunded or is in an invalid state"
            elif all(s in PAID_STATES for s in states):
                await self._generate_nda(deal)
        elif deal["status"] == "revealed":
            await self._submit_results(deal)

    async def _create_payment(self, deal: dict[str, Any], dev: str) -> None:
        d = deal["data"]
        identifier = uuid.uuid4().hex[:20]
        if self.s.escrow_bypass_payment:
            d["payments"][dev] = {"identifier": identifier, "blockchain_identifier": f"bypass-{identifier}",
                                  "state": "FundsLocked", "submitted": False, "info": {}}
            return
        try:
            pay = await self.payment.create(
                identifier, input_hash({"match_id": d["match_id"], "service": "reveal"}, identifier),
                metadata=f"reveal {d['match_id']}")
        except Exception as e:  # noqa: BLE001
            d["warning"] = f"payment request failed, will retry: {e}"[:300]
            return
        d["payments"][dev] = {"identifier": identifier, "blockchain_identifier": pay["blockchainIdentifier"],
                              "state": None, "submitted": False,
                              "info": {k: pay.get(k) for k in ("payByTime", "submitResultTime", "unlockTime")}}

    async def _generate_nda(self, deal: dict[str, Any]) -> None:
        d = deal["data"]
        profiles = [await self.store.get_developer(dev) for dev in d["developers"]]
        if not all(profiles):
            d["warning"] = "company profile missing for a party; NDA not generated"
            return
        params = NdaParams(
            nda_id=uuid.uuid4().hex, match_id=d["match_id"], created_at=datetime.now(timezone.utc),
            platform_name=self.s.platform_name, term_years=self.s.nda_term_years,
            aftereffect_years=self.s.nda_aftereffect_years, non_circumvention_months=self.s.nda_non_circumvention_months,
            max_penalty_eur=self.s.nda_max_penalty_eur, jurisdiction_city=self.s.nda_jurisdiction_city)
        pdf = build_nda_pdf(_party(profiles[0]), _party(profiles[1]), params)
        d["profiles"] = dict(zip(d["developers"], profiles))
        d["nda"] = {"id": params.nda_id, "sha256": hashlib.sha256(pdf).hexdigest(), "created_at": params.created_at.timestamp()}
        deal["nda_pdf"], deal["status"] = pdf, "awaiting_signatures"
        d["deadline"] = _now() + self.s.signature_window_h * 3600
        d.pop("warning", None)

    def _finalize(self, deal: dict[str, Any]) -> None:
        d = deal["data"]
        a, b = (_party(d["profiles"][dev]) for dev in d["developers"])
        params = NdaParams(
            nda_id=d["nda"]["id"], match_id=d["match_id"], created_at=datetime.fromtimestamp(d["nda"]["created_at"], timezone.utc),
            platform_name=self.s.platform_name, term_years=self.s.nda_term_years, aftereffect_years=self.s.nda_aftereffect_years,
            non_circumvention_months=self.s.nda_non_circumvention_months, max_penalty_eur=self.s.nda_max_penalty_eur,
            jurisdiction_city=self.s.nda_jurisdiction_city)
        sigs = {label: Signature(label, d["signatures"][dev]["name"], d["signatures"][dev]["role"],
                                 datetime.fromtimestamp(d["signatures"][dev]["signed_at"], timezone.utc), d["signatures"][dev]["ip"])
                for label, dev in zip("AB", d["developers"])}
        pdf = build_nda_pdf(a, b, params, sigs, d["nda"]["sha256"])
        deal["signed_pdf"], d["signed_sha256"] = pdf, hashlib.sha256(pdf).hexdigest()
        d["revealed_at"] = _now()
        deal["status"] = "revealed"

    async def _submit_results(self, deal: dict[str, Any]) -> None:
        """Release the funds: submit the result hash for every payment not yet settled."""
        d = deal["data"]
        if self.s.escrow_bypass_payment:
            return
        result = json.dumps({"match_id": d["match_id"], "signed_nda_sha256": d["signed_sha256"],
                             "revealed_at": _iso(d["revealed_at"])}, sort_keys=True, separators=(",", ":"))
        for dev, p in d["payments"].items():
            if p["submitted"]:
                continue
            try:
                await self.payment.submit_result(p["blockchain_identifier"], output_hash(result, p["identifier"]))
                p["submitted"] = True
            except Exception as e:  # noqa: BLE001
                d["warning"] = f"result submission pending: {e}"[:300]

    # ------------------------------------------------------------------ views

    def _view(self, deal: dict[str, Any], dev: str) -> dict[str, Any]:
        d = deal["data"]
        other = next(x for x in d["developers"] if x != dev)
        mine = d["payments"].get(dev)
        locked = lambda x: d["payments"].get(x, {}).get("state") in PAID_STATES  # noqa: E731
        v: dict[str, Any] = {
            "match_id": d["match_id"], "status": deal["status"], "deadline": _iso(d["deadline"]),
            "you": {"consent": d["consent"].get(dev), "deposit_locked": locked(dev), "signed": dev in d["signatures"]},
            "counterpart": {"consented": d["consent"].get(other) == "accept", "deposit_locked": locked(other),
                            "signed": other in d["signatures"]},
        }
        if mine:
            v["you"]["payment"] = {"identifier_from_purchaser": mine["identifier"], "blockchainIdentifier": mine["blockchain_identifier"],
                                   "state": mine["state"], "price": {"amount": self.s.deposit_tada, "unit": "tADA"}, **mine["info"]}
        if d.get("nda"):
            v["nda"] = {"id": d["nda"]["id"], "document_sha256": d["nda"]["sha256"], "language": "de",
                        "download": f"/deals/{d['match_id']}/nda.pdf"}
            if d.get("signed_sha256"):
                v["nda"]["signed_document_sha256"] = d["signed_sha256"]
        if deal["status"] == "revealed":
            v["reveal"] = f"/deals/{d['match_id']}/reveal"
        for k in ("error", "warning"):
            if d.get(k):
                v[k] = d[k]
        return v

    async def nda_pdf(self, match_id: str, dev: str) -> tuple[bytes, str]:
        deal = await self._deal(match_id, dev)
        if deal["status"] == "revealed" and deal["signed_pdf"]:
            return deal["signed_pdf"], f"NDA_{match_id[:8]}_unterschrieben.pdf"
        if deal["nda_pdf"] and deal["status"] == "awaiting_signatures":
            return deal["nda_pdf"], f"NDA_{match_id[:8]}_entwurf.pdf"
        raise DealError(409, f"no NDA available in status '{deal['status']}'")

    async def package(self, match_id: str) -> dict[str, Any]:
        """Both parties' company data and parcels once the deal is revealed — for Agent 4 only."""
        deal = await self.store.load_deal(match_id)
        if deal is None:
            raise DealError(404, "no deal for this match")
        d = deal["data"]
        if deal["status"] != "revealed":
            raise DealError(409, f"deal is '{deal['status']}'; the package exists only after both signatures")
        return {"match_id": match_id, "signed_nda": {"nda_id": d["nda"]["id"], "document_sha256": d["signed_sha256"],
                                                      "revealed_at": _iso(d["revealed_at"])},
                "parties": [{"developer_id": dev, "company": d["profiles"][dev], "parcel": d["parcels_full"][dev]}
                            for dev in d["developers"]]}

    async def reveal(self, match_id: str, dev: str) -> dict[str, Any]:
        deal = await self._deal(match_id, dev)
        d = deal["data"]
        if deal["status"] != "revealed":
            raise DealError(403, f"contacts and boundaries are released after both signatures (status: {deal['status']})")
        other = next(x for x in d["developers"] if x != dev)
        prof, par = d["profiles"][other], d["parcels_full"][other]
        mine = d["parcels_full"][dev]

        def parcel(p: dict[str, Any]) -> dict[str, Any]:
            return {"parcel_id": p["id"], "label": p.get("label"), "geometry": p["geometry"], "area_ha": p["area_ha"],
                    "municipality": p["municipality"], "bundesland": p["bundesland"], "alkis_parcel_id": p["alkis_id"],
                    "technology": p["assessed_technology"], "audit_verdict": p["verdict"], "audit_score": p["score"],
                    "audit_report": p["report"]}

        return {"match_id": match_id, "signed_nda": {"document_sha256": d["signed_sha256"], "download": f"/deals/{match_id}/nda.pdf"},
                "counterpart": {"company": prof, "parcel": parcel(par)}, "your_parcel": parcel(mine),
                "revealed_at": _iso(d["revealed_at"])}
