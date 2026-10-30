"""Deterministic scoring and verdict logic: GisResult + Site -> AuditReport.

Same inputs always give the same report. All thresholds are in this file.
"""
from __future__ import annotations

from typing import Optional

from .gis_engine import ALKIS_CLOSED, GisResult, Site
from .models import (
    AuditReport,
    EnvironmentalVeto,
    GridAccess,
    LandCheckInput,
    LocationInfo,
    RegulatoryChecks,
    ResourcePotential,
    TechnologyAssessment,
    Verdict,
)

GO_MIN_SCORE = 65
NO_GO_MAX_SCORE = 40
VETO_SCORE_CAP = 10
PROTECTED_PENALTY = 15  # FFH / SPA overlap

WIND_MIN_MS = 5.5  # required annual-mean-like speed at 100 m
WIND_HARD_FLOOR_MS = 5.0  # below this: not viable; 5.0-5.5 -> conditional (data is a 30-day mean)
RES_GO_M, RES_COND_M = 800.0, 600.0

_RANK = {"GO": 2, "CONDITIONAL_GO": 1, "NO_GO": 0}


def _lin(x: float, zero_at: float, full_at: float) -> float:
    """0..1 ramp: 0 at `zero_at`, 1 at `full_at` (works for either direction)."""
    if zero_at == full_at:
        return 1.0
    return max(0.0, min(1.0, (x - zero_at) / (full_at - zero_at)))


def grid_score(sub_m: Optional[float], line_m: Optional[float]) -> int:
    """0-100: 60% substation proximity (full <=1 km, zero >=10 km), 40% power line (full <=250 m, zero >=5 km)."""
    s = 100 * _lin(sub_m, 10_000, 1_000) if sub_m is not None else 0.0
    ln = 100 * _lin(line_m, 5_000, 250) if line_m is not None else 0.0
    return round(0.6 * s + 0.4 * ln)


def _verdict(score: float, blockers: list[str], conditions: list[str]) -> Verdict:
    if blockers or score < NO_GO_MAX_SCORE:
        return "NO_GO"
    if score >= GO_MIN_SCORE and not conditions:
        return "GO"
    return "CONDITIONAL_GO"


def _solar(e_y, slope, area_ha, grid, corridor, missing) -> TechnologyAssessment:
    b: dict[str, float] = {}
    blockers: list[str] = []
    conds: list[str] = []
    b["resource"] = 30 * _lin(e_y, 850, 1100) if e_y is not None else 0.0
    b["grid"] = 25 * grid / 100
    b["terrain"] = 15 * _lin(slope, 15, 5) if slope is not None else 7.5
    b["eeg_corridor"] = 20.0 if corridor else 8.0
    b["size"] = 10 * _lin(area_ha, 0, 2)
    if e_y is None:
        conds.append("solar yield unavailable (PVGIS)")
    if slope is not None and slope > 20:
        blockers.append(f"terrain too steep for PV ({slope:.1f}° > 20°)")
    elif slope is not None and slope > 10:
        conds.append(f"steep terrain ({slope:.1f}°) raises mounting cost")
    if area_ha < 0.5:
        conds.append(f"very small site ({area_ha:.2f} ha)")
    if not corridor:
        conds.append("outside the 500 m EEG highway/rail corridor — needs auction/Freiflächen designation")
    conds += [f"{m} unavailable" for m in missing]
    score = round(sum(b.values()))
    return TechnologyAssessment(
        verdict=_verdict(score, blockers, conds), score=score,
        score_breakdown={k: round(v, 1) for k, v in b.items()}, blockers=blockers, conditions=conds,
    )


