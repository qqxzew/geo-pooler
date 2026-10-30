"""Real pan-German open-data queries, executed concurrently.

Nothing in here is mocked: every check calls a live public endpoint. A check that
fails is reported in ``GisResult.errors`` (and later in the report's
``data_quality`` / ``warnings``) — it never silently falls back to made-up values.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from lxml import etree
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.ops import transform as shp_transform
from shapely.validation import make_valid

from .config import Settings
from .models import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, LandCheckInput

# EPSG:25832 (ETRS89 / UTM 32N) is the metric CRS mandated for all distance/area maths.
_TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)
_TO_WGS = Transformer.from_crs(25832, 4326, always_xy=True)

URN_4326 = "urn:ogc:def:crs:EPSG::4326"  # unambiguous lat,lon axis order in WFS 2.0

VG250_URL = "https://sgx.geodatenzentrum.de/wfs_vg250"
BFN_WFS_URL = "https://geodienste.bfn.de/ogc/wfs/schutzgebiet"
PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
OPEN_METEO_WIND_URL = "https://api.open-meteo.com/v1/dwd-icon"
OPEN_METEO_ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
EEA_BASE = "https://bio.discomap.eea.europa.eu/arcgis/rest/services/ProtectedSites"

BUNDESLAENDER = {
    "01": "Schleswig-Holstein", "02": "Hamburg", "03": "Niedersachsen", "04": "Bremen",
    "05": "Nordrhein-Westfalen", "06": "Hessen", "07": "Rheinland-Pfalz", "08": "Baden-Württemberg",
    "09": "Bayern", "10": "Saarland", "11": "Berlin", "12": "Brandenburg",
    "13": "Mecklenburg-Vorpommern", "14": "Sachsen", "15": "Sachsen-Anhalt", "16": "Thüringen",
}

# Overpass search radii (m) around the site — also the "not found" cut-offs reported to the user.
R_SUBSTATION, R_POWERLINE, R_RESIDENTIAL, R_CORRIDOR, R_TURBINE = 10000, 5000, 1500, 600, 3000


# --------------------------------------------------------------------------- site


@dataclass
class Site:
    poly_wgs: Polygon  # lon/lat
    poly_utm: Polygon  # EPSG:25832, metres
    lat: float  # representative point (always inside the polygon)
    lon: float
    area_ha: float
    reach_m: float  # max distance from the representative point to the polygon boundary


def _to_utm(geom):
    return shp_transform(_TO_UTM.transform, geom)


def _to_wgs(geom):
    return shp_transform(_TO_WGS.transform, geom)


def build_site(inp: LandCheckInput, settings: Settings) -> Site:
    """Build the analysis polygon from lat/lon(+radius) or a GeoJSON polygon."""
    if inp.polygon_geojson is not None:
        gj = inp.polygon_geojson
        if gj.get("type") == "Feature":
            gj = gj.get("geometry") or {}
        if gj.get("type") != "Polygon":
            raise ValueError("polygon_geojson must be a GeoJSON Polygon in EPSG:4326")
        poly_wgs = shape(gj)
        if not poly_wgs.is_valid:
            poly_wgs = make_valid(poly_wgs)
            if poly_wgs.geom_type == "MultiPolygon":
                poly_wgs = max(poly_wgs.geoms, key=lambda g: g.area)
            if poly_wgs.geom_type != "Polygon":
                raise ValueError("polygon_geojson is not a valid polygon")
        minx, miny, maxx, maxy = poly_wgs.bounds
        if not (LON_MIN <= minx and maxx <= LON_MAX and LAT_MIN <= miny and maxy <= LAT_MAX):
            raise ValueError("polygon must lie within Germany's bounding box (coordinates are lon,lat)")
        poly_utm = _to_utm(poly_wgs)
    else:
        x, y = _TO_UTM.transform(inp.lon, inp.lat)
        poly_utm = Point(x, y).buffer(inp.radius_m, quad_segs=16)
        poly_wgs = _to_wgs(poly_utm)

    area_ha = poly_utm.area / 10_000.0
    if area_ha <= 0:
        raise ValueError("polygon has zero area")
    if area_ha > settings.max_area_ha:
        raise ValueError(f"area {area_ha:.0f} ha exceeds the limit of {settings.max_area_ha:.0f} ha")

    rp = poly_utm.representative_point()
    lon, lat = _TO_WGS.transform(rp.x, rp.y)
    reach = max(math.dist((rp.x, rp.y), c) for c in poly_utm.exterior.coords)
    return Site(poly_wgs, poly_utm, lat, lon, area_ha, reach)


# --------------------------------------------------------------------------- http


async def _request(
    client: httpx.AsyncClient, method: str, url: str, *, retries: int, **kw: Any
) -> httpx.Response:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = await client.request(method, url, **kw)
            if r.status_code in (429, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last or RuntimeError("request failed")  # pragma: no cover


def _explain(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    return f"{type(e).__name__}: {e}"[:200]


# --------------------------------------------------------------------------- 1. admin (BKG VG250)


async def query_admin(client: httpx.AsyncClient, site: Site, retries: int) -> dict[str, Any]:
    lat, lon = site.lat, site.lon
    params = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": "vg250:vg250_gem", "SRSNAME": "EPSG:4326",
        # lat,lon order with the URN CRS; the plain "EPSG:4326" label is interpreted lon,lat by this server.
        "BBOX": f"{lat - 0.001},{lon - 0.001},{lat + 0.001},{lon + 0.001},{URN_4326}",
        "OUTPUTFORMAT": "application/json",
    }
    r = await _request(client, "GET", VG250_URL, retries=retries, params=params)
    feats = r.json().get("features", [])
    if not feats:
        raise LookupError("no municipality found (point outside Germany's land area?)")
    pt = Point(lon, lat)
    chosen = None
    for f in feats:  # prefer the land-area feature (gf=4) that actually contains the point
        try:
            if shape(f["geometry"]).contains(pt) and str(f["properties"].get("gf", "4")) in ("4", "4.0"):
                chosen = f
                break
        except Exception:
            continue
    if chosen is None:
        chosen = next((f for f in feats if str(f["properties"].get("gf", "4")) in ("4", "4.0")), feats[0])
    p = chosen["properties"]
    sn_l = str(p.get("sn_l", "")).zfill(2)
    return {
        "municipality": p.get("gen"),
        "bundesland": BUNDESLAENDER.get(sn_l) or p.get("nbd_land") or None,
        "ags": p.get("ags"),
    }


# --------------------------------------------------------------------------- 2. OSM Overpass


def _overpass_query(site: Site) -> str:
    lat, lon, ex = site.lat, site.lon, math.ceil(site.reach_m)
    return f"""[out:json][timeout:25];
