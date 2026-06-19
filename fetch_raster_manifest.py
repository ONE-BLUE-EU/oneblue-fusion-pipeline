"""Build raster_manifest.csv: one row per (campaign x raster/area product).

We do NOT download the raster bytes. The mashup map widget can stream them
directly from the upstream WMS/WCS/WMTS endpoints inside a campaign bbox.
The manifest just records the source URL, the access protocol, the layer
identifier, the data theme, the perturbation_class and a per-campaign
WMS GetMap URL template that the front-end can drop straight into a
Leaflet/OpenLayers layer.

Run:
    python fetch_raster_manifest.py
"""

from __future__ import annotations

import os
from pathlib import Path
import pandas as pd
from urllib.parse import urlencode
from query_external_data import load_campaigns_from_samples

_ROOT = Path(__file__).parent
OUT = _ROOT / "dkan_resources" / "raster_manifest.csv"


# A curated catalogue of upstream raster / area products that can be
# overlaid per campaign. Each entry: theme, source, layer_id (WMS layer
# name), title, capabilities_url (WMS GetCapabilities), wms_base (the
# WMS endpoint to use for GetMap), format, perturbation_class, notes.
PRODUCTS = [
    # --- EMODnet Chemistry: eutrophication gridded assessment ----------
    {
        "theme": "eutrophication",
        "source": "EMODnet-Chemistry",
        "layer_id": "emodnet:eutrophication_chlorophyll-a_winter_baltic",
        "title": "Chlorophyll-a winter mean (Baltic, EMODnet eutrophication assessment)",
        "wms_base": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms",
        "capabilities_url": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "natural",
        "notes": "Basin-scale gridded product; pick the basin matching the campaign.",
    },
    {
        "theme": "eutrophication",
        "source": "EMODnet-Chemistry",
        "layer_id": "emodnet:eutrophication_chlorophyll-a_winter_med",
        "title": "Chlorophyll-a winter mean (Mediterranean, EMODnet eutrophication assessment)",
        "wms_base": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms",
        "capabilities_url": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "natural",
        "notes": "",
    },
    {
        "theme": "eutrophication",
        "source": "EMODnet-Chemistry",
        "layer_id": "emodnet:eutrophication_DIN_winter_med",
        "title": "Dissolved inorganic nitrogen winter mean (Mediterranean)",
        "wms_base": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms",
        "capabilities_url": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "natural",
        "notes": "",
    },
    {
        "theme": "contaminants",
        "source": "EMODnet-Chemistry",
        "layer_id": "emodnet:contaminants_heavy_metals_sediment",
        "title": "Heavy metals in sediment (EMODnet contaminants assessment)",
        "wms_base": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms",
        "capabilities_url": "https://prod-erddap.emodnet-chemistry.eu/geoserver/emodnet/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "anthropic",
        "notes": "Gridded basin assessment.",
    },
    # --- EMODnet Bathymetry --------------------------------------------
    {
        "theme": "bathymetry",
        "source": "EMODnet-Bathymetry",
        "layer_id": "emodnet:mean_atlas_land",
        "title": "EMODnet bathymetry mean depth raster (GEBCO-derived)",
        "wms_base": "https://ows.emodnet-bathymetry.eu/wms",
        "capabilities_url": "https://ows.emodnet-bathymetry.eu/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "context",
        "notes": "Native GeoTIFF tiles also at https://tiles.emodnet-bathymetry.eu",
    },
    # --- Copernicus Marine ---------------------------------------------
    {
        "theme": "sea_surface_temperature",
        "source": "Copernicus Marine",
        "layer_id": "METOFFICE-GLO-SST-L4-REP-OBS-SST",
        "title": "Global SST L4 reanalysis (daily, 0.05deg)",
        "wms_base": "https://wmts.marine.copernicus.eu/teroWmts",
        "capabilities_url": "https://wmts.marine.copernicus.eu/teroWmts?service=WMTS&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "climatic",
        "notes": "WMTS preview tiles; full NetCDF requires CMEMS credentials.",
    },
    {
        "theme": "chlorophyll",
        "source": "Copernicus Marine",
        "layer_id": "OCEANCOLOUR_GLO_BGC_L4_MY_009_104",
        "title": "Global ocean colour L4 chlorophyll-a (daily, 4 km)",
        "wms_base": "https://wmts.marine.copernicus.eu/teroWmts",
        "capabilities_url": "https://wmts.marine.copernicus.eu/teroWmts?service=WMTS&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "natural",
        "notes": "",
    },
    # --- Copernicus Atmosphere (CAMS) ----------------------------------
    {
        "theme": "uv_index",
        "source": "Copernicus CAMS",
        "layer_id": "cams-europe-air-quality-forecasts",
        "title": "CAMS UV index (forecast + reanalysis)",
        "wms_base": "https://eccharts.ecmwf.int/wms/",
        "capabilities_url": "https://eccharts.ecmwf.int/wms/?service=WMS&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "climatic",
        "notes": "Closes the uv_index gap in climate.csv. Requires ADS account for raw NetCDF.",
    },
    # --- EMODnet Human Activities: vessel-density rasters --------------
    {
        "theme": "vessel_density",
        "source": "EMODnet-HumanActivities",
        "layer_id": "emodnet:vesseldensity_all",
        "title": "AIS vessel density (all ship types, monthly mean)",
        "wms_base": "https://ows.emodnet-humanactivities.eu/wms",
        "capabilities_url": "https://ows.emodnet-humanactivities.eu/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "anthropic",
        "notes": "Monthly raster of vessel pressure.",
    },
    {
        "theme": "vessel_density",
        "source": "EMODnet-HumanActivities",
        "layer_id": "emodnet:vesseldensity_cargo",
        "title": "AIS vessel density (cargo, monthly mean)",
        "wms_base": "https://ows.emodnet-humanactivities.eu/wms",
        "capabilities_url": "https://ows.emodnet-humanactivities.eu/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "anthropic",
        "notes": "",
    },
    {
        "theme": "vessel_density",
        "source": "EMODnet-HumanActivities",
        "layer_id": "emodnet:vesseldensity_fishing",
        "title": "AIS vessel density (fishing, monthly mean)",
        "wms_base": "https://ows.emodnet-humanactivities.eu/wms",
        "capabilities_url": "https://ows.emodnet-humanactivities.eu/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "anthropic",
        "notes": "",
    },
    # --- EMODnet Biology -----------------------------------------------
    {
        "theme": "biology_density",
        "source": "EMODnet-Biology",
        "layer_id": "emodnet:biology_occurrence_density",
        "title": "EMODnet Biology occurrence density (ICES-rectangle gridded)",
        "wms_base": "https://geo.vliz.be/geoserver/wms",
        "capabilities_url": "https://geo.vliz.be/geoserver/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "format": "image/png",
        "perturbation_class": "natural",
        "notes": "Kernel-density surface of species records.",
    },
]


