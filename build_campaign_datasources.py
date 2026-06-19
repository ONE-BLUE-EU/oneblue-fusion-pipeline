"""
build_campaign_datasources.py

Builds geo-ready CSV resources for the ONE-BLUE DKAN dataset
  https://data.one-blue.eu/dataset/e0622a5f-1874-44ed-aa29-f80d27a8bcee

For every ONE-BLUE sampling campaign (discovered from the SAMPLES folder
via load_campaigns_from_samples), this script downloads the matching
slices of external public datasets and writes one CSV per
source x data-type into ./dkan_resources/.

Each output CSV carries a common geo-ready header so it can be filtered
in DKAN by campaign and rendered on the DKAN map widget:

    campaign_code, campaign_area,
    campaign_lat_min, campaign_lat_max,
    campaign_lon_min, campaign_lon_max,
    campaign_date_min, campaign_date_max,
    source, data_type, dataset_id, feature_id,
    time, lat, lon, depth_m,
    geom_wkt,         <- WKT geometry (POINT / POLYGON / LINESTRING)
    <source-specific value columns>,
    extra_json,       <- raw properties for HA-style layers
    source_url        <- provenance / re-download URL

Output (one CSV per data-type category, with a `source` column inside)
---------------------------------------------------------------------
  oceanography.csv     <- Euro-Argo + EMSO-ERIC + Copernicus Marine
  chemistry.csv        <- EMODnet Chemistry (station-level sampling records)
  human_activities.csv <- EMODnet Human Activities (all themes merged;
                          'data_type' column = aquaculture/energy/
                          protection/pressures/ports)
  bathymetry.csv       <- EMODnet Bathymetry via GEBCO 2020 / opentopodata
  biology.csv          <- EMODnet Biology / OBIS occurrences

The campaign index is intentionally NOT emitted as a resource: DKAN
consumers can rebuild it from the SAMPLES folder, and each row above
already carries the campaign bbox/date columns.

Usage
-----
    python build_campaign_datasources.py [--only oceanography,biology,...] [--out DIR]

Re-running only refreshes the requested categories. Empty results still
produce an (empty) CSV so it's obvious which sources returned nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import traceback
import warnings
from io import StringIO
from typing import Iterable

import pandas as pd
import requests

# Reuse the campaign loader + helpers from the existing discovery script
from query_external_data import (
    load_campaigns_from_samples,
    safe_get,
    haversine_km,
    EMSO_NODES,
    EMSO_ERDDAP,
    EMODCHEM_ERDDAP,
    HA_WFS,
    BBOX_PAD,
    EMSO_RADIUS_KM,
)

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dkan_resources")

# Argo: only sample these standard depths (m) to keep file size manageable
ARGO_STD_DEPTHS = [0, 10, 50, 100, 500, 1000, 2000]
ARGO_DEPTH_TOL  = 5     # +/- m around each standard depth

# Bathymetry probe grid (NxN per campaign)
BATHY_GRID_N = 5

# OBIS query cap per campaign (server hard-cap ~10_000)
OBIS_SIZE = 5000

# Common header columns (everything before source-specific values)
COMMON_COLS = [
    "campaign_code", "campaign_area",
    "campaign_lat_min", "campaign_lat_max",
    "campaign_lon_min", "campaign_lon_max",
    "campaign_date_min", "campaign_date_max",
    "source", "data_type", "dataset_id", "feature_id",
    "time", "lat", "lon", "depth_m",
    "geom_wkt",
]
TRAILING_COLS = ["extra_json", "source_url"]

# Human Activities layer grouping -> output resource
HA_GROUPS: dict[str, list[tuple[str, str]]] = {
    "aquaculture": [
        ("shellfish",     "Shellfish farms"),
        ("finfish",       "Finfish farms"),
        ("finfishnew",    "Finfish farms (new)"),
    ],
    "energy": [
        ("platforms",     "Oil & gas platforms"),
        ("boreholes",     "Offshore wells"),
        ("activelicenses","Active O&G licences"),
        ("pipelines",     "Offshore pipelines"),
        ("windfarms",     "Wind farms (points)"),
        ("windfarmspoly", "Wind farms (polygons)"),
    ],
    "protection": [
        ("natura2000areas",      "Natura 2000 MPAs"),
        ("marineprotectedareas", "Marine protected areas"),
    ],
    "pressures": [
        ("dredging",         "Dredging sites"),
        ("dredgespoil",      "Dredge spoil dumps"),
        ("dischargepoints",  "Discharge points"),
        ("treatmentplants",  "Treatment plants"),
        ("militaryareaspoly","Military areas"),
        ("munitions",        "Dumped munitions"),
    ],
    "ports": [
        ("portlocations",    "Port locations"),
    ],
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _wkt_point(lon, lat) -> str:
    return f"POINT({lon} {lat})"


def _wkt_from_geojson(geom: dict | None) -> str:
    """Minimal GeoJSON -> WKT for Point/LineString/Polygon/MultiPolygon.

    Returns '' when geometry is missing or unsupported. Avoids pulling in
    shapely as a hard dependency.
    """
    if not geom:
        return ""
    t = geom.get("type", "")
    c = geom.get("coordinates")
    if c is None:
        return ""

    def _pt(p): return f"{p[0]} {p[1]}"
    def _ring(r): return "(" + ", ".join(_pt(p) for p in r) + ")"
    def _poly(p): return "(" + ", ".join(_ring(r) for r in p) + ")"

    try:
        if t == "Point":
            return f"POINT({_pt(c)})"
        if t == "MultiPoint":
            return "MULTIPOINT(" + ", ".join(_pt(p) for p in c) + ")"
        if t == "LineString":
            return f"LINESTRING({', '.join(_pt(p) for p in c)})"
        if t == "MultiLineString":
            return "MULTILINESTRING(" + ", ".join(
                "(" + ", ".join(_pt(p) for p in ls) + ")" for ls in c
            ) + ")"
        if t == "Polygon":
            return f"POLYGON{_poly(c)}"
        if t == "MultiPolygon":
            return "MULTIPOLYGON(" + ", ".join(_poly(p) for p in c) + ")"
    except Exception:
        return ""
    return ""


def _centroid_of_geojson(geom: dict | None) -> tuple[float | None, float | None]:
    """Quick (not area-weighted) centroid for HA features lacking a coord."""
    if not geom:
        return None, None
    coords = geom.get("coordinates")
    if coords is None:
        return None, None
    pts: list[tuple[float, float]] = []

    def _walk(x):
        if isinstance(x, (list, tuple)):
            if len(x) >= 2 and all(isinstance(v, (int, float)) for v in x[:2]):
                pts.append((float(x[0]), float(x[1])))
            else:
                for el in x:
                    _walk(el)

    _walk(coords)
    if not pts:
        return None, None
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return lat, lon


def _row(camp: dict, **kw) -> dict:
    """Build a row prefilled with the campaign block + supplied overrides."""
    base = {
        "campaign_code":     camp["name"],
        "campaign_area":     camp["area"],
        "campaign_lat_min":  camp["lat_min"],
        "campaign_lat_max":  camp["lat_max"],
        "campaign_lon_min":  camp["lon_min"],
        "campaign_lon_max":  camp["lon_max"],
        "campaign_date_min": camp["date_min"],
        "campaign_date_max": camp["date_max"],
        "source":      "", "data_type": "", "dataset_id": "",
        "feature_id":  "", "time":      "", "lat":  None,
        "lon":  None,      "depth_m":   None, "geom_wkt": "",
        "extra_json":  "", "source_url": "",
    }
    base.update(kw)
    return base


def _write_csv(rows: list[dict], path: str, value_cols: list[str]):
    """Write rows ordered as: COMMON_COLS, value_cols, TRAILING_COLS.

    Always writes the file (even when there are zero rows) so the user can
    see which sources came back empty.
    """
    cols = COMMON_COLS + [c for c in value_cols if c not in COMMON_COLS + TRAILING_COLS] + TRAILING_COLS
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  wrote {len(df):>6d} rows -> {os.path.relpath(path, os.path.dirname(__file__))}")


# ---------------------------------------------------------------------------
# 1. Argo profiles (argopy)
# ---------------------------------------------------------------------------

def fetch_argo(campaigns: list[dict]) -> list[dict]:
    try:
        from argopy import DataFetcher as ArgoFetcher
    except ImportError:
        print("  argopy not installed -> skipping Argo (pip install argopy)")
        return []

    rows: list[dict] = []
    for camp in campaigns:
        if not camp["date_min"] or not camp["date_max"]:
            print(f"  Argo | {camp['name'][:45]:45s} SKIP (no date range)")
            continue
        print(f"  Argo | {camp['name'][:45]:45s}", end="  ")
        try:
            fetcher = ArgoFetcher(src="erddap", parallel=False, progress=False)
            ds = fetcher.region([
                camp["lon_min"], camp["lon_max"],
                camp["lat_min"], camp["lat_max"],
                0, 2050,
                camp["date_min"], camp["date_max"],
            ]).to_xarray()
            df = ds.to_dataframe().reset_index()
        except FileNotFoundError:
            # ERDDAP returns 404 when no floats match -> argopy raises FNF
            print("0 rows (no floats in bbox/period)")
            continue
        except Exception as e:
            print(f"ERROR ({str(e)[:60]})")
            continue

        if df.empty:
            print("0 rows")
            continue

        # Normalise expected column names
        cmap = {c.upper(): c for c in df.columns}
        col_t = cmap.get("TIME")
        col_la = cmap.get("LATITUDE")
        col_lo = cmap.get("LONGITUDE")
        col_p  = cmap.get("PRES") or cmap.get("PRES_ADJUSTED")
        col_T  = cmap.get("TEMP") or cmap.get("TEMP_ADJUSTED")
        col_S  = cmap.get("PSAL") or cmap.get("PSAL_ADJUSTED")
        col_O  = cmap.get("DOXY") or cmap.get("DOXY_ADJUSTED")
        col_pl = cmap.get("PLATFORM_NUMBER")
        col_cy = cmap.get("CYCLE_NUMBER")

        if col_p is None:
            print("0 rows (no PRES col)")
            continue

        # Slice to rows near standard depths (1 dbar ~= 1 m near surface)
        keep_mask = pd.Series(False, index=df.index)
        for d in ARGO_STD_DEPTHS:
            keep_mask |= df[col_p].between(d - ARGO_DEPTH_TOL, d + ARGO_DEPTH_TOL)
        sub = df[keep_mask].copy()
        if sub.empty:
            print("0 rows (none near std depths)")
            continue

        # Map each row to its nearest std depth
        def _nearest(p):
            try:
                return min(ARGO_STD_DEPTHS, key=lambda d: abs(d - float(p)))
            except (TypeError, ValueError):
                return None
        sub["_std_depth"] = sub[col_p].map(_nearest)

        for _, r in sub.iterrows():
            la = r.get(col_la); lo = r.get(col_lo)
            if pd.isna(la) or pd.isna(lo):
                continue
            t = r.get(col_t)
            t_iso = t.isoformat() if hasattr(t, "isoformat") else (str(t) if not pd.isna(t) else "")
            plat = r.get(col_pl) if col_pl else ""
            cyc  = r.get(col_cy) if col_cy else ""
            rows.append(_row(
                camp,
                source="Euro-Argo", data_type="profile", dataset_id="ArgoGDAC",
                feature_id=f"{plat}_c{cyc}_{int(r['_std_depth'])}m",
                time=t_iso, lat=float(la), lon=float(lo),
                depth_m=float(r['_std_depth']),
                geom_wkt=_wkt_point(float(lo), float(la)),
                pressure_dbar=float(r[col_p]) if not pd.isna(r[col_p]) else None,
                temperature_c=float(r[col_T]) if col_T and not pd.isna(r[col_T]) else None,
                salinity_psu=float(r[col_S]) if col_S and not pd.isna(r[col_S]) else None,
                oxygen_umol_kg=float(r[col_O]) if col_O and not pd.isna(r[col_O]) else None,
                platform_number=str(plat) if not pd.isna(plat) else "",
                cycle_number=int(cyc) if cyc != "" and not pd.isna(cyc) else "",
                extra_json="",
                source_url=(
                    f"https://erddap.ifremer.fr/erddap/tabledap/ArgoFloats.html"
                    f"?latitude%2Clongitude%2Ctime%2Cplatform_number"
                    f"&latitude%3E={camp['lat_min']}&latitude%3C={camp['lat_max']}"
                    f"&longitude%3E={camp['lon_min']}&longitude%3C={camp['lon_max']}"
                    f"&time%3E={camp['date_min']}&time%3C={camp['date_max']}"
                ),
            ))
        print(f"{len(sub)} rows -> {len([r for r in rows if r['campaign_code']==camp['name']])} kept")
    return rows

ARGO_VALUE_COLS = [
    "pressure_dbar", "temperature_c", "salinity_psu", "oxygen_umol_kg",
    "platform_number", "cycle_number",
]


# ---------------------------------------------------------------------------
# 2. EMSO time series (hourly mean)
# ---------------------------------------------------------------------------

# Cache discovered EMSO variable lists across the run
_EMSO_VAR_CACHE: dict[str, list[str]] = {}

# Conceptual category -> ordered list of variable-name prefixes to look for
# in EMSO datasets (CF / OceanSITES style). First match wins per dataset.
EMSO_VAR_MAP = {
    "temperature_c":  ["TEMP"],
    "salinity_psu":   ["PSAL"],
    "oxygen_umol_kg": ["DOX1", "DOXY"],
    "pressure_dbar":  ["PRES"],
    "ph":             ["PHPH"],
}


def _emso_discover_vars(ds_id: str) -> list[str]:
    """Return list of variable names defined in this EMSO ERDDAP dataset."""
    if ds_id in _EMSO_VAR_CACHE:
        return _EMSO_VAR_CACHE[ds_id]
    url = f"{EMSO_ERDDAP}/info/{ds_id}/index.csv"
    resp = safe_get(url, timeout=30)
    if resp is None or resp.status_code != 200:
        _EMSO_VAR_CACHE[ds_id] = []
        return []
    try:
        df = pd.read_csv(StringIO(resp.text))
        vs = df.loc[df["Row Type"] == "variable", "Variable Name"].astype(str).tolist()
    except Exception:
        vs = []
    _EMSO_VAR_CACHE[ds_id] = vs
    return vs


def _emso_pick_vars(all_vars: list[str]) -> dict[str, str]:
    """Map our canonical column names -> actual EMSO variable name."""
    picked: dict[str, str] = {}
    for canon, prefixes in EMSO_VAR_MAP.items():
        for v in all_vars:
            if v.endswith("_QC"):
                continue
            if any(v == p or v.startswith(p + "_") or v.startswith(p) for p in prefixes):
                picked[canon] = v
                break
    return picked


def fetch_emso(campaigns: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for camp in campaigns:
        for node in EMSO_NODES:
            dist = haversine_km(camp["clat"], camp["clon"], node["lat"], node["lon"])
            if dist > EMSO_RADIUS_KM:
                continue
            time_filter = ""
            if camp["date_min"] and camp["date_max"]:
                time_filter = (
                    f"&time%3E={camp['date_min']}T00:00:00Z"
                    f"&time%3C={camp['date_max']}T23:59:59Z"
                )
            for ds_id in node["dataset_ids"]:
                # Discover what variables this dataset actually exposes
                all_vars = _emso_discover_vars(ds_id)
                picked = _emso_pick_vars(all_vars)
                base_vars = [v for v in ("time", "latitude", "longitude", "depth") if v in all_vars]
                if "time" not in all_vars:
                    base_vars = ["time"] + base_vars  # safety: ERDDAP always has time
                proj = base_vars + list(picked.values())
                vars_csv = ",".join(proj) if proj else "time"
                url = (
                    f"{EMSO_ERDDAP}/tabledap/{ds_id}.csv?"
                    + vars_csv + time_filter
                )
                print(f"  EMSO | {camp['name'][:25]:25s} -> {node['node']:18s} "
                      f"{ds_id[:30]:30s} vars={len(picked)}", end="  ")
                resp = safe_get(url, timeout=120)
                if resp is None:
                    print("ERR")
                    continue
                if resp.status_code == 404:
                    print("no-data")
                    continue
                try:
                    df = pd.read_csv(StringIO(resp.text), skiprows=[1], low_memory=False)
                except Exception as e:
                    print(f"parse-err ({str(e)[:30]})")
                    continue
                if df.empty:
                    print("0 rows")
                    continue

                # Downsample if huge (cap per dataset)
                if len(df) > 5000:
                    df = df.iloc[:: max(1, len(df) // 5000)]

                # Rename source columns -> canonical
                inv = {v: k for k, v in picked.items()}
                df = df.rename(columns=inv)

                for _, r in df.iterrows():
                    la = r.get("latitude", node["lat"])
                    lo = r.get("longitude", node["lon"])
                    if pd.isna(la): la = node["lat"]
                    if pd.isna(lo): lo = node["lon"]
                    def _gf(c):
                        if c not in r: return None
                        v = r[c]
                        return None if pd.isna(v) else float(v)
                    rows.append(_row(
                        camp,
                        source="EMSO-ERIC", data_type="timeseries", dataset_id=ds_id,
                        feature_id=f"{node['node']}_{r.get('time','')}",
                        time=str(r.get("time", "")),
                        lat=float(la), lon=float(lo),
                        depth_m=_gf("depth"),
                        geom_wkt=_wkt_point(float(lo), float(la)),
                        temperature_c=_gf("temperature_c"),
                        salinity_psu=_gf("salinity_psu"),
                        node=node["node"], node_description=node["description"],
                        distance_km=round(dist, 1),
                        extra_json=json.dumps({
                            k: _gf(k) for k in ("oxygen_umol_kg", "pressure_dbar", "ph") if k in df.columns
                        }, default=str),
                        source_url=url,
                    ))
                print(f"{len(df)} rows")
                break  # one dataset per node per campaign is enough
            time.sleep(0.3)
    return rows

EMSO_VALUE_COLS = ["temperature_c", "salinity_psu", "node", "node_description", "distance_km"]


# ---------------------------------------------------------------------------
# 3. EMODnet Chemistry — station-level sampling records (real data)
# ---------------------------------------------------------------------------
#
# Each EMODnet Chemistry ERDDAP dataset exposes one row per SeaDataNet CDI
# (a sampling event). Per row we get: Cruise/Station/time/lon/lat, the water
# depth, the gear used, the list of variables actually measured at that
# station (BODC P02 codes), and the LOCAL_CDI_ID that links to the underlying
# numeric values on the SeaDataNet portal.
#
# We pick the basin-relevant datasets per campaign, query each one filtered to
# the campaign bbox (with PAD), parse the `Variables_measured` field into
# parameter flags so the columns are immediately filterable in DKAN.

# Compact list of EMODnet Chemistry datasets per basin
EMODCHEM_BASINS: dict[str, list[str]] = {
    "med": [
        "EUT_MED_PROFILES", "EUT_MED_TIMESERIES",
        "CONTAMINANTS_MED_WATER_PROFILES", "CONTAMINANTS_MED_WATER_TIMESERIES",
        "CONTAMINANTS_MED_SEDIMENT_PROFILES", "CONTAMINANTS_MED_SEDIMENT_TIMESERIES",
        "CONTAMINANTS_MED_BIOTA_PROFILES", "CONTAMINANTS_MED_BIOTA_TIMESERIES",
    ],
    "atlantic": [
        "EUT_ATLANTIC_PROFILES", "EUT_ATLANTIC_TIMESERIES",
        "CONTAMINANTS_ATLANTIC_WATER_PROFILES",
        "CONTAMINANTS_ATLANTIC_SEDIMENT_PROFILES", "CONTAMINANTS_ATLANTIC_SEDIMENT_TIMESERIES",
        "CONTAMINANTS_ATLANTIC_BIOTA_TIMESERIES",
    ],
    "northsea": [
        "EUT_NORTHSEA_PROFILES", "EUT_NORTHSEA_TIMESERIES",
        "CONTAMINANTS_NORTHSEA_WATER_PROFILES", "CONTAMINANTS_NORTHSEA_WATER_TIMESERIES",
        "CONTAMINANTS_NORTHSEA_SEDIMENT_PROFILES", "CONTAMINANTS_NORTHSEA_SEDIMENT_TIMESERIES",
        "CONTAMINANTS_NORTHSEA_BIOTA_PROFILES", "CONTAMINANTS_NORTHSEA_BIOTA_TIMESERIES",
    ],
    "baltic": [
        "EUT_BALTIC_PROFILES", "EUT_BALTIC_TIMESERIES",
        "CONTAMINANTS_BALTIC_WATER_PROFILES",
        "CONTAMINANTS_BALTIC_SEDIMENT_PROFILES", "CONTAMINANTS_BALTIC_SEDIMENT_TIMESERIES",
        "CONTAMINANTS_BALTIC_BIOTA_PROFILES", "CONTAMINANTS_BALTIC_BIOTA_TIMESERIES",
    ],
    "blacksea": [
        "EUT_BLACKSEA_PROFILES", "EUT_BLACKSEA_TIMESERIES",
        "CONTAMINANTS_BLACKSEA_WATER_PROFILES",
        "CONTAMINANTS_BLACKSEA_SEDIMENT_PROFILES",
        "CONTAMINANTS_BLACKSEA_BIOTA_PROFILES",
    ],
    "arctic": [
        "EUT_ARCTIC_PROFILES",
        "CONTAMINANTS_ARCTIC_WATER_PROFILES",
        "CONTAMINANTS_ARCTIC_SEDIMENT_PROFILES",
    ],
}

# BODC P02 parameter code -> short canonical name (only the ones we surface as flags)
EMODCHEM_PARAM_FLAGS: dict[str, str] = {
    "TEMP": "has_temperature",
    "PSAL": "has_salinity",
    "DOXY": "has_oxygen",
    "ALKY": "has_alkalinity",
    "PHOS": "has_phosphate",
    "NTRA": "has_nitrate",
    "NTRI": "has_nitrite",
    "AMON": "has_ammonium",
    "SLCA": "has_silicate",
    "CPWC": "has_chlorophyll",
    "HCBT": "has_hydrocarbons",          # petroleum hydrocarbons
    "PCBW": "has_pcbs",                  # polychlorinated biphenyls
    "OPCW": "has_organochlorine_pesticides",
    "MTLW": "has_heavy_metals",          # heavy metals in water
    "MTLS": "has_heavy_metals_sediment",
    "MTLB": "has_heavy_metals_biota",
    "TBTW": "has_organotin",
    "PAHW": "has_pahs",                  # polycyclic aromatic hydrocarbons
    "RADW": "has_radionuclides",
    "PESW": "has_pesticides_water",
    "CO2W": "has_co2_system",
    "PHPH": "has_ph",
}

_EMODCHEM_PARAM_RE = re.compile(r"\(([A-Z0-9]{3,5})\)")

# Pull chemical symbol from P01_preflabel like
#   "Concentration of cadmium {Cd CAS 7440-43-9} per unit volume ..."
# group 1 is the symbol token (e.g. Cd, Pb, total_Hg, PCB-153)
_EMODCHEM_SYMBOL_RE = re.compile(r"\{([^\s}]+)(?:\s+CAS\s+[\d\-]+)?\}")

# Crude chemical-group classifier driven by the clean S27_preflabel name.
# Order matters — first match wins. Patterns intentionally use only a leading
# word boundary so stems like "dibutyl" match "dibutyltin" and "acenaphth"
# matches both "acenaphthene" and "acenaphthylene".
_EMODCHEM_GROUP_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("PCB",                re.compile(r"(\bpcb\b|polychlorinated biphenyl|chlorobiphenyl)", re.I)),
    ("PAH",                re.compile(r"\b(naphthalene|phenanthrene|anthracene|fluoranthene|pyrene|benzo[\[(]|chrysene|benz[\[(]|acenaphth|fluorene|perylene|indeno|pah)", re.I)),
    ("organochlorine pesticide", re.compile(r"\b(ddt|ddd|dde|lindane|hch|hcb|hexachlorocyclohexane|hexachlorobenzene|pentachlorobenzene|trichlorobenzene|dichlorodiphenyl|hexachloro-?1,3-?butadien|aldrin|dieldrin|endrin|isodrin|endosulfan|heptachlor|chlordane|mirex|toxaphene)", re.I)),
    ("pesticide",          re.compile(r"\b(atrazine|simazine|diuron|isoproturon|chlorpyrifos|chlorfenvinphos|alachlor|trifluralin|metolachlor|terbuthylazine|malathion|parathion|glyphosate|carbofuran|carbaryl|propanil|molinate|pendimethalin|prometryn)", re.I)),
    ("organotin",          re.compile(r"\b(tributyl|dibutyl|monobutyl|tbt|dbt|mbt|triphenyltin)", re.I)),
    ("VOC",                re.compile(r"\b(benzene|toluene|xylene|ethylbenzene|trichloromethane|tetrachloromethane|dichloromethane|chloroform|trichloroethylene|trichloroethene|tetrachloroethylene|tetrachloroethene|vinyl chloride|1,2-dichloroethane|1,1,2-trichloroethene)", re.I)),
    ("radionuclide",       re.compile(r"\b(cs-?137|sr-?90|caesium-137|strontium-90|tritium|radio)", re.I)),
    ("nutrient",           re.compile(r"\b(nitrate|nitrite|ammoni|phosphate|silicate|phosphorus|nitrogen|chlorophyll|organic carbon|alkalinity|oxygen|ph|salinity|temperature)", re.I)),
    ("hydrocarbon",        re.compile(r"\b(hydrocarbon|petroleum|oil)", re.I)),
    ("heavy metal",        re.compile(r"\b(lead|mercury|cadmium|copper|zinc|nickel|chromium|arsenic|iron|manganese|aluminium|titanium|tin|silver|cobalt|vanadium|barium|antimony|selenium|molybdenum|lithium|beryllium|thallium|uranium|thorium)", re.I)),
    ("major element",      re.compile(r"\b(calcium|magnesium|potassium|sodium|sulfate|chloride|fluoride|bromide|carbonate|bicarbonate)", re.I)),
]


def _classify_compound(name: str) -> str:
    if not name:
        return ""
    for label, rx in _EMODCHEM_GROUP_RULES:
        if rx.search(name):
            return label
    return "other"


# High-level matrix derived from EMODnet Chemistry dataset id
def _matrix_from_dataset_id(ds_id: str) -> str:
    up = ds_id.upper()
    if "BIOTA" in up:
        return "biota"
    if "SEDIMENT" in up:
        return "sediment"
    if "WATER" in up:
        return "water"
    if "EUT_" in up:
        return "water_column"
    return ""


# Per-dataset variable cache: ds_id -> set of available variable names
_EMODCHEM_VAR_CACHE: dict[str, set[str]] = {}


def _emodchem_vars(ds_id: str) -> set[str]:
    """Fetch (and cache) the set of variable names exposed by an EMODnet Chemistry dataset."""
    if ds_id in _EMODCHEM_VAR_CACHE:
        return _EMODCHEM_VAR_CACHE[ds_id]
    url = f"{EMODCHEM_ERDDAP}/info/{ds_id}/index.csv"
    resp = safe_get(url, timeout=30)
    if resp is None or resp.status_code != 200:
        _EMODCHEM_VAR_CACHE[ds_id] = set()
        return set()
    try:
        df = pd.read_csv(StringIO(resp.text), low_memory=False)
        names = set(df[df["Row Type"] == "variable"]["Variable Name"].dropna().astype(str))
    except Exception:
        names = set()
    _EMODCHEM_VAR_CACHE[ds_id] = names
    return names


def _basins_for_campaign(camp: dict) -> list[str]:
    """Pick which EMODnet Chemistry basin sets are relevant for a campaign.

    Lon/lat rectangles are intentionally generous; empty bbox queries on
    irrelevant datasets simply return zero rows."""
    lat, lon = camp["clat"], camp["clon"]
    basins: list[str] = []
    # Mediterranean + Adriatic + Aegean + Ionian
    if -6.0 <= lon <= 37.0 and 30.0 <= lat <= 47.0:
        basins.append("med")
    # NE Atlantic shelf (Iberia, Bay of Biscay, Celtic Sea, Gulf of Cadiz)
    if -20.0 <= lon <= -2.0 and 30.0 <= lat <= 56.0:
        basins.append("atlantic")
    # Cadiz / Alboran straddle Atlantic+Med
    if -7.0 <= lon <= -2.0 and 34.0 <= lat <= 40.0 and "atlantic" not in basins:
        basins.append("atlantic")
    # North Sea
    if -5.0 <= lon <= 12.0 and 50.0 <= lat <= 62.0:
        basins.append("northsea")
    # Baltic
    if 9.0 <= lon <= 31.0 and 53.0 <= lat <= 66.0:
        basins.append("baltic")
    # Black Sea
    if 27.0 <= lon <= 42.0 and 40.0 <= lat <= 48.0:
        basins.append("blacksea")
    # Arctic
    if lat >= 66.0:
        basins.append("arctic")
    return basins or ["atlantic", "med"]  # safe fallback


def _parse_variables_measured(s: str) -> tuple[list[str], dict[str, bool]]:
    """Extract BODC P02 parameter codes from a Variables_measured cell.

    Returns (codes, flags) where flags maps EMODCHEM_PARAM_FLAGS column
    names to True/False."""
    if not isinstance(s, str) or not s:
        return [], {v: False for v in EMODCHEM_PARAM_FLAGS.values()}
    codes = sorted(set(_EMODCHEM_PARAM_RE.findall(s)))
    flags = {col: False for col in EMODCHEM_PARAM_FLAGS.values()}
    for c in codes:
        col = EMODCHEM_PARAM_FLAGS.get(c)
        if col:
            flags[col] = True
    return codes, flags


def fetch_emodnet_chemistry(campaigns: list[dict]) -> list[dict]:
    PAD = 1.0          # degrees of padding around campaign bbox
    HALF_BBOX = 2.0    # degrees half-width clip around campaign centroid (caps wide bboxes)
    MAX_PER_DS = 1500  # cap rows per (campaign, dataset) after parse
    SERVER_LIMIT = 25000  # ERDDAP-side row cap so we don't pull GB-scale CSVs
    # Candidate columns we want; actual projection is intersected with what
    # the dataset exposes (EUT vs CONTAMINANTS schemas differ).
    CANDIDATE_COLS = [
        "Cruise", "Station", "time", "longitude", "latitude",
        "Bot_Depth", "Water_depth",
        "Minimum_instrument_depth", "Maximum_instrument_depth",
        "Variables_measured", "Instrument_gear_type",
        "LOCAL_CDI_ID", "EDMO_code",
        # contaminants per-measurement schema:
        "Value", "Units", "P01_preflabel", "CAS_no",
        "S26_preflabel",   # matrix detail (e.g. "sediment <63um")
        "S27_preflabel",   # clean compound name (e.g. "cadmium", "fluoranthene")
        "CmpDep", "DepBelowBed",
    ]
    rows: list[dict] = []
    for camp in campaigns:
        basins = _basins_for_campaign(camp)
        ds_ids: list[str] = []
        for b in basins:
            ds_ids.extend(EMODCHEM_BASINS.get(b, []))
        # Start from the campaign bbox padded by PAD, then clip to a
        # reasonable neighbourhood around the centroid so very wide or
        # antimeridian-crossing campaigns (Arctic, Iberian, Spanish coast)
        # do not request multi-GB ERDDAP responses for entire basins.
        lat_min = max(camp["lat_min"] - PAD, camp["clat"] - HALF_BBOX)
        lat_max = min(camp["lat_max"] + PAD, camp["clat"] + HALF_BBOX)
        lon_min = max(camp["lon_min"] - PAD, camp["clon"] - HALF_BBOX)
        lon_max = min(camp["lon_max"] + PAD, camp["clon"] + HALF_BBOX)
        camp_start = camp.get("date_min") or ""
        camp_end = camp.get("date_max") or ""

        for ds_id in ds_ids:
            available = _emodchem_vars(ds_id)
            if not available:
                print(f"  Chem | {camp['name'][:25]:25s} {ds_id[:38]:38s}  info-err")
                continue
            proj_cols = [c for c in CANDIDATE_COLS if c in available]
            # We need lat/lon as a minimum
            if "longitude" not in proj_cols or "latitude" not in proj_cols:
                print(f"  Chem | {camp['name'][:25]:25s} {ds_id[:38]:38s}  no-geom")
                continue
            proj = ",".join(proj_cols)
            url = (
                f"{EMODCHEM_ERDDAP}/tabledap/{ds_id}.csv?"
                f"{proj}"
                f"&longitude%3E={lon_min}&longitude%3C={lon_max}"
                f"&latitude%3E={lat_min}&latitude%3C={lat_max}"
            )
            print(f"  Chem | {camp['name'][:25]:25s} {ds_id[:38]:38s}", end="  ")
            # Stream the ERDDAP CSV with a hard byte cap so we don't pull
            # multi-GB responses for very wide bbox * basin queries (which
            # can swallow all RAM during pd.read_csv). 50 MB is enough to
            # cover ~250-300k rows of the typical EMODnet Chemistry schema.
            CSV_BYTE_CAP = 50 * 1024 * 1024
            csv_text = None
            truncated = False
            try:
                with requests.get(url, timeout=300, stream=True) as r:
                    if r.status_code == 404:
                        print("0")
                        continue
                    if r.status_code != 200:
                        print(f"http {r.status_code}")
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= CSV_BYTE_CAP:
                            truncated = True
                            break
                    raw = b"".join(chunks)
                    csv_text = raw.decode("utf-8", errors="replace")
                    if truncated:
                        # Drop the last (likely partial) line so pd.read_csv
                        # doesn't choke on a half-row.
                        nl = csv_text.rfind("\n")
                        if nl > 0:
                            csv_text = csv_text[:nl]
            except requests.exceptions.RequestException:
                print("ERR")
                continue
            if not csv_text:
                print("ERR")
                continue
            try:
                df = pd.read_csv(StringIO(csv_text), low_memory=False, skiprows=[1])
            except Exception as e:
                print(f"parse-err ({type(e).__name__})")
                continue
            if df.empty:
                print("0")
                continue
            if len(df) > MAX_PER_DS:
                df = df.iloc[:: max(1, len(df) // MAX_PER_DS)].copy()
            # Heuristic data_type from dataset id
            up = ds_id.upper()
            if "EUT_" in up:
                dtype = "eutrophication"
            elif "BIOTA" in up:
                dtype = "contaminants_biota"
            elif "SEDIMENT" in up:
                dtype = "contaminants_sediment"
            elif "WATER" in up:
                dtype = "contaminants_water"
            else:
                dtype = "chemistry"
            matrix = _matrix_from_dataset_id(ds_id)
            has_measurement_cols = "Value" in df.columns and "P01_preflabel" in df.columns
            df_cols = set(df.columns)

            for r in df.to_dict("records"):
                try:
                    la = float(r.get("latitude"))
                    lo = float(r.get("longitude"))
                except (TypeError, ValueError):
                    continue
                if pd.isna(la) or pd.isna(lo):
                    continue
                t_raw = r.get("time", "")
                t_str = "" if pd.isna(t_raw) else str(t_raw)
                # Depth: prefer per-measurement depth, then station bathymetry
                depth = None
                for col in ("CmpDep", "DepBelowBed", "Maximum_instrument_depth",
                            "Bot_Depth", "Water_depth"):
                    if col not in df_cols:
                        continue
                    v = r.get(col)
                    if v is not None and not pd.isna(v):
                        try:
                            depth = float(v); break
                        except (TypeError, ValueError):
                            pass
                # Variables measured (EUT_*): parse BODC P02 codes into flags
                vm = r.get("Variables_measured", "") if "Variables_measured" in df_cols else ""
                codes, flags = _parse_variables_measured(vm if isinstance(vm, str) else "")
                # Per-measurement value (CONTAMINANTS_*)
                p_name = ""
                p_value = None
                p_unit = ""
                cas = ""
                compound_name = ""
                compound_symbol = ""
                matrix_detail = ""
                chemical_group = ""
                if has_measurement_cols:
                    p_name = str(r.get("P01_preflabel", "") or "")
                    pv = r.get("Value")
                    if pv is not None and not pd.isna(pv):
                        try:
                            p_value = float(pv)
                        except (TypeError, ValueError):
                            p_value = None
                    p_unit = str(r.get("Units", "") or "")
                    cas = str(r.get("CAS_no", "") or "")
                    # Clean compound name (BODC S27)
                    if "S27_preflabel" in df_cols:
                        sv = r.get("S27_preflabel")
                        if sv is not None and not pd.isna(sv):
                            compound_name = str(sv)
                    # Compound symbol parsed from P01 ("{Cd CAS 7440-43-9}")
                    if p_name:
                        m = _EMODCHEM_SYMBOL_RE.search(p_name)
                        if m:
                            compound_symbol = m.group(1)
                    # Matrix detail (BODC S26)
                    if "S26_preflabel" in df_cols:
                        mv = r.get("S26_preflabel")
                        if mv is not None and not pd.isna(mv):
                            matrix_detail = str(mv)
                    chemical_group = _classify_compound(compound_name or p_name)
                cdi = str(r.get("LOCAL_CDI_ID", "") or "") if "LOCAL_CDI_ID" in df_cols else ""
                cdi_url = (
                    f"https://cdi.seadatanet.org/search/welcome.php?query={cdi}"
                    if cdi else ""
                )
                in_window = bool(
                    t_str and camp_start and camp_end
                    and camp_start <= t_str[:10] <= camp_end
                )
                # Unique-ish feature id
                if has_measurement_cols:
                    fid = f"{cdi or ds_id}:{r.get('Cruise','')}:{r.get('Station','')}:{p_name[:20]}"
                else:
                    fid = cdi or f"{ds_id}:{r.get('Cruise','')}:{r.get('Station','')}"
                rows.append(_row(
                    camp,
                    source="EMODnet-Chemistry", data_type=dtype, dataset_id=ds_id,
                    feature_id=fid,
                    time=t_str, lat=la, lon=lo,
                    depth_m=depth,
                    geom_wkt=_wkt_point(lo, la),
                    matrix=matrix,
                    matrix_detail=matrix_detail,
                    cruise=str(r.get("Cruise", "") or "") if "Cruise" in df_cols else "",
                    station=str(r.get("Station", "") or "") if "Station" in df_cols else "",
                    instrument=str(r.get("Instrument_gear_type", "") or "") if "Instrument_gear_type" in df_cols else "",
                    edmo_code=str(r.get("EDMO_code", "") or "") if "EDMO_code" in df_cols else "",
                    local_cdi_id=cdi,
                    cdi_url=cdi_url,
                    compound_name=compound_name,
                    compound_symbol=compound_symbol,
                    chemical_group=chemical_group,
                    parameter_name=p_name,
                    parameter_value=p_value,
                    parameter_unit=p_unit,
                    cas_number=cas,
                    parameter_codes="|".join(codes),
                    parameter_count=len(codes),
                    in_campaign_window=in_window,
                    **flags,
                    extra_json=json.dumps({
                        "variables_measured": vm if isinstance(vm, str) else "",
                    }, default=str)[:2000],
                    source_url=url,
                ))
            print(f"{len(df)} rows{' (truncated)' if truncated else ''}")
            time.sleep(0.25)
    return rows


CHEM_VALUE_COLS = [
    "matrix", "matrix_detail",
    "cruise", "station", "instrument", "edmo_code",
    "local_cdi_id", "cdi_url",
    "compound_name", "compound_symbol", "chemical_group",
    "parameter_name", "parameter_value", "parameter_unit", "cas_number",
    "parameter_codes", "parameter_count", "in_campaign_window",
    *EMODCHEM_PARAM_FLAGS.values(),
]


# ---------------------------------------------------------------------------
# 4. EMODnet Human Activities (grouped by theme)
# ---------------------------------------------------------------------------

def fetch_human_activities(campaigns: list[dict], groups: dict[str, list[tuple[str, str]]] = HA_GROUPS) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {g: [] for g in groups}
    for camp in campaigns:
        bb = (
            camp["lat_min"] - BBOX_PAD, camp["lon_min"] - BBOX_PAD,
            camp["lat_max"] + BBOX_PAD, camp["lon_max"] + BBOX_PAD,
        )
        for group, layers in groups.items():
            for layer_id, layer_label in layers:
                url = (
                    f"{HA_WFS}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
                    f"&typeName=emodnet:{layer_id}"
                    f"&BBOX={bb[0]},{bb[1]},{bb[2]},{bb[3]},urn:ogc:def:crs:EPSG::4326"
                    f"&outputFormat=application/json&count=2000"
                )
                print(f"  HA/{group:11s} | {camp['name'][:25]:25s} {layer_label[:30]:30s}", end="  ")
                resp = safe_get(url, timeout=60)
                if resp is None:
                    print("ERR")
                    continue
                try:
                    feats = resp.json().get("features", [])
                except Exception:
                    print("parse-err")
                    continue
                if not feats:
                    print("0")
                    continue
                for f in feats:
                    geom = f.get("geometry")
                    props = f.get("properties") or {}
                    lat, lon = _centroid_of_geojson(geom)
                    out[group].append(_row(
                        camp,
                        source="EMODnet-HumanActivities", data_type=group, dataset_id=layer_id,
                        feature_id=str(f.get("id", "")),
                        time=str(props.get("year") or props.get("date") or ""),
                        lat=lat, lon=lon, depth_m=None,
                        geom_wkt=_wkt_from_geojson(geom),
                        layer_label=layer_label,
                        name=str(props.get("name", "") or props.get("NAME", "") or props.get("portname", "")),
                        country=str(props.get("country", "") or props.get("COUNTRY", "")),
                        status=str(props.get("status", "") or props.get("STATUS", "")),
                        purpose=str(props.get("purpose", "") or props.get("PURPOSE", "")),
                        extra_json=json.dumps(props, default=str)[:4000],
                        source_url=url,
                    ))
                print(f"{len(feats)}")
                time.sleep(0.2)
    return out

HA_VALUE_COLS = ["layer_label", "name", "country", "status", "purpose"]


# ---------------------------------------------------------------------------
# 5. EMODnet Bathymetry (opentopodata GEBCO grid probes)
# ---------------------------------------------------------------------------

def fetch_bathymetry(campaigns: list[dict], n: int = BATHY_GRID_N) -> list[dict]:
    rows: list[dict] = []
    base = "https://api.opentopodata.org/v1/gebco2020"
    for camp in campaigns:
        # Build NxN grid
        lats = [camp["lat_min"] + (camp["lat_max"] - camp["lat_min"]) * i / (n - 1) for i in range(n)]
        lons = [camp["lon_min"] + (camp["lon_max"] - camp["lon_min"]) * i / (n - 1) for i in range(n)]
        pts = [(la, lo) for la in lats for lo in lons]
        # opentopodata caps at 100 locations and 1 req/s
        locs = "|".join(f"{la:.4f},{lo:.4f}" for la, lo in pts)
        url = f"{base}?locations={locs}"
        print(f"  Bathy | {camp['name'][:45]:45s}", end="  ")
        resp = safe_get(url, timeout=60)
        if resp is None or resp.status_code != 200:
            print("ERR")
            continue
        try:
            data = resp.json()
        except Exception:
            print("parse-err")
            continue
        results = data.get("results") or []
        for r in results:
            loc = r.get("location") or {}
            la, lo = loc.get("lat"), loc.get("lng")
            elev = r.get("elevation")
            if la is None or lo is None:
                continue
            depth = -float(elev) if elev is not None else None
            rows.append(_row(
                camp,
                source="EMODnet-Bathymetry (GEBCO_2020 via opentopodata)",
                data_type="bathy_probe", dataset_id="gebco2020",
                feature_id=f"{la:.4f}_{lo:.4f}",
                time="", lat=float(la), lon=float(lo),
                depth_m=depth,
                geom_wkt=_wkt_point(float(lo), float(la)),
                elevation_m=float(elev) if elev is not None else None,
                extra_json="", source_url=url,
            ))
        print(f"{len(results)} probes")
        time.sleep(1.1)   # respect 1 req/s
    return rows

BATHY_VALUE_COLS = ["elevation_m"]


# ---------------------------------------------------------------------------
# 6. EMODnet Biology / OBIS occurrences
# ---------------------------------------------------------------------------

def fetch_biology(campaigns: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for camp in campaigns:
        # OBIS expects a WKT polygon
        wkt = (
            "POLYGON(("
            f"{camp['lon_min']} {camp['lat_min']}, {camp['lon_max']} {camp['lat_min']}, "
            f"{camp['lon_max']} {camp['lat_max']}, {camp['lon_min']} {camp['lat_max']}, "
            f"{camp['lon_min']} {camp['lat_min']}))"
        )
        params = {"geometry": wkt, "size": OBIS_SIZE}
        if camp["date_min"]: params["startdate"] = camp["date_min"]
        if camp["date_max"]: params["enddate"]   = camp["date_max"]
        url = "https://api.obis.org/v3/occurrence"
        print(f"  OBIS | {camp['name'][:45]:45s}", end="  ")
        try:
            r = requests.get(url, params=params, timeout=90)
            if r.status_code != 200:
                print(f"HTTP {r.status_code}")
                continue
            payload = r.json()
        except Exception as e:
            print(f"ERR ({str(e)[:40]})")
            continue
        results = payload.get("results", [])
        for o in results:
            la = o.get("decimalLatitude"); lo = o.get("decimalLongitude")
            if la is None or lo is None:
                continue
            rows.append(_row(
                camp,
                source="EMODnet-Biology/OBIS", data_type="occurrence",
                dataset_id=str(o.get("dataset_id", "")),
                feature_id=str(o.get("id", "")),
                time=str(o.get("eventDate", "")),
                lat=float(la), lon=float(lo),
                depth_m=float(o["depth"]) if o.get("depth") not in (None, "") else None,
                geom_wkt=_wkt_point(float(lo), float(la)),
                scientific_name=str(o.get("scientificName", "")),
                aphia_id=str(o.get("aphiaID", "")),
                kingdom=str(o.get("kingdom", "")),
                phylum=str(o.get("phylum", "")),
                class_=str(o.get("class", "")),
                order=str(o.get("order", "")),
                family=str(o.get("family", "")),
                genus=str(o.get("genus", "")),
                individual_count=o.get("individualCount", ""),
                basis_of_record=str(o.get("basisOfRecord", "")),
                institution_code=str(o.get("institutionCode", "")),
                extra_json="",
                source_url=r.url,
            ))
        total = payload.get("total", len(results))
        print(f"{len(results)} of {total} returned")
        time.sleep(0.5)
    return rows

BIO_VALUE_COLS = [
    "scientific_name", "aphia_id", "kingdom", "phylum", "class_", "order",
    "family", "genus", "individual_count", "basis_of_record", "institution_code",
]


# ---------------------------------------------------------------------------
# 7. Copernicus Marine — optional, requires credentials
# ---------------------------------------------------------------------------

# Surface (single depth) datasets per region. Keep the list small and global.
# In copernicusmarine v2 (2024+) the global physics analysis-forecast catalogue
# is split per variable, so thetao and so live in separate datasets. CHL only
# exists daily for older windows; the multi-year monthly L4 product is the
# robust pick. SST L4 reprocessed covers the full historical record.
CMEMS_PRODUCTS = [
    # (dataset_id, variables, depth_m_or_None)
    # First model level is 0.494 m, so depth bounds [0, 1] select the surface.
    ("cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m", ["thetao"], (0.0, 1.0)),
    ("cmems_mod_glo_phy-so_anfc_0.083deg_P1D-m",     ["so"],     (0.0, 1.0)),
    ("cmems_obs-oc_glo_bgc-plankton_my_l4-multi-4km_P1M", ["CHL"], None),
    ("METOFFICE-GLO-SST-L4-REP-OBS-SST",             ["analysed_sst"], None),
]

# Per-workspace location of the Copernicus Marine credentials file. The user
# placed it under the workspace root so we do not assume the home directory.
CMEMS_CREDS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".copernicusmarine", ".copernicusmarine-credentials",
)


def fetch_copernicus(campaigns: list[dict]) -> list[dict]:
    """Subset surface CMEMS layers via copernicusmarine if creds are present.

    Credentials are read from, in order:
      1. environment variables COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD,
      2. environment variable COPERNICUSMARINE_CREDENTIALS_FILE (path),
      3. the workspace-local file .copernicusmarine/.copernicusmarine-credentials,
      4. the default ~/.copernicusmarine/.copernicusmarine-credentials.

    The workspace-local file must be in the official base64-encoded INI
    format that copernicusmarine writes; generate it with
        copernicusmarine login --credentials-file <path> --force-overwrite
    or in Python:
        cm.login(username=..., password=..., credentials_file=<path>,
                 force_overwrite=True)

    If anything is missing the function writes a manifest row only (so users
    know what to fetch manually) rather than failing the whole build.
    """
    rows: list[dict] = []
    try:
        import copernicusmarine as cm  # type: ignore
    except ImportError:
        cm = None

    creds_file: str | None = None
    if cm is not None:
        for cand in (
            os.environ.get("COPERNICUSMARINE_CREDENTIALS_FILE"),
            CMEMS_CREDS_FILE,
            os.path.expanduser("~/.copernicusmarine/.copernicusmarine-credentials"),
        ):
            if cand and os.path.exists(cand):
                creds_file = cand
                break
    have_env_creds = bool(os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")) \
                     and bool(os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD"))
    have_creds = have_env_creds or creds_file is not None

    if cm is not None and have_creds:
        print(f"  CMEMS auth: env={have_env_creds}, file={creds_file or '(none)'}")

    tmp = os.path.join(OUT_DIR, "_cmems_tmp")
    os.makedirs(tmp, exist_ok=True)

    import re as _re
    def _slug(s: str) -> str:
        # ASCII-only slug for Windows-safe filenames
        s2 = (s.encode("ascii", "ignore").decode("ascii"))
        return _re.sub(r"[^A-Za-z0-9_-]+", "_", s2).strip("_")[:30] or "camp"

    for camp in campaigns:
        for ds_id, varlist, depth in CMEMS_PRODUCTS:
            manifest_url = f"https://data.marine.copernicus.eu/product/description?dataset={ds_id}"
            if cm is None or not have_creds:
                rows.append(_row(
                    camp,
                    source="Copernicus Marine", data_type="manifest", dataset_id=ds_id,
                    feature_id=f"{ds_id}@{camp['name']}",
                    time=f"{camp['date_min']}/{camp['date_max']}",
                    lat=camp["clat"], lon=camp["clon"],
                    depth_m=(depth[0] if isinstance(depth, tuple) else depth),
                    geom_wkt=_wkt_point(camp["clon"], camp["clat"]),
                    variables=",".join(varlist),
                    note="copernicusmarine package or credentials missing - fetch manually",
                    extra_json=json.dumps({
                        "minimum_longitude": camp["lon_min"], "maximum_longitude": camp["lon_max"],
                        "minimum_latitude":  camp["lat_min"], "maximum_latitude":  camp["lat_max"],
                        "start_datetime":    camp["date_min"], "end_datetime":     camp["date_max"],
                        "minimum_depth":     (depth[0] if isinstance(depth, tuple) else depth),
                        "maximum_depth":     (depth[1] if isinstance(depth, tuple) else depth),
                    }),
                    source_url=manifest_url,
                ))
                continue
            try:
                print(f"  CMEMS | {camp['name'][:30]:30s} {ds_id[:50]:50s}", end="  ")
                # Clip the per-call bbox to a reasonable neighbourhood around
                # the campaign centroid so very large or antimeridian-crossing
                # boxes (Arctic 322 degrees wide; Iberian/Spanish coast >15
                # degrees wide) do not generate gigabyte-scale NetCDFs that
                # blow up memory. The mashup only needs a representative
                # sample of the gridded surface fields per campaign.
                _CMEMS_HALF = 2.0  # degrees around centroid
                lon_min = max(camp["lon_min"], camp["clon"] - _CMEMS_HALF)
                lon_max = min(camp["lon_max"], camp["clon"] + _CMEMS_HALF)
                lat_min = max(camp["lat_min"], camp["clat"] - _CMEMS_HALF)
                lat_max = min(camp["lat_max"], camp["clat"] + _CMEMS_HALF)
                subset_kwargs: dict = dict(
                    dataset_id=ds_id, variables=varlist,
                    minimum_longitude=lon_min, maximum_longitude=lon_max,
                    minimum_latitude=lat_min,  maximum_latitude=lat_max,
                    start_datetime=camp["date_min"],   end_datetime=camp["date_max"],
                    output_directory=tmp,
                    output_filename=f"{_slug(ds_id)}__{_slug(camp['name'])}.nc",
                    overwrite=True,
                    disable_progress_bar=True,
                )
                if isinstance(depth, tuple):
                    subset_kwargs.update(minimum_depth=depth[0], maximum_depth=depth[1])
                elif depth is not None:
                    subset_kwargs.update(minimum_depth=depth, maximum_depth=depth)
                if creds_file:
                    subset_kwargs["credentials_file"] = creds_file
                res = cm.subset(**subset_kwargs)
                file_path = getattr(res, "file_path", None) or res.output[0].file_path  # type: ignore[attr-defined]
                import xarray as xr
                ds = xr.open_dataset(file_path)
                df = ds.to_dataframe().reset_index().dropna(subset=varlist, how="all")
                # Coords come back as latitude/longitude OR lat/lon depending on the product
                lat_col = "latitude" if "latitude" in df.columns else ("lat" if "lat" in df.columns else None)
                lon_col = "longitude" if "longitude" in df.columns else ("lon" if "lon" in df.columns else None)
                if lat_col is None or lon_col is None:
                    print("no lat/lon cols; skipping")
                    ds.close()
                    continue
                # Cap massive subsets (e.g. Arctic / cross-meridian bboxes) to a
                # tractable per-call row count by uniform subsampling so the CSV
                # stays manageable and the iteration finishes in seconds.
                CMEMS_MAX_ROWS_PER_CALL = 50000
                if len(df) > CMEMS_MAX_ROWS_PER_CALL:
                    step = len(df) // CMEMS_MAX_ROWS_PER_CALL + 1
                    df = df.iloc[::step].reset_index(drop=True)
                # Vectorised conversion to records (orders of magnitude faster
                # than iterrows on 100k+ row frames).
                records = df.to_dict("records")
                count = 0
                for r in records:
                    la = r.get(lat_col); lo = r.get(lon_col)
                    if pd.isna(la) or pd.isna(lo):
                        continue
                    t = r.get("time")
                    t_iso = t.isoformat() if hasattr(t, "isoformat") else str(t)
                    extra = {v: (float(r[v]) if (v in r and not pd.isna(r[v])) else None) for v in varlist}
                    rows.append(_row(
                        camp,
                        source="Copernicus Marine", data_type="surface_grid", dataset_id=ds_id,
                        feature_id=f"{ds_id}_{t_iso}_{float(la):.3f}_{float(lo):.3f}",
                        time=t_iso, lat=float(la), lon=float(lo),
                        depth_m=(depth[0] if isinstance(depth, tuple) else depth),
                        geom_wkt=_wkt_point(float(lo), float(la)),
                        variables=",".join(varlist),
                        note="",
                        extra_json=json.dumps(extra),
                        source_url=manifest_url,
                    ))
                    count += 1
                ds.close()
                print(f"{count} rows")
            except KeyboardInterrupt:
                # Treat Ctrl-C / transient SSL/network interrupt as a per-call
                # failure so the rest of the campaign×dataset matrix can finish.
                print("INTERRUPTED - skipping this call")
                continue
            except BaseException as e:
                print(f"ERR ({type(e).__name__}: {str(e)[:80]})")
                continue
    return rows

CMEMS_VALUE_COLS = ["variables", "note"]


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

README_TEMPLATE = """# ONE-BLUE External Data — Campaign-Indexed Resources

