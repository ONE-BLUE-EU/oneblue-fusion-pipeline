"""Retrieve contemporary contaminant observations from additional providers."""
from __future__ import annotations

import hashlib
import json
import math
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

from query_external_data import campaign_date_window


DOME_API = "https://dome.ices.dk/api/Download"
DOME_MATRICES = {"water": "CW", "sediment": "CS", "biota": "CF"}
EMPODAT_API = "https://www.norman-network.com/nds/api/empodat"
VOCAB_API = "https://vocab.ices.dk/services/api"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def request_json(method: str, url: str, cache: Path, payload: dict | None = None):
    key = hashlib.sha256(json.dumps([method, url, payload], sort_keys=True).encode()).hexdigest()
    path = cache / (key + ".json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    with requests.request(method, url, json=payload, timeout=(30, 180), stream=True) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(1024 * 1024):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise RuntimeError("response exceeded 64 MiB; not imported")
            chunks.append(chunk)
    data = json.loads(b"".join(chunks))
    if isinstance(data, dict) and "Fault" in data:
        raise RuntimeError(f"EMPODAT API fault: {data['Fault']}")
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    return data


def dome_query(campaign: dict, page: int = 1, page_size: int = 1000) -> dict:
    start, end = campaign_date_window(campaign)
    return {
        "startDate": f"{start}T00:00:00Z",
        "endDate": f"{end}T23:59:59Z",
        "minLat": campaign["lat_min"], "maxLat": campaign["lat_max"],
        "minLon": campaign["lon_min"], "maxLon": campaign["lon_max"],
        "page": page, "pageSize": page_size,
    }


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def in_scope(campaign: dict, latitude, longitude, date) -> bool:
    latitude, longitude = _number(latitude), _number(longitude)
    if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return False
    stamp = pd.to_datetime(date, format="%Y-%m-%d", errors="coerce")
    if pd.isna(stamp):
        return False
    start, end = campaign_date_window(campaign)
    return (start <= stamp.strftime("%Y-%m-%d") <= end
            and campaign["lat_min"] <= latitude <= campaign["lat_max"]
            and campaign["lon_min"] <= longitude <= campaign["lon_max"])


def dome_records(campaign: dict, matrix: str, cache: Path, max_pages: int = 200):
    url = f"{DOME_API}/Get{DOME_MATRICES[matrix]}data"
    rows, seen_pages, expected = [], set(), None
    for page in range(1, max_pages + 1):
        data = request_json("POST", url, cache, dome_query(campaign, page))
        if not isinstance(data, dict) or not isinstance(data.get("data"), list) or "totalCount" not in data:
            raise ValueError("Unexpected DOME response schema")
        total, batch = int(data["totalCount"]), data["data"]
        if expected is not None and total != expected:
            raise RuntimeError("DOME total changed during pagination; refresh cache and retry")
        expected = total
        if total == 0 and not batch:
            return [], url
        fingerprint = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
        if not batch or fingerprint in seen_pages:
            raise RuntimeError("DOME pagination stopped before the reported total")
        seen_pages.add(fingerprint)
        rows.extend(batch)
        if len(rows) == total:
            return rows, url
        if len(rows) > total:
            raise RuntimeError("DOME returned more records than reported")
    raise RuntimeError("DOME page budget exhausted; request not imported")


def dome_parameter(code: str, cache: Path) -> dict:
    url = f"{VOCAB_API}/CodeDetail/PARAM/{quote(code, safe='')}"
    data = request_json("GET", url, cache)
    if not isinstance(data, dict) or str(data.get("key", "")).casefold() != code.casefold() or not data.get("description"):
        raise ValueError(f"No ICES vocabulary identity for {code}")
    groups, identifiers = [], []
    for relation in data.get("parentRelation", []) + data.get("childRelation", []):
        code_type = relation.get("codeType", {}).get("key", "")
        related = relation.get("code", {})
        if code_type.lower() == "pargroup":
            groups.append(related.get("description", ""))
        if code_type.startswith("CAS"):
            identifiers.append(related.get("key", ""))
    return {"compound_name": data["description"],
            "cas_number": identifiers[0] if len(set(identifiers)) == 1 else "",
            "chemical_group": "|".join(groups), "vocabulary_url": url}


def normalize_dome(record: dict, campaign: dict, matrix: str, identity: dict, url: str) -> dict:
    from build_campaign_datasources import _row
    flag = str(record.get("QFLAG") or "").strip()
    qualifier = {"": "=", "Q": "<LOQ", "D": "<LOD", "<": "<", ">": ">"}.get(flag, "unknown")
    reported = _number(record.get("Value"))
    latitude, longitude = float(record["Latitude"]), float(record["Longitude"])
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:32]
    depth_top, depth_bottom = _number(record.get("DEPHU")), _number(record.get("DEPHL"))
    depth = depth_top if matrix == "water" and depth_bottom in (None, depth_top) else None
    context = identity.get("chemical_group") in {"Physical measurements", "Major organic constituents"}
    return _row(
        campaign, source="ICES DOME", data_type=f"contaminants_{matrix}",
        dataset_id=f"DOME_{DOME_MATRICES[matrix]}", feature_id=f"{record.get('tblParamID')}:{digest}",
        provider_feature_id=str(record.get("tblParamID", "")),
        time=record["Date"], lat=latitude, lon=longitude, depth_m=depth,
        geom_wkt=f"POINT({longitude} {latitude})", matrix=matrix,
        matrix_detail=record.get("MATRX"), station=record.get("STATN"),
        **identity, parameter_name=record["PARAM"],
        parameter_value=reported if qualifier == "=" else None,
        parameter_reported_value=reported, parameter_unit=record.get("MUNIT"),
        concentration_qualifier=qualifier, detection_limit=_number(record.get("DETLI")),
        quantification_limit=_number(record.get("LMQNT")),
        provider_quality_flag=record.get("VFLAG"), measurement_basis=record.get("BASIS"),
        provider_quality_status={"A": "acceptable_by_originator", "S": "suspect_by_originator"}.get(
            record.get("VFLAG"), "other_or_unverified"),
        measurement_role="supporting_parameter" if context else "contaminant_result",
        species=record.get("Species"), sample_identifier=str(record.get("tblSampleID", "")),
        license="CC BY 4.0", source_url=url,
        extra_json=json.dumps({"provider_record": record, "query": dome_query(campaign)}, ensure_ascii=True),
    )


