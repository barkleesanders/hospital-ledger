"""Counts for the registry and price index, joined by unique CCN."""

import json
from pathlib import Path
import re

CCN_RE = re.compile(r"^[0-9A-Z]{6}$")
COHORT_DEFINITIONS = {
    "required_live_price_hospitals": "Unique CMS-required hospitals with a live MRF and a matching price-index CCN with n > 0.",
    "required_live_zero_price_hospitals": "Unique CMS-required hospitals with a live MRF and a matching price-index CCN with n = 0.",
    "required_live_unindexed_hospitals": "Unique CMS-required hospitals with a live MRF and no matching price-index CCN.",
    "price_hospitals_outside_required_live": "Unique price-index CCNs with n > 0 outside the CMS-required/live registry cohort.",
}
COHORT_FIELDS = tuple(COHORT_DEFINITIONS)


def rows_by_ccn(rows, source):
    if not isinstance(rows, list):
        raise ValueError(f"{source}: expected a list")
    result = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{source}[{position}]: expected an object")
        ccn = row.get("ccn")
        if not isinstance(ccn, str) or not CCN_RE.fullmatch(ccn):
            raise ValueError(f"{source}[{position}]: invalid CCN {ccn!r}")
        if ccn in result:
            raise ValueError(f"{source}: duplicate CCN {ccn}")
        result[ccn] = row
    return result


def price_index_metrics(hospitals, entries):
    registry = rows_by_ccn(hospitals, "hospitals")
    index = rows_by_ccn(entries, "prices/index")
    for ccn, row in registry.items():
        for field in ("required", "has_live_mrf"):
            if not isinstance(row.get(field), bool):
                raise ValueError(f"hospitals {ccn}: {field} must be a boolean")
    for ccn, row in index.items():
        for field in ("n", "cpt_indexed"):
            value = row.get(field, 0) if field == "cpt_indexed" else row.get(field)
            if type(value) is not int or value < 0:
                raise ValueError(f"prices/index {ccn}: {field} must be a nonnegative integer (got {value!r})")
    required_live = {ccn for ccn, row in registry.items()
                     if row["required"] and row["has_live_mrf"]}
    positive = {ccn for ccn, row in index.items() if row["n"] > 0}
    zero = set(index) - positive
    return {
        "standardized_price_index_hospitals": len(index),
        "standardized_price_hospitals": len(positive),
        "standardized_price_rows": sum(row["n"] for row in index.values()),
        "cpt_indexed_hospitals": sum(row.get("cpt_indexed", 0) > 0 for row in index.values()),
        "cpt_indexed_rows": sum(row.get("cpt_indexed", 0) for row in index.values()),
        "zero_price_index_entries": len(zero),
        "required_live_price_hospitals": len(required_live & positive),
        "required_live_zero_price_hospitals": len(required_live & zero),
        "required_live_unindexed_hospitals": len(required_live - set(index)),
        "price_hospitals_outside_required_live": len(positive - required_live),
    }


def refresh_summary_metrics(public_data, entries=None):
    """Keep an existing summary in sync after an index writer runs.

    Isolated wave workspaces intentionally have no summary or registry yet.
    Their summary is produced by build_site_data after the wave index merges.
    """
    public_data = Path(public_data)
    summary_path = public_data / "summary.json"
    if not summary_path.exists():
        return
    summary = json.loads(summary_path.read_text())
    hospitals = json.loads((public_data / "hospitals.json").read_text())
    if entries is None:
        entries = json.loads((public_data / "prices/index.json").read_text())["hospitals"]
    summary.update(price_index_metrics(hospitals, entries))
    summary.setdefault("count_definitions", {}).update(COHORT_DEFINITIONS)
    temporary = summary_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, separators=(",", ":")))
    temporary.replace(summary_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Refresh summary metrics from a contract artifact tree.")
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    entries = json.loads((args.root / "prices/index.json").read_text())["hospitals"]
    refresh_summary_metrics(args.root / "meta", entries)
