"""
query_external_data.py

For each ONE-BLUE campaign (read from GEOTEMPORAL_OVERVIEW.xlsx),
queries external data sources:
  1. EMSO-ERIC ERDDAP  — fixed observatory time-series
  2. Euro-Argo (argopy) — profiling float T/S data
  3. EMODnet Chemistry ERDDAP — contaminants, eutrophication
  4. EMODnet Human Activities WFS — fishing, aquaculture, platforms, etc.

Produces EXTERNAL_DATA_MATCHES.xlsx with one sheet per source.

Usage:
    python query_external_data.py
"""

import math
import time
import warnings
import traceback
import requests
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent
OVERVIEW_FILE   = str(_ROOT / "GEOTEMPORAL_OVERVIEW.xlsx")
SAMPLES_FOLDER  = str(_ROOT / "SAMPLES")
SAMPLES_CSV     = str(_ROOT / "samples.csv")
OUTPUT_FILE     = str(_ROOT / "EXTERNAL_DATA_MATCHES.xlsx")

EMSO_ERDDAP     = "https://erddap.emso.eu/erddap"
EMODCHEM_ERDDAP = "https://erddap.emodnet-chemistry.eu/erddap"
HA_WFS          = "https://ows.emodnet-humanactivities.eu/wfs"
BBOX_PAD        = 0.5   # degrees padding on each side for WFS queries
EMSO_RADIUS_KM  = 300   # max distance for EMSO node selection

# ---------------------------------------------------------------------------
# EMSO node catalogue
# Each node lists candidate dataset_ids (preferred first).
# We query just `time` (csv0 format) to avoid variable-name differences across
# instruments. ERDDAP returns HTTP 404 when no rows match a time filter.
# ---------------------------------------------------------------------------
EMSO_NODES = [
    {
        "node":        "E1M3A",
        "description": "Cretan Sea",
        "lat":          35.00,
        "lon":          24.00,
        "dataset_ids": [
            "E1M3A_20240313_20241126",
            "E1M3A_20221202_20240312",
            "E1M3A_20201127_20210912",
        ],
    },
    {
        "node":        "E2M3A",
        "description": "South Adriatic (Bari Canyon)",
        "lat":          41.30,
        "lon":          18.10,
        "dataset_ids": [
            "E2M3A_CTD_meteo_CO2_pH_NRT",
            "E2M3A_CTD_2006_2023_TS",
        ],
    },
    {

        "node":        "W1M3A",
        "description": "Western Mediterranean",
        "lat":          39.00,
        "lon":          12.50,
        "dataset_ids": [
            "W1M3A_deploy08",
            "W1M3A_deploy07",
        ],
    },
    {
        "node":        "EMSO-Ligurian",
        "description": "Western Ligurian Sea",
        "lat":          43.25,
        "lon":          7.87,
        "dataset_ids": [
            "Emso_Western_Ligurian_Albatross_Microcat_NetCDF_2024",
            "Emso_Western_Ligurian_Albatross_Microcat_NetCDF_2021",
        ],
    },
    {
        "node":        "OBSEA",
        "description": "Catalan Coast (seabed)",
        "lat":          41.18,
        "lon":          1.75,
        "dataset_ids": [
            "OBSEA_seabed_station_TS_L1c",
        ],
    },
    {
        "node":        "SmartBay",
        "description": "Galway Bay, Ireland",
        "lat":          53.23,
        "lon":         -9.90,
        "dataset_ids": [
            "smartbay_obs_hour_mean",
        ],
    },
]