def fetch_dome(campaigns: list[dict], cache: Path) -> tuple[list[dict], list[dict]]:
    rows, reports, identities = [], [], {}
    for campaign in campaigns:
        for matrix in DOME_MATRICES:
            report = {"source": "ICES DOME", "campaign_id": campaign["campaign_id"],
                      "matrix": matrix, "query": dome_query(campaign), "status": "error", "retained": 0}
            try:
                records, url = dome_records(campaign, matrix, cache)
                selected = [record for record in records if in_scope(
                    campaign, record.get("Latitude"), record.get("Longitude"), record.get("Date"))]
                normalized = []
                for record in selected:
                    code = record["PARAM"]
                    if code not in identities:
                        identities[code] = dome_parameter(code, cache)
                    normalized.append(normalize_dome(record, campaign, matrix, identities[code], url))
                rows.extend(normalized)
                report.update(status="complete_query", retrieved=len(records), retained=len(normalized),
                              excluded_geotemporal=len(records) - len(selected), url=url)
            except (requests.RequestException, ValueError, KeyError, RuntimeError) as error:
                report["error"] = str(error)
            reports.append(report)
            print(f"DOME {campaign['name'][:32]} {matrix}: {report['status']}, {report['retained']} rows", flush=True)
    return rows, reports


def normalize_empodat(record: dict, campaign: dict, url: str) -> dict:
    from build_campaign_datasources import _row
    substance = record.get("Substance") or {}
    concentration = record.get("Concentration") or {}
    if not isinstance(concentration, dict):
        raise ValueError("Unexpected EMPODAT concentration structure")
    qualifier = {"Individual Value": "=", "Less than LoQ": "<LOQ", "Less than LoD": "<LOD"}.get(
        record.get("Individual concentration"), "unknown")
    reported = _number(concentration.get("Value"))
    latitude, longitude = float(record["Latitude"]), float(record["Longitude"])
    matrix_detail = str(record.get("Sample matrix") or "")
    matrix = next((canonical for prefix, canonical in [
        ("surface water", "water"), ("ground water", "groundwater"),
        ("waste water", "wastewater"), ("sediment", "sediment"), ("biota", "biota"),
    ] if matrix_detail.lower().startswith(prefix)), "other")
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:32]
    return _row(
        campaign, source="NORMAN EMPODAT", data_type=f"contaminants_{matrix}",
        dataset_id="EMPODAT", feature_id=f"{record['id']}:{digest}", provider_feature_id=str(record["id"]),
        provider_norman_id=substance.get("NORMAN SusDat ID"),
        compound_name=substance.get("Name"), cas_number=substance.get("CAS RN"),
        provider_inchikey=substance.get("InChIKey"), chemical_group="",
        time=record["Sampling date"], lat=latitude, lon=longitude,
        depth_m=_number(record.get("Depth [m]")), geom_wkt=f"POINT({longitude} {latitude})",
        matrix=matrix, matrix_detail=matrix_detail, station=record.get("Station name"),
        parameter_name=substance.get("Name"), parameter_unit=concentration.get("Unit"),
        parameter_value=reported if qualifier == "=" else None,
        parameter_reported_value=reported, concentration_qualifier=qualifier,
        measurement_basis=record.get("Basis of measurement"),
        sample_identifier=record.get("Sample code"),
        source_url=url, license="NORMAN terms; redistribution rights not verified",
        extra_json=json.dumps({"provider_record": record}, ensure_ascii=True),
    )