def _wind(v100, slope, res_m, turbines, grid, missing) -> TechnologyAssessment:
    b: dict[str, float] = {}
    blockers: list[str] = []
    conds: list[str] = []
    b["wind_speed"] = 35 * _lin(v100, 4.5, 6.5) if v100 is not None else 0.0
    if res_m is None or res_m >= RES_GO_M:
        b["setback"] = 25.0
    elif res_m >= RES_COND_M:
        b["setback"] = 12.0
        conds.append(f"residential area {res_m:.0f} m away (< {RES_GO_M:.0f} m) — TA Lärm / permitting risk")
    else:
        b["setback"] = 0.0
        blockers.append(f"residential area only {res_m:.0f} m away (< {RES_COND_M:.0f} m)")
    b["grid"] = 20 * grid / 100
    b["terrain"] = 10 * _lin(slope, 20, 10) if slope is not None else 5.0
    # existing turbines nearby hint at a designated wind area (positive) until wake effects dominate
    b["neighbourhood"] = 6.0 if turbines == 0 else (10.0 if turbines <= 5 else 5.0)
    if v100 is None:
        conds.append("wind speed unavailable (Open-Meteo)")
    elif v100 < WIND_HARD_FLOOR_MS:
        blockers.append(f"mean wind {v100:.2f} m/s at 100 m (< {WIND_HARD_FLOOR_MS} m/s)")
    elif v100 < WIND_MIN_MS:
        conds.append(f"mean wind {v100:.2f} m/s at 100 m below {WIND_MIN_MS} m/s requirement")
    if slope is not None and slope > 25:
        blockers.append(f"terrain too steep for turbine foundations/cranes ({slope:.1f}° > 25°)")
    conds += [f"{m} unavailable" for m in missing]
    score = round(sum(b.values()))
    return TechnologyAssessment(
        verdict=_verdict(score, blockers, conds), score=score,
        score_breakdown={k: round(v, 1) for k, v in b.items()}, blockers=blockers, conditions=conds,
    )