# ---------------------------------------------------------------------------
# EMODnet Human Activities layers to query (name, typeName keyword)
# ---------------------------------------------------------------------------
HA_LAYERS = [
    ("shellfish",         "Shellfish farms"),
    ("finfish",           "Finfish farms"),
    ("finfishnew",        "Finfish farms (new)"),
    ("platforms",         "Oil & gas platforms"),
    ("boreholes",         "Offshore wells"),
    ("activelicenses",    "Active O&G licences"),
    ("pipelines",         "Offshore pipelines"),
    ("windfarms",         "Wind farms (points)"),
    ("windfarmspoly",     "Wind farms (polygons)"),
    ("dredging",          "Dredging sites"),
    ("dredgespoil",       "Dredge spoil dumps"),
    ("portlocations",     "Port locations"),
    ("dischargepoints",   "Discharge points"),
    ("treatmentplants",   "Treatment plants"),
    ("militaryareaspoly", "Military areas"),
    ("munitions",         "Dumped munitions"),
    ("natura2000areas",   "Natura 2000 MPAs"),
    ("marineprotectedareas", "Marine protected areas"),
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Return great-circle distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def safe_get(url, timeout=30, retries=2):
    """GET with retry and timeout; return Response or None.

    Returns the Response for both 200 (data) and 404 (ERDDAP "no matching
    results") so callers can distinguish real errors from empty result sets.
    Only returns None on connection/5xx failures.
    Falls back to verify=False if SSL certificate has expired.
    """
    for attempt in range(retries):
        for verify in (True, False):
            try:
                if not verify:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                r = requests.get(url, timeout=timeout, verify=verify)
                if r.status_code in (200, 404):
                    return r
                # 4xx other than 404 → no point retrying
                if 400 <= r.status_code < 500:
                    return None
                break  # 5xx: retry outer loop
            except requests.exceptions.SSLError:
                if verify:
                    continue  # retry with verify=False
                pass
            except requests.exceptions.RequestException:
                break  # non-SSL network error: retry outer loop
        time.sleep(2)
    return None


# ---------------------------------------------------------------------------
# Load campaigns from GEOTEMPORAL_OVERVIEW.xlsx
# ---------------------------------------------------------------------------

def load_campaigns():
    """Return list of campaign dicts from the 'Datasets' sheet."""
    df = pd.read_excel(OVERVIEW_FILE, sheet_name="Datasets")
    campaigns = []
    for _, row in df.iterrows():
        # Normalise column names (handle possible whitespace variations)
        r = {str(k).strip(): v for k, v in row.items()}

        # Try to extract lat/lon bounds — column names from explore_geotemporal output
        lat_min = r.get("Lat Min") or r.get("lat_min")
        lat_max = r.get("Lat Max") or r.get("lat_max")
        lon_min = r.get("Lon Min") or r.get("lon_min")
        lon_max = r.get("Lon Max") or r.get("lon_max")
        date_min = r.get("Sampling Date Min") or r.get("Date Min") or r.get("date_min") or r.get("Start Date")
        date_max = r.get("Sampling Date Max") or r.get("Date Max") or r.get("date_max") or r.get("End Date")
        name = r.get("Campaign Code") or r.get("campaign_code") or r.get("File") or "Unknown"
        area = r.get("Area of Study") or r.get("area_of_study") or ""

        # Skip if no bounding box
        try:
            lat_min = float(lat_min)
            lat_max = float(lat_max)
            lon_min = float(lon_min)
            lon_max = float(lon_max)
        except (TypeError, ValueError):
            continue

        # Compute centroid
        clat = (lat_min + lat_max) / 2
        clon = (lon_min + lon_max) / 2

        # Normalise dates to string YYYY-MM-DD
        def _date_str(d):
            if d is None or (isinstance(d, float) and math.isnan(d)):
                return None
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m-%d")
            return str(d)[:10]

        campaigns.append({
            "name":     str(name)[:60],
            "area":     str(area),
            "lat_min":  lat_min,
            "lat_max":  lat_max,
            "lon_min":  lon_min,
            "lon_max":  lon_max,
            "clat":     clat,
            "clon":     clon,
            "date_min": _date_str(date_min),
            "date_max": _date_str(date_max),
        })
    return campaigns


# ---------------------------------------------------------------------------
# Load campaigns from SAMPLES folder xlsx files
# ---------------------------------------------------------------------------

# Known bounding boxes for sampling sites that carry no lat/lon in the file.
# Format: name_fragment (lower-case, matched with 'in') → (lat_min, lat_max, lon_min, lon_max)
_NAMED_COORDS = {
    "mali ston":      (42.80, 42.95, 17.45, 17.72),   # Mali Ston Bay, Croatia
    "ionian":         (38.60, 40.60, 16.40, 17.30),   # Ionian Sea (IDAEA samples)
    "north adriatic": (44.70, 45.60, 12.30, 13.10),   # North Adriatic
    "adriatic":       (44.70, 45.60, 12.30, 13.10),   # fallback for Adriatic labels
    "sant carles":    (40.55, 40.70,  0.48,  0.65),   # Sant Carles de la Ràpita
    "cadiz":          (36.40, 36.65, -6.55, -6.10),   # Cádiz, Spain
    "cadis":          (36.40, 36.65, -6.55, -6.10),   # alt spelling
    "portuguese":     (37.00, 42.00,-10.80, -8.50),   # NE Atlantic Portuguese Coast
    "portugal":       (37.00, 42.00,-10.80, -8.50),
    "atlantic":       (37.00, 42.00,-10.80, -8.50),
}


def _lookup_coords(name: str):
    """Return (lat_min, lat_max, lon_min, lon_max) from _NAMED_COORDS or None."""
    nl = name.lower()
    for fragment, bbox in _NAMED_COORDS.items():
        if fragment in nl:
            return bbox
    return None


_DMS_NUM_RE = __import__("re").compile(r"-?\d+(?:[.,]\d+)?")
_HEMI_RE    = __import__("re").compile(r"[NSEWOnsewо]")


def _parse_coord(val):
    """Return a point coordinate; ranges must use _parse_coord_extent instead."""
    extent = _parse_coord_extent(val)
    return extent[0] if extent and extent[0] == extent[1] else None


def _parse_coord_extent(val):
    """Return both coordinate bounds, or None for invalid/incomplete cells."""
    import re

    if val is None:
        return None
    if isinstance(val, (int, float)):
        return (float(val), float(val)) if math.isfinite(val) else None
    text = str(val).strip().upper().replace(",", ".")
    text = re.sub(r"(\d)\s+\.(\d)", r"\1.\2", text)
    endpoints = re.split(r"(?<=[0-9NSEWO])\s*[-\u2013\u2014]\s*(?=[+-]?\d|X)", text)
    if len(endpoints) > 2:
        return None
    values = []
    for endpoint in endpoints:
        endpoint = re.sub(r"[\u00b0\u2032\u2033'\"]", " ", endpoint).strip()
        match = re.fullmatch(
            r"([+-]?\d+(?:\.\d+)?)(?:\s+(\d+(?:\.\d+)?))?"
            r"(?:\s+(\d+(?:\.\d+)?))?\s*([NSEWO])?", endpoint)
        if not match:
            return None
        degrees, minutes, seconds, hemisphere = match.groups()
        if minutes is not None and hemisphere is None and not degrees.startswith(("+", "-")):
            return None
        degrees = float(degrees)
        minutes, seconds = float(minutes or 0), float(seconds or 0)
        if minutes >= 60 or seconds >= 60:
            return None
        if degrees < 0 and hemisphere in ("N", "E"):
            return None
        sign = -1 if degrees < 0 or hemisphere in ("S", "W", "O") else 1
        values.append(sign * (abs(degrees) + minutes / 60 + seconds / 3600))
    return min(values), max(values)


def _date_str(d):
    """Normalize native Excel dates or ISO strings, rejecting ambiguous text."""
    if d is None or (isinstance(d, float) and math.isnan(d)):
        return None
    try:
        if pd.isna(d):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(d, "strftime"):
        try:
            return d.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    s = str(d).strip()
    if not s:
        return None
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].+)?", s):
        return None
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


