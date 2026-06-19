# ONE-BLUE Fusion Pipeline

Standalone pipeline that builds the geo-indexed external-data resources as part of the ONE-BLUE data fusion activities. For every ONE-BLUE sampling campaign it fetches matching slices of public marine datasets and writes one CSV per data category into `output/`.

All output CSVs share a common geo-ready header so they can be filtered by campaign and rendered on a map widget directly from the CECsMarineGUI.

---

## Pipeline code overview

```
build_samples_csv.py          Step 1 — build the master campaign index from a local SAMPLES/ folder containing the filled ONE-BLUE DCTs
explore_geotemporal.py        Optional — inspect bboxes and date ranges before fetching
build_campaign_datasources.py Step 2 — main fetcher (oceanography, chemistry, HA, bathymetry, biology)
fetch_climate.py              Step 3 — ERA5 climate per campaign day (Open-Meteo, no credentials)
fetch_gbif_occurrences.py     Step 3 — GBIF species occurrences per campaign bbox
fetch_msfd.py                 Step 3 — MSFD marine regions overlay
fetch_raster_manifest.py      Step 3 — curated raster/WMS layer catalogue
enrich_outputs.py             Step 4 — add perturbation_class and suspect-list matches
query_external_data.py        Shared utilities (campaign loader, coord parsers, ERDDAP helpers)

SAMPLES/                      Input: one .xlsx per campaign (ONE-BLUE Data Collection Template)
ONE-BLUE suspect list_v1.1.xlsx  Reference: NORMAN-database suspect chemical list
output/               Output: one CSV per data category
```

---

## Input: sample collection templates

Place all filled-in Data Collection Template `.xlsx` files inside a `SAMPLES/` folder at the project root. Each file represents one campaign. The template must follow the ONE-BLUE Data Collection Template format (Campaign sheet + Sample sheet).


---

## Running the pipeline

All steps are run from the project root with a Python 3.10+ environment.

**Step 1 — build the campaign index**

```bash
python build_samples_csv.py
```

Reads every `.xlsx` in `SAMPLES/`, normalises coordinates and dates, and writes `samples.csv`. All downstream scripts use this file as their starting point.

**Step 2 — fetch external datasets**

```bash
python build_campaign_datasources.py
```

Fetches oceanography (Euro-Argo, EMSO-ERIC, Copernicus Marine), chemistry (EMODnet Chemistry), human activities (EMODnet Human Activities), bathymetry (GEBCO via opentopodata) and biology (OBIS) for every campaign. Outputs into `output/`.

Run a single category only:

```bash
python build_campaign_datasources.py --only oceanography
python build_campaign_datasources.py --only oceanography,biology --skip copernicus
python build_campaign_datasources.py --out custom_output_dir
```

**Step 3 — supplementary fetchers** (can run in any order, independently)

```bash
python fetch_climate.py           # ERA5 daily climate per campaign
python fetch_gbif_occurrences.py  # GBIF species occurrences
python fetch_msfd.py              # MSFD marine region polygons
python fetch_raster_manifest.py   # Curated WMS/WCS raster layer catalogue
```

**Step 4 — enrich and classify**

```bash
python enrich_outputs.py
```

Adds `perturbation_class` to `chemistry.csv` and `human_activities.csv`, and cross-references chemistry records against the ONE-BLUE suspect list. Must be run after Step 2.

---

## Credentials

Most services are open APIs requiring no authentication.

| Service | Credential required | How to provide |
|---|---|---|
| Copernicus Marine (CMEMS) | Yes | Set env vars `COPERNICUSMARINE_SERVICE_USERNAME` and `COPERNICUSMARINE_SERVICE_PASSWORD`, or run `copernicusmarine login` once to write `~/.copernicusmarine/.copernicusmarine-credentials` |
| Open-Meteo ERA5 (climate) | No | — |
| EMODnet Chemistry ERDDAP | No | — |
| EMODnet Human Activities WFS | No | — |
| EMODnet Biology / OBIS | No | — |
| EMSO-ERIC ERDDAP | No | — |
| Euro-Argo (argopy) | No | — |
| GBIF occurrence search | No | — |
| EEA MSFD ArcGIS REST | No | — |
| opentopodata (GEBCO bathymetry) | No | Public API, rate-limited to ~1 req/s |

If Copernicus Marine credentials are absent, `build_campaign_datasources.py` skips the CMEMS download and instead writes a manifest row with the bounding-box parameters so the layer can be fetched manually.

---

## Output columns (shared across all CSVs)

Every output CSV starts with these fusion-key columns:

| Column | Description |
|---|---|
| `campaign_code` | Campaign identifier (from the Data Collection Template) |
| `campaign_area` | Free-text area name |
| `campaign_lat_min/max` | Campaign bounding box (decimal degrees) |
| `campaign_lon_min/max` | Campaign bounding box (decimal degrees) |
| `campaign_date_min/max` | Campaign date window (YYYY-MM-DD) |
| `source` | Upstream provider name |
| `data_type` | Category within the source |
| `dataset_id` | Provider dataset or layer identifier |
| `feature_id` | Unique row identifier |
| `time` | Observation timestamp (ISO 8601 UTC) |
| `lat`, `lon` | Observation coordinates (decimal degrees) |
| `depth_m` | Depth below sea surface (m), if available |
| `geom_wkt` | WKT geometry for map rendering (POINT / POLYGON) |
| `perturbation_class` | Classification: `natural`, `anthropic`, `climatic`, `context` |

Source-specific measurement columns follow, then `extra_json` and `source_url`.

---

## Output files

| File | Sources merged |
|---|---|
| `dkan_resources/oceanography.csv` | Euro-Argo, EMSO-ERIC, Copernicus Marine |
| `dkan_resources/chemistry.csv` | EMODnet Chemistry ERDDAP (EUT and contaminant station series) |
| `dkan_resources/human_activities.csv` | EMODnet Human Activities (aquaculture, energy, ports, pressures, protection) |
| `dkan_resources/bathymetry.csv` | GEBCO 2020 via opentopodata |
| `dkan_resources/biology.csv` | OBIS occurrence API |
| `dkan_resources/biology_gbif.csv` | GBIF occurrence API |
| `dkan_resources/climate.csv` | Open-Meteo ERA5 archive |
| `dkan_resources/msfd_regions.csv` | EEA MSFD regions and subregions |
| `dkan_resources/raster_manifest.csv` | Curated WMS/WCS raster layer catalogue |

---

## Dependencies

```bash
pip install pandas requests argopy copernicusmarine openpyxl geopandas shapely
```
