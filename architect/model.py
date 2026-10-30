"""Pool economics: layout estimate, project value (NPV), and each firm's share.

Deterministic and fully transparent — every number comes from the revealed parcels/audit reports and the
`Assumptions` below (defaults are screening values, NOT a bankable model).

Fair split: a two-player cooperative game. v(A), v(B) = value of developing a parcel alone, v(AB) = value of
the joint project (shared grid connection, one permitting effort for the same technology, wake-aware layout).
Shapley value: phi_A = v(A) + (v(AB) - v(A) - v(B)) / 2 — each firm keeps what it could achieve alone and the
synergy is split equally. Alternative keys: by area, by contributed annual revenue.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from shapely.prepared import prep

_TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)
_TO_WGS = Transformer.from_crs(25832, 4326, always_xy=True)

Method = Literal["shapley", "area", "revenue"]
DEFAULT_GRID_DISTANCE_M = 10_000.0  # used when the audit found no substation within 10 km


class Assumptions(BaseModel):
    # wind
    wind_rated_mw: float = Field(6.0, gt=0)
    wind_rotor_diameter_m: float = Field(150.0, gt=0)
    wind_spacing_rotor_diameters: float = Field(4.0, gt=0, description="min. turbine distance in rotor diameters")
    wind_cf_at_6ms: float = Field(0.28, gt=0, lt=1)
    wind_cf_per_ms: float = Field(0.07, ge=0)
    wind_capex_eur_per_kw: float = Field(1500.0, gt=0)
    wind_opex_eur_per_kw_a: float = Field(55.0, ge=0)
    wind_price_eur_mwh: float = Field(72.0, gt=0)
    wind_dev_fixed_eur: float = Field(150_000.0, ge=0, description="permitting, surveys, legal — once per project")
    # solar
    solar_mwp_per_ha: float = Field(1.0, gt=0)
    solar_usable_fraction: float = Field(0.85, gt=0, le=1)
    solar_capex_eur_per_kwp: float = Field(550.0, gt=0)
    solar_opex_eur_per_kwp_a: float = Field(12.0, ge=0)
    solar_price_eur_mwh: float = Field(60.0, gt=0)
    solar_degradation_per_year: float = Field(0.004, ge=0, lt=0.05)
    solar_dev_fixed_eur: float = Field(60_000.0, ge=0)
    # grid & finance
    grid_fixed_eur: float = Field(150_000.0, ge=0, description="transformer/transfer station, metering, protection")
    cable_eur_per_m: float = Field(150.0, ge=0, description="medium-voltage cable incl. civil works")
    discount_rate: float = Field(0.06, gt=0, lt=0.5)
    lifetime_years: int = Field(25, ge=5, le=40)


@dataclass
class ParcelIn:
    label: str  # "A" | "B"
    developer_id: str
    company_name: str
    parcel_id: str
    area_ha: float
    technology: str  # "wind" | "solar"
    solar_kwh_per_kwp: Optional[float]
    wind_ms: Optional[float]
    slope_deg: Optional[float]
    d_substation_m: Optional[float]
    poly_utm: Any  # shapely polygon, EPSG:25832


@dataclass
class Econ:
    capacity_mw: float = 0.0
    energy_mwh: float = 0.0
    revenue: float = 0.0
    opex: float = 0.0
    capex: float = 0.0


def parcel_from_package(label: str, party: dict[str, Any]) -> ParcelIn:
    p, rep = party["parcel"], party["parcel"]["report"]
    res, grid = rep.get("resource_potential", {}), rep.get("grid_access", {})
    return ParcelIn(
        label=label, developer_id=party["developer_id"], company_name=party["company"]["company_name"],
        parcel_id=p["id"], area_ha=p["area_ha"], technology=p["assessed_technology"],
        solar_kwh_per_kwp=res.get("solar_kwh_per_kwp_year"), wind_ms=res.get("wind_speed_100m_ms"),
        slope_deg=res.get("slope_degrees"), d_substation_m=grid.get("nearest_substation_m"),
        poly_utm=shp_transform(_TO_UTM.transform, shape(p["geometry"])))


# ---------------------------------------------------------------- layout & energy


def _candidates(poly, d: float, step: float = 25.0) -> list[tuple[float, float]]:
    inner = poly.buffer(-d / 2)  # rotor must not oversail the pool boundary
    if inner.is_empty:
        return []
    pp = prep(inner)
    minx, miny, maxx, maxy = inner.bounds
    pts, x = [], minx
    while x <= maxx:
        y = miny
        while y <= maxy:
            if pp.contains(Point(x, y)):
                pts.append((round(x, 1), round(y, 1)))
            y += step
        x += step
    return pts


def _greedy(cands: list[tuple[float, float]], chosen: list[tuple[float, float]], min_d: float) -> list[tuple[float, float]]:
    out = list(chosen)
    for c in cands:
        if all(math.dist(c, o) >= min_d for o in out):
            out.append(c)
    return out[len(chosen):]


def wind_layout(a: ParcelIn, b: Optional[ParcelIn], asm: Assumptions) -> dict[str, list[tuple[float, float]]]:
    """Turbine positions (UTM) per label. With two parcels: best of both placement orders, spacing enforced across."""
    D, min_d = asm.wind_rotor_diameter_m, asm.wind_rotor_diameter_m * asm.wind_spacing_rotor_diameters
    ca = _candidates(a.poly_utm, D)
    if b is None:
        return {a.label: _greedy(ca, [], min_d)}
    cb = _candidates(b.poly_utm, D)
    best = None
    for first, second, f_lab, s_lab in ((ca, cb, a.label, b.label), (cb, ca, b.label, a.label)):
        p1 = _greedy(first, [], min_d)
        p2 = _greedy(second, p1, min_d)
        if best is None or len(p1) + len(p2) > len(best[f_lab]) + len(best[s_lab]):
            best = {f_lab: p1, s_lab: p2}
    return best  # type: ignore[return-value]


def econ_wind(n: int, ms: Optional[float], asm: Assumptions) -> Econ:
    if n == 0 or ms is None:
        return Econ()
    cf = min(max(asm.wind_cf_at_6ms + asm.wind_cf_per_ms * (ms - 6.0), 0.12), 0.55)
    mw = n * asm.wind_rated_mw
    mwh = mw * 8760 * cf
    return Econ(mw, mwh, mwh * asm.wind_price_eur_mwh, mw * 1000 * asm.wind_opex_eur_per_kw_a, mw * 1000 * asm.wind_capex_eur_per_kw)


def econ_solar(area_ha: float, slope: Optional[float], kwh_per_kwp: Optional[float], asm: Assumptions) -> Econ:
    if kwh_per_kwp is None:
        return Econ()
    derate = 1.0 if slope is None else 0.5 + 0.5 * min(max((15.0 - slope) / 10.0, 0.0), 1.0)  # 5 deg -> 1.0, 15 deg -> 0.5
    mwp = area_ha * asm.solar_usable_fraction * asm.solar_mwp_per_ha * derate
    mwh = mwp * kwh_per_kwp  # kWh/kWp == MWh/MWp
    return Econ(mwp, mwh, mwh * asm.solar_price_eur_mwh, mwp * 1000 * asm.solar_opex_eur_per_kwp_a, mwp * 1000 * asm.solar_capex_eur_per_kwp)


def _annuity(asm: Assumptions) -> float:
    r, t = asm.discount_rate, asm.lifetime_years
    return (1 - (1 + r) ** -t) / r


def _operating_value(e: Econ, tech: str, asm: Assumptions) -> float:
    deg = 1 - asm.solar_degradation_per_year * (asm.lifetime_years - 1) / 2 if tech == "solar" else 1.0
    return _annuity(asm) * (e.revenue * deg - e.opex)


def _dev_cost(techs: set[str], asm: Assumptions) -> float:
    return sum(asm.wind_dev_fixed_eur if t == "wind" else asm.solar_dev_fixed_eur for t in techs)


# ---------------------------------------------------------------- shares


def _bp(weights: list[float]) -> list[int]:
    """Largest-remainder rounding to basis points (0.01 %), summing to exactly 10 000."""
    total = sum(weights)
    if total <= 0:
        weights, total = [1.0] * len(weights), float(len(weights))
    raw = [Decimal(repr(w)) / Decimal(repr(total)) * 10000 for w in weights]
    floors = [int(r.to_integral_value(ROUND_FLOOR)) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[: 10000 - sum(floors)]:
        floors[i] += 1
    return floors


def compute_plan(a: ParcelIn, b: ParcelIn, asm: Assumptions, method: Method = "shapley") -> dict[str, Any]:
    warnings: list[str] = []
    for p in (a, b):
        if p.d_substation_m is None:
            warnings.append(f"Partei {p.label}: no substation within 10 km in the audit; {DEFAULT_GRID_DISTANCE_M:.0f} m assumed")
        if p.technology not in ("wind", "solar"):
            raise ValueError(f"unsupported technology '{p.technology}'")
    dist = {p.label: p.d_substation_m if p.d_substation_m is not None else DEFAULT_GRID_DISTANCE_M for p in (a, b)}
    gap = a.poly_utm.distance(b.poly_utm)

    # --- standalone
    alone: dict[str, dict[str, Any]] = {}
    layouts_alone = {p.label: wind_layout(p, None, asm)[p.label] if p.technology == "wind" else [] for p in (a, b)}
    for p in (a, b):
        e = (econ_wind(len(layouts_alone[p.label]), p.wind_ms, asm) if p.technology == "wind"
             else econ_solar(p.area_ha, p.slope_deg, p.solar_kwh_per_kwp, asm))
        grid = asm.grid_fixed_eur + asm.cable_eur_per_m * dist[p.label]
        dev = _dev_cost({p.technology}, asm)
        npv = _operating_value(e, p.technology, asm) - e.capex - grid - dev if e.capacity_mw > 0 else -(grid + dev)
        alone[p.label] = {"econ": e, "grid": grid, "dev": dev, "npv": npv}

    # --- joint
    lay = wind_layout(a, b, asm) if a.technology == b.technology == "wind" else {
        p.label: layouts_alone[p.label] for p in (a, b)}
    joint_econ: dict[str, Econ] = {}
    for p in (a, b):
        joint_econ[p.label] = (econ_wind(len(lay[p.label]), p.wind_ms, asm) if p.technology == "wind"
                               else econ_solar(p.area_ha, p.slope_deg, p.solar_kwh_per_kwp, asm))
    grid_joint = asm.grid_fixed_eur + asm.cable_eur_per_m * (min(dist.values()) + gap)
    dev_joint = _dev_cost({a.technology, b.technology}, asm)
    op_joint = sum(_operating_value(joint_econ[p.label], p.technology, asm) - joint_econ[p.label].capex for p in (a, b))
    npv_joint = op_joint - grid_joint - dev_joint if any(e.capacity_mw > 0 for e in joint_econ.values()) else -(grid_joint + dev_joint)

    # --- Shapley and alternative keys
    vA, vB = max(alone[a.label]["npv"], 0.0), max(alone[b.label]["npv"], 0.0)
    vAB = max(npv_joint, 0.0)
    synergy = npv_joint - vA - vB  # reported as computed; negative = pooling is worse than going separate
    viable = vAB > 0
    beneficial = viable and synergy > 0
    phi = [vA + synergy / 2, vB + synergy / 2] if beneficial else [vA, vB]
    if not viable:
        warnings.append("joint project has no positive value under these assumptions")
    elif not beneficial:
        warnings.append("pooling does not create value versus developing the parcels separately (e.g. one parcel is unviable "
                        "on its own or a joint layout loses turbines) — reconsider the pool or the assumptions")
    area_bp = _bp([a.area_ha, b.area_ha])
    rev = [joint_econ[a.label].revenue, joint_econ[b.label].revenue]
    keys = {"shapley": _bp(phi) if beneficial else area_bp, "area": area_bp,
            "revenue": _bp(rev if sum(rev) > 0 else [a.area_ha, b.area_ha])}
    method_applied = method
    if method == "shapley" and not beneficial:
        method_applied = "area"
        warnings.append("Shapley split is only meaningful when pooling creates value — shares fall back to the area key")
    chosen = keys[method]

    total_rev = sum(rev)
    to_wgs = lambda xy: _TO_WGS.transform(*xy)  # noqa: E731
    parties = []
    for i, p in enumerate((a, b)):
        s, j = alone[p.label], joint_econ[p.label]
        parties.append({
            "label": p.label, "developer_id": p.developer_id, "company_name": p.company_name,
            "parcel": {"parcel_id": p.parcel_id, "area_ha": p.area_ha, "technology": p.technology,
                       "distance_to_substation_m": p.d_substation_m},
            "standalone": {"capacity_mw": round(s["econ"].capacity_mw, 2), "energy_mwh_a": round(s["econ"].energy_mwh),
                           "npv_eur": round(s["npv"]), "viable": s["npv"] > 0, "grid_cost_eur": round(s["grid"]),
                           "development_cost_eur": round(s["dev"])},
            "in_joint_project": {"capacity_mw": round(j.capacity_mw, 2), "energy_mwh_a": round(j.energy_mwh),
                                 "revenue_eur_a": round(j.revenue), "capex_eur": round(j.capex)},
            "value_contribution_shapley_eur": round(phi[i]),
            "share_bp": {"chosen": chosen[i], "shapley": keys["shapley"][i], "area": keys["area"][i], "revenue": keys["revenue"][i]},
            "indicative_revenue_share_eur_a": round(total_rev * chosen[i] / 10000),
        })
    grid_alone = alone[a.label]["grid"] + alone[b.label]["grid"]
    dev_alone = alone[a.label]["dev"] + alone[b.label]["dev"]
    return {
        "method": method, "method_applied": method_applied, "viable": viable, "pooling_beneficial": beneficial,
        "shares_bp": {a.label: chosen[0], b.label: chosen[1]},
        "joint_project": {
            "capacity_mw": round(sum(e.capacity_mw for e in joint_econ.values()), 2),
            "energy_mwh_a": round(sum(e.energy_mwh for e in joint_econ.values())),
            "revenue_eur_a": round(total_rev), "capex_eur": round(sum(e.capex for e in joint_econ.values())),
            "grid_connection_cost_eur": round(grid_joint), "development_cost_eur": round(dev_joint),
            "internal_cable_m": round(gap), "npv_eur": round(npv_joint),
            "wind_turbines": {lab: [dict(zip(("lon", "lat"), map(lambda v: round(v, 6), to_wgs(xy)))) for xy in pts]
                              for lab, pts in lay.items() if pts},
        },
        "synergy": {"total_eur": round(synergy), "grid_savings_eur": round(grid_alone - grid_joint),
                    "development_savings_eur": round(dev_alone - dev_joint),
                    "layout_and_other_eur": round(synergy - (grid_alone - grid_joint) - (dev_alone - dev_joint))},
        "parties": parties, "assumptions": asm.model_dump(), "warnings": warnings,
        "disclaimer": "Screening model with default assumptions; not a bankable financial model.",
    }