def _bbox_str(camp: dict) -> str:
    return f"{camp['lon_min']},{camp['lat_min']},{camp['lon_max']},{camp['lat_max']}"


def _wms_getmap_url(prod: dict, camp: dict, width: int = 1024, height: int = 768) -> str:
    """Per-campaign WMS GetMap URL the mashup can drop straight into Leaflet."""
    qs = {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetMap",
        "layers": prod["layer_id"],
        "styles": "",
        "format": prod["format"],
        "transparent": "true",
        "crs": "EPSG:4326",
        "bbox": _bbox_str(camp),
        "width": width,
        "height": height,
    }
    return f"{prod['wms_base']}?{urlencode(qs)}"


def main() -> None:
    camps = load_campaigns_from_samples()
    rows = []
    for camp in camps:
        for prod in PRODUCTS:
            rows.append({
                "campaign_code":     camp["name"],
                "campaign_area":     camp["area"],
                "campaign_lat_min":  camp["lat_min"],
                "campaign_lat_max":  camp["lat_max"],
                "campaign_lon_min":  camp["lon_min"],
                "campaign_lon_max":  camp["lon_max"],
                "campaign_date_min": camp["date_min"],
                "campaign_date_max": camp["date_max"],
                "source":             prod["source"],
                "data_type":          "raster_manifest",
                "dataset_id":         prod["layer_id"],
                "theme":              prod["theme"],
                "title":              prod["title"],
                "perturbation_class": prod["perturbation_class"],
                "protocol":           "WMS",
                "format":             prod["format"],
                "wms_base":           prod["wms_base"],
                "capabilities_url":   prod["capabilities_url"],
                "wms_getmap_url":     _wms_getmap_url(prod, camp),
                "campaign_bbox":      _bbox_str(camp),
                "notes":              prod["notes"],
            })
    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"wrote {len(df)} rows -> {OUT}")
    print("themes:", df["theme"].value_counts().to_dict())


if __name__ == "__main__":
    main()
