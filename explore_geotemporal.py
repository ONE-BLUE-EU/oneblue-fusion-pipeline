"""
Geotemporal exploration of all filled-in sample collection templates.
Produces GEOTEMPORAL_OVERVIEW.xlsx with:
  - Datasets     : one row per campaign, with bounding box, date range, sample counts
  - Stations     : one row per unique station × dataset
  - Env Coverage : matrix of in-situ environmental parameter counts per dataset
  - Matrices     : matrix of sample matrix type counts per dataset
"""

import os
import re
import datetime
from pathlib import Path
import openpyxl
import pandas as pd
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent
SAMPLES_DIR = str(_ROOT / "SAMPLES")
OUTPUT_FILE = str(_ROOT / "GEOTEMPORAL_OVERVIEW.xlsx")

# File to skip (blank template)
SKIP_FILES = {"Sample_Collection_Template_v0.11.xlsx"}

# Columns we care about in the Sample sheet (normalised key → display label)
KEY_COLS = {
    "sample code":                  "sample_code",
    "date of sampling start":       "date",
    "name of country":              "country",
    "institution code":             "institution",
    "station label":                "station",
    "name of sea":                  "sea",
    "name of area":                 "area",
    "latitude":                     "lat",
    "longitude":                    "lon",
    "depth (m)":                    "depth",
    "sample matrix":                "matrix",
}

ENV_COLS = {
    "ph":                                           "pH",
    "temperature (c)":                              "Temperature (°C)",
    "salinity (psu)":                               "Salinity (PSU)",
    "conductivity (ms/cm)":                         "Conductivity (mS/cm)",
    "dissolved oxygen concentration (μmol/kg)":     "Dissolved O₂ (μmol/kg)",
    "turbidity (ftu)":                              "Turbidity (FTU)",
    "fluorescence":                                 "Fluorescence",
    "downward par (me/m˄2/s)":                      "Downward PAR",
    "phosphate (μmol/l)":                           "Phosphate (μmol/L)",
    "silicate (μmol/l)":                            "Silicate (μmol/L)",
    "ammonium (μmol/l)":                            "Ammonium (μmol/L)",
    "nitrate (μmol/l)":                             "Nitrate (μmol/L)",
    "nitrite (μmol/l)":                             "Nitrite (μmol/L)",
    "pigment concentrations (mg/m˄3)":              "Pigments (mg/m³)",
    "picoplankton - flow cytometry (m˄3)":          "Picoplankton",
    "nano/microplankton  (m˄3)":                    "Nano/Microplankton",
    "primary production - isotope uptake (mg/m˄3/d)": "Primary Production (isotope)",
    "bacterial production, isotope uptake (mg/m˄3/d)": "Bacterial Production",
}


def normalise_col(name):
    """Strip asterisks, extra spaces; lowercase for matching."""
    if name is None:
        return ""
    return re.sub(r"\s+", " ", str(name).replace("*", "")).strip().lower()


def parse_date(val):
    """Return a date object or None from various possible date representations."""
    if val is None:
        return None
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val if isinstance(val, datetime.date) else val.date()
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%B %Y", "%b %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # Try extracting year+month from free text like "September 2024"
    m = re.search(r"(\w+ \d{4})", s)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), "%B %Y").date()
        except ValueError:
            pass
    return None


