"""Fetch additional biology occurrences from GBIF per campaign.

OBIS is the marine occurrence backbone but GBIF aggregates many sources OBIS
does not have direct feeds from: iNaturalist (citizen-science coastal
records), museum collection specimens, national biodiversity registers, the
fish market datasets, photo-id catalogues, etc. This fetcher pulls GBIF
occurrences inside each campaign bbox and date window, with coordinates
present, and writes biology_gbif.csv with the same fusion keys as
biology.csv.

To keep the dataset manageable and the campaigns marine-flavoured, results
are post-filtered to Animalia + a curated set of marine-relevant
plant/algae phyla. This still includes coastal observations (intentionally;
they are useful biology context for the campaigns).

Run:
    python fetch_gbif_occurrences.py
"""

from __future__ import annotations

import os
import time
import json
import requests
from pathlib import Path
import pandas as pd
from query_external_data import load_campaigns_from_samples, campaign_date_window, temporal_scope

_ROOT = Path(__file__).parent
OUT = _ROOT / "dkan_resources" / "biology_gbif.csv"

API = "https://api.gbif.org/v1/occurrence/search"
PAGE_SIZE = 300
MAX_PER_CAMP = 5000  # match OBIS cap

# Marine + coastal flavour: keep Animalia plus a few plant/algae phyla that
# are routinely observed in nearshore campaigns (seagrasses, macroalgae,
# salt-marsh plants are valid biology context).
KEEP_KINGDOMS = {"Animalia", "Chromista", "Bacteria", "Protozoa"}
KEEP_PLANT_PHYLA = {
    "Tracheophyta",   # incl. Posidonia, Zostera, salt-marsh halophytes
    "Rhodophyta", "Chlorophyta", "Ochrophyta",
    "Bacillariophyta", "Haptophyta",
}


def _keep(rec: dict) -> bool:
    k = rec.get("kingdom")
    if k in KEEP_KINGDOMS:
        return True
    if k == "Plantae" and rec.get("phylum") in KEEP_PLANT_PHYLA:
        return True
    if k == "Fungi":
        return False
    # Default: skip unmatched (mostly land plants/fungi)
    return False


def fetch_one(camp: dict) -> list[dict]:
    rows: list[dict] = []
    query_start, query_end = campaign_date_window(camp)
    offset = 0
    seen = 0
    while seen < MAX_PER_CAMP:
        params = {
            "decimalLatitude":  f"{camp['lat_min']},{camp['lat_max']}",
            "decimalLongitude": f"{camp['lon_min']},{camp['lon_max']}",
            "hasCoordinate":    "true",
            "hasGeospatialIssue": "false",
            "limit":            PAGE_SIZE,
            "offset":           offset,
        }
        params["eventDate"] = f"{query_start},{query_end}"
        r = requests.get(API, params=params, timeout=90)
        if r.status_code != 200:
            print(f"    HTTP {r.status_code}; stopping")
            break
        payload = r.json()
        results = payload.get("results", [])
        if not results:
            break
        for o in results:
            if not _keep(o):
                continue
            la = o.get("decimalLatitude"); lo = o.get("decimalLongitude")
            if la is None or lo is None:
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
                "query_date_min": query_start,
                "query_date_max": query_end,
                "temporal_scope": temporal_scope(camp, o.get("eventDate")),
                "source":             "GBIF",
                "data_type":          "occurrence",
                "dataset_id":         str(o.get("datasetKey", "")),
                "feature_id":         str(o.get("key", "")),
                "time":               str(o.get("eventDate", "") or ""),
                "lat":                float(la),
                "lon":                float(lo),
                "depth_m":            o.get("depth") if isinstance(o.get("depth"), (int, float)) else None,
                "geom_wkt":           f"POINT({lo} {la})",
                "perturbation_class": "natural",
                "scientific_name":    str(o.get("scientificName", "")),
                "accepted_name":      str(o.get("acceptedScientificName", "")),
                "taxon_key":          str(o.get("acceptedTaxonKey", o.get("taxonKey", ""))),
                "kingdom":            str(o.get("kingdom", "")),
                "phylum":             str(o.get("phylum", "")),
                "class_":             str(o.get("class", "")),
                "order":              str(o.get("order", "")),
                "family":             str(o.get("family", "")),
                "genus":              str(o.get("genus", "")),
                "basis_of_record":    str(o.get("basisOfRecord", "")),
                "publishing_org":     str(o.get("publishingOrgKey", "")),
                "occurrence_url":     str(o.get("occurrenceID", "") or ""),
                "extra_json":         "",
                "source_url":         r.url,
            })
        seen += len(results)
        offset += len(results)
        if payload.get("endOfRecords"):
            break
        time.sleep(0.2)
    return rows


def main() -> None:
    camps = load_campaigns_from_samples()
    print(f"Loaded {len(camps)} campaigns")
    all_rows: list[dict] = []
    for camp in camps:
        print(f"-> {camp['name']} ({camp['date_min']} .. {camp['date_max']})", end="  ")
        try:
            rows = fetch_one(camp)
        except Exception as e:
            print(f"ERR {str(e)[:60]}")
            continue
        print(f"{len(rows)} marine/coastal occurrences kept")
        all_rows.extend(rows)
    df = pd.DataFrame(all_rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\nwrote {len(df)} rows -> {OUT}")
    if len(df):
        print("kingdoms:", df["kingdom"].value_counts().to_dict())
        print("top families:")
        print(df["family"].value_counts().head(10).to_string())


if __name__ == "__main__":
    main()
