"""Fetch MSFD marine regions and subregions per campaign.

Source: EEA SDI, dataset "MSFD regions and subregions - version 2, Oct. 2022"
       (https://sdi.eea.europa.eu/catalogue/srv/eng/catalog.search#/metadata/
        a60e171d-e2a8-4dc5-a765-c2bdabbdbce6)
ESRI REST: https://water.discomap.eea.europa.eu/arcgis/rest/services/
           Marine/MSFD_regions_and_subregions/MapServer/0

Output: dkan_resources/msfd_regions.csv
        One row per (campaign x MSFD region/subregion whose bbox overlaps
        the campaign bbox). Each row carries the MSFD attributes plus a
        simplified POLYGON / MULTIPOLYGON WKT for direct map rendering.
        perturbation_class = 'context' (MSFD regions are the comparison
        surface for natural / anthropic / climatic perturbations).

Geometry is simplified server-side via maxAllowableOffset to keep CSV
size manageable while preserving usability for mashup overlays.

Run:
    python fetch_msfd.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path
import requests
import pandas as pd
from query_external_data import load_campaigns_from_samples

_ROOT = Path(__file__).parent
OUT = _ROOT / "dkan_resources" / "msfd_regions.csv"

BASE = ("https://water.discomap.eea.europa.eu/arcgis/rest/services/"
        "Marine/MSFD_regions_and_subregions/MapServer/0")

# ~0.02 degrees ≈ 2 km on the equator. Good trade-off for mashup overlays.
MAX_ALLOWABLE_OFFSET = 0.02


def _query_count() -> int:
    r = requests.get(f"{BASE}/query",
                     params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
                     timeout=60).json()
    return int(r.get("count", 0))


def _fetch_feature(oid: int) -> dict:
    """One feature with simplified WGS84 geometry as GeoJSON."""
    r = requests.get(f"{BASE}/query",
                     params={
                         "where":              f"OBJECTID={oid}",
                         "outFields":          "*",
                         "f":                  "geojson",
                         "outSR":              4326,
                         "returnGeometry":     "true",
                         "maxAllowableOffset": MAX_ALLOWABLE_OFFSET,
                         "geometryPrecision":  4,
                     },
                     timeout=180)
    return r.json()


def _geom_bbox(coords) -> tuple[float, float, float, float] | None:
    """Compute (lon_min, lat_min, lon_max, lat_max) from any GeoJSON coords."""
    xs, ys = [], []

    def walk(x):
        if isinstance(x, (list, tuple)):
            if len(x) >= 2 and all(isinstance(v, (int, float)) for v in x[:2]):
                xs.append(float(x[0])); ys.append(float(x[1]))
            else:
                for el in x:
                    walk(el)

    walk(coords)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_overlap(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _ring(r): return "(" + ", ".join(f"{p[0]} {p[1]}" for p in r) + ")"
def _poly(p): return "(" + ", ".join(_ring(r) for r in p) + ")"


def _geom_to_wkt(geom: dict) -> str:
    if not geom:
        return ""
    t = geom.get("type", "")
    c = geom.get("coordinates")
    if c is None:
        return ""
    if t == "Polygon":
        return f"POLYGON{_poly(c)}"
    if t == "MultiPolygon":
        return "MULTIPOLYGON(" + ", ".join(_poly(p) for p in c) + ")"
    return ""


def main() -> None:
    n = _query_count()
    print(f"MSFD layer has {n} features; fetching one by one")

    features: list[dict] = []
    for oid in range(1, n + 1):
        gj = _fetch_feature(oid)
        for feat in gj.get("features", []):
            props = feat.get("properties", {}) or {}
            geom = feat.get("geometry") or {}
            bbox = _geom_bbox(geom.get("coordinates"))
            wkt = _geom_to_wkt(geom)
            features.append({
                "oid":            oid,
                "subregion":      props.get("subregion") or "",
                "subregion_name": props.get("subregionName") or "",
                "region":         props.get("region") or "",
                "region_name":    props.get("regionName") or "",
                "zone_type":      props.get("zoneType") or "",
                "size_km2":       props.get("sizeValue"),
                "bbox":           bbox,
                "wkt":            wkt,
            })
            print(f"  OID {oid:>2}  {props.get('subregionName','?'):45s}  "
                  f"wkt {len(wkt)//1000:>4} kchars")
        time.sleep(0.3)

    camps = load_campaigns_from_samples()
    rows = []
    for camp in camps:
        # Use centroid for the spatial join so that a wide campaign bbox
        # does not spuriously pick up distant MSFD regions whose bbox
        # merely overlaps the campaign extent.
        clon = (camp["lon_min"] + camp["lon_max"]) / 2
        clat = (camp["lat_min"] + camp["lat_max"]) / 2
        for f in features:
            if not f["bbox"]:
                continue
            flon_min, flat_min, flon_max, flat_max = f["bbox"]
            if not (flon_min <= clon <= flon_max and flat_min <= clat <= flat_max):
                continue
            rows.append({
                "campaign_code":     camp["name"],
                "campaign_area":     camp["area"],
                "campaign_lat_min":  camp["lat_min"],
                "campaign_lat_max":  camp["lat_max"],
                "campaign_lon_min":  camp["lon_min"],
                "campaign_lon_max":  camp["lon_max"],
                "campaign_date_min": camp["date_min"],
                "campaign_date_max": camp["date_max"],
                "source":             "EEA-SDI",
                "data_type":          "msfd_region",
                "dataset_id":         "msfd-regions-subregions-v02",
                "feature_id":         str(f["oid"]),
                "perturbation_class": "context",
                "subregion":          f["subregion"],
                "subregion_name":     f["subregion_name"],
                "region":             f["region"],
                "region_name":        f["region_name"],
                "zone_type":          f["zone_type"],
                "size_km2":           f["size_km2"],
                "feature_lon_min":    f["bbox"][0],
                "feature_lat_min":    f["bbox"][1],
                "feature_lon_max":    f["bbox"][2],
                "feature_lat_max":    f["bbox"][3],
                "geom_wkt":           f["wkt"],
                "source_url": ("https://water.discomap.eea.europa.eu/arcgis/rest/"
                               "services/Marine/MSFD_regions_and_subregions/"
                               f"MapServer/0/query?where=OBJECTID%3D{f['oid']}"
                               "&outFields=*&f=geojson&outSR=4326"),
            })

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\nwrote {len(df)} (campaign x MSFD region) rows -> {OUT}")
    if len(df):
        print(df.groupby("campaign_code")["subregion_name"]
                .apply(lambda s: ", ".join(sorted(set(s))))
                .to_string())


if __name__ == "__main__":
    main()
