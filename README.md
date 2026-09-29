# ONE-BLUE Fusion Pipeline

Standalone pipeline that builds the geo-indexed external-data resources as part of the ONE-BLUE data fusion activities. For every ONE-BLUE sampling campaign it fetches matching slices of public marine datasets, writes source CSVs into `dkan_resources/`, and publishes analytical Parquet plus static map bundles into `output/fused/`.

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
fetch_chemical_observations.py Step 3 — ICES DOME and selected NORMAN EMPODAT substances
enrich_outputs.py             Step 4 — add perturbation_class and suspect-list matches
build_fused_outputs.py        Step 5 — publish Parquet, candidate matches, catalog and static GeoJSON
query_external_data.py        Shared utilities (campaign loader, coord parsers, ERDDAP helpers)

SAMPLES/                      Input: one .xlsx per campaign (ONE-BLUE Data Collection Template)
ONE-BLUE suspect list_v1.1.xlsx  Reference: NORMAN-database suspect chemical list
dkan_resources/               Intermediate: one CSV per data category
output/fused/                 Published Parquet and campaign-scoped map bundles
```

---

## Input: sample collection templates

Place all filled-in Data Collection Template `.xlsx` files inside a `SAMPLES/` folder at the project root. Each file represents one campaign. The template must follow the ONE-BLUE Data Collection Template format (Campaign sheet + Sample sheet).


---

## Running the pipeline

All steps are run from the project root with a Python 3.10+ environment.

**Implementation status:** The pipeline has been run against all nine workbooks. Publication contains 7,334 sample rows, 498,635 oceanographic observations (27,664 Argo and 470,971 EMSO), and 6,646 candidate matches. Supplemental chemistry retrieval found 147 ICES DOME records at five Irish Sea stations, including 18 supporting measurements and 122 censored results; 75 rows match the ONE-BLUE suspect list. An EMPODAT pilot checked 289,591 records for four substances and retained no geotemporal matches; many records lacked usable coordinates/dates. This is not exhaustive chemistry coverage, and older observations are not substituted. Oceanography remains partial: one Iberian OBSEA deployment exceeded the 50 MiB response budget, and no Copernicus observations were downloaded. Inspect `output/fused/catalog.json` and its chemistry coverage report before use. [The ingestion findings](docs/sample-review.md) document retained source issues, and [the output guide](docs/output-and-leaflet.md) defines retrieval, QC, classification and visualization limits.

**Step 1 — build the campaign index**

```bash
python build_samples_csv.py
```

Reads every `.xlsx` in `SAMPLES/`, normalises coordinates and dates, and writes [samples.csv](samples.csv) and [samples.audit.json](samples.audit.json). All downstream scripts use the same normalized records. The audit includes workbook checksums, source-row quality issues and coordinate/date extrema. Invalid rows are retained, not silently dropped.

`latitude` and `longitude` are populated only for actual points. Coordinate ranges use `latitude_min/max` and `longitude_min/max` with `spatial_kind=extent`; they must not be rendered as measured point samples. `campaign_id` distinguishes workbooks even when their original `campaign_code` is duplicated. `sample_id` is stable for a workbook filename and Excel row; inserting/reordering rows changes affected IDs. Original identifiers, matrix, raw coordinate/time/duration values and source rows are preserved.

Durations use inclusive calendar days by default: 1 day is the sampling date itself, 2 days includes the following date. The default can also be specified explicitly:

```bash
python build_samples_csv.py --duration-mode inclusive
```

`inclusive` computes start + max(days - 1, 0). The optional `--duration-mode elapsed` retains the alternative start + days calculation. Mixed numeric/clock/range hours are preserved in `duration_hours_raw`, flagged `hours_ignored_day_resolution`, and do not affect day-level bounds or block fetching. Sub-day/overnight timing is deliberately not inferred.

Suspect coordinates and dates are retained literally, with audit flags. Valid but unusual values still contribute to campaign bounds. Unparseable values remain as raw data with null normalized values; no location, hemisphere or date is invented. Missing geometry does not remove a sample from the index. Copied campaign metadata stays visible, but workbook IDs keep campaigns separate. Unreadable workbooks and campaigns without usable spatial/date bounds still fail validation.

A changed workbook, missing audit, or edited CSV invalidates the index. Automatic regeneration uses the inclusive default.

### Day-Level Query Padding

Sampling dates stay in `campaign_date_min/max`. Dated provider requests use a separate `query_date_min/max`, extending the campaign window **30 calendar days on each side**. `campaign_date_window()` centralizes this policy; `CONTEXT_DAYS` is the default and a campaign's `context_days` can override it. Zero padding requests the exact sampling dates. Timestamp-based requests include the final day through 23:59:59 UTC as a query convention, not a claim that local sample timestamps were recorded in UTC.

For example, sampling on 2026-08-03 queries 2026-07-04 through 2026-09-02. Padding is applied to the original dates once, never to an already padded window. The retained Arctic 2023-2024 and Iberian August 2026 dates are not corrected or shortened automatically.

The main fetcher, climate and GBIF outputs include `temporal_scope`: `within_sample_window`, `overlaps_sample_window`, `context` or `unknown`. This describes temporal support only, not spatial/depth matching or scientific equivalence. Interval records crossing the sample-window boundary remain distinguishable from observations entirely within it. Undated records and service manifests are not labelled as observed in-window data. The broader spatial-buffer and bounded-download work remains pending.

Run the offline ingestion and mocked provider regression tests:

```bash
python -m unittest discover -s tests -p test_pipeline.py
```

**Step 2 — fetch external datasets**

```bash
python build_campaign_datasources.py
```

Fetches oceanography (Argo GDAC via Ifremer ERDDAP, EMSO-ERIC, optional Copernicus Marine), chemistry (EMODnet Chemistry), human activities (EMODnet Human Activities), bathymetry (GEBCO via opentopodata) and biology (OBIS) for every campaign. Outputs into `dkan_resources/`.

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
python fetch_chemical_observations.py --providers dome
python fetch_chemical_observations.py --providers empodat --empodat-cas 3380-34-5 15307-86-5 335-67-1 1763-23-1 --max-pages 5
```

