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

- `samples.parquet`: one row per source sample row, including samples without usable geometry. Point and extent geometry is WKB in `geometry`.
- Category Parquet files: one row per fetched observation, feature, model value, or manifest record. Check `data_type`, `temporal_scope`, and the catalog status before interpreting a row.
- `sample_matches.parquet`: precomputed candidate associations between point samples and point records. Defaults are 25 km, 7 days, and 10 m where both depths are known.
- `assets.parquet` and per-campaign `assets.json`: links and metadata for external WMS/raster resources. Raster bytes are not copied into the bundle.
- Per-campaign GeoJSON: display-oriented subsets for static web maps. Missing-geometry samples remain in Parquet but cannot appear in GeoJSON.

Matches are candidates, not equivalent measurements. Unknown record depth is unverified, not a confirmed depth match. Polygon and general context layers are deliberately excluded from point matching. Sampling dates remain separate from the padded provider query window.

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
        const text = Object.entries(properties)
          .filter(([, value]) => value !== null && value !== "")
          .map(([key, value]) => `<b>${key}</b>: ${String(value)}`)
          .join("<br>");
        layer.bindPopup(text);
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

For a production map, load layers on demand from a layer control, cluster dense occurrence points, and remove a layer's `L.featureGroup` when disabled. Show the top-level catalog `status` and `note` near the layer toggle so partial and modeled layers cannot be mistaken for complete observations.

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

Parquet is the authoritative compact output for analysis and full geometry. Geometry is WKB with GeoParquet metadata and can be read with PyArrow, GeoPandas, DuckDB spatial, or another GeoParquet-aware client. Filter by `campaign_id` before transferring data to a browser.

```python
import pandas as pd

matches = pd.read_parquet("output/fused/parquet/sample_matches.parquet")
selected = matches[matches["campaign_id"] == campaign_id]
```

Use `record_id` to join a match to its category Parquet file and `sample_id` to join it to `samples.parquet`. Preserve the catalog schema version in consumers and fail clearly on an unsupported major version.
