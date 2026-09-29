"""Fetch ERA5 climatic parameters per campaign from Open-Meteo's free historical
archive API (no credentials required).

Variables fetched (hourly, then aggregated to daily):
  - temperature_2m         air temperature at 2 m (degC)
  - wind_speed_10m         wind speed at 10 m (m/s)
  - wind_direction_10m     wind direction at 10 m (deg)
  - shortwave_radiation    surface incoming shortwave (W/m^2)
  - uv_index               UV index
  - precipitation          precipitation (mm)
  - surface_pressure       surface pressure (hPa)
  - cloud_cover            cloud cover (%)

Output rows: one per campaign per day in the campaign window, at the bbox
centroid. Same fusion keys as the other dkan_resources CSVs so the row
joins cleanly with SAMPLES and the other context layers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from query_external_data import load_campaigns_from_samples, campaign_date_window, temporal_scope  # noqa: E402

OUT = ROOT / "dkan_resources" / "climate.csv"

API_URL = "https://archive-api.open-meteo.com/v1/era5"
HOURLY_VARS = [
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "shortwave_radiation",
    "uv_index",
    "precipitation",
    "surface_pressure",
    "cloud_cover",
]

COMMON_COLS = [
    "campaign_code", "campaign_area",
    "campaign_lat_min", "campaign_lat_max",
    "campaign_lon_min", "campaign_lon_max",
    "campaign_date_min", "campaign_date_max",
    "query_date_min", "query_date_max", "temporal_scope",
    "source", "data_type", "dataset_id", "feature_id",
    "time", "lat", "lon", "depth_m",
    "geom_wkt",
    "perturbation_class",
]

CLIMATE_COLS = [
    "temperature_2m_mean_c",
    "temperature_2m_min_c",
    "temperature_2m_max_c",
    "wind_speed_10m_mean_ms",
    "wind_speed_10m_max_ms",
    "wind_direction_10m_mean_deg",
    "shortwave_radiation_mean_wm2",
    "uv_index_max",
    "precipitation_sum_mm",
    "surface_pressure_mean_hpa",
    "cloud_cover_mean_pct",
]

TRAILING_COLS = ["extra_json", "source_url"]


def _circular_mean(deg: pd.Series) -> float:
    import math
    rad = pd.Series(deg).dropna().astype(float) * math.pi / 180.0
    if len(rad) == 0:
        return float("nan")
    s = rad.apply(math.sin).mean()
    c = rad.apply(math.cos).mean()
    out = math.degrees(math.atan2(s, c))
    if out < 0:
        out += 360.0
    return out


def fetch_one(camp: dict) -> list[dict]:
    if not camp.get("date_min") or not camp.get("date_max"):
        return []
    lat = camp["clat"]
    lon = camp["clon"]
    query_start, query_end = campaign_date_window(camp)
    params = {
        "latitude":  f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": query_start,
        "end_date":   query_end,
        "hourly":     ",".join(HOURLY_VARS),
        "timezone":   "UTC",
    }
    r = requests.get(API_URL, params=params, timeout=60)
    if r.status_code != 200:
        print(f"  [WARN] Open-Meteo {r.status_code} for {camp['name']}: {r.text[:200]}")
        return []
    js = r.json()
    h = js.get("hourly", {})
    if not h or "time" not in h:
        return []
    df = pd.DataFrame(h)
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["day"] = df["time"].dt.date

    rows: list[dict] = []
    for day, g in df.groupby("day"):
        wkt = f"POINT({lon:.6f} {lat:.6f})"
        rows.append({
            "campaign_code":     camp["name"],
            "campaign_area":     camp["area"],
            "campaign_lat_min":  camp["lat_min"],
            "campaign_lat_max":  camp["lat_max"],
            "campaign_lon_min":  camp["lon_min"],
            "campaign_lon_max":  camp["lon_max"],
            "campaign_date_min": camp["date_min"],
            "campaign_date_max": camp["date_max"],
            "query_date_min": query_start,
            "query_date_max": query_end,
            "temporal_scope": temporal_scope(camp, day.isoformat()),
            "source":            "Open-Meteo (ERA5 archive)",
            "data_type":         "climate_daily",
            "dataset_id":        "open-meteo-era5",
            "feature_id":        f"{camp['name']}|{day.isoformat()}",
            "time":              f"{day.isoformat()}T00:00:00Z",
            "lat":               lat,
            "lon":               lon,
            "depth_m":           None,
            "geom_wkt":          wkt,
            "perturbation_class": "climatic",
            "temperature_2m_mean_c":         g["temperature_2m"].mean(),
            "temperature_2m_min_c":          g["temperature_2m"].min(),
            "temperature_2m_max_c":          g["temperature_2m"].max(),
            "wind_speed_10m_mean_ms":        g["wind_speed_10m"].mean(),
            "wind_speed_10m_max_ms":         g["wind_speed_10m"].max(),
            "wind_direction_10m_mean_deg":   _circular_mean(g["wind_direction_10m"]),
            "shortwave_radiation_mean_wm2":  g["shortwave_radiation"].mean(),
            "uv_index_max":                  g["uv_index"].max(),
            "precipitation_sum_mm":          g["precipitation"].sum(),
            "surface_pressure_mean_hpa":     g["surface_pressure"].mean(),
            "cloud_cover_mean_pct":          g["cloud_cover"].mean(),
            "extra_json":                    json.dumps({"hourly_records": int(len(g))}),
            "source_url":                    r.url,
        })
    return rows


def main() -> None:
    camps = load_campaigns_from_samples()
    print(f"Loaded {len(camps)} campaigns")
    all_rows: list[dict] = []
    for camp in camps:
        print(f"-> {camp['name']} ({camp['date_min']} .. {camp['date_max']}) "
              f"centroid {camp['clat']:.3f},{camp['clon']:.3f}", end=" ")
        rows = fetch_one(camp)
        print(f"{len(rows)} daily rows")
        all_rows.extend(rows)

    cols = COMMON_COLS + CLIMATE_COLS + TRAILING_COLS
    df = pd.DataFrame(all_rows, columns=cols)
    OUT.parent.mkdir(exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"wrote {len(df)} rows -> {OUT}")


if __name__ == "__main__":
    main()