def fetch_empodat(campaigns: list[dict], cas_numbers: list[str], cache: Path,
                  max_pages: int = 5) -> tuple[list[dict], list[dict]]:
    rows, reports = [], []
    for cas in dict.fromkeys(cas_numbers):
        report = {"source": "NORMAN EMPODAT", "cas_number": cas, "status": "partial_page_budget",
                  "retrieved": 0, "retained": 0, "pages": 0, "missing_coordinates": 0,
                  "unusable_dates": 0, "geotemporal_policy": "full campaign bounds and padded dates"}
        expected, seen_pages, selected = None, set(), []
        try:
            for page in range(1, max_pages + 1):
                url = f"{EMPODAT_API}/casrn/{quote(cas, safe='')}/{page}/JSON"
                data = request_json("GET", url, cache)
                if not isinstance(data, dict) or not isinstance(data.get("Data"), list):
                    raise ValueError("Unexpected EMPODAT response schema")
                total, batch = int(data["Total records"]), data["Data"]
                if expected is not None and total != expected:
                    raise RuntimeError("EMPODAT total changed during pagination; refresh cache and retry")
                expected = total
                report["provider_total"] = total
                report["url"] = url
                fingerprint = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
                if total == 0 and not batch:
                    report["status"] = "complete_substance_query"
                    break
                if not batch or fingerprint in seen_pages or int(data.get("Show page", page)) != page:
                    raise RuntimeError("EMPODAT pagination stopped or repeated before completion")
                seen_pages.add(fingerprint)
                report["pages"] = page
                report["retrieved"] += len(batch)
                for record in batch:
                    latitude, longitude = _number(record.get("Latitude")), _number(record.get("Longitude"))
                    if latitude is None or longitude is None:
                        report["missing_coordinates"] += 1
                        continue
                    stamp = pd.to_datetime(record.get("Sampling date"), format="%Y-%m-%d", errors="coerce")
                    if pd.isna(stamp):
                        report["unusable_dates"] += 1
                        continue
                    for campaign in campaigns:
                        if in_scope(campaign, latitude, longitude, record.get("Sampling date")):
                            selected.append(normalize_empodat(record, campaign, url))
                print(f"EMPODAT {cas} page {page}: {report['retrieved']}/{total}, {len(selected)} campaign rows", flush=True)
                if report["retrieved"] == total:
                    report["status"] = "complete_substance_query"
                    break
                if report["retrieved"] > total:
                    raise RuntimeError("EMPODAT returned more records than reported")
            rows.extend(selected)
            report["retained"] = len(selected)
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as error:
            report.update(status="error", error=str(error))
        reports.append(report)
    return rows, reports


