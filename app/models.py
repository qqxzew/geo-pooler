"""Pydantic models: job input and the audit report."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Germany bounding box used for input validation.
LAT_MIN, LAT_MAX = 47.2, 55.1
LON_MIN, LON_MAX = 5.8, 15.1

ProjectType = Literal["wind", "solar", "hybrid"]
Verdict = Literal["GO", "CONDITIONAL_GO", "NO_GO"]


class LandCheckInput(BaseModel):
    """Either (lat, lon[, radius_m]) or polygon_geojson."""

    lat: Optional[float] = Field(None, description="Latitude (WGS84), 47.2..55.1")
    lon: Optional[float] = Field(None, description="Longitude (WGS84), 5.8..15.1")
    radius_m: float = Field(300.0, gt=0, le=5000, description="Radius of the analysed area around the point")
    polygon_geojson: Optional[dict[str, Any]] = Field(
        None, description="GeoJSON Polygon (or Feature wrapping one) in EPSG:4326"
    )
    project_type: ProjectType = "hybrid"

    @field_validator("lat")
    @classmethod
    def _lat(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (LAT_MIN <= v <= LAT_MAX):
            raise ValueError(f"lat must be within Germany ({LAT_MIN}..{LAT_MAX})")
        return v

    @field_validator("lon")
    @classmethod
    def _lon(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (LON_MIN <= v <= LON_MAX):
            raise ValueError(f"lon must be within Germany ({LON_MIN}..{LON_MAX})")
        return v

    @model_validator(mode="after")
    def _one_geometry(self) -> "LandCheckInput":
        has_point = self.lat is not None and self.lon is not None
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be given together")
        if not has_point and self.polygon_geojson is None:
            raise ValueError("provide either lat+lon or polygon_geojson")
        return self


# --- report ---------------------------------------------------------------


class LocationInfo(BaseModel):
    bundesland: Optional[str] = None
    municipality: Optional[str] = None
    area_hectares: float
    alkis_parcel_id: Optional[str] = None


class GridAccess(BaseModel):
    # null = nothing found inside the search radius (substation 10 km, power line 5 km)
    nearest_substation_m: Optional[float] = None
    substation_name: Optional[str] = None
    substation_operator: Optional[str] = None  # OSM operator tag of that substation (grid operator hint)
    nearest_powerline_m: Optional[float] = None
    grid_score: int = 0


class RegulatoryChecks(BaseModel):
    # null = no residential area within 1500 m of the site
    distance_to_residential_m: Optional[float] = None
    in_eeg_highway_rail_corridor_500m: bool = False
    existing_wind_turbines_within_2km: int = 0


class EnvironmentalVeto(BaseModel):
    has_veto: bool = False
    intersecting_protected_zones: list[str] = Field(default_factory=list)


class ResourcePotential(BaseModel):
    solar_kwh_per_kwp_year: Optional[float] = None
    wind_speed_100m_ms: Optional[float] = None
    slope_degrees: Optional[float] = None
    elevation_m: Optional[float] = None


class TechnologyAssessment(BaseModel):
    verdict: Verdict
    score: int
    score_breakdown: dict[str, float]
    blockers: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    overall_verdict: Verdict
    score: int = Field(ge=0, le=100)
    project_type: ProjectType
    assessed_technology: Optional[str] = None
    location_info: LocationInfo
    grid_access: GridAccess
    regulatory_checks: RegulatoryChecks
    environmental_veto: EnvironmentalVeto
    resource_potential: ResourcePotential
    technology_assessment: dict[str, TechnologyAssessment]
    warnings: list[str] = Field(default_factory=list)
    # per check: "ok" | "ok (fallback: ...)" | "unavailable: <reason>"
    data_quality: dict[str, str] = Field(default_factory=dict)
    methodology: str = "land-audit-worker/1 (see README: scoring)"