def build_report(inp: LandCheckInput, site: Site, gis: GisResult) -> AuditReport:
    warnings: list[str] = []
    dq: dict[str, str] = {}

    def status(key: str, ok_note: str = "ok") -> str:
        return f"unavailable: {gis.errors[key]}" if key in gis.errors else ok_note

    # ---- location
    admin = gis.admin or {}
    dq["admin_boundaries"] = status("admin", "ok (BKG VG250)")
    if gis.errors.get("alkis"):
        dq["alkis"] = f"unavailable: {gis.errors['alkis']}"
    else:
        dq["alkis"] = "closed state: area computed from input geometry" if gis.parcel == ALKIS_CLOSED else "ok"
    location = LocationInfo(
        bundesland=admin.get("bundesland"), municipality=admin.get("municipality"),
        area_hectares=round(site.area_ha, 2), alkis_parcel_id=gis.parcel,
    )

    # ---- grid / regulatory (OSM)
    osm = gis.osm
    dq["osm_overpass"] = status("osm", "ok (OpenStreetMap)")
    if osm:
        gscore = grid_score(osm["substation_m"], osm["powerline_m"])
        grid = GridAccess(
            nearest_substation_m=osm["substation_m"],
            substation_name=osm["substation_name"] or "none within 10 km",
            substation_operator=osm.get("substation_operator"),
            nearest_powerline_m=osm["powerline_m"], grid_score=gscore,
        )
        reg = RegulatoryChecks(
            distance_to_residential_m=osm["residential_m"],
            in_eeg_highway_rail_corridor_500m=osm["eeg_corridor"],
            existing_wind_turbines_within_2km=osm["wind_turbines_2km"],
        )
        if osm["substation_m"] is None:
            warnings.append("no substation within 10 km in OpenStreetMap")
    else:
        grid, reg, gscore = GridAccess(), RegulatoryChecks(), 0
        warnings.append("grid and setback data unavailable (OpenStreetMap/Overpass query failed)")

    # ---- environment
    pr = gis.protected
    dq["protected_areas"] = status("protected", f"ok ({pr.source})" if pr else "n/a")
    zones = pr.zones if pr else []
    veto_zones = [z for z in zones if z.veto]
    soft_zones = [z for z in zones if not z.veto]
    env = EnvironmentalVeto(has_veto=bool(veto_zones), intersecting_protected_zones=[z.label() for z in zones])
    if soft_zones:
        warnings.append("site intersects Natura 2000 area(s): appropriate assessment (FFH-/SPA-Verträglichkeitsprüfung) required")
    if pr is None:
        warnings.append("protected-area check unavailable — veto status could not be verified")

    # ---- resources
    solar, wind, slope = gis.solar, gis.wind, gis.slope
    dq["solar_pvgis"] = status("solar", "ok (EU JRC PVGIS 5.2)")
    dq["wind_open_meteo"] = status("wind", f"ok ({wind['method']})" if wind else "n/a")
    dq["terrain_slope"] = status("slope", "ok (Copernicus DEM 90 m via Open-Meteo, 3x3 Horn)")
    elevation = (slope or {}).get("elevation") or (solar or {}).get("elevation") or (wind or {}).get("elevation")
    res = ResourcePotential(
        solar_kwh_per_kwp_year=round(solar["e_y"], 1) if solar else None,
        wind_speed_100m_ms=round(wind["v100"], 2) if wind else None,
        slope_degrees=round(slope["slope_deg"], 1) if slope else None,
        elevation_m=round(elevation, 0) if elevation is not None else None,
    )
    if wind:
        warnings.append("wind speed is a 30-day DWD ICON mean (seasonal bias) — confirm with an annual wind-resource study")

    # ---- per-technology assessment
    osm_missing = ["grid/setback data (OSM)"] if osm is None else []
    tech: dict[str, TechnologyAssessment] = {}
    if inp.project_type in ("solar", "hybrid"):
        tech["solar"] = _solar(
            res.solar_kwh_per_kwp_year, res.slope_degrees, site.area_ha, gscore,
            reg.in_eeg_highway_rail_corridor_500m, osm_missing,
        )
    if inp.project_type in ("wind", "hybrid"):
        tech["wind"] = _wind(
            res.wind_speed_100m_ms, res.slope_degrees, reg.distance_to_residential_m,
            reg.existing_wind_turbines_within_2km, gscore, osm_missing,
        )
    if "wind" in tech and reg.existing_wind_turbines_within_2km > 5:
        warnings.append(
            f"{reg.existing_wind_turbines_within_2km} wind turbines within 2 km: likely an existing wind area, "
            "but check wake losses and remaining capacity"
        )
    best_name = max(tech, key=lambda k: (_RANK[tech[k].verdict], tech[k].score))  # ties -> first (solar)
    best = tech[best_name]
    verdict, score = best.verdict, best.score

    # ---- environmental overlay (applied last, always wins)
    if env.has_veto:
        verdict, score = "NO_GO", min(score, VETO_SCORE_CAP)
        warnings.append("environmental veto: site intersects " + "; ".join(z.label() for z in veto_zones))
    else:
        if soft_zones:
            score = max(0, score - PROTECTED_PENALTY)
            if verdict == "GO":
                verdict = "CONDITIONAL_GO"
        # unverifiable core checks can never produce an unconditional GO
        if verdict == "GO" and (pr is None or osm is None):
            verdict = "CONDITIONAL_GO"
        if score < NO_GO_MAX_SCORE:  # e.g. the Natura 2000 penalty pushed a marginal site under the floor
            verdict = "NO_GO"
    warnings += [f"[{best_name}] {c}" for c in best.conditions] + [f"[{best_name}] BLOCKER: {x}" for x in best.blockers]

    return AuditReport(
        overall_verdict=verdict, score=max(0, min(100, int(score))), project_type=inp.project_type,
        assessed_technology=best_name, location_info=location, grid_access=grid, regulatory_checks=reg,
        environmental_veto=env, resource_potential=res, technology_assessment=tech,
        warnings=warnings, data_quality=dq,
    )
