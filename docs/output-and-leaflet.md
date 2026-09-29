# Fused output and Leaflet guide

`python build_fused_outputs.py` publishes the fetched CSV resources as an analytical Parquet collection and static, campaign-scoped map bundles under `output/fused/`.

## Output layout

```text
output/fused/
  catalog.json
  parquet/
    samples.parquet
    sample_matches.parquet
    oceanography.parquet
    chemistry.parquet
    human_activities.parquet
    bathymetry.parquet
    biology_obis.parquet
    biology_gbif.parquet
    climate.parquet
    msfd_regions.parquet
    assets.parquet
  map/
    <campaign-slug>/
      samples.geojson
      <category>-001.geojson
      <category>-002.geojson
      assets.json
```

`catalog.json` is the entry point. Its `campaigns` array contains campaign bounds, sample and query windows, and the map files available for each layer. Its top-level `layers` object gives row counts, Parquet paths, completeness statuses, and caveats. Do not infer completeness from a non-zero row count.

GeoJSON files are bounded to approximately 2 MiB for browser loading. Human-activity and MSFD polygon geometry is simplified in GeoJSON only. The full provider geometry remains in Parquet.

## Data roles

- `samples.parquet`: one row per source sample row, including samples without usable geometry. Only point samples currently have WKB in `geometry`. The 12 extent samples retain their coordinate-bound columns but have null WKB; their extent geometries are emitted in sample GeoJSON. Do not discard those rows or treat their bounds as measured points.
- Category Parquet files: one row per fetched observation, feature, model value, or manifest record. Check `data_type`, `temporal_scope`, and the catalog status before interpreting a row.
- `sample_matches.parquet`: at most one selected candidate per point sample/category, not every eligible association. Defaults are 25 km, 7 days, and 10 m where both depths are known. All fetched records remain available in the category Parquet files.
- `assets.parquet` and per-campaign `assets.json`: links and metadata for external WMS/raster resources. Raster bytes are not copied into the bundle.
- Per-campaign GeoJSON: display-oriented subsets for static web maps. Missing-geometry samples remain in Parquet but cannot appear in GeoJSON.

Matches are candidates, not equivalent measurements. Unknown record depth is unverified, not a confirmed depth match. Polygon and general context layers are deliberately excluded from point matching. Sampling dates remain separate from the padded provider query window.

The current matcher covers oceanography, OBIS, GBIF, bathymetry and climate, not chemistry. Among eligible records it prioritizes absolute time difference, then spatial distance, then depth difference, with `record_id` as a deterministic tie-breaker. It compares dates against `collection_date` (the sample start date), not the full `collection_date`/`collection_end` interval. `candidate_count` reports the number eligible before selecting one. Bathymetry is timeless; when a sample date is missing, the time constraint is not applied and `time_gap_days` is null. These cases must not be interpreted as verified temporal matches.

## Oceanography and chemistry policies

Argo is fetched directly from the public Ifremer ArgoFloats ERDDAP service. Adjusted temperature, salinity and pressure are preferred when QC is 1 or 2; accepted raw values are the fallback. Time and position also require QC 1 or 2. Retained readings are within 5 dbar of 0, 10, 50, 100, 500, 1000 or 2000 dbar. `depth_m` is actual selected pressure treated approximately as metres, not a latitude-aware pressure-to-depth conversion. The method, QC and raw/adjusted selection are recorded in `extra_json`. Responses over 32 MiB are split temporally; requests that cannot be resolved are reported as errors, not as absence.

EMSO deployments are discovered from the live catalogue, filtered by date and spatial metadata, and queried within the same padded date window. Measurements must lie within 300 km of an actual campaign sample point; campaigns with no usable points use an explicitly recorded centroid fallback. This context radius is distinct from the stricter 25 km candidate-match threshold. CF standard names and compatible units determine supported measurements. QC 1 or 2 is required when a measurement QC variable exists; absent QC is labelled `not_provided`, not passed QC. Unsupported units are not relabelled. Responses exceeding 50 MiB are reported and omitted, not silently truncated. The current run omitted Iberian `OBSEA_seabed_station_TS_L1b` for this reason and returned no oxygen in supported units.

Oceanography observation IDs preserve different measurements sharing station, time and depth; `provider_feature_id` retains the original identity. Exact repeats within a campaign/source/dataset are removed. High-frequency series and overlapping EMSO L1b/L1c products remain distinct and are not independent sampling evidence. Analytical rows are not silently aggregated. Map consumers should filter time and dataset before displaying these dense series, rather than loading every oceanography point simultaneously.

Copernicus request manifests are written separately to `dkan_resources/oceanography_requests.csv` and excluded from observation publication and matching. No Copernicus observations were downloaded in the current run.

