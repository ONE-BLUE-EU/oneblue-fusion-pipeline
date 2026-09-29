"""Normalize Sample sheets into samples.csv and samples.audit.json.

Keep every identified sample row, including records without usable geometry.
Point coordinates and spatial interval bounds are separate. Raw source values,
Excel row references and quality flags preserve provenance. Date-only query
bounds do not assign a timezone to the raw sampling timestamps.

Durations count inclusive calendar days; mixed hours fields are audit-only.
The audit fingerprints both the input workbooks and the normalized CSV.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

import pandas as pd

# Re-use coord/date parsers already tested in the pipeline
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from query_external_data import _parse_coord_extent, _date_str, SAMPLES_FOLDER  # noqa: E402

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples.csv")

OUTPUT_COLS = [
    "campaign_id",
    "sample_id",
    "campaign_code",
    "campaign_area",
    "source_file",
    "source_sheet",
    "source_row",
    "sample_code",
    "station_label",
    "sea_name",
    "area_name",
    "sample_matrix",
    "latitude",
    "longitude",
    "latitude_min",
    "latitude_max",
    "longitude_min",
    "longitude_max",
    "collection_date",
    "collection_end",
    "collection_time_raw",
    "duration_days_raw",
    "duration_hours_raw",
    "duration_mode",
    "depth_m",
    "latitude_raw",
    "longitude_raw",
    "spatial_kind",
    "quality_flags",
]

SCHEMA_VERSION = 4


def input_fingerprints(folder):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(folder).glob("*.xlsx"))
        if not path.name.startswith(("~$", "SEEMS OLD"))
    }


def _text(value):
    return "" if value is None or pd.isna(value) else str(value).strip()


def _identifier(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()[:24]


def _first_col(df: pd.DataFrame, *patterns: str):
    """Return the first column name whose stripped-lower value starts with any pattern."""
    for col in df.columns:
        cl = str(col).strip().lower()
        for p in patterns:
            if cl.startswith(p.lower()):
                return col
    return None


def read_samples(folder=SAMPLES_FOLDER, duration_mode="inclusive"):
    if duration_mode not in (None, "elapsed", "inclusive"):
        raise ValueError("duration_mode must be elapsed, inclusive or None")
    rows: list[dict] = []
    audit = []
    for fname in input_fingerprints(folder):
        path = Path(folder) / fname
        report = {"source_file": fname, "sample_rows": 0, "status": "ok", "issues": []}
        audit.append(report)
        try:
            with pd.ExcelFile(path) as workbook:
                sdf = pd.read_excel(workbook, sheet_name="Sample", dtype=object, keep_default_na=False)
                cdf = (pd.read_excel(workbook, sheet_name="Campaign", dtype=object, keep_default_na=False).dropna(how="all")
                       if "Campaign" in workbook.sheet_names else pd.DataFrame())
        except Exception as error:
            report.update(status="error", error=str(error))
            continue
        def metadata(pattern, fallback):
            column = _first_col(cdf, pattern)
            return (_text(cdf.iloc[0][column]) if column and len(cdf) else "") or fallback

        camp_code = metadata("campaign code", path.stem)
        camp_area = metadata("area of study", camp_code)
        campaign_id = _identifier("campaign", fname)
        columns = {key: _first_col(sdf, pattern) for key, pattern in {
            "sample_code": "sample code", "station_label": "station label",
            "sea_name": "name of sea", "area_name": "name of area",
            "sample_matrix": "sample matrix", "latitude": "latitude",
            "longitude": "longitude", "date": "date of sampling start",
            "days": "sampling duration - days", "hours": "sampling duration - hours",
            "depth": "depth",
        }.items()}
        report["missing_columns"] = [key for key in ("latitude", "longitude", "date")
                                     if columns[key] is None]
        for index, source in sdf.iterrows():
            def value(key):
                return source[columns[key]] if columns[key] is not None else None

            if not any(_text(value(key)) for key in
                       ("sample_code", "station_label", "latitude", "longitude", "date")):
                continue
            source_row = int(index) + 2
            flags = []
            record = {
                "campaign_id": campaign_id, "sample_id": _identifier(campaign_id, source_row),
                "campaign_code": camp_code, "campaign_area": camp_area,
                "source_file": fname, "source_sheet": "Sample", "source_row": source_row,
                **{key: _text(value(key)) for key in
                   ("sample_code", "station_label", "sea_name", "area_name", "sample_matrix")},
                "collection_time_raw": _text(value("date")),
                "duration_days_raw": _text(value("days")),
                "duration_hours_raw": _text(value("hours")),
                "duration_mode": duration_mode or "unconfirmed",
            }
            for axis, limit in (("latitude", 90), ("longitude", 180)):
                raw = value(axis)
                extent = _parse_coord_extent(raw)
                hemisphere = _text(raw).upper().rstrip()[-1:]
                if (axis == "latitude" and hemisphere in ("E", "W", "O")
                        or axis == "longitude" and hemisphere in ("N", "S")):
                    extent = None
                if extent and not (-limit <= extent[0] <= extent[1] <= limit):
                    extent = None
                record[axis + "_raw"] = _text(raw)
                record[axis + "_min"] = extent[0] if extent else None
                record[axis + "_max"] = extent[1] if extent else None
                record[axis] = extent[0] if extent and extent[0] == extent[1] else None
                if not extent:
                    flags.append(("invalid_" if _text(raw) else "missing_") + axis)
            has_extent = all(record[axis + "_min"] is not None for axis in ("latitude", "longitude"))
            point = has_extent and all(record[axis] is not None for axis in ("latitude", "longitude"))
            record["spatial_kind"] = "point" if point else "extent" if has_extent else "missing"
            if not point:
                record["latitude"] = record["longitude"] = None
            record["collection_date"] = _date_str(value("date"))
            record["collection_end"] = record["collection_date"]
            if not record["collection_date"]:
                flags.append("missing_or_invalid_date")
            if _text(value("days")):
                try:
                    days = float(value("days"))
                    if not math.isfinite(days) or days < 0:
                        raise ValueError("invalid duration")
                    if days and duration_mode is None:
                        flags.append("duration_semantics_unconfirmed")
                        record["collection_end"] = None
                    elif record["collection_date"]:
                        elapsed = days if duration_mode != "inclusive" else max(0, days - 1)
                        record["collection_end"] = (pd.Timestamp(record["collection_date"])
                                                    + pd.Timedelta(days=elapsed)).date().isoformat()
                except (ValueError, TypeError, OverflowError):
                    flags.append("invalid_duration_days")
                    record["collection_end"] = None
            if _text(value("hours")):
                flags.append("hours_ignored_day_resolution")
            depth = pd.to_numeric(value("depth"), errors="coerce")
            record["depth_m"] = float(depth) if pd.notna(depth) and math.isfinite(depth) and depth >= 0 else None
            if _text(value("depth")) and record["depth_m"] is None:
                flags.append("invalid_depth")
            record["quality_flags"] = "|".join(flags)
            rows.append(record)
            if flags:
                report["issues"].append({"source_row": source_row, "flags": flags})
            report["sample_rows"] += 1
        if report["issues"] or report["missing_columns"]:
            report["status"] = "needs_review"
    frame = pd.DataFrame(rows, columns=OUTPUT_COLS)
    if not frame.empty:
        duplicated = frame.groupby("campaign_code")["campaign_id"].nunique()
        for report in audit:
            group = frame[frame["source_file"] == report["source_file"]]
            if group.empty:
                continue
            report["campaign_id"] = group["campaign_id"].iloc[0]
            report["campaign_code"] = group["campaign_code"].iloc[0]
            report["duplicate_campaign_code"] = bool(duplicated[report["campaign_code"]] > 1)
            report["spatial_counts"] = group["spatial_kind"].value_counts().to_dict()
            report["bounds"] = {key: (float(group[key].min() if key.endswith("_min") else group[key].max())
                                      if group[key].notna().any() else None)
                                for key in ("latitude_min", "latitude_max", "longitude_min", "longitude_max")}
            report["date_min"] = min(group["collection_date"].dropna(), default=None)
            report["date_max"] = max(group["collection_end"].dropna(), default=None)
            report["extrema"] = {}
            for column in ("latitude_min", "latitude_max", "longitude_min", "longitude_max", "collection_date"):
                values = group[column].dropna()
                if values.empty:
                    continue
                targets = (values.min(), values.max()) if column == "collection_date" else (
                    values.min() if column.endswith("_min") else values.max(),)
                for target in targets:
                    selected = group[group[column] == target]
                    raw_column = "collection_time_raw" if column == "collection_date" else column.split("_")[0] + "_raw"
                    report["extrema"][f"{column}:{target}"] = {
                        "rows": selected["source_row"].astype(int).tolist(),
                        "raw_values": selected[raw_column].unique().tolist(),
                    }
            if report["duplicate_campaign_code"]:
                report["status"] = "needs_review"
    return frame, audit


def build_samples_csv(folder=SAMPLES_FOLDER, output=OUTPUT, duration_mode="inclusive") -> pd.DataFrame:
    frame, audit = read_samples(folder, duration_mode)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".csv.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(output)
    manifest = {"schema_version": SCHEMA_VERSION, "duration_mode": duration_mode,
                "hours_policy": "ignore_at_day_resolution", "suspect_value_policy": "retain_literal",
                "inputs": input_fingerprints(folder), "workbooks": audit,
                "csv_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    audit_path = output.with_suffix(".audit.json")
    temporary_audit = audit_path.with_suffix(".json.tmp")
    temporary_audit.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    temporary_audit.replace(audit_path)
    print(f"Wrote {len(frame):,} sample rows from {len(audit)} workbooks to {output}")
    print(f"Audit: {audit_path}")
    return frame


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Normalize Sample sheets without discarding invalid rows.")
    parser.add_argument("--samples", default=SAMPLES_FOLDER)
    parser.add_argument("--out", default=OUTPUT)
    parser.add_argument("--duration-mode", choices=("elapsed", "inclusive"), default="inclusive")
    args = parser.parse_args()
    build_samples_csv(args.samples, args.out, args.duration_mode)
