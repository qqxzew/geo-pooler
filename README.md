# Land Audit Worker — Masumi agent (Cardano Preprod)

Takes a coordinate or polygon in Germany, queries live open-data services in parallel and returns a
deterministic wind/solar-PV due-diligence report (verdict + score 0-100). Implements Masumi's MIP-003
agent API and the pay-then-deliver flow on Cardano **Preprod**.

## Run it (no blockchain needed)

```bash
python -m venv .venv && .venv/Scripts/activate    # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                               # BYPASS_PAYMENT=true by default
uvicorn app.main:app --port 8000
python test_worker.py                              # 3 real locations, prints the full JSON reports
```

```bash
curl -X POST localhost:8000/start_job -H 'content-type: application/json' \
  -d '{"lat":48.7286,"lon":11.4770,"radius_m":250,"project_type":"solar"}'
```

Docker: `docker compose up -d --build land-audit-worker` (worker only) or `docker compose up -d --build` (worker +
Masumi payment service + PostgreSQL).

## API (MIP-003)

| Endpoint | |
|---|---|
| `GET /availability` | `{"status":"available","network":"Preprod",…}` |
| `GET /input_schema` | JSON Schema + MIP-003 field list |
| `POST /start_job` | body = `{lat,lon,radius_m?,project_type?}` **or** `{polygon_geojson,…}`, flat or wrapped as `{identifier_from_purchaser, input_data:{…}}`. Validates the Germany bbox (lat 47.2–55.1, lon 5.8–15.1). `BYPASS_PAYMENT=true` → runs now, returns `status: completed` + `report`. Otherwise → creates the Masumi payment request, returns `job_id`, `blockchainIdentifier`, `payByTime`, `sellerVKey`, … |
| `GET /status?job_id=` | checks the payment; on `FundsLocked` runs the pipeline in the background (`running` → `completed`), hashes the result and submits the hash to unlock the funds. Returns `result` (canonical JSON string, MIP-003), `report` (parsed) and `result_hash`. |

Output hash (MIP-004): `sha256("<identifier_from_purchaser>;<json-escaped result string>")`. The report is serialised with
sorted keys and no whitespace, so the hash is reproducible. The report contains no timestamps.

## Going live on Preprod (`BYPASS_PAYMENT=false`)

1. `.env`: `BLOCKFROST_API_KEY`, `WALLET_SEED_PHRASE` (+ optional `WALLET_ADDRESS`), and set `ENCRYPTION_KEY`, `ADMIN_KEY`.
2. `docker compose up -d --build`. `payment-setup` migrates + seeds the DB, then the payment service starts. Admin UI:
   <http://localhost:3001/admin/> (log in with `ADMIN_KEY`). The seed also generates a *purchase* wallet and prints its
   mnemonic once (`docker logs masumi-payment-setup`).