Every standardized chemistry resource is enriched before writing and again before publication. Exact CAS identifiers across CAS1 through CAS11 take precedence; case/whitespace-normalized exact names provide a fallback without removing punctuation. Matching CAS and name identities are intersected when both are recognized. `suspect_match_status` distinguishes `matched`, `ambiguous`, `conflicting_identifiers`, `unmatched` and `not_applicable`; candidate IDs and the match method remain available. Unique matches use the ONE-BLUE workbook's group/subgroup/subsubgroup, with `provider_chemical_group` preserved and `classification_source` recorded. Providers must expose chemical identity in `cas_number` and/or `compound_name` to enable this linkage.

Chemistry retains the campaign window plus/minus 30 days. Historical contaminants are not substituted when contemporary queries are empty. Supplemental DOME/EMPODAT filtering uses the full campaign bounding box with no additional spatial padding, unlike the legacy clipped EMODnet queries. Neither suspect membership nor the broad `perturbation_class` heuristic establishes environmental detection, source attribution, hazard or safety.

### Additional chemistry providers

`fetch_chemical_observations.py` uses the [DOME download API](https://dome.ices.dk/api/swagger/index.html) for water, sediment and biota. It verifies pagination against the reported total, validates dates/coordinates locally, and resolves parameter names/CAS identifiers through the official ICES vocabulary. Provider `tblParamID`, sample ID, method, matrix, units, measurement basis and original record remain available. DOME data are CC BY 4.0; cite ICES DOME and the access date. Sediment/biota depth fields are not treated as seawater sampling depth. Interval water depths remain in raw metadata rather than being converted to a fabricated point depth.

The current DOME retrieval completed all 27 campaign/matrix queries: 147 rows at five Irish Sea stations, zero for the other queries. These include 129 contaminant-related results and 18 supporting parameters (`measurement_role`). There are 122 results below detection/quantification limits, 25 unqualified numeric results, 111 originator-acceptable flags and 36 originator-suspect flags. Only four records fall within the actual sampling window; 143 are padded temporal context. Seventy-five rows match 15 ONE-BLUE suspect identities. These counts do not establish spatial/depth equivalence to individual samples; chemistry is not automatically included in the generic nearest-record matching table.

Count-only diagnostics on **2026-09-29** investigated the eight empty campaigns using the same DOME water, sediment and biota download endpoints. They did not alter the production queries or import historical observations:

| Campaign area | Diagnostic result |
| --- | --- |
| Greek, Italian and Croatian campaign bounds | Zero records in all three matrices even with date filtering removed, indicating gaps in these endpoints' geographic coverage. |
| Iberian campaign (2026) | Records exist inside the bounds without date filtering, but the three endpoints returned zero 2026 records even without geographic restrictions. |
| Spanish Mediterranean coast | Some water and biota records exist without date filtering, but none matched the campaign window. |
| High Arctic | 119 sediment records exist without date filtering, but none matched the campaign window; water and biota remained empty. |

DOME returned substantial 2025 data elsewhere, so a blanket claim that the database is outdated would be incorrect. These are dated endpoint diagnostics, not proof that no relevant measurements exist in other databases. The outcome remains one populated campaign and eight unresolved chemistry coverage gaps. The diagnostics are recorded here, not as additional production queries in `chemistry_coverage.json`.

`parameter_value` contains only unqualified numeric values. `parameter_reported_value` preserves the provider's number even when it is a threshold; interpret it together with `concentration_qualifier`, `detection_limit`, `quantification_limit` and the original record. Never replace `<LOD` or `<LOQ` with zero or count them as detections. Provider quality flags are independent of concentration qualifiers. No cross-matrix, dry/wet basis or unit conversion is inferred. Map properties expose these qualifiers and quality flags as well as the measurement role.

[EMPODAT's API](https://www.norman-network.com/nds/api) is queried by explicitly selected CAS identifiers, not by an undocumented date or bbox filter. The live API returned 20,000 records per page despite documentation describing 200; pagination follows response totals rather than assuming a page size. The initial complete substance queries covered triclosan (68,147), diclofenac (70,476), PFOA (86,057) and PFOS (64,911). None yielded a usable match within the campaign bounds/dates. Of 289,591 records, 197,590 lacked coordinates and another 7,700 lacked usable full dates. Missing coordinates are not invented from station names. Country-query API faults are not evidence of absence. This pilot is neither an exhaustive suspect-list search nor proof that EMPODAT has no relevant data. Full export, QA/QC and redistribution access must be checked against NORMAN's terms; no EMPODAT rows were published in this run.

`chemistry_coverage.json`, referenced by the published catalog, records completed queries, errors, exclusions and the cache policy. A complete query means all API-reported rows were examined, not complete environmental coverage. Responses are cached and limited to 64 MiB each; page-budget failures leave the previous chemistry CSV unchanged. Source refreshes preserve other providers; repeated identical source rows are deduplicated by campaign/source/dataset/feature identity. Cross-provider duplicates cannot reliably be removed without shared sample identifiers: no heuristic value-based merging is performed. Keep source provenance when combining overlapping EMODnet, DOME and EMPODAT contributions.

## Coordinate conventions

All map output uses `OGC:CRS84`: GeoJSON coordinates are `[longitude, latitude]`. Leaflet APIs generally accept positions as `[latitude, longitude]`; Leaflet converts GeoJSON coordinates internally. Catalog bounds are `[west, south, east, north]`, so convert them before calling `fitBounds`.

```js
const leafletBounds = [
  [campaign.bounds[1], campaign.bounds[0]],
  [campaign.bounds[3], campaign.bounds[2]],
];
map.fitBounds(leafletBounds);
```

## Load a campaign in Leaflet

Serve the repository over HTTP rather than opening the page from `file://`. For example:

```bash
python -m http.server 8000
```

This example loads samples plus one selected category. It reads every chunk listed for the layer instead of guessing filenames.

```js
const root = "/output/fused/";
const catalog = await fetch(`${root}catalog.json`).then((response) => response.json());
const campaign = catalog.campaigns.find((item) => item.campaign_id === campaignId);

map.fitBounds([
  [campaign.bounds[1], campaign.bounds[0]],
  [campaign.bounds[3], campaign.bounds[2]],
]);

async function addCatalogLayer(layerName, options = {}) {
  const group = L.featureGroup().addTo(map);
  for (const entry of campaign.layers[layerName] ?? []) {
    const data = await fetch(`${root}${entry.path}`).then((response) => response.json());
    L.geoJSON(data, {
      ...options,
      onEachFeature(feature, layer) {
        const properties = feature.properties ?? {};
        const popup = document.createElement("div");
        for (const [key, value] of Object.entries(properties)) {
          if (value === null || value === undefined || value === "") continue;
          const row = document.createElement("div");
          const label = document.createElement("strong");
          label.textContent = key;
          row.append(label, document.createTextNode(`: ${String(value)}`));
          popup.append(row);
        }
        layer.bindPopup(popup);
      },
    }).addTo(group);
  }
  return group;
}

await addCatalogLayer("samples", {
  pointToLayer: (_feature, latlng) => L.circleMarker(latlng, { radius: 5 }),
});
await addCatalogLayer("biology_obis", {
  pointToLayer: (_feature, latlng) => L.circleMarker(latlng, { radius: 3 }),
});
```

The popup uses text-only DOM nodes for both property names and values; do not replace them with unescaped HTML interpolation from provider data.

For a production map, load layers on demand from a layer control, cluster dense occurrence points, and remove a layer's `L.featureGroup` when disabled. Show `catalog.layers[layerName].status` and `catalog.layers[layerName].note` near the category layer toggle so partial and modeled layers cannot be mistaken for complete observations. These fields are not directly on `catalog`; the `samples` map layer has no entry in `catalog.layers` and needs separate handling.

## WMS and raster assets

Each campaign's `assets.json` contains fields such as `format`, `wms_base`, `dataset_id`, `capabilities_url`, and `wms_getmap_url`. A WMS layer can be attached without downloading the raster:

```js
const assets = await fetch(`${root}${campaign.layers.assets[0].path}`)
  .then((response) => response.json());
const asset = assets.find((item) => item.format === "image/png" && item.wms_base);

const wms = L.tileLayer.wms(asset.wms_base, {
  layers: asset.dataset_id,
  format: asset.format,
  transparent: true,
  version: "1.3.0",
});
wms.addTo(map);
```

Confirm layer names and supported CRS values from `capabilities_url`; WMS 1.3.0 axis-order rules differ by CRS. The curated `wms_getmap_url` is also useful as a direct preview.

Leaflet does not natively render arbitrary TIF/COG files. Use a COG-capable client such as `georaster-layer-for-leaflet`, or publish the raster through a tile/WMS service. Direct browser access requires the remote host to allow CORS and HTTP range requests. Treat URLs as external dependencies that may expire or reject browser requests.

## Analytical use

Parquet is the authoritative compact output for analysis and full provider geometry, subject to the sample-extent limitation above. Geometry is WKB with GeoParquet metadata and can be read with PyArrow, GeoPandas, DuckDB spatial, or another GeoParquet-aware client. Filter by `campaign_id` before transferring data to a browser.

**Current CRS limitation:** the publisher explicitly writes `"crs": null` in GeoParquet metadata, which means undefined CRS, not an implicit CRS84 declaration. The coordinates follow the pipeline's `OGC:CRS84` longitude/latitude convention, but spatial clients cannot infer that from the Parquet metadata. After verifying that convention for the file being read, explicitly assign `OGC:CRS84` in the client before spatial operations; assigning a CRS is not a coordinate transformation. The GeoJSON/catalog convention described above does not repair the Parquet metadata. This is a documented publisher limitation, not a completed metadata fix.

```python
import pandas as pd

matches = pd.read_parquet("output/fused/parquet/sample_matches.parquet")
selected = matches[matches["campaign_id"] == campaign_id]
```

Use `record_id` to join a match to its category Parquet file and `sample_id` to join it to `samples.parquet`. Preserve the catalog schema version in consumers and fail clearly on an unsupported major version.
