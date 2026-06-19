"""Post-build enrichment of dkan_resources CSVs.

Adds two pieces of context that turn the raw external data into a fused
mashup-ready product:

1. `perturbation_class` on chemistry.csv and human_activities.csv, with
   values "natural", "anthropic", "infrastructure", "protection" or
   "context". The class is derived from the chemical group / matrix
   (chemistry) and from the EMODnet HA layer label (human_activities).

2. Suspect-list linkage on chemistry.csv: a row is matched against the
   ONE-BLUE suspect list (NORMAN database extract) by exact CAS match
   across CAS1..CAS11, with a normalised-name fallback. Matched rows get
   `suspect_norman_id`, `suspect_name`, `suspect_chem_group`,
   `suspect_chem_subgroup`, `suspect_chem_subsubgroup`, `suspect_formula`
   and `suspect_inchikey` columns.

Climatic parameters (sunshine, wind speed, UV) are not yet present in
the harvested data; a dedicated fetcher is proposed in the report.
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

_NAME_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_name(s) -> str:
    if not isinstance(s, str):
        return ""
    return _NAME_NORM_RE.sub("", s.lower())


def _norm_cas(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip().replace(" ", "")


def build_suspect_index(path: Path) -> tuple[dict, dict]:
    """Return (cas_index, name_index) mapping to suspect metadata dict."""
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
                cas_idx.setdefault(key, meta)
        nkey = _norm_name(row.get("Name"))
        if nkey:
            name_idx.setdefault(nkey, meta)
    return cas_idx, name_idx


def _suspect_lookup(cas_idx, name_idx, cas, name) -> dict | None:
    k = _norm_cas(cas)
    if k and k in cas_idx:
        return cas_idx[k]
    nk = _norm_name(name)
    if nk and nk in name_idx:
        return name_idx[nk]
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Chemistry
    chem_path = SRC / "chemistry.csv"
    print(f"reading {chem_path}")
    chem = pd.read_csv(chem_path, low_memory=False)

    chem["perturbation_class"] = chem.apply(_classify_chem, axis=1)

    print(f"building suspect-list index from {SUSPECT_XLSX.name}")
    cas_idx, name_idx = build_suspect_index(SUSPECT_XLSX)
    print(f"  {len(cas_idx)} CAS keys, {len(name_idx)} name keys")

    susp_cols = [
        "suspect_norman_id", "suspect_name",
        "suspect_chem_group", "suspect_chem_subgroup", "suspect_chem_subsubgroup",
        "suspect_formula", "suspect_inchikey",
    ]
    out_rows = []
    for cas, name in zip(chem["cas_number"], chem["compound_name"]):
        m = _suspect_lookup(cas_idx, name_idx, cas, name)
        if m:
            out_rows.append([m[c] for c in susp_cols])
        else:
            out_rows.append([None] * len(susp_cols))
    susp_df = pd.DataFrame(out_rows, columns=susp_cols, index=chem.index)
    for c in susp_cols:
        chem[c] = susp_df[c]
    chem["in_suspect_list"] = chem["suspect_norman_id"].notna()

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