CONTEXT_DAYS = 30


def campaign_date_window(campaign, padding_days=None):
    """Return an inclusive day-level search window without changing sample dates."""
    from datetime import date, timedelta

    padding = campaign.get("context_days", CONTEXT_DAYS) if padding_days is None else padding_days
    if isinstance(padding, bool) or not isinstance(padding, int) or padding < 0:
        raise ValueError("Context padding must be a non-negative integer number of days")
    if not campaign.get("date_min") or not campaign.get("date_max"):
        raise ValueError("Cannot build a query without both sampling dates")
    start = date.fromisoformat(campaign["date_min"])
    end = date.fromisoformat(campaign["date_max"])
    if end < start:
        raise ValueError("Sampling end precedes sampling start")
    return (start - timedelta(days=padding)).isoformat(), (end + timedelta(days=padding)).isoformat()


def temporal_scope(campaign, observation_time):
    """Classify day-level temporal support separately from spatial matching."""
    if observation_time is None or not campaign.get("date_min") or not campaign.get("date_max"):
        return "unknown"
    endpoints = str(observation_time).split("/")
    if len(endpoints) > 2:
        return "unknown"
    start, end = _date_str(endpoints[0]), _date_str(endpoints[-1])
    if not start or not end or end < start:
        return "unknown"
    if campaign["date_min"] <= start <= end <= campaign["date_max"]:
        return "within_sample_window"
    if start <= campaign["date_max"] and end >= campaign["date_min"]:
        return "overlaps_sample_window"
    return "context"


