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
`MASUMI_PAYMENT_SOURCE_TYPE`, `MASUMI_SUPPORTED_PAYMENT_SOURCE_INDEX` (see `app/config.py`).

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
