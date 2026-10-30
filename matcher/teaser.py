"""Anonymous teaser construction.

A teaser tells a developer "a compatible parcel exists next to yours" without letting them locate it:
no coordinates, no municipality, no parcel id, no developer identity — only banded values.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

AREA_BANDS = [(5, "<5 ha"), (10, "5-10 ha"), (25, "10-25 ha"), (50, "25-50 ha"), (100, "50-100 ha"), (math.inf, ">100 ha")]
DISTANCE_BANDS = [(500, "<500 m"), (1000, "500-1000 m"), (math.inf, "1000-1500 m")]
SCORE_BANDS = [(60, "40-59"), (80, "60-79"), (math.inf, "80-100")]


def band(value: float, bands: list[tuple[float, str]]) -> str:
    for upper, label in bands:
        if value < upper:
            return label
    return bands[-1][1]


def build_teaser(match_id: str, own: dict[str, Any], other: dict[str, Any], distance_m: float) -> dict[str, Any]:
    same_tech = own["assessed_technology"] == other["assessed_technology"]
    return {
        "teaser_id": uuid.uuid4().hex,
        "match_id": match_id,
        "your_parcel_id": own["id"],
        "neighbour": {
            "technology": other["assessed_technology"],
            "area_band": band(other["area_ha"], AREA_BANDS),
            "audit_verdict": other["verdict"],
            "audit_score_band": band(other["score"], SCORE_BANDS),
            "bundesland": other["bundesland"],
        },
        "distance_band": band(distance_m, DISTANCE_BANDS),
        "combined_area_band": band(own["area_ha"] + other["area_ha"], AREA_BANDS),
        "synergy": (
            "same technology: shared layout, grid connection and permitting"
            if same_tech
            else f"complementary: {own['assessed_technology']} + {other['assessed_technology']} (hybrid project)"
        ),
        "note": "Identity, exact location and boundaries are disclosed only after both sides agree and sign the NDA.",
    }