def _campaigns_from_frame(df):
    campaigns: list[dict] = []
    code_counts = df.groupby("campaign_code")["campaign_id"].nunique()
    for campaign_id, group in df.groupby("campaign_id", sort=False):
        spatial = group[group["spatial_kind"].isin(("point", "extent"))]
        bounds = {
            "lat_min": spatial["latitude_min"].min(), "lat_max": spatial["latitude_max"].max(),
            "lon_min": spatial["longitude_min"].min(), "lon_max": spatial["longitude_max"].max(),
        }
        bounds = {key: float(value) if pd.notna(value) else None for key, value in bounds.items()}
        starts = group["collection_date"].dropna()
        ends = group["collection_end"].dropna()
        code = str(group["campaign_code"].iloc[0])
        reasons = set()
        for flags in group["quality_flags"].dropna():
            reasons.update(flag for flag in str(flags).split("|")
                           if "unconfirmed" in flag or flag.startswith(("invalid_", "ambiguous_")))
        if spatial.empty:
            reasons.add("no_valid_spatial_extent")
        if starts.empty or ends.empty:
            reasons.add("no_valid_date_window")
        if code_counts[code] > 1:
            reasons.add("duplicate_campaign_metadata")
        if bounds["lon_min"] is not None and bounds["lon_max"] - bounds["lon_min"] > 180:
            reasons.add("wide_or_antimeridian_extent")
        blocking_reasons = reasons.intersection({
            "no_valid_spatial_extent", "no_valid_date_window", "duration_semantics_unconfirmed",
        })
        has_bounds = all(value is not None for value in bounds.values())
        campaigns.append({
            "name": code if code_counts[code] == 1 else f"{code} ({campaign_id[:8]})",
            "campaign_id": str(campaign_id), "campaign_code": code,
            "campaign_name": str(group["campaign_area"].iloc[0]),
            "area": str(group["campaign_area"].iloc[0]), "case_study": "",
            "n_stations": int(group["station_label"].replace("", pd.NA).nunique()),
            "n_samples": len(group), "source_file": str(group["source_file"].iloc[0]),
            **bounds,
            "clat": (bounds["lat_min"] + bounds["lat_max"]) / 2 if has_bounds else None,
            "clon": (bounds["lon_min"] + bounds["lon_max"]) / 2 if has_bounds else None,
            "date_min": str(starts.min()) if len(starts) else None,
            "date_max": str(ends.max()) if len(ends) else None,
            "review_reasons": sorted(reasons),
            "blocking_reasons": sorted(blocking_reasons),
            "context_days": CONTEXT_DAYS,
            "suspect_value_policy": "retain_literal",
        })
        campaign = campaigns[-1]
        if campaign["date_min"] and campaign["date_max"]:
            campaign["query_date_min"], campaign["query_date_max"] = campaign_date_window(campaign)
        else:
            campaign["query_date_min"] = campaign["query_date_max"] = None
    return campaigns