The chemistry supplement preserves other providers and applies the ONE-BLUE suspect classification. DOME queries water, sediment and biota using full campaign bounds and the existing padded dates. EMPODAT queries only the explicitly supplied CAS identifiers (the example covers triclosan, diclofenac, PFOA and PFOS), with local date/coordinate filtering. API faults and exhausted page budgets are not treated as empty success and leave the existing chemistry CSV unchanged. Responses are cached under `dkan_resources/chemistry_cache/`; remove that cache to request fresh data. `dkan_resources/chemistry_coverage.json` records query scope, completion and exclusions. EMODnet-only refreshes preserve these supplemental sources.

**Step 4 — enrich and classify**

```bash
python enrich_outputs.py
```

Adds `perturbation_class` to chemistry and human activities. Chemistry is also matched automatically whenever the main fetcher writes it and whenever the publisher reads it, regardless of provider. Exact CAS aliases and conservative normalized exact names are checked against the ONE-BLUE suspect workbook. Unique matches use its chemical groups; ambiguous or conflicting identifiers remain unresolved, with provider classification preserved separately. Run this step after Step 2 to enrich human activities as well.

**Step 5 — publish analytical and map outputs**

```bash
python build_fused_outputs.py
```

Writes GeoParquet-compatible category files, precomputed sample-to-record candidate matches, `catalog.json`, and campaign-scoped GeoJSON/assets bundles to `output/fused/`. Map chunks are capped at approximately 2 MiB; display polygons may be simplified while complete geometry remains in Parquet. See [the output and Leaflet guide](docs/output-and-leaflet.md) for schemas, limitations, and loading examples.

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
| Argo GDAC (Ifremer ERDDAP) | No | Direct HTTP; no argopy dependency |
| GBIF occurrence search | No | — |
| EEA MSFD ArcGIS REST | No | — |
| opentopodata (GEBCO bathymetry) | No | Public API, rate-limited to ~1 req/s |

If Copernicus Marine credentials are absent, the main fetcher writes request parameters to `dkan_resources/oceanography_requests.csv`, separate from observations and excluded from observation Parquet, maps and sample matches. With `--skip copernicus`, this request table is empty.

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
| `dkan_resources/oceanography.csv` | Argo GDAC, EMSO-ERIC, optional Copernicus observations |
| `dkan_resources/oceanography_requests.csv` | Copernicus requests only; not observations |
| `dkan_resources/chemistry.csv` | EMODnet Chemistry, ICES DOME, explicitly queried NORMAN EMPODAT substances |
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
python -m pip install -r requirements.txt
```

Argo and EMSO use the core HTTP dependencies. Only `copernicusmarine` remains an optional provider client; install it separately in a compatible Python environment and configure credentials to download Copernicus products.