3. **Register the agent** (admin UI → AI agents → register; or via the Masumi registry service). Set the API URL to where this worker is
   reachable (public URL), price **10 tADA** (10 000 000 lovelace), and fund the selling wallet with a few tADA for fees
   ([Cardano Preprod faucet](https://docs.cardano.org/cardano-testnets/tools/faucet/)). Copy the resulting identifier into
   `AGENT_IDENTIFIER` in `.env`.
4. Set `MASUMI_API_KEY` (use `ADMIN_KEY`, or create a restricted key in the admin UI), `BYPASS_PAYMENT=false`,
   then `docker compose up -d land-audit-worker`.

The price is defined by the agent's registry entry — the worker only reports it (`price` in the `start_job` response). If your
payment-service version requires explicit amounts or a payment-source index, set `MASUMI_REQUESTED_FUNDS_LOVELACE`,
`MASUMI_PAYMENT_SOURCE_TYPE`, `MASUMI_SUPPORTED_PAYMENT_SOURCE_INDEX` (see `app/config.py`). Payment-service 0.29.0 seeds
only a `Web3CardanoV2` source, so docker compose defaults these to `Web3CardanoV2` / `0` and agents must be registered as V2
(pricing inside `supportedPaymentSources`, e.g. `POST /api/v1/registry`).

On a host whose SSH gateway forbids port forwarding (Hack Club Nest: `ssh -L` → "administratively prohibited"),
open the server's admin UI with `python scripts/admin_tunnel.py user@host` → <http://localhost:3001/admin/>.

## Agent 2 — Matcher (`matcher/`)

Registers a developer's parcel, finds **other developers'** parcels within 1.5 km (polygon-to-polygon distance in
EPSG:25832) and queues an anonymous teaser for both sides. Own service, own database; calls Agent 1 over HTTP.

```bash
uvicorn matcher.main:app --port 8001          # needs Agent 1 on VALIDATOR_URL (default :8000)
python tests/test_matcher.py                   # offline test (validator stubbed)
```

| Endpoint | |
|---|---|
| `POST /register_parcel` | `{developer_id, lat, lon, radius_m? \| polygon_geojson, project_type?, label?}`. Runs Agent 1; `NO_GO` or < 0.5 ha → `status: rejected` + reasons (nothing stored). Otherwise stores it and returns the teasers for new matches. 409 if you registered the same parcel already, 429 above 50 active parcels. |
| `GET /teasers?developer_id=&unread_only=` | the developer's teaser outbox (polling; the orchestrator can push later) |
| `GET /parcels?developer_id=` · `DELETE /parcels/{id}?developer_id=` | own parcels · withdraw (closes its matches) |

A teaser contains only: neighbour technology, area band, audit verdict, score band, Bundesland, distance band
(<500 / 500–1000 / 1000–1500 m), combined-area band and a synergy hint. **Never** coordinates, municipality, parcel id or
developer identity — those are released by Agent 3 after both sides agree and sign the NDA. Exact geometry, ALKIS id and the
full audit report are stored for that step. Parcels of the same developer are never matched with each other.

Auth: the service expects the orchestrator to supply `developer_id`; protect it with `MATCHER_API_KEY` (`X-API-Key`).
Not handled yet: consent/escrow/NDA (Agent 3), paid validation (Agent 1 with `BYPASS_PAYMENT=false` answers 402 here — the
orchestrator will pay for it). Storage is SQLite with a bounding-box prefilter; move to PostGIS (`ST_DWithin`) beyond a few thousand parcels.

## Agent 3 — Escrow & NDA (`escrow/`)

```bash
uvicorn escrow.main:app --port 8002        # needs Agent 2 (MATCHER_URL) and MATCHER_API_KEY set on both
pip install -r requirements-dev.txt && python tests/test_escrow.py     # offline end-to-end test incl. PDF checks
```

Flow per match (`match_id` = the id in the teasers): **consent → deposits → NDA → signatures → reveal**.

| Step | Call |
|---|---|
| Party data for the NDA (company, address, representative, e-mail) | `PUT /developers/{id}/profile` |
| Consent (both must `accept`; either `decline` ends the deal) | `POST /deals/{match_id}/consent` `{developer_id, decision}` |
| Status, payment instructions, NDA info | `GET /deals/{match_id}?developer_id=` (polling advances the state machine) |
| NDA PDF (draft; after both signed: signed version with Signaturprotokoll) | `GET /deals/{match_id}/nda.pdf?developer_id=` |
| Confirm the NDA | `POST /deals/{match_id}/sign` `{developer_id, signer_name, signer_role, document_sha256, accept: true}` |
| Contacts + parcel boundaries + audit report of the counterpart | `GET /deals/{match_id}/reveal?developer_id=` — **403 until both signed** |

**Money (Masumi):** after both consent, each developer gets their own payment request (the agent is the seller of a "reveal"
service). Funds are locked on-chain; only when both signed does the agent submit the result hash
(MIP-004 over `{match_id, signed_nda_sha256, revealed_at}`), which lets Masumi release the funds. If the deal ends
earlier (decline / deadline / failed payment) no result is submitted and buyers use Masumi's refund. Deadlines: 7 days
to consent, 48 h to deposit, 7 days to sign (`CONSENT_WINDOW_H`, `DEPOSIT_WINDOW_H`, `SIGNATURE_WINDOW_H`).
`ESCROW_BYPASS_PAYMENT=true` skips the chain for testing. Register this agent in Masumi separately from Agent 1
(its own `ESCROW_AGENT_IDENTIFIER`).

**NDA** (`escrow/nda.py`, template `NDA-DE-1.0`, German): Vertraulichkeitsdefinition, Ausnahmen (angelehnt an § 2 GeschGehG),
Need-to-know, **Nichtumgehung / Schutz der Flächeneigentümer** (24 Monate), Vertragsstrafe nach Hamburger Brauch (§ 315 BGB, mit
Höchstbetrag), Rückgabe/Löschung, DSGVO, Laufzeit/Nachwirkung, Textform § 126b BGB, deutsches Recht, Gerichtsstand. The PDF
is deterministic (same inputs → same SHA-256); the signer must submit the hash of the document they were shown. The signed version
adds a *Signaturprotokoll* (names, roles, UTC time, IP, document hash). Confirmation is a *simple electronic* acceptance in Textform,
**not** a qualified e-signature (eIDAS) — plug in a QES provider if you need one.
The NDA names both companies and addresses (unavoidable for a contract); phone numbers, parcel locations and boundaries stay
hidden until both signed.

> **Legal notice:** the NDA text is a template written to common German B2B practice. It is not legal advice — have a
> German lawyer review it (especially the Vertragsstrafe and Nichtumgehung clauses) before production use.

## Agent 4 — Pool Architect (`architect/`)

```bash
uvicorn architect.main:app --port 8003     # needs Agent 3 (ESCROW_URL) and ESCROW_API_KEY set on both
python tests/test_architect.py             # offline end-to-end test (real Agents 2+3, stubbed validator)
```

Works only on a **revealed** deal (both NDAs signed): it pulls both companies and parcels from Agent 3's internal
`/internal/deals/{match_id}/package` endpoint (needs `ESCROW_API_KEY`), computes the joint project and each firm's share,
and renders two German PDFs.

| Step | Call |
|---|---|
| Compute plan + both documents (new version on every call) | `POST /pools/{match_id}` `{developer_id, method?: shapley\|area\|revenue, assumptions?: {…}}` |
| Plan as JSON (shares, synergy, layout, warnings, document hashes) | `GET /pools/{match_id}?developer_id=` |
| Draft **Flächenpoolvertrag** (POOL-DE-1.0) | `GET /pools/{match_id}/agreement.pdf?developer_id=` |
| **Netzanschlussbegehren** (NAB-DE-1.0) to the grid operator | `GET /pools/{match_id}/grid_application.pdf?developer_id=` |

**Model** (`architect/model.py`, every constant is an overridable `assumptions` field):
wind layout = greedy turbine placement on a 25 m grid (rotor radius inside the parcel, 4 rotor diameters spacing, also *across*
the two parcels); wind energy from the audited 100 m speed via a linear capacity-factor curve; solar capacity from area × usable
fraction × slope derating, energy from the audited PVGIS yield; NPV over 25 years at 6 % after capex, opex, grid connection
(fixed + cable to the nearest substation) and development costs. Fair split = **Shapley value of a 2-player game**: each firm
keeps the value of developing alone, the pooling synergy (shared grid connection, one permitting effort per technology) is
split equally; alternatives by area or by revenue. Shares are integer **basis points** (largest-remainder rounding, always
summing to exactly 10 000). If pooling does not create value (e.g. one parcel is unviable on its own), the Shapley split is not
meaningful: the plan says so in `warnings` and falls back to the area key (`method_applied`).

**Documents:** the Flächenpoolvertrag is a draft cooperation agreement (Projektgesellschaft, quotas, Flächensicherung, Kosten,
Netzanschluss, Beschlüsse, Exklusivität, Vorkaufsrecht, Ausstieg) with the areas and the calculation as annexes; it cites the hash
of the signed NDA. The Netzanschlussbegehren is addressed to the operator found in OSM (`operator` tag of the nearest substation,
placeholder otherwise), cites § 8 EEG 2023 / § 17 EnWG and lists plant data, coordinates of the planned turbines and the
questions to answer (Netzverknüpfungspunkt, Kapazität, Kosten, Zeitplan). Placeholders `[bitte ergänzen]` mark what only you know.
Phone numbers are never printed. No Masumi payment is attached to this agent yet (the orchestrator will handle that).

> **Legal / financial notice:** both documents are machine-generated drafts, the model is a screening model with default
> assumptions — not bankable, not legal or tax advice. Have a German lawyer review the contract (Gesellschaftsrecht,
> § 550 / § 311b BGB for the Flächenverträge, tax) before anyone signs.

## Data sources (all keyless, all live)

| Check | Source | Notes |
|---|---|---|
| Bundesland / Gemeinde | BKG VG250 WFS | |
| Grid, setbacks, EEG corridor, turbines | OSM Overpass (one combined query, 3 mirrors, hedged) | `out geom` for true distances to lines/polygons |
| Protected areas | BfN WFS; **fallback** EEA CDDA + Natura 2000 (ArcGIS REST) when BfN is down | layers discovered from GetCapabilities |
| Solar yield | EU JRC PVGIS 5.2 `PVcalc` (`E_y`) | optimal tilt/azimuth |
| Wind @100 m | Open-Meteo DWD ICON, 30-day mean | m/s; interpolated 80↔120 m if 100 m is missing |
| Slope / elevation | Copernicus DEM (90 m) via Open-Meteo, 3×3 Horn gradient | PVGIS has no slope output |
| Parcel (ALKIS) | NRW and Brandenburg open WFS | other states → `ALKIS_CLOSED_STATE_FALLBACK`, area from the input polygon |

A failing source is **never faked**: it appears as `unavailable: …` in `data_quality`, and adds a warning. If a core check (grid/OSM,
protected areas, solar or wind data) is missing, the verdict cannot be an unconditional `GO`.

## Scoring (all constants in `app/scoring.py`)

Distances/areas are computed in EPSG:25832. `null` distance = nothing found inside the search radius (substation 10 km,
power line 5 km, residential 1.5 km).

* **Solar** (100): resource 30 (850→1100 kWh/kWp) · grid 25 · terrain 15 (15°→5°) · EEG corridor 20 (8 if outside) · size 10 (≥2 ha).
  Blocker: slope > 20°.
* **Wind** (100): wind speed 35 (4.5→6.5 m/s) · setback 25 · grid 20 · terrain 10 · neighbouring turbines 10.
  Blockers: residential < 600 m, mean wind < 5.0 m/s, slope > 25°. Conditions: residential 600–800 m, wind 5.0–5.5 m/s.
* **Grid score** = 60 % substation (full ≤ 1 km, zero ≥ 10 km) + 40 % power line (full ≤ 250 m, zero ≥ 5 km).
* **Hybrid** = the better of wind/solar. Verdict per technology: blocker or score < 40 → `NO_GO`; score ≥ 65 without conditions → `GO`; else `CONDITIONAL_GO`.
* **Overlay**: Naturschutzgebiet / Nationalpark → `NO_GO`, score capped at 10. FFH / Vogelschutzgebiet → −15 points and no unconditional `GO`.

## Known limits

* Wind speed is a 30-day mean, not an annual one — treat as a screening value.
* OSM completeness varies; the EEG corridor flag counts `motorway` and `railway=rail` (no sidings/yards), not `trunk` roads.
* This is a screening tool, not a legal opinion (no Flächennutzungsplan, Windenergiegebiete, Denkmalschutz, military or aviation checks).
* Public Overpass servers are sometimes at capacity; the worker retries across mirrors for up to 70 s, then reports the check as unavailable.