def _campaigns_from_csv(folder=SAMPLES_FOLDER, csv_path=SAMPLES_CSV):
    """Read only a verified normalized index matching the current workbooks."""
    import hashlib
    import json
    from build_samples_csv import SCHEMA_VERSION, OUTPUT_COLS, input_fingerprints

    path = Path(csv_path)
    try:
        manifest = json.loads(path.with_suffix(".audit.json").read_text(encoding="utf-8"))
        if (manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("inputs") != input_fingerprints(folder)
                or manifest.get("csv_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
            return None
        if any(report.get("status") == "error" for report in manifest.get("workbooks", [])):
            raise ValueError("Unreadable workbook: inspect samples.audit.json before fetching")
        numeric = {"source_row", "latitude", "longitude", "latitude_min", "latitude_max",
               "longitude_min", "longitude_max", "depth_m"}
        frame = pd.read_csv(path, low_memory=False, keep_default_na=False, na_values=[""],
                    float_precision="round_trip",
                    dtype={column: str for column in OUTPUT_COLS if column not in numeric})
        if not set(OUTPUT_COLS).issubset(frame.columns):
            return None
    except (OSError, KeyError, pd.errors.ParserError, json.JSONDecodeError):
        return None
    return _campaigns_from_frame(frame)


def load_campaigns_from_samples(folder=SAMPLES_FOLDER, csv_path=SAMPLES_CSV, *, allow_unresolved=False):
    """Retain flagged inputs; block only campaigns without usable query bounds."""
    campaigns = _campaigns_from_csv(folder, csv_path)
    if campaigns is None:
        from build_samples_csv import build_samples_csv
        build_samples_csv(folder, csv_path)
        campaigns = _campaigns_from_csv(folder, csv_path)
    if campaigns is None or not campaigns:
        raise ValueError("No normalized campaigns available; inspect the sample audit")
    unresolved = [camp for camp in campaigns if camp["blocking_reasons"]]
    if unresolved and not allow_unresolved:
        details = "; ".join(f"{camp['source_file']}: {', '.join(camp['blocking_reasons'])}" for camp in unresolved)
        raise ValueError(f"Sample extent review required before fetching. {details}")
    return campaigns


# ---------------------------------------------------------------------------
# 1. EMSO-ERIC query
# ---------------------------------------------------------------------------

def query_emso(campaigns):
    """For each campaign, find nearby EMSO nodes and query ERDDAP.

    Tries each dataset_id for the node in order; stops at the first that
    returns data within the campaign date range. Uses csv0 with `?time` only
    to avoid variable-name differences across instruments.
    ERDDAP returns HTTP 404 when no rows match a time filter — treated as
    0 rows (not an error).
    """
    results = []
    for camp in campaigns:
        for node in EMSO_NODES:
            dist = haversine_km(camp["clat"], camp["clon"], node["lat"], node["lon"])
            if dist > EMSO_RADIUS_KM:
                continue

            time_filter = ""
            if camp["date_min"] and camp["date_max"]:
                time_filter = (
                    f"&time>={camp['date_min']}T00:00:00Z"
                    f"&time<={camp['date_max']}T23:59:59Z"
                )

            print(f"  EMSO | {camp['name'][:35]} → {node['node']} ({dist:.0f} km)  ", end="")

            n_rows = 0
            used_ds = "none"
            status = "NO_DATA"
            note = ""

            for ds_id in node["dataset_ids"]:
                url = f"{EMSO_ERDDAP}/tabledap/{ds_id}.csv0?time{time_filter}"
                resp = safe_get(url, timeout=60)

                if resp is None:
                    status = "ERROR"
                    note = f"HTTP error on {ds_id}"
                    used_ds = ds_id
                    break

                if resp.status_code == 404:
                    # ERDDAP says no matching results — try next dataset
                    continue

                # 200 → count rows (csv0 has no header row)
                rows = [l for l in resp.text.strip().splitlines() if l.strip()]
                if rows:
                    n_rows = len(rows)
                    used_ds = ds_id
                    status = "OK"
                    break

            if status == "NO_DATA":
                used_ds = ", ".join(node["dataset_ids"])
                note = "No data found in campaign period across all candidate datasets"

            print(f"{status} ({n_rows} rows)")
            results.append({
                "Campaign":         camp["name"],
                "Area":             camp["area"],
                "Date Min":         camp["date_min"],
                "Date Max":         camp["date_max"],
                "EMSO Node":        node["node"],
                "Node Description": node["description"],
                "Distance (km)":    round(dist, 1),
                "Dataset Used":     used_ds,
                "Rows Retrieved":   n_rows,
                "Status":           status,
                "Notes":            note,
            })
            time.sleep(0.5)
    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# 2. Euro-Argo query via argopy
# ---------------------------------------------------------------------------

def query_argo(campaigns):
    """Query Argo float profiles per campaign bounding box + date range."""
    try:
        from argopy import DataFetcher as ArgoFetcher
    except ImportError:
        print("  argopy not available — skipping Argo query")
        return pd.DataFrame()

    results = []
    for camp in campaigns:
        if camp["date_min"] is None or camp["date_max"] is None:
            results.append({
                "Campaign":       camp["name"],
                "Area":           camp["area"],
                "Date Min":       camp["date_min"],
                "Date Max":       camp["date_max"],
                "Profiles Found": 0,
                "Floats Found":   0,
                "Status":         "SKIPPED",
                "Notes":          "No date range available",
            })
            continue

        print(f"  Argo | {camp['name'][:55]}  ", end="")
        try:
            fetcher = ArgoFetcher(src="erddap")
            ds = fetcher.region([
                camp["lon_min"], camp["lon_max"],
                camp["lat_min"], camp["lat_max"],
                0, 2000,
                camp["date_min"], camp["date_max"],
            ]).to_xarray()
            n_profiles = int(ds.dims.get("N_POINTS", 0))
            n_floats = len(set(ds["PLATFORM_NUMBER"].values)) if "PLATFORM_NUMBER" in ds else 0
            status = "OK"
            note = ""
        except Exception as e:
            n_profiles = 0
            n_floats = 0
            status = "ERROR"
            note = str(e)[:120]

        print(f"{status} ({n_profiles} points, {n_floats} floats)")
        results.append({
            "Campaign":       camp["name"],
            "Area":           camp["area"],
            "Date Min":       camp["date_min"],
            "Date Max":       camp["date_max"],
            "Profiles Found": n_profiles,
            "Floats Found":   n_floats,
            "Status":         status,
            "Notes":          note,
        })
        time.sleep(1)
    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# 3. EMODnet Chemistry ERDDAP — dataset discovery
# ---------------------------------------------------------------------------

def query_emodnet_chemistry(campaigns):
    """
    Use the EMODnet Chemistry ERDDAP search API to discover relevant datasets
    per campaign bounding box.  This is pure catalogue discovery — no data
    is downloaded — so it is fast regardless of dataset size.

    ERDDAP search endpoint:
      /erddap/search/index.csv?searchFor=KEYWORDS&minLat=...&maxLat=...&minLon=...&maxLon=...
    """
    from io import StringIO

    # Keywords — ERDDAP fullText search: + = space = AND (all terms required).
    # Keep to 2–3 terms so datasets don't need to mention every word.
    SEARCH_TERMS = "contaminant+biota"
    # Widen bbox slightly so narrow campaign boxes still hit broader Chemistry datasets
    BBOX_PAD_CHEM = 2.0

    results = []
    for camp in campaigns:
        url = (
            f"{EMODCHEM_ERDDAP}/search/index.csv"
            f"?page=1&itemsPerPage=100"
            f"&searchFor={SEARCH_TERMS}"
            f"&minLat={camp['lat_min'] - BBOX_PAD_CHEM}&maxLat={camp['lat_max'] + BBOX_PAD_CHEM}"
            f"&minLon={camp['lon_min'] - BBOX_PAD_CHEM}&maxLon={camp['lon_max'] + BBOX_PAD_CHEM}"
        )

        print(f"  EMODnet Chem | {camp['name'][:50]}  ", end="")
        resp = safe_get(url, timeout=30)

        if resp is None or resp.status_code == 404:
            ds_ids = []
            titles = []
            status = "NO_MATCH" if (resp is not None and resp.status_code == 404) else "ERROR"
        else:
            try:
                df = pd.read_csv(StringIO(resp.text), low_memory=False)
                ds_ids  = df["Dataset ID"].tolist()  if "Dataset ID" in df.columns else []
                titles  = df["Title"].tolist()        if "Title"      in df.columns else []
                status  = "OK"
            except Exception:
                ds_ids, titles, status = [], [], "PARSE_ERROR"

        print(f"{status} ({len(ds_ids)} datasets)")
        results.append({
            "Campaign":          camp["name"],
            "Area":              camp["area"],
            "Date Min":          camp["date_min"],
            "Date Max":          camp["date_max"],
            "Matching Datasets": len(ds_ids),
            "Dataset IDs":       "; ".join(ds_ids),
            "Dataset Titles":    "; ".join(str(t) for t in titles),
            "Status":            status,
            "Search URL":        url,
        })
        time.sleep(0.5)

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# 4. EMODnet Human Activities WFS
# ---------------------------------------------------------------------------

def query_human_activities(campaigns):
    """Query EMODnet Human Activities WFS for each campaign bounding box."""
    results = []
    for camp in campaigns:
        # Expand bbox by BBOX_PAD
        b_lat_min = camp["lat_min"] - BBOX_PAD
        b_lat_max = camp["lat_max"] + BBOX_PAD
        b_lon_min = camp["lon_min"] - BBOX_PAD
        b_lon_max = camp["lon_max"] + BBOX_PAD

        row = {
            "Campaign": camp["name"],
            "Area":     camp["area"],
            "BBox":     f"{b_lat_min:.2f},{b_lon_min:.2f},{b_lat_max:.2f},{b_lon_max:.2f}",
        }

        print(f"  HA WFS | {camp['name'][:45]}")
        for layer_id, layer_label in HA_LAYERS:
            url = (
                f"{HA_WFS}"
                f"?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
                f"&typeName=emodnet:{layer_id}"
                f"&BBOX={b_lat_min},{b_lon_min},{b_lat_max},{b_lon_max},"
                f"urn:ogc:def:crs:EPSG::4326"
                f"&outputFormat=application/json&count=500"
            )
            resp = safe_get(url, timeout=45)
            if resp is None:
                row[layer_label] = "ERR"
            else:
                try:
                    data = resp.json()
                    count = len(data.get("features", []))
                    row[layer_label] = count
                except Exception:
                    row[layer_label] = "ERR"
            time.sleep(0.3)

        results.append(row)

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Loading campaigns from SAMPLES folder ...")
    campaigns = load_campaigns_from_samples()
    print(f"  {len(campaigns)} campaigns loaded")
    for c in campaigns:
        print(f"    {c['name'][:55]:55s}  [{c['lat_min']:.1f},{c['lat_max']:.1f}] "
              f"[{c['lon_min']:.1f},{c['lon_max']:.1f}]  "
              f"{c['date_min']} → {c['date_max']}")

    print("\n--- 1. EMSO-ERIC ---")
    df_emso = query_emso(campaigns)

    print("\n--- 2. Euro-Argo ---")
    df_argo = query_argo(campaigns)

    print("\n--- 3. EMODnet Chemistry ---")
    df_chem = query_emodnet_chemistry(campaigns)

    print("\n--- 4. EMODnet Human Activities ---")
    df_ha = query_human_activities(campaigns)

    print(f"\nWriting {OUTPUT_FILE} ...")
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        if not df_emso.empty:
            df_emso.to_excel(writer, sheet_name="EMSO", index=False)
        if not df_argo.empty:
            df_argo.to_excel(writer, sheet_name="Euro-Argo", index=False)
        if not df_chem.empty:
            df_chem.to_excel(writer, sheet_name="EMODnet Chemistry", index=False)
        if not df_ha.empty:
            df_ha.to_excel(writer, sheet_name="Human Activities", index=False)

    print("Done.")


if __name__ == "__main__":
    main()