(
  node["power"="substation"](around:{R_SUBSTATION + ex},{lat},{lon});
  way["power"="substation"](around:{R_SUBSTATION + ex},{lat},{lon});
  way["power"="line"](around:{R_POWERLINE + ex},{lat},{lon});
  way["landuse"="residential"](around:{R_RESIDENTIAL + ex},{lat},{lon});
  way["highway"~"^(motorway|trunk)$"](around:{R_CORRIDOR + ex},{lat},{lon});
  way["railway"="rail"](around:{R_CORRIDOR + ex},{lat},{lon});
  node["power"="generator"]["generator:source"="wind"](around:{R_TURBINE + ex},{lat},{lon});
);
out geom;"""
    # NB: `out geom` instead of `out center` — distances to long lines / large polygons need real geometry.


def _osm_geom(el: dict[str, Any]):
    if el["type"] == "node":
        return Point(el["lon"], el["lat"])
    coords = [(g["lon"], g["lat"]) for g in el.get("geometry", []) if g]
    if len(coords) < 2:
        return None
    if len(coords) >= 4 and coords[0] == coords[-1] and el.get("tags", {}).get("landuse"):
        return Polygon(coords)
    return LineString(coords)


async def _overpass_fetch(client: httpx.AsyncClient, query: str, endpoints: list[str]) -> dict[str, Any]:
    """Hedged request over the Overpass mirrors.

    Public Overpass servers are often "at capacity" (instant 429/502/504) or simply hang, so the
    mirrors are started staggered (0 s, 6 s, 12 s, …) and the first valid answer wins; the rest are
    cancelled. Hard deadline 70 s.
    """
    errs: list[str] = []

    async def one(url: str, delay: float) -> dict[str, Any]:
        await asyncio.sleep(delay)
        r = await _request(client, "POST", url, retries=0, data={"data": query},
                           timeout=httpx.Timeout(40.0, connect=8.0))
        body = r.json()
        if "elements" not in body:
            raise ValueError("no 'elements' in Overpass response")
        if "runtime error" in str(body.get("remark", "")).lower():
            raise ValueError(str(body["remark"])[:120])
        return body

    tasks = {asyncio.create_task(one(u, 6.0 * i)): u for i, u in enumerate(endpoints)}
    pending = set(tasks)
    try:
        async with asyncio.timeout(70):
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() is None:
                        return t.result()
                    errs.append(f"{tasks[t].split('/')[2]}: {_explain(t.exception())}")
    except TimeoutError:
        errs.append("overall 70 s deadline")
    finally:
        for t in tasks:
            t.cancel()
    raise ConnectionError("all Overpass mirrors failed (" + "; ".join(errs) + ")")


async def query_osm(client: httpx.AsyncClient, site: Site, settings: Settings) -> dict[str, Any]:
    q = _overpass_query(site)
    data = await _overpass_fetch(client, q, settings.overpass_endpoints)

    poly = site.poly_utm
    best_sub: tuple[float, str] | None = None
    best_line: float | None = None
    best_res: float | None = None
    corridor = False
    turbines: set[int] = set()
    for el in data["elements"]:
        tags = el.get("tags", {})
        g = _osm_geom(el)
        if g is None:
            continue
        d = poly.distance(_to_utm(g))
        if tags.get("power") == "substation":
            label = tags.get("name") or tags.get("operator") or "unnamed substation"
            if tags.get("voltage"):
                label += f" ({tags['voltage']} V)"
            if best_sub is None or d < best_sub[0]:
                best_sub = (d, label)
        elif tags.get("power") == "line":
            best_line = d if best_line is None else min(best_line, d)
        elif tags.get("landuse") == "residential":
            best_res = d if best_res is None else min(best_res, d)
        elif tags.get("highway") == "motorway":
            corridor = corridor or d <= 500
        elif tags.get("railway") == "rail" and not tags.get("service"):
            corridor = corridor or d <= 500  # ignore sidings/yards
        elif tags.get("power") == "generator" and tags.get("generator:source") == "wind":
            if d <= 2000:
                turbines.add(el["id"])
    if best_sub is not None and best_sub[0] > R_SUBSTATION:
        best_sub = None
    if best_line is not None and best_line > R_POWERLINE:
        best_line = None
    if best_res is not None and best_res > R_RESIDENTIAL:
        best_res = None
    return {
        "substation_m": round(best_sub[0], 1) if best_sub else None,
        "substation_name": best_sub[1] if best_sub else None,
        "powerline_m": round(best_line, 1) if best_line is not None else None,
        "residential_m": round(best_res, 1) if best_res is not None else None,
        "eeg_corridor": corridor,
        "wind_turbines_2km": len(turbines),
    }


# --------------------------------------------------------------------------- 3. protected areas


@dataclass
class Zone:
    category: str  # NSG | NP | FFH | SPA
    name: str

    @property
    def veto(self) -> bool:
        return self.category in ("NSG", "NP")

    def label(self) -> str:
        pretty = {"NSG": "Naturschutzgebiet", "NP": "Nationalpark", "FFH": "FFH-Gebiet", "SPA": "Vogelschutzgebiet"}
        return f"{pretty[self.category]}: {self.name}"


@dataclass
class ProtectedResult:
    zones: list[Zone]
    source: str


_BFN_CATEGORIES = [
    ("NSG", re.compile(r"naturschutzgebiet", re.I)),
    ("NP", re.compile(r"nationalpark", re.I)),
    ("FFH", re.compile(r"ffh|fauna.?flora|habitat", re.I)),
    ("SPA", re.compile(r"vogelschutz", re.I)),
]
_NAME_KEYS = ("name", "gebietsname", "gebiet_name", "ge_name", "sitename", "gebiet", "bezeichnung", "titel")


def _swap_if_latlon(x: float, y: float) -> tuple[float, float]:
    """Germany: lat 47..55, lon 5..15 never overlap, so a swapped pair is detectable."""
    if LAT_MIN - 1 <= x <= LAT_MAX + 1 and LON_MIN - 1 <= y <= LON_MAX + 1:
        return y, x
    return x, y


def _poly_from_gml_pos(text: str) -> list[tuple[float, float]]:
    nums = [float(t) for t in text.split()]
    pts = [_swap_if_latlon(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
    return pts


def _gml_polygons(root, crs_utm: bool = False) -> list[Polygon]:
    """All gml:Polygon elements as shapely polygons (coordinates as found in the document)."""
    out = []
    for p in root.iter("{*}Polygon"):
        ext = p.find("{*}exterior//{*}posList")
        if ext is None or not (ext.text or "").strip():
            continue
        shell = _poly_from_gml_pos(ext.text) if not crs_utm else _pairs(ext.text)
        holes = []
        for h in p.findall("{*}interior//{*}posList"):
            holes.append(_poly_from_gml_pos(h.text) if not crs_utm else _pairs(h.text))
        try:
            out.append(Polygon(shell, holes))
        except Exception:
            continue
    return out


def _pairs(text: str) -> list[tuple[float, float]]:
    nums = [float(t) for t in text.split()]
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def _feature_name(props: dict[str, Any]) -> str:
    low = {k.lower(): v for k, v in props.items() if isinstance(v, str) and v.strip()}
    for k in _NAME_KEYS:
        if k in low:
            return low[k].strip()
    for k, v in low.items():
        if "name" in k:
            return v.strip()
    return next(iter(low.values()), "unnamed")


def _parse_wfs_features(resp: httpx.Response, site: Site) -> list[str]:
    """Return names of features intersecting the site. Accepts GeoJSON or GML 3.x."""
    ctype = resp.headers.get("content-type", "")
    text_head = resp.content[:200].lstrip()
    names: list[str] = []
    if "json" in ctype or text_head.startswith(b"{"):
        for f in resp.json().get("features", []):
            geom = f.get("geometry")
            if not geom:
                continue
            g = shape(geom)
            # GeoServer may emit lat,lon for some CRS declarations — normalise.
            if g.bounds[0] > 40:
                g = shp_transform(lambda x, y, z=None: (y, x), g)
            if g.intersects(site.poly_wgs):
                names.append(_feature_name(f.get("properties", {})))
        return names
    root = etree.fromstring(resp.content)
    if root.tag.endswith("ExceptionReport") or root.tag.endswith("ServiceExceptionReport"):
        raise ValueError("WFS returned an exception report")
    for member in root.iter("{*}member"):
        polys = _gml_polygons(member)
        if not polys:
            continue
        if any(p.buffer(0).intersects(site.poly_wgs) for p in polys):
            props = {c.tag.split("}")[-1]: (c.text or "") for c in member.iter() if c.text and c.text.strip()}
            names.append(_feature_name(props))
    return names


async def _query_bfn(client: httpx.AsyncClient, site: Site, retries: int) -> ProtectedResult:
    caps = await _request(
        client, "GET", BFN_WFS_URL, retries=retries,
        params={"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetCapabilities"},
    )
    root = etree.fromstring(caps.content)  # raises on maintenance HTML
    types: dict[str, str] = {}
    for ft in root.iter("{*}FeatureType"):
        name = ft.findtext("{*}Name") or ""
        title = ft.findtext("{*}Title") or ""
        for cat, rx in _BFN_CATEGORIES:
            if cat not in types and rx.search(f"{name} {title}"):
                types[cat] = name
    if not types:
        raise LookupError("BfN capabilities list none of the expected layers")
    minx, miny, maxx, maxy = site.poly_wgs.bounds
    zones: list[Zone] = []

    async def one(cat: str, typename: str) -> None:
        params = {
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": typename,
            "SRSNAME": "EPSG:4326", "BBOX": f"{miny},{minx},{maxy},{maxx},{URN_4326}",
            "OUTPUTFORMAT": "application/json",
        }
        try:
            r = await _request(client, "GET", BFN_WFS_URL, retries=retries, params=params)
            names = _parse_wfs_features(r, site)
        except (httpx.HTTPStatusError, ValueError, etree.XMLSyntaxError):
            params.pop("OUTPUTFORMAT")  # server may not offer JSON -> GML fallback
            r = await _request(client, "GET", BFN_WFS_URL, retries=retries, params=params)
            names = _parse_wfs_features(r, site)
        zones.extend(Zone(cat, n) for n in names)

    await asyncio.gather(*(one(c, t) for c, t in types.items()))
    return ProtectedResult(zones, "BfN WFS (geodienste.bfn.de)")


def _esri_polygon(site: Site) -> str:
    return json.dumps({"rings": [list(map(list, site.poly_wgs.exterior.coords))], "spatialReference": {"wkid": 4326}})


async def _query_eea(client: httpx.AsyncClient, site: Site, retries: int) -> ProtectedResult:
    """Fallback: EEA CDDA (national designations) + Natura 2000 ArcGIS REST, German sites only."""
    geom = _esri_polygon(site)
    base = {
        "f": "json", "geometry": geom, "geometryType": "esriGeometryPolygon", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
    }
    cdda = _request(
        client, "GET", f"{EEA_BASE}/CDDAv21_Dyna_WM/MapServer/4/query", retries=retries,
        params={**base, "where": "cddaCountryCode='DE'", "outFields": "siteName,designationTypeCode,nationalId"},
    )
    n2k = _request(
        client, "GET", f"{EEA_BASE}/Natura2000_Dyna_WM/MapServer/0/query", retries=retries,
        params={**base, "where": "MS='DE'", "outFields": "SITECODE,SITENAME,SITETYPE"},
    )
    r1, r2 = await asyncio.gather(cdda, n2k)
    zones: list[Zone] = []
    for body in (r1.json(), r2.json()):
        if "error" in body:
            raise ValueError(f"EEA service error: {str(body['error'])[:150]}")
    for f in r1.json().get("features", []):
        a = f["attributes"]
        m = re.match(r"^DE\d+([a-z]+)_", a.get("nationalId") or "")
        kind = m.group(1) if m else ""
        if kind == "nlp":
            zones.append(Zone("NP", a.get("siteName") or "unnamed"))
        elif kind == "nsg":
            zones.append(Zone("NSG", a.get("siteName") or "unnamed"))
    for f in r2.json().get("features", []):
        a = f["attributes"]
        name = f"{a.get('SITENAME') or 'unnamed'} ({a.get('SITECODE')})"
        if a.get("SITETYPE") in ("B", "C"):
            zones.append(Zone("FFH", name))
        if a.get("SITETYPE") in ("A", "C"):
            zones.append(Zone("SPA", name))
    return ProtectedResult(zones, "EEA CDDA + Natura 2000 (fallback)")


async def query_protected(client: httpx.AsyncClient, site: Site, retries: int) -> ProtectedResult:
    try:
        return await _query_bfn(client, site, retries)
    except Exception as bfn_err:
        try:
            res = await _query_eea(client, site, retries)
        except Exception as eea_err:
            raise ConnectionError(f"BfN: {_explain(bfn_err)}; EEA fallback: {_explain(eea_err)}") from eea_err
        res.source += f" — BfN unavailable ({_explain(bfn_err)})"
        return res


# --------------------------------------------------------------------------- 4. solar / wind / slope


async def query_solar(client: httpx.AsyncClient, site: Site, retries: int) -> dict[str, Any]:
    params = {
        "lat": round(site.lat, 5), "lon": round(site.lon, 5), "peakpower": 1, "loss": 14,
        "optimalangles": 1,  # without this PVGIS assumes a flat (0°) array and under-reports yield
        "outputformat": "json",
    }
    r = await _request(client, "GET", PVGIS_URL, retries=retries, params=params)
    body = r.json()
    fixed = body["outputs"]["totals"]["fixed"]
    mount = body["inputs"].get("mounting_system", {}).get("fixed", {})
    return {
        "e_y": float(fixed["E_y"]),
        "elevation": body["inputs"]["location"].get("elevation"),
        "opt_slope": mount.get("slope", {}).get("value"),
        "opt_azimuth": mount.get("azimuth", {}).get("value"),
    }


def _mean(vals: list[Optional[float]]) -> Optional[float]:
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


async def query_wind(client: httpx.AsyncClient, site: Site, retries: int) -> dict[str, Any]:
    params = {
        "latitude": round(site.lat, 5), "longitude": round(site.lon, 5),
        "hourly": "wind_speed_100m,wind_speed_80m,wind_speed_120m",
        "past_days": 30, "forecast_days": 1,
        "wind_speed_unit": "ms",  # API default is km/h
    }
    r = await _request(client, "GET", OPEN_METEO_WIND_URL, retries=retries, params=params)
    body = r.json()
    h = body["hourly"]
    v100 = _mean(h.get("wind_speed_100m", []))
    how = "DWD ICON wind_speed_100m, 30-day mean"
    if v100 is None:
        v80, v120 = _mean(h.get("wind_speed_80m", [])), _mean(h.get("wind_speed_120m", []))
        if v80 is None or v120 is None:
            raise LookupError("Open-Meteo returned no wind data for this location")
        v100 = v80 + (v120 - v80) * 0.5  # linear interpolation between 80 m and 120 m
        how = "interpolated between DWD ICON 80 m and 120 m, 30-day mean"
    return {"v100": v100, "elevation": body.get("elevation"), "method": how, "n": len(h["time"])}


async def query_slope(client: httpx.AsyncClient, site: Site, retries: int) -> dict[str, Any]:
    """Horn slope from a 3x3 Copernicus-DEM (90 m) grid sampled via Open-Meteo."""
    d = min(max(site.reach_m / 2, 100.0), 300.0)
    x0, y0 = _TO_UTM.transform(site.lon, site.lat)
    pts = []
    for j in (1, 0, -1):  # rows north -> south
        for i in (-1, 0, 1):
            lon, lat = _TO_WGS.transform(x0 + i * d, y0 + j * d)
            pts.append((lat, lon))
    params = {
        "latitude": ",".join(f"{p[0]:.6f}" for p in pts),
        "longitude": ",".join(f"{p[1]:.6f}" for p in pts),
    }
    r = await _request(client, "GET", OPEN_METEO_ELEVATION_URL, retries=retries, params=params)
    z = r.json()["elevation"]
    if len(z) != 9 or any(v is None for v in z):
        raise LookupError("incomplete elevation grid")
    a, b, c, dd, e, f, g, h, i = z
    dzdx = ((c + 2 * f + i) - (a + 2 * dd + g)) / (8 * d)
    dzdy = ((a + 2 * b + c) - (g + 2 * h + i)) / (8 * d)
    return {"slope_deg": math.degrees(math.atan(math.hypot(dzdx, dzdy))), "elevation": e, "spacing_m": d}


# --------------------------------------------------------------------------- 5. ALKIS (best effort)


@dataclass(frozen=True)
class AlkisEndpoint:
    url: str
    typename: str
    epsg: int
    kennz_tag: str


ALKIS_ENDPOINTS: dict[str, AlkisEndpoint] = {
    # Spec URLs `wfs_nw_alkis_vektor` / `alkis_wfs.php` are gone (404 / redirect to shutdown page);
    # these are the currently live keyless successors.
    "Nordrhein-Westfalen": AlkisEndpoint(
        "https://www.wfs.nrw.de/geobasis/wfs_nw_alkis_vereinfacht", "ave:Flurstueck", 25832, "flstkennz"
    ),
    "Brandenburg": AlkisEndpoint(
        "https://isk.geobasis-bb.de/ows/alkis_sf_wfs", "adv:AX_Flurstueck", 25833, "flurstueckskennzeichen"
    ),
}
ALKIS_CLOSED = "ALKIS_CLOSED_STATE_FALLBACK"


def _format_kennzeichen(raw: str) -> str:
    """20-char ALKIS key LL GGGG FFF ZZZZZ NNNN SS -> 'LL-GGGG-FFF-Z/N'."""
    k = raw.strip()
    if len(k) < 14:
        return k
    land, gem, flur, zaehler = k[:2], k[2:6], k[6:9], k[9:14].lstrip("0") or "0"
    nenner = k[14:18].strip("_").lstrip("0")
    return f"{land}-{gem}-{flur}-{zaehler}" + (f"/{nenner}" if nenner else "")


async def query_alkis(client: httpx.AsyncClient, site: Site, bundesland: Optional[str], retries: int) -> str:
    ep = ALKIS_ENDPOINTS.get(bundesland or "")
    if ep is None:
        return ALKIS_CLOSED
    t = Transformer.from_crs(25832, ep.epsg, always_xy=True)
    rp = site.poly_utm.representative_point()
    x, y = t.transform(rp.x, rp.y)
    crs = f"urn:ogc:def:crs:EPSG::{ep.epsg}"
    params = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": ep.typename,
        "SRSNAME": crs, "BBOX": f"{x - 1},{y - 1},{x + 1},{y + 1},{crs}", "COUNT": 10,
    }
    r = await _request(client, "GET", ep.url, retries=retries, params=params)
    root = etree.fromstring(r.content)
    back = Transformer.from_crs(ep.epsg, 25832, always_xy=True)
    best: tuple[float, str] | None = None
    for member in root.iter("{*}member"):
        kennz = next((e.text for e in member.iter(f"{{*}}{ep.kennz_tag}") if e.text), None)
        polys = _gml_polygons(member, crs_utm=True)
        if not kennz or not polys:
            continue
        geom = shp_transform(back.transform, MultiPolygon(polys) if len(polys) > 1 else polys[0])
        score = geom.intersection(site.poly_utm).area + (1e-6 if geom.contains(rp) else 0.0)
        if best is None or score > best[0]:
            best = (score, kennz)
    if best is None:
        return "ALKIS_NO_PARCEL_FOUND"
    return _format_kennzeichen(best[1])


# --------------------------------------------------------------------------- orchestration


@dataclass
class GisResult:
    admin: Optional[dict] = None
    parcel: Optional[str] = None
    osm: Optional[dict] = None
    protected: Optional[ProtectedResult] = None
    solar: Optional[dict] = None
    wind: Optional[dict] = None
    slope: Optional[dict] = None
    errors: dict[str, str] = field(default_factory=dict)


class GisEngine:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.s = settings

    async def run(self, site: Site) -> GisResult:
        res = GisResult()
        retries = self.s.http_retries

        async def admin_then_alkis():
            # ALKIS routing depends on the Bundesland, so these two run back-to-back inside one task.
            admin = await query_admin(self.client, site, retries)
            res.admin = admin
            try:
                res.parcel = await query_alkis(self.client, site, admin.get("bundesland"), retries)
            except Exception as e:
                res.parcel = "ALKIS_LOOKUP_FAILED"
                res.errors["alkis"] = _explain(e)

        jobs = {
            "admin": admin_then_alkis(),
            "osm": query_osm(self.client, site, self.s),
            "protected": query_protected(self.client, site, retries),
            "solar": query_solar(self.client, site, retries),
            "wind": query_wind(self.client, site, retries),
            "slope": query_slope(self.client, site, retries),
        }
        outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
        for key, out in zip(jobs, outcomes):
            if isinstance(out, BaseException):
                res.errors[key] = _explain(out) if isinstance(out, Exception) else repr(out)
            elif key != "admin":
                setattr(res, key, out)
        return res