def parse_latlon(v):
    """Parse a latitude or longitude value that may be decimal or DDM format.
    Handles:
      43.7638          → decimal degrees (float or string)
      "43 45.83 N"     → DDM space-separated
      "42°23.6115'N"   → DDM with degree/tick symbols
    Returns float or None.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    # DDM pattern: digits, then optional °, space or ' separators, then minutes, then N/S/E/W
    m = re.match(
        r"(\d+)[°\s]+(\d+(?:\.\d+)?)['\s]*([NSEWnsew])", s
    )
    if m:
        deg = float(m.group(1))
        mins = float(m.group(2))
        hemi = m.group(3).upper()
        dd = deg + mins / 60.0
        if hemi in ("S", "W"):
            dd = -dd
        return round(dd, 6)
    return None


def is_real_value(v):
    """True if v is a non-None, non-empty, non-dash value."""
    if v is None:
        return False
    s = str(v).strip()
    return s not in ("", "-", "n/a", "na", "N/A")


def read_campaign(wb):
    """Return dict with campaign metadata."""
    meta = {}
    if "Campaign" not in wb.sheetnames:
        return meta
    ws = wb["Campaign"]
    rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
    if len(rows) < 2:
        return meta
    hdr = [normalise_col(h) for h in rows[0]]
    data = rows[1]
    field_map = {
        "campaign code":            "campaign_code",
        "campaign name":            "campaign_name",
        "campaign start":           "campaign_start_raw",
        "case study":               "case_study",
        "area of study":            "area_of_study",
        "responsible institution":  "institution",
        "research vessel":          "vessel",
        "number of stations":       "n_stations_declared",
    }
    for col_norm, key in field_map.items():
        for i, h in enumerate(hdr):
            if h == col_norm and i < len(data):
                meta[key] = data[i]
                break
    meta["campaign_start"] = parse_date(meta.get("campaign_start_raw"))
    return meta


def read_samples(wb, fname):
    """Return list of dicts, one per sample row."""
    if "Sample" not in wb.sheetnames:
        return []
    ws = wb["Sample"]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # Header row (first non-empty row)
    hdr_idx = 0
    for i, r in enumerate(rows):
        if any(c is not None for c in r):
            hdr_idx = i
            break
    hdr_norm = [normalise_col(h) for h in rows[hdr_idx]]

    # Build column-index maps
    key_idx = {}
    for col_norm, field in KEY_COLS.items():
        for i, h in enumerate(hdr_norm):
            if h == col_norm:
                key_idx[field] = i
                break

    env_idx = {}
    for col_norm, label in ENV_COLS.items():
        for i, h in enumerate(hdr_norm):
            if h == col_norm:
                env_idx[label] = i
                break

    records = []
    for row in rows[hdr_idx + 1:]:
        if not any(c is not None for c in row):
            continue

        rec = {"_file": fname}
        for field, idx in key_idx.items():
            rec[field] = row[idx] if idx < len(row) else None

        rec["date"] = parse_date(rec.get("date"))

        # Clean lat/lon (handles decimal degrees and DDM formats)
        for coord in ("lat", "lon"):
            rec[coord] = parse_latlon(rec.get(coord))

        # Clean station label (strip newlines)
        if rec.get("station"):
            rec["station"] = str(rec["station"]).split("\n")[0].strip()

        # Env parameters present in this row
        env_present = []
        for label, idx in env_idx.items():
            val = row[idx] if idx < len(row) else None
            if is_real_value(val):
                env_present.append(label)
        rec["env_params"] = env_present

        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    files = sorted(
        f for f in os.listdir(SAMPLES_DIR)
        if f.endswith(".xlsx") and f not in SKIP_FILES
    )

    all_records = []
    all_campaign_meta = {}

    for fname in files:
        path = os.path.join(SAMPLES_DIR, fname)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        meta = read_campaign(wb)
        meta["_file"] = fname
        recs = read_samples(wb, fname)
        wb.close()

        all_campaign_meta[fname] = meta
        for r in recs:
            r.update({k: v for k, v in meta.items() if not k.startswith("_")})
        all_records.extend(recs)
        print(f"  {fname}: {len(recs)} sample rows")

    df = pd.DataFrame(all_records)

    # -----------------------------------------------------------------------
    # 1. Datasets summary
    # -----------------------------------------------------------------------
    dataset_rows = []
    for fname, meta in all_campaign_meta.items():
        sub = df[df["_file"] == fname]
        valid_dates = sub["date"].dropna()
        valid_lats  = sub["lat"].dropna()
        valid_lons  = sub["lon"].dropna()

        # Unique env params across all samples in this dataset
        all_env = set()
        for ep in sub["env_params"]:
            all_env.update(ep)

        # Count samples that have at least one env param
        n_with_env = sum(1 for ep in sub["env_params"] if len(ep) > 0)

        dataset_rows.append({
            "File":                 fname,
            "Campaign Code":        meta.get("campaign_code", ""),
            "Campaign Name":        meta.get("campaign_name", ""),
            "Institution":          meta.get("institution", ""),
            "Case Study":           meta.get("case_study", ""),
            "Area of Study":        meta.get("area_of_study", ""),
            "Vessel":               meta.get("vessel", ""),
            "Campaign Start":       meta.get("campaign_start"),
            "Sampling Date Min":    valid_dates.min() if len(valid_dates) > 0 else None,
            "Sampling Date Max":    valid_dates.max() if len(valid_dates) > 0 else None,
            "Lat Min":              round(valid_lats.min(), 5) if len(valid_lats) > 0 else None,
            "Lat Max":              round(valid_lats.max(), 5) if len(valid_lats) > 0 else None,
            "Lon Min":              round(valid_lons.min(), 5) if len(valid_lons) > 0 else None,
            "Lon Max":              round(valid_lons.max(), 5) if len(valid_lons) > 0 else None,
            "N Samples":            len(sub),
            "N Stations (unique)":  sub["station"].nunique(),
            "N Matrices (unique)":  sub["matrix"].nunique(),
            "N Samples w/ Env Data":n_with_env,
            "Env Parameters":       "; ".join(sorted(all_env)) if all_env else "—",
        })

    df_datasets = pd.DataFrame(dataset_rows)

    # -----------------------------------------------------------------------
    # 2. Stations
    # -----------------------------------------------------------------------
    station_rows = []
    for (fname, station), grp in df.groupby(["_file", "station"]):
        meta = all_campaign_meta[fname]
        valid_dates = grp["date"].dropna()
        all_env = set()
        for ep in grp["env_params"]:
            all_env.update(ep)
        lat_vals = grp["lat"].dropna()
        lon_vals = grp["lon"].dropna()
        depth_vals = grp["depth"].dropna()

        station_rows.append({
            "Campaign Code":        meta.get("campaign_code", ""),
            "Campaign Name":        meta.get("campaign_name", ""),
            "Institution":          meta.get("institution", ""),
            "Case Study":           meta.get("case_study", ""),
            "Station":              station,
            "Sea":                  grp["sea"].dropna().mode()[0] if len(grp["sea"].dropna()) > 0 else "",
            "Area":                 grp["area"].dropna().mode()[0] if len(grp["area"].dropna()) > 0 else "",
            "Country":              grp["country"].dropna().mode()[0] if len(grp["country"].dropna()) > 0 else "",
            "Latitude":             lat_vals.mean() if len(lat_vals) > 0 else None,
            "Longitude":            lon_vals.mean() if len(lon_vals) > 0 else None,
            "Date First":           valid_dates.min() if len(valid_dates) > 0 else None,
            "Date Last":            valid_dates.max() if len(valid_dates) > 0 else None,
            "Depth Min (m)":        depth_vals.min() if len(depth_vals) > 0 else None,
            "Depth Max (m)":        depth_vals.max() if len(depth_vals) > 0 else None,
            "N Samples":            len(grp),
            "Env Parameters":       "; ".join(sorted(all_env)) if all_env else "—",
        })

    df_stations = pd.DataFrame(station_rows).sort_values(
        ["Case Study", "Campaign Code", "Station"]
    )

    # -----------------------------------------------------------------------
    # 3. Environmental parameter coverage matrix (counts)
    # -----------------------------------------------------------------------
    env_labels = list(ENV_COLS.values())
    env_matrix_rows = []
    for fname, meta in all_campaign_meta.items():
        sub = df[df["_file"] == fname]
        row = {
            "Campaign Code": meta.get("campaign_code", fname),
            "Campaign Name": meta.get("campaign_name", ""),
        }
        for label in env_labels:
            count = sum(1 for ep in sub["env_params"] if label in ep)
            row[label] = count if count > 0 else ""
        env_matrix_rows.append(row)

    df_env = pd.DataFrame(env_matrix_rows)

    # -----------------------------------------------------------------------
    # 4. Sample matrix counts
    # -----------------------------------------------------------------------
    all_matrices = sorted(df["matrix"].dropna().unique())
    matrix_rows = []
    for fname, meta in all_campaign_meta.items():
        sub = df[df["_file"] == fname]
        row = {
            "Campaign Code": meta.get("campaign_code", fname),
            "Campaign Name": meta.get("campaign_name", ""),
        }
        vc = sub["matrix"].value_counts()
        for mat in all_matrices:
            row[mat] = int(vc.get(mat, 0)) or ""
        matrix_rows.append(row)

    df_matrices = pd.DataFrame(matrix_rows)

    # -----------------------------------------------------------------------
    # Write Excel output
    # -----------------------------------------------------------------------
    HEADER_FILL = PatternFill("solid", fgColor="2E75B6")
    HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
    ALT_FILL    = PatternFill("solid", fgColor="DEEAF1")
    THIN        = Side(style="thin", color="B8CCE4")
    BORDER      = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    def style_sheet(ws, freeze="B2"):
        # Header row
        for cell in ws[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = BORDER
        ws.row_dimensions[1].height = 36

        # Data rows — alternate shading
        for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
            fill = ALT_FILL if i % 2 == 0 else PatternFill()
            for cell in row:
                cell.fill = fill
                cell.border = BORDER
                cell.alignment = Alignment(vertical="top", wrap_text=False)

        # Auto-width (capped at 60)
        for col in ws.columns:
            max_len = max(
                (len(str(cell.value)) if cell.value is not None else 0 for cell in col),
                default=10,
            )
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 2, 60)

        if freeze:
            ws.freeze_panes = freeze

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        df_datasets.to_excel(writer, sheet_name="Datasets", index=False)
        df_stations.to_excel(writer, sheet_name="Stations", index=False)
        df_env.to_excel(writer, sheet_name="Env Coverage", index=False)
        df_matrices.to_excel(writer, sheet_name="Sample Matrices", index=False)

    wb_out = openpyxl.load_workbook(OUTPUT_FILE)
    for sname in wb_out.sheetnames:
        style_sheet(wb_out[sname])
    wb_out.save(OUTPUT_FILE)

    print(f"\nSaved → {OUTPUT_FILE}")
    print(f"  Datasets      : {len(df_datasets)} campaigns")
    print(f"  Stations      : {len(df_stations)} unique stations")
    print(f"  Total samples : {len(df)}")


if __name__ == "__main__":
    main()
