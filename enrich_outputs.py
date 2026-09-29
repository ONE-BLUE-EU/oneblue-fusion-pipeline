"""Post-build enrichment of dkan_resources CSVs.

Adds two pieces of context that turn the raw external data into a fused
mashup-ready product:

1. `perturbation_class` on chemistry.csv and human_activities.csv, with
   values "natural", "anthropic", "infrastructure", "protection" or
   "context". The class is derived from the chemical group / matrix
   (chemistry) and from the EMODnet HA layer label (human_activities).

2. Suspect-list linkage on chemistry.csv: a row is matched against the
   ONE-BLUE suspect list (NORMAN database extract) by exact CAS match
    across CAS1..CAS11, with a normalized exact-name fallback. Ambiguous
    and conflicting identifiers remain unresolved. Unique matches use
    the workbook chemical groups and preserve the original provider group.
    Matched rows get
   `suspect_norman_id`, `suspect_name`, `suspect_chem_group`,
   `suspect_chem_subgroup`, `suspect_chem_subsubgroup`, `suspect_formula`
   and `suspect_inchikey` columns.

The main chemistry writer and publisher also invoke this enrichment.
Climate is fetched separately by fetch_climate.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
SRC = ROOT / "dkan_resources"
SUSPECT_XLSX = ROOT / "ONE-BLUE suspect list_v1.1.xlsx"


# ---------------------------------------------------------------------------
# Perturbation class mapping
# ---------------------------------------------------------------------------

# Chemistry: chemical_group (and matrix for nutrients) -> class
_CHEM_GROUP_CLASS = {
    "nutrient":                 "natural",      # eutrophication signal
    "major element":            "natural",      # bulk seawater chemistry
    "heavy metal":              "anthropic",
    "PAH":                      "anthropic",
    "PCB":                      "anthropic",
    "organochlorine pesticide": "anthropic",
    "pesticide":                "anthropic",
    "organotin":                "anthropic",
    "VOC":                      "anthropic",
    "hydrocarbon":              "anthropic",
    "radionuclide":             "anthropic",
    "other":                    "context",
}

# Human activities: layer_label -> class
# Per user direction: ports and wind farms count as anthropic (they need not
# pollute to be anthropic pressure / footprint); MPAs and Natura 2000 are
# context, used as comparison surface rather than perturbation.
_HA_LAYER_CLASS = {
    "Treatment plants":         "anthropic",
    "Discharge points":         "anthropic",
    "Dredging sites":           "anthropic",
    "Dredge spoil dumps":       "anthropic",
    "Dumped munitions":         "anthropic",
    "Oil & gas platforms":      "anthropic",
    "Active O&G licences":      "anthropic",
    "Offshore pipelines":       "anthropic",
    "Finfish farms":            "anthropic",
    "Military areas":           "anthropic",
    "Port locations":           "anthropic",
    "Wind farms (points)":      "anthropic",
    "Natura 2000 MPAs":         "context",
    "Marine protected areas":   "context",
}


def _classify_chem(row) -> str:
    grp = row.get("chemical_group")
    matrix = row.get("matrix")
    # EUT_* eutrophication stations: water_column matrix with no chem group
    # grp may be NaN *or* an empty string depending on how the CSV was written
    if not grp or pd.isna(grp):
        if matrix == "water_column":
            return "natural"
        return "context"
    return _CHEM_GROUP_CLASS.get(grp, "context")


def _classify_ha(row) -> str:
    return _HA_LAYER_CLASS.get(row.get("layer_label"), "context")


# ---------------------------------------------------------------------------
# Suspect-list index
# ---------------------------------------------------------------------------

_NAME_NORM_RE = re.compile(r"\s+")


def _norm_name(s) -> str:
    if not isinstance(s, str):
        return ""
    return _NAME_NORM_RE.sub(" ", s.strip().lower())


def _norm_cas(s) -> str:
    if s is None or pd.isna(s):
        return ""
    return str(s).strip().replace(" ", "")


def build_suspect_index(path: Path) -> tuple[dict, dict]:
    """Index all candidate identities without silently resolving ambiguous keys."""
    df = pd.read_excel(path, sheet_name="ONE-BLUE suspect list")
    cas_cols = [c for c in df.columns if re.fullmatch(r"CAS\d+", c)]
    cas_idx: dict[str, dict] = {}
    name_idx: dict[str, dict] = {}
    for _, row in df.iterrows():
        meta = {
            "suspect_norman_id":        row.get("NORMAN_ID"),
            "suspect_name":             row.get("Name"),
            "suspect_chem_group":       row.get("Chemical Group"),
            "suspect_chem_subgroup":    row.get("Chemical SubGroup"),
            "suspect_chem_subsubgroup": row.get("Chemical Subsubgroup"),
            "suspect_formula":          row.get("Formula"),
            "suspect_inchikey":         row.get("Std InChIKey"),
        }
        for col in cas_cols:
            key = _norm_cas(row.get(col))
            if key:
                candidates = cas_idx.setdefault(key, [])
                if not any(candidate["suspect_norman_id"] == meta["suspect_norman_id"] for candidate in candidates):
                    candidates.append(meta)
        nkey = _norm_name(row.get("Name"))
        if nkey:
            name_idx.setdefault(nkey, []).append(meta)
    return cas_idx, name_idx


def _suspect_lookup(cas_idx, name_idx, cas, name) -> dict | None:
    match = _suspect_match(cas_idx, name_idx, cas, name)
    return match if match["suspect_match_status"] == "matched" else None


SUSPECT_COLUMNS = [
    "suspect_norman_id", "suspect_name", "suspect_chem_group", "suspect_chem_subgroup",
    "suspect_chem_subsubgroup", "suspect_formula", "suspect_inchikey",
]


def _suspect_match(cas_idx, name_idx, cas, name) -> dict:
    cas_key, name_key = _norm_cas(cas), _norm_name(name)
    cas_matches = cas_idx.get(cas_key, [])
    name_matches = name_idx.get(name_key, [])
    candidates = cas_matches or name_matches
    method = "cas" if cas_matches else "exact_normalized_name" if name_matches else "none"
    status = "unmatched" if cas_key or name_key else "not_applicable"
    if cas_matches and name_matches:
        name_ids = {item["suspect_norman_id"] for item in name_matches}
        intersection = [item for item in cas_matches if item["suspect_norman_id"] in name_ids]
        if not intersection:
            status = "conflicting_identifiers"
        else:
            candidates = intersection
            method = "cas_and_name"
    if candidates and status != "conflicting_identifiers":
        status = "matched" if len(candidates) == 1 else "ambiguous"
    result = {key: None for key in SUSPECT_COLUMNS}
    if status == "matched":
        result.update(candidates[0])
    result.update(suspect_match_status=status, suspect_match_method=method,
                  suspect_candidate_ids="|".join(str(item["suspect_norman_id"]) for item in candidates))
    return result


def enrich_chemistry(frame: pd.DataFrame, suspect_path: Path = SUSPECT_XLSX) -> pd.DataFrame:
    result = frame.copy()
    cas_index, name_index = build_suspect_index(suspect_path)
    matches = [_suspect_match(cas_index, name_index, row.get("cas_number"), row.get("compound_name"))
               for row in result.to_dict("records")]
    fields = SUSPECT_COLUMNS + ["suspect_match_status", "suspect_match_method", "suspect_candidate_ids"]
    for field in fields:
        result[field] = pd.Series([match[field] for match in matches], index=result.index, dtype=object)
    result["in_suspect_list"] = result["suspect_match_status"].eq("matched")
    if "provider_chemical_group" not in result:
        result["provider_chemical_group"] = result.get("chemical_group", pd.Series(index=result.index, dtype=object))
    result["chemical_group"] = result["suspect_chem_group"].where(result["in_suspect_list"], result["provider_chemical_group"])
    result["classification_source"] = result["in_suspect_list"].map({True: "ONE-BLUE suspect list", False: "provider_or_unclassified"})
    result["suspect_list_file"] = suspect_path.name
    result["perturbation_class"] = pd.Series([_classify_chem(row) for row in result.to_dict("records")], index=result.index, dtype=object)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Chemistry
    chem_path = SRC / "chemistry.csv"
    print(f"reading {chem_path}")
    chem = pd.read_csv(chem_path, low_memory=False)

    chem = enrich_chemistry(chem)

    matched = int(chem["in_suspect_list"].sum())
    distinct = chem.loc[chem["in_suspect_list"], "suspect_norman_id"].nunique()
    print(f"  chemistry: {matched} rows matched suspect list ({distinct} distinct NORMAN compounds)")
    print("  perturbation_class:")
    for k, v in chem["perturbation_class"].value_counts().items():
        print(f"    {k:15s} {v}")

    chem.to_csv(chem_path, index=False, encoding="utf-8-sig")
    print(f"wrote {chem_path}")

    # Human activities
    ha_path = SRC / "human_activities.csv"
    print(f"reading {ha_path}")
    ha = pd.read_csv(ha_path, low_memory=False)
    ha["perturbation_class"] = ha.apply(_classify_ha, axis=1)
    print("  perturbation_class:")
    for k, v in ha["perturbation_class"].value_counts().items():
        print(f"    {k:15s} {v}")
    ha.to_csv(ha_path, index=False, encoding="utf-8-sig")
    print(f"wrote {ha_path}")


if __name__ == "__main__":
    main()