def merge_chemistry(existing: pd.DataFrame, rows: list[dict], replaced_sources: set[str]) -> pd.DataFrame:
    from enrich_outputs import enrich_chemistry
    retained = existing[~existing["source"].isin(replaced_sources)] if "source" in existing else existing
    incoming = pd.DataFrame(rows)
    if not incoming.empty:
        incoming["provider_chemical_group"] = incoming.get("chemical_group", "")
    result = pd.concat([retained, incoming], ignore_index=True)
    if not result.empty:
        result = result.drop_duplicates(["campaign_code", "source", "dataset_id", "feature_id"])
    return enrich_chemistry(result)


def main() -> None:
    from build_campaign_datasources import CHEM_VALUE_COLS, COMMON_COLS, TRAILING_COLS
    from query_external_data import load_campaigns_from_samples
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).parent / "dkan_resources")
    parser.add_argument("--providers", nargs="+", choices=["dome", "empodat"], default=["dome"])
    parser.add_argument("--empodat-cas", nargs="+", default=[])
    parser.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args()
    if args.max_pages < 1 or ("empodat" in args.providers and not args.empodat_cas):
        parser.error("EMPODAT requires --empodat-cas and --max-pages must be positive")
    campaigns = load_campaigns_from_samples()
    root, rows, reports, sources = args.source_dir, [], [], set()
    if "dome" in args.providers:
        retrieved, coverage = fetch_dome(campaigns, root / "chemistry_cache")
        rows.extend(retrieved)
        reports.extend(coverage)
        sources.add("ICES DOME")
    if "empodat" in args.providers:
        retrieved, coverage = fetch_empodat(campaigns, args.empodat_cas, root / "chemistry_cache", args.max_pages)
        rows.extend(retrieved)
        reports.extend(coverage)
        sources.add("NORMAN EMPODAT")
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "chemistry_coverage.json"
    previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    retained_reports = [entry for entry in previous.get("queries", []) if entry.get("source") not in sources]
    report_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_policy": "Cached API responses reused; remove chemistry_cache for fresh requests",
        "empodat_scope": "Only explicitly requested substances; not an exhaustive database search",
        "queries": retained_reports + reports,
    }, indent=2), encoding="utf-8")
    if any(report["status"] not in {"complete_query", "complete_substance_query"} for report in reports):
        raise RuntimeError(f"Incomplete provider requests recorded in {report_path}; chemistry.csv left unchanged")
    path = root / "chemistry.csv"
    existing = pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()
    if "NORMAN EMPODAT" in sources and "source" in existing:
        refresh = existing["source"].eq("NORMAN EMPODAT") & existing["cas_number"].isin(args.empodat_cas)
        existing = existing.loc[~refresh].copy()
    frame = merge_chemistry(existing, rows, sources - {"NORMAN EMPODAT"})
    columns = list(dict.fromkeys(COMMON_COLS + CHEM_VALUE_COLS + list(frame.columns) + TRAILING_COLS))
    frame = frame.reindex(columns=columns)
    temporary = path.with_suffix(".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)
    print(f"Wrote {len(frame)} chemistry rows; coverage: {report_path}")


if __name__ == "__main__":
    main()