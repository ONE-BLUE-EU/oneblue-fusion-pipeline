"""
build_samples_csv.py

Creates the master samples.csv file for the ONE-BLUE pipeline.

Reads every .xlsx in the SAMPLES/ folder (same files consumed by
load_campaigns_from_samples), normalises coordinates to decimal degrees
and dates to YYYY-MM-DD ISO strings, and writes a single flat CSV that
becomes the authoritative starting point for all downstream scripts:

    build_campaign_datasources.py  – uses it via load_campaigns_from_samples()
    fetch_climate.py               – same
    fetch_gbif_occurrences.py      – same
    fetch_msfd.py                  – same
    fetch_raster_manifest.py       – same
    build_findings_report.py       – loads it directly for map figures

Output: samples.csv (workspace root)

Columns
-------
    campaign_code      – from Campaign sheet; file stem used as fallback
    campaign_area      – "Area of Study" from Campaign sheet
    source_file        – original filename
    sample_code        – Sample Code column
    station_label      – Station Label column
    sea_name           – Name of sea column
    area_name          – Name of area column
    latitude           – decimal degrees (float), north positive
    longitude          – decimal degrees (float), east positive
    collection_date    – YYYY-MM-DD string
    depth_m            – Depth (m) if present, else blank

Usage
-----
    python build_samples_csv.py
"""

from __future__ import annotations

import glob
import math
import os
import sys

import pandas as pd

# Re-use coord/date parsers already tested in the pipeline
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from query_external_data import _parse_coord, _date_str, SAMPLES_FOLDER  # noqa: E402

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples.csv")

OUTPUT_COLS = [
    "campaign_code",
    "campaign_area",
    "source_file",
    "sample_code",
    "station_label",
    "sea_name",
    "area_name",
    "latitude",
    "longitude",
    "collection_date",
    "depth_m",
]


def _first_col(df: pd.DataFrame, *patterns: str):
    """Return the first column name whose stripped-lower value starts with any pattern."""
    for col in df.columns:
        cl = str(col).strip().lower()
        for p in patterns:
            if cl.startswith(p.lower()):
                return col
    return None


def build_samples_csv() -> pd.DataFrame:
    rows: list[dict] = []
    seen_codes: dict[str, str] = {}  # campaign_code → first source_file

    for path in sorted(glob.glob(os.path.join(SAMPLES_FOLDER, "*.xlsx"))):
        fname = os.path.basename(path)
        if fname.startswith("SEEMS OLD"):
            continue
        stem = os.path.splitext(fname)[0]
        print(f"  {fname} …", end=" ", flush=True)

        try:
            xl = pd.ExcelFile(path)
        except Exception as e:
            print(f"SKIP (cannot open): {e}")
            continue

        # ── Campaign sheet ────────────────────────────────────────────────
        camp_code = None
        camp_area = None
        if "Campaign" in xl.sheet_names:
            try:
                cdf = pd.read_excel(path, sheet_name="Campaign", dtype=str)
                cdf = cdf.dropna(how="all")
                if len(cdf):
                    row0 = cdf.iloc[0]
                    code_col = _first_col(cdf, "campaign code")
                    area_col = _first_col(cdf, "area of study")
                    if code_col and str(row0[code_col]).strip() not in ("nan", ""):
                        camp_code = str(row0[code_col]).strip()
                    if area_col and str(row0[area_col]).strip() not in ("nan", ""):
                        camp_area = str(row0[area_col]).strip()
            except Exception as e:
                print(f"[WARN Campaign sheet: {e}]", end=" ")

        if camp_code is None:
            camp_code = stem
        if camp_area is None:
            camp_area = camp_code

        # Disambiguate duplicate campaign codes exactly as load_campaigns_from_samples does
        if camp_code in seen_codes:
            print(f"[WARN: duplicate code '{camp_code}' — appending stem]", end=" ")
            camp_code = f"{camp_code} ({stem})"
        seen_codes[camp_code] = fname

        # ── Sample sheet ──────────────────────────────────────────────────
        if "Sample" not in xl.sheet_names:
            print("SKIP (no Sample sheet)")
            continue
        try:
            sdf = pd.read_excel(path, sheet_name="Sample")
        except Exception as e:
            print(f"SKIP (cannot read Sample sheet): {e}")
            continue

        lat_col  = _first_col(sdf, "latitude")
        lon_col  = _first_col(sdf, "longitude")
        date_col = _first_col(sdf, "date of sampling start")
        if lat_col is None or lon_col is None or date_col is None:
            print(f"SKIP (missing lat/lon/date columns)")
            continue

        sc_col    = _first_col(sdf, "sample code")
        sl_col    = _first_col(sdf, "station label")
        sea_col   = _first_col(sdf, "name of sea")
        area_col2 = _first_col(sdf, "name of area")
        dep_col   = _first_col(sdf, "depth")

        n_valid = 0
        for i, sr in sdf.iterrows():
            lat = _parse_coord(sr[lat_col])
            lon = _parse_coord(sr[lon_col])
            if lat is None or lon is None:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            cdate = _date_str(sr[date_col])

            def _str_or_blank(col):
                if col is None:
                    return ""
                v = sr[col]
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return ""
                return str(v).strip()

            depth_val = ""
            if dep_col is not None:
                dv = sr[dep_col]
                try:
                    if dv is not None and not (isinstance(dv, float) and math.isnan(dv)):
                        depth_val = float(dv)
                except (TypeError, ValueError):
                    depth_val = ""

            rows.append({
                "campaign_code":  camp_code,
                "campaign_area":  camp_area,
                "source_file":    fname,
                "sample_code":    _str_or_blank(sc_col),
                "station_label":  _str_or_blank(sl_col),
                "sea_name":       _str_or_blank(sea_col),
                "area_name":      _str_or_blank(area_col2),
                "latitude":       round(lat, 6),
                "longitude":      round(lon, 6),
                "collection_date": cdate or "",
                "depth_m":        depth_val,
            })
            n_valid += 1

        print(f"{n_valid} stations")

    df = pd.DataFrame(rows, columns=OUTPUT_COLS)
    df.to_csv(OUTPUT, index=False)
    print(f"\nWrote {len(df):,} rows → {OUTPUT}")
    return df


if __name__ == "__main__":
    build_samples_csv()
