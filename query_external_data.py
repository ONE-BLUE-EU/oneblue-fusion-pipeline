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
    """Parse a Latitude / Longitude cell to a decimal-degree float, or None.

    Accepts:
      * numeric values (already decimal degrees, signed)
      * decimal-degrees with hemisphere letter, e.g. '15.727E', '38.0175 N'
      * deg-decmin with hemisphere, e.g. '43 45.83 N', '002 50,211 E'
      * deg-min-sec with hemisphere, e.g. '7 12 44.278 W'
      * range form 'a - b' / 'a-b' (the first endpoint is taken)
      * ',' as decimal separator
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if isinstance(val, float) and math.isnan(val):
            return None
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # Range form: take the first endpoint (split on dash NOT followed by digits in deg)
    if " - " in s or "- " in s or " -" in s:
        s = s.split("-", 1)[0].strip()
    elif "—" in s:
        s = s.split("—", 1)[0].strip()
    s = s.replace(",", ".")
    # Merge "DD .ddd" (space before bare decimal) into "DD.ddd" — e.g. "23 .670" → "23.670"
    s = __import__("re").sub(r"(\d)\s+\.(\d)", r"\1.\2", s)
    hemi_match = _HEMI_RE.search(s)
    hemi = hemi_match.group(0).upper() if hemi_match else ""
    nums = _DMS_NUM_RE.findall(s if not hemi_match else s[: hemi_match.start()])
    if not nums:
        return None
    try:
        nums = [float(n) for n in nums[:3]]
    except ValueError:
        return None
    deg = nums[0]
    sign = -1 if deg < 0 else 1
    deg = abs(deg)
    if len(nums) >= 2:
        deg += nums[1] / 60.0
    if len(nums) >= 3:
        deg += nums[2] / 3600.0
    if hemi in ("S", "W", "O"):  # O = Oeste/Ouest (Portuguese/French for West)
        sign = -1
    elif hemi in ("N", "E"):
        sign = 1 if sign > 0 else -1
    return sign * deg


def _date_str(d):
    """Normalise a date value to YYYY-MM-DD string or None."""
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
    # try pandas parser as a last resort
    try:
        ts = pd.to_datetime(s, errors="coerce", dayfirst=False)
        if pd.notna(ts):
            return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


def _campaigns_from_csv() -> list[dict] | None:
    """Derive campaign dicts from samples.csv if it exists.

    Returns a list of campaign dicts (same schema as load_campaigns_from_samples)
    or None if samples.csv is not present.
    """
    import os as _os
    if not _os.path.exists(SAMPLES_CSV):
        return None
    try:
        df = pd.read_csv(SAMPLES_CSV, low_memory=False)
    except Exception as e:
        print(f"  [WARN] Cannot read samples.csv: {e}")
        return None

    required = {"campaign_code", "latitude", "longitude", "collection_date"}
    if not required.issubset(df.columns):
        print("  [WARN] samples.csv missing required columns — falling back to xlsx")
        return None

    df["latitude"]  = pd.to_numeric(df["latitude"],  errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"])

    campaigns: list[dict] = []
    for code, grp in df.groupby("campaign_code", sort=False):
        lat_vals = grp["latitude"].values
        lon_vals = grp["longitude"].values
        lat_min, lat_max = float(lat_vals.min()), float(lat_vals.max())
        lon_min, lon_max = float(lon_vals.min()), float(lon_vals.max())

        if lat_min == lat_max:
            lat_min -= 0.05; lat_max += 0.05
        if lon_min == lon_max:
            lon_min -= 0.05; lon_max += 0.05

        dates = grp["collection_date"].dropna()
        dates = dates[dates.astype(str).str.match(r"\d{4}-\d{2}-\d{2}")]
        date_min = str(dates.min()) if len(dates) else None
        date_max = str(dates.max()) if len(dates) else None

        camp_area = str(grp["campaign_area"].iloc[0]) if "campaign_area" in grp.columns else str(code)
        source_file = str(grp["source_file"].iloc[0]) if "source_file" in grp.columns else ""
        clat = (lat_min + lat_max) / 2
        clon = (lon_min + lon_max) / 2

        campaigns.append({
            "name":          str(code),
            "campaign_code": str(code),
            "campaign_name": camp_area,
            "area":          camp_area,
            "case_study":    "",
            "n_stations":    len(grp),
            "source_file":   source_file,
            "lat_min":  lat_min, "lat_max": lat_max,
            "lon_min":  lon_min, "lon_max": lon_max,
            "clat":     clat,    "clon":    clon,
            "date_min": date_min,
            "date_max": date_max,
        })
    return campaigns


def load_campaigns_from_samples():
    """Read every .xlsx in SAMPLES_FOLDER and emit one campaign dict per file.

    If samples.csv exists in the workspace root (produced by build_samples_csv.py),
    campaign extents are derived from that pre-normalised file instead of re-parsing
    the xlsx files, which is both faster and guarantees the same coordinate
    normalisation that the retrieval pipeline uses.

    Each Excel follows the v0.11 Sample Collection Template:
      * Sheet 'Campaign'  → first non-empty row gives Campaign Code,
        Campaign Name, Area of Study, Case study, Number of Stations.
      * Sheet 'Sample'    → per-row Latitude / Longitude (decimal degrees
        or deg-decmin / DMS strings with hemisphere) and
        'Date of sampling start*' (+ optional 'Sampling duration - days').

    The bbox is the min/max of valid coordinates from the Sample sheet;
    the campaign window is min(start) … max(start + duration_days). If
    Campaign Code is missing the file stem is used. Files starting
    with 'SEEMS OLD' are skipped.
    """
    # Fast path: use pre-built samples.csv
    csv_camps = _campaigns_from_csv()
    if csv_camps is not None:
        print(f"  [samples] Loaded {len(csv_camps)} campaigns from samples.csv")
        return csv_camps

    import glob, os, datetime as _dt

    campaigns: list[dict] = []
    seen_codes: dict = {}

    for path in sorted(glob.glob(os.path.join(SAMPLES_FOLDER, "*.xlsx"))):
        fname = os.path.basename(path)
        if fname.startswith("SEEMS OLD"):
            continue
        stem = os.path.splitext(fname)[0]
        try:
            xl = pd.ExcelFile(path)
        except Exception as e:
            print(f"  [WARN] Cannot open {fname}: {e}")
            continue

        # --- Campaign sheet ---
        camp_code = None
        camp_name = None
        camp_area = None
        case_study = None
        n_stations = None
        if "Campaign" in xl.sheet_names:
            try:
                cdf = pd.read_excel(path, sheet_name="Campaign")
                cdf = cdf.dropna(how="all")
                if len(cdf):
                    row = cdf.iloc[0]
                    def _g(*keys):
                        for k in keys:
                            if k in cdf.columns:
                                v = row.get(k)
                                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                                    return v
                        return None
                    camp_code  = _g("Campaign Code")
                    camp_name  = _g("Campaign Name")
                    camp_area  = _g("Area of Study")
                    case_study = _g("Case study*", "Case study")
                    n_stations = _g("Number of Stations")
            except Exception as e:
                print(f"  [WARN] Cannot read 'Campaign' sheet in {fname}: {e}")

        if camp_code is None or (isinstance(camp_code, float) and math.isnan(camp_code)):
            camp_code = stem
        camp_code = str(camp_code).strip()
        if camp_code in seen_codes:
            print(f"  [WARN] Duplicate Campaign Code '{camp_code}' in {fname}; "
                  f"appending file stem to disambiguate")
            camp_code = f"{camp_code} ({stem})"
        seen_codes[camp_code] = path

        if camp_name is None:
            camp_name = camp_area or stem
        if camp_area is None:
            camp_area = camp_name

        # --- Sample sheet (bbox + dates) ---
        if "Sample" not in xl.sheet_names:
            print(f"  [WARN] No 'Sample' sheet in {fname} - skipping")
            continue
        try:
            sdf = pd.read_excel(path, sheet_name="Sample")
        except Exception as e:
            print(f"  [WARN] Cannot read 'Sample' sheet in {fname}: {e}")
            continue

        lat_col = next((c for c in sdf.columns if str(c).strip().lower() == "latitude"), None)
        lon_col = next((c for c in sdf.columns if str(c).strip().lower() == "longitude"), None)
        date_col = next((c for c in sdf.columns
                         if str(c).strip().lower().startswith("date of sampling start")), None)
        dur_col = next((c for c in sdf.columns
                        if str(c).strip().lower().startswith("sampling duration - days")), None)

        if lat_col is None or lon_col is None or date_col is None:
            print(f"  [WARN] {fname}: missing Latitude/Longitude/Date column - skipping")
            continue

        lats = [_parse_coord(v) for v in sdf[lat_col]]
        lons = [_parse_coord(v) for v in sdf[lon_col]]
        valid = [(la, lo) for la, lo in zip(lats, lons)
                 if la is not None and lo is not None
                 and -90 <= la <= 90 and -180 <= lo <= 180]
        if not valid:
            print(f"  [WARN] {fname}: no valid coordinates in Sample sheet - skipping")
            continue
        lat_vals = [la for la, _ in valid]
        lon_vals = [lo for _, lo in valid]
        lat_min, lat_max = min(lat_vals), max(lat_vals)
        lon_min, lon_max = min(lon_vals), max(lon_vals)
        if lat_min == lat_max:
            lat_min -= 0.05; lat_max += 0.05
        if lon_min == lon_max:
            lon_min -= 0.05; lon_max += 0.05

        # Dates: combine sampling start + duration to widen window
        starts: list[str] = []
        ends: list[str] = []
        dur_series = sdf[dur_col] if dur_col else None
        for i, dval in enumerate(sdf[date_col]):
            ds = _date_str(dval)
            if not ds:
                continue
            starts.append(ds)
            dur_days = 0
            if dur_series is not None:
                dv = dur_series.iloc[i] if i < len(dur_series) else None
                try:
                    if dv is not None and not (isinstance(dv, float) and math.isnan(dv)):
                        dur_days = int(float(dv))
                except (TypeError, ValueError):
                    dur_days = 0
            try:
                end_dt = _dt.datetime.strptime(ds, "%Y-%m-%d") + _dt.timedelta(days=max(dur_days, 0))
                ends.append(end_dt.strftime("%Y-%m-%d"))
            except ValueError:
                ends.append(ds)
        date_min = min(starts) if starts else None
        date_max = max(ends) if ends else None

        clat = (lat_min + lat_max) / 2
        clon = (lon_min + lon_max) / 2
        campaigns.append({
            "name":          camp_code,           # primary key (= Campaign Code)
            "campaign_code": camp_code,
            "campaign_name": str(camp_name) if camp_name is not None else camp_code,
            "area":          str(camp_area) if camp_area is not None else camp_code,
            "case_study":    str(case_study) if case_study is not None else "",
            "n_stations":    n_stations,
            "source_file":   fname,
            "lat_min":  lat_min, "lat_max": lat_max,
            "lon_min":  lon_min, "lon_max": lon_max,
            "clat":     clat,    "clon":    clon,
            "date_min": date_min,
            "date_max": date_max,
        })

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