Generated by `build_campaign_datasources.py` for DKAN dataset
[e0622a5f-1874-44ed-aa29-f80d27a8bcee](https://data.one-blue.eu/dataset/e0622a5f-1874-44ed-aa29-f80d27a8bcee).

One CSV per data-type category. Each row is tagged with the ONE-BLUE
sampling campaign whose spatio-temporal bounding box it falls into
(`campaign_code`, `campaign_area`, `campaign_date_min/max`). Geo-render
via the `geom_wkt` column or the plain `lat`/`lon` columns.

The `source` column inside each CSV identifies the upstream provider
(Euro-Argo, EMSO-ERIC, Copernicus Marine, EMODnet-*, OBIS).

## Resources

| File | Category | Sources merged |
|------|----------|----------------|
| `oceanography.csv`     | In-situ + gridded seawater observations | Euro-Argo, EMSO-ERIC, Copernicus Marine |
| `chemistry.csv`        | Eutrophication & contaminants sampling stations (water / sediment / biota) with parameter flags and SeaDataNet CDI links | EMODnet Chemistry ERDDAP (per-basin tabledap) |
| `human_activities.csv` | Anthropogenic features                   | EMODnet Human Activities (aquaculture / energy / protection / pressures / ports — see `data_type`) |
| `bathymetry.csv`       | Seafloor depth                           | EMODnet Bathymetry via GEBCO 2020 (opentopodata.org) |
| `biology.csv`          | Species occurrences                      | EMODnet Biology / OBIS API |

## Querying by campaign

In the DKAN datastore SQL endpoint, filter by `campaign_code`:

```sql
SELECT * FROM "<resource_id>"
WHERE campaign_code = 'Ionian Sea'
```

Or by source within a category:

```sql
SELECT * FROM "<oceanography_resource_id>"
WHERE campaign_code = 'Ionian Sea' AND source = 'Euro-Argo'
```

## Regenerating

```powershell
python build_campaign_datasources.py                          # all categories
python build_campaign_datasources.py --only oceanography      # just one
python build_campaign_datasources.py --only oceanography,biology --skip copernicus
python build_campaign_datasources.py --out custom_dir
```

Copernicus needs `pip install copernicusmarine` and credentials (env
vars `COPERNICUSMARINE_SERVICE_USERNAME` / `_PASSWORD` or the file
`~/.copernicusmarine/.copernicusmarine-credentials`); otherwise the
Copernicus rows in `oceanography.csv` are emitted as a manifest with
the bbox parameters needed to fetch each layer manually.
"""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

CATEGORIES = ["oceanography", "chemistry", "human_activities", "bathymetry", "biology"]

# Union of value columns for the merged oceanography file
OCEAN_VALUE_COLS = sorted(set(ARGO_VALUE_COLS + EMSO_VALUE_COLS + CMEMS_VALUE_COLS))


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", default="", help="Comma-separated subset of: " + ",".join(CATEGORIES))
    p.add_argument("--skip", default="", help="Comma-separated sub-sources to skip within a category. "
                                              "Valid: argo, emso, copernicus")
    p.add_argument("--out",  default=OUT_DIR)
    args = p.parse_args(argv)

    targets = [s.strip() for s in args.only.split(",") if s.strip()] or CATEGORIES
    unknown = [t for t in targets if t not in CATEGORIES]
    if unknown:
        sys.exit(f"Unknown category(s): {unknown}. Allowed: {CATEGORIES}")
    skips = {s.strip().lower() for s in args.skip.split(",") if s.strip()}

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    print(f"Loading campaigns from SAMPLES folder ...")
    campaigns = load_campaigns_from_samples()
    print(f"  {len(campaigns)} campaigns loaded\n")
    if not campaigns:
        sys.exit(
            "ERROR: 0 campaigns loaded. The most common cause is running this "
            "script outside the project venv (openpyxl missing). Re-run with:\n"
            "  .\\.venv\\Scripts\\python.exe build_campaign_datasources.py"
        )

    if "oceanography" in targets:
        print("--- Oceanography (Argo + EMSO + Copernicus) ---")
        rows: list[dict] = []
        if "argo" not in skips:
            print(" * Euro-Argo")
            rows += fetch_argo(campaigns)
        if "emso" not in skips:
            print(" * EMSO-ERIC")
            rows += fetch_emso(campaigns)
        if "copernicus" not in skips:
            print(" * Copernicus Marine")
            rows += fetch_copernicus(campaigns)
        _write_csv(rows, os.path.join(out_dir, "oceanography.csv"),
                   value_cols=OCEAN_VALUE_COLS)

    if "chemistry" in targets:
        print("\n--- Chemistry (EMODnet Chemistry catalogue) ---")
        _write_csv(fetch_emodnet_chemistry(campaigns),
                   os.path.join(out_dir, "chemistry.csv"),
                   value_cols=CHEM_VALUE_COLS)

    if "human_activities" in targets:
        print("\n--- Human Activities (EMODnet HA) ---")
        ha = fetch_human_activities(campaigns)
        merged = [r for grp_rows in ha.values() for r in grp_rows]
        _write_csv(merged,
                   os.path.join(out_dir, "human_activities.csv"),
                   value_cols=HA_VALUE_COLS)

    if "bathymetry" in targets:
        print("\n--- Bathymetry (GEBCO 2020 via opentopodata) ---")
        _write_csv(fetch_bathymetry(campaigns),
                   os.path.join(out_dir, "bathymetry.csv"),
                   value_cols=BATHY_VALUE_COLS)

    if "biology" in targets:
        print("\n--- Biology (EMODnet Biology / OBIS) ---")
        _write_csv(fetch_biology(campaigns),
                   os.path.join(out_dir, "biology.csv"),
                   value_cols=BIO_VALUE_COLS)

    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(README_TEMPLATE)

    print(f"\nDone. Output: {out_dir}")


if __name__ == "__main__":
    main()
