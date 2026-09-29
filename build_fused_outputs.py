"""Publish analytical Parquet and static Leaflet bundles from pipeline CSVs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import wkt
from shapely.geometry import Point, box, mapping

from query_external_data import load_campaigns_from_samples

ROOT = Path(__file__).parent
DEFAULT_SOURCE = ROOT / "dkan_resources"
DEFAULT_OUTPUT = ROOT / "output" / "fused"
SCHEMA_VERSION = "1.0.0"
MAP_CHUNK_SIZE = 5000
MAP_CHUNK_BYTES = 2 * 1024 * 1024
MATCH_DISTANCE_KM = 25.0
MATCH_TIME_DAYS = 7.0
MATCH_DEPTH_M = 10.0

CATEGORY_FILES = {
    "oceanography": "oceanography.csv",
    "chemistry": "chemistry.csv",
    "human_activities": "human_activities.csv",
    "bathymetry": "bathymetry.csv",
    "biology_obis": "biology.csv",
    "biology_gbif": "biology_gbif.csv",
    "climate": "climate.csv",
    "msfd_regions": "msfd_regions.csv",
}

COMPLETENESS = {
    "oceanography": ("partial", "Argo QC-filtered standard-pressure bands and metadata-discovered EMSO observations. EMSO responses above 50 MiB are skipped; Copernicus downloads are optional. Row counts do not imply complete coverage."),
    "chemistry": ("partial", "EMODnet plus optional ICES DOME and explicitly selected EMPODAT substances. No historical fallback. Qualifiers, matrix and provider quality flags must be checked; censored values are not detections. See chemistry_coverage.json when present."),
    "human_activities": ("partial", "Legacy WFS requests used a 2,000-feature layer cap and some layers failed."),
    "bathymetry": ("sampled", "GEBCO values are a 5x5 campaign grid, not a complete raster."),
    "biology_obis": ("complete_api_count", "Spatial tiling retrieved the API-advertised campaign totals."),
    "biology_gbif": ("partial", "Campaign retrieval stops near 5,000 unfiltered GBIF results."),
    "climate": ("modelled_centroid", "ERA5 daily values are evaluated at campaign centroids."),
    "msfd_regions": ("complete_layer", "No Arctic association is expected for the European MSFD layer."),
    "assets": ("manifest", "Links describe external raster/services; raster bytes are not bundled."),
}

MAP_PROPERTIES = [
    "record_id", "source", "data_type", "dataset_id", "feature_id", "time",
    "depth_m", "temporal_scope", "perturbation_class", "scientific_name",
    "compound_name", "parameter_name", "parameter_value", "parameter_unit",
    "layer_label", "name", "region_name", "subregion_name", "elevation_m",
    "temperature_c", "salinity_psu", "oxygen_umol_kg",
    "ph", "chemical_group", "suspect_chem_group", "suspect_chem_subgroup",
    "suspect_norman_id", "suspect_match_status", "classification_source",
    "matrix", "matrix_detail", "measurement_role", "measurement_basis",
    "concentration_qualifier", "parameter_reported_value", "detection_limit", "quantification_limit",
    "provider_quality_flag", "provider_quality_status",
]


def _slug(value: str) -> str:
    text = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-").lower()[:80] or "campaign"


def _stable_id(*parts) -> str:
    payload = json.dumps(parts, ensure_ascii=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _native(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _geometry_from_row(row):
    text = row.get("geom_wkt")
    if isinstance(text, str) and text.strip():
        try:
            geometry = wkt.loads(text)
            return geometry if not geometry.is_empty else None
        except Exception:
            return None
    lat, lon = row.get("lat"), row.get("lon")
    if pd.notna(lat) and pd.notna(lon):
        return Point(float(lon), float(lat))
    return None


def _write_geoparquet(frame: pd.DataFrame, path: Path, geometry_wkb: pd.Series | None = None) -> None:
    data = frame.copy()
    if geometry_wkb is not None:
        data["geometry"] = geometry_wkb
    table = pa.Table.from_pandas(data, preserve_index=False)
    if geometry_wkb is not None:
        metadata = dict(table.schema.metadata or {})
        metadata[b"geo"] = json.dumps({
            "version": "1.1.0",
            "primary_column": "geometry",
            "columns": {"geometry": {"encoding": "WKB", "geometry_types": [], "crs": None}},
        }, separators=(",", ":")).encode()
        table = table.replace_schema_metadata(metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def _write_geojson(features: list[dict], path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_native), encoding="utf-8")
    return {
        "path": path.as_posix(), "features": len(features), "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _sample_features(samples: pd.DataFrame) -> list[dict]:
    features = []
    for row in samples.to_dict("records"):
        if row.get("spatial_kind") == "point" and pd.notna(row.get("latitude")) and pd.notna(row.get("longitude")):
            geometry = Point(float(row["longitude"]), float(row["latitude"]))
        elif row.get("spatial_kind") == "extent" and all(pd.notna(row.get(key)) for key in (
                "longitude_min", "latitude_min", "longitude_max", "latitude_max")):
            geometry = box(float(row["longitude_min"]), float(row["latitude_min"]),
                           float(row["longitude_max"]), float(row["latitude_max"]))
        else:
            continue
        properties = {key: _native(row.get(key)) for key in (
            "sample_id", "campaign_id", "campaign_code", "sample_code", "station_label",
            "sample_matrix", "collection_date", "collection_end", "depth_m", "spatial_kind",
            "quality_flags", "source_file", "source_row",
        )}
        features.append({"type": "Feature", "geometry": mapping(geometry), "properties": properties})
    return features


def _record_features(frame: pd.DataFrame, simplify_tolerance: float = 0) -> list[dict]:
    features = []
    for row in frame.to_dict("records"):
        geometry = _geometry_from_row(row)
        if geometry is None:
            continue
        if simplify_tolerance and geometry.geom_type not in ("Point", "MultiPoint"):
            geometry = geometry.simplify(simplify_tolerance, preserve_topology=True)
        properties = {key: _native(row.get(key)) for key in MAP_PROPERTIES if key in row}
        features.append({"type": "Feature", "geometry": mapping(geometry), "properties": properties})
    return features


def _feature_chunks(features: list[dict]):
    """Bound both feature count and serialized payload for browser-friendly files."""
    chunk, size = [], 42
    for feature in features:
        feature_size = len(json.dumps(feature, ensure_ascii=False, separators=(",", ":"), default=_native).encode()) + 1
        if chunk and (len(chunk) >= MAP_CHUNK_SIZE or size + feature_size > MAP_CHUNK_BYTES):
            yield chunk
            chunk, size = [], 42
        chunk.append(feature)
        size += feature_size
    if chunk:
        yield chunk


def _parse_record_dates(frame: pd.DataFrame) -> np.ndarray:
    if "time" not in frame:
        return np.full(len(frame), np.datetime64("NaT"), dtype="datetime64[D]")
    values = frame["time"].map(lambda value: "" if pd.isna(value) else str(value).split("/", 1)[0])
    return pd.to_datetime(values, errors="coerce", utc=True).dt.tz_localize(None).values.astype("datetime64[D]")


def _haversine(lat, lon, lats, lons):
    radius = 6371.0088
    lat1, lon1 = np.radians(lat), np.radians(lon)
    lat2, lon2 = np.radians(lats), np.radians(lons)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return radius * 2 * np.arctan2(np.sqrt(value), np.sqrt(1 - value))


def _best_matches(samples: pd.DataFrame, records: pd.DataFrame, category: str) -> list[dict]:
    if records.empty or not {"lat", "lon"}.issubset(records):
        return []
    located = records[pd.to_numeric(records["lat"], errors="coerce").notna()
                      & pd.to_numeric(records["lon"], errors="coerce").notna()].copy()
    if located.empty:
        return []
    record_lats = located["lat"].astype(float).to_numpy()
    record_lons = located["lon"].astype(float).to_numpy()
    record_dates = _parse_record_dates(located)
    record_depths = pd.to_numeric(located.get("depth_m"), errors="coerce").to_numpy(dtype=float)
    timeless = category in {"bathymetry"}
    matches = []
    cache = {}
    for sample in samples.to_dict("records"):
        if sample.get("spatial_kind") != "point" or pd.isna(sample.get("latitude")) or pd.isna(sample.get("longitude")):
            continue
        key = (sample["latitude"], sample["longitude"], sample.get("collection_date"), sample.get("depth_m"))
        if key not in cache:
            distances = _haversine(float(sample["latitude"]), float(sample["longitude"]), record_lats, record_lons)
            eligible = distances <= MATCH_DISTANCE_KM
            sample_date = pd.to_datetime(sample.get("collection_date"), errors="coerce")
            gaps = np.full(len(located), np.nan)
            if not timeless and pd.notna(sample_date):
                valid_dates = ~np.isnat(record_dates)
                gaps[valid_dates] = np.abs((record_dates[valid_dates] - np.datetime64(sample_date.date())).astype(int))
                eligible &= valid_dates & (gaps <= MATCH_TIME_DAYS)
            sample_depth = pd.to_numeric(sample.get("depth_m"), errors="coerce")
            depth_gaps = np.full(len(located), np.nan)
            if pd.notna(sample_depth):
                known_depth = ~np.isnan(record_depths)
                depth_gaps[known_depth] = np.abs(record_depths[known_depth] - float(sample_depth))
                eligible &= ~known_depth | (depth_gaps <= MATCH_DEPTH_M)
            candidates = np.where(eligible)[0]
            if len(candidates):
                ordering = sorted(candidates, key=lambda index: (
                    gaps[index] if not np.isnan(gaps[index]) else float("inf"),
                    distances[index],
                    depth_gaps[index] if not np.isnan(depth_gaps[index]) else float("inf"),
                    str(located.iloc[index]["record_id"]),
                ))
                chosen = ordering[0]
                cache[key] = (chosen, distances[chosen], gaps[chosen], depth_gaps[chosen], len(candidates))
            else:
                cache[key] = None
        result = cache[key]
        if result is None:
            continue
        chosen, distance, time_gap, depth_gap, count = result
        record = located.iloc[chosen]
        matches.append({
            "match_id": _stable_id(sample["sample_id"], record["record_id"], category),
            "campaign_id": sample["campaign_id"], "sample_id": sample["sample_id"],
            "record_id": record["record_id"], "category": category,
            "method": "nearest_model_support" if timeless else "nearest_observation_candidate",
            "distance_km": round(float(distance), 6),
            "time_gap_days": None if np.isnan(time_gap) else int(time_gap),
            "depth_gap_m": None if np.isnan(depth_gap) else round(float(depth_gap), 6),
            "depth_status": "verified" if not np.isnan(depth_gap) else "unverified",
            "candidate_count": int(count), "policy_version": SCHEMA_VERSION,
        })
    return matches


def publish(source: Path, output: Path) -> dict:
    for generated in (output / "parquet", output / "map"):
        if generated.exists():
            shutil.rmtree(generated)
    if (output / "catalog.json").exists():
        (output / "catalog.json").unlink()
    campaigns = load_campaigns_from_samples()
    campaign_by_name = {campaign["name"]: campaign for campaign in campaigns}
    samples = pd.read_csv(ROOT / "samples.csv", low_memory=False)
    samples["geometry_wkb"] = samples.apply(
        lambda row: Point(float(row.longitude), float(row.latitude)).wkb
        if row.spatial_kind == "point" and pd.notna(row.latitude) and pd.notna(row.longitude) else None, axis=1)
    _write_geoparquet(samples, output / "parquet" / "samples.parquet", samples.pop("geometry_wkb"))

    catalog = {"schema_version": SCHEMA_VERSION, "crs": "OGC:CRS84", "campaigns": [], "layers": {}}
    coverage_path = source / "chemistry_coverage.json"
    if coverage_path.exists():
        shutil.copyfile(coverage_path, output / "chemistry_coverage.json")
        catalog["chemistry_coverage"] = "chemistry_coverage.json"
    all_matches = []
    loaded = {}
    for category, filename in CATEGORY_FILES.items():
        path = source / filename
        frame = pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()
        if category == "chemistry":
            from enrich_outputs import enrich_chemistry
            frame = enrich_chemistry(frame)
        if category == "oceanography" and "data_type" in frame:
            frame = frame[frame["data_type"].ne("manifest")].copy()
        if not frame.empty:
            frame["campaign_id"] = frame["campaign_code"].map(
                {name: campaign["campaign_id"] for name, campaign in campaign_by_name.items()})
            frame["record_id"] = frame.apply(lambda row: _stable_id(
                row.get("source"), row.get("dataset_id"), row.get("feature_id"), row.get("time"),
                row.get("lat"), row.get("lon")), axis=1)
            geometries = frame.apply(lambda row: (_geometry_from_row(row) or Point()).wkb
                                     if _geometry_from_row(row) is not None else None, axis=1)
        else:
            frame = frame.assign(campaign_id=pd.Series(dtype=str), record_id=pd.Series(dtype=str))
            geometries = pd.Series(dtype=object)
        _write_geoparquet(frame, output / "parquet" / f"{category}.parquet", geometries)
        loaded[category] = frame
        status, note = COMPLETENESS[category]
        catalog["layers"][category] = {"rows": len(frame), "status": status, "note": note,
                                        "parquet": f"parquet/{category}.parquet"}

    assets_path = source / "raster_manifest.csv"
    assets = pd.read_csv(assets_path, low_memory=False) if assets_path.exists() else pd.DataFrame()
    _write_geoparquet(assets, output / "parquet" / "assets.parquet")
    catalog["layers"]["assets"] = {"rows": len(assets), "status": COMPLETENESS["assets"][0],
                                     "note": COMPLETENESS["assets"][1], "parquet": "parquet/assets.parquet"}

    for campaign in campaigns:
        campaign_samples = samples[samples["campaign_id"] == campaign["campaign_id"]]
        campaign_slug = _slug(f"{campaign['name']}-{campaign['campaign_id'][:8]}")
        bundle_dir = output / "map" / campaign_slug
        sample_entry = _write_geojson(_sample_features(campaign_samples), bundle_dir / "samples.geojson")
        layer_entries = {"samples": [{**sample_entry, "path": f"map/{campaign_slug}/samples.geojson"}]}
        for category, frame in loaded.items():
            subset = frame[frame["campaign_id"] == campaign["campaign_id"]] if not frame.empty else frame
            tolerance = 0.005 if category == "human_activities" else 0.01 if category == "msfd_regions" else 0
            features = _record_features(subset, tolerance)
            entries = []
            for chunk_index, feature_chunk in enumerate(_feature_chunks(features), start=1):
                filename = f"{category}-{chunk_index:03d}.geojson"
                entry = _write_geojson(feature_chunk, bundle_dir / filename)
                entry["path"] = f"map/{campaign_slug}/{filename}"
                entries.append(entry)
            layer_entries[category] = entries
            if category in {"biology_obis", "biology_gbif", "bathymetry", "climate", "oceanography"}:
                all_matches.extend(_best_matches(campaign_samples, subset, category))
        campaign_assets = assets[assets["campaign_code"] == campaign["name"]] if not assets.empty else assets
        assets_file = bundle_dir / "assets.json"
        assets_file.write_text(campaign_assets.to_json(orient="records", force_ascii=False), encoding="utf-8")
        layer_entries["assets"] = [{"path": f"map/{campaign_slug}/assets.json", "features": len(campaign_assets),
                                     "bytes": assets_file.stat().st_size}]
        catalog["campaigns"].append({
            "campaign_id": campaign["campaign_id"], "campaign_code": campaign["campaign_code"],
            "display_name": campaign["name"], "area": campaign["area"],
            "bounds": [campaign["lon_min"], campaign["lat_min"], campaign["lon_max"], campaign["lat_max"]],
            "sample_window": [campaign["date_min"], campaign["date_max"]],
            "query_window": [campaign["query_date_min"], campaign["query_date_max"]],
            "sample_rows": len(campaign_samples), "layers": layer_entries,
        })

    matches = pd.DataFrame(all_matches)
    _write_geoparquet(matches, output / "parquet" / "sample_matches.parquet")
    catalog["matching"] = {
        "rows": len(matches), "parquet": "parquet/sample_matches.parquet",
        "distance_km": MATCH_DISTANCE_KM, "time_days": MATCH_TIME_DAYS, "depth_m": MATCH_DEPTH_M,
        "meaning": "Candidate associations, not equivalent measurements. Polygon/context layers are not point-matched.",
    }
    catalog_path = output / "catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, default=_native), encoding="utf-8")
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    catalog = publish(args.source, args.out)
    print(f"Published {len(catalog['campaigns'])} campaigns to {args.out}")
    print(f"Sample matches: {catalog['matching']['rows']:,}")


if __name__ == "__main__":
    main()
