#!/usr/bin/env python3
"""Merge fresh compliance counts into the live Tier-3 summary.

The counts refresh rebuilds summary.json from the local ~/hospital-ledger/
working copy, which is STALE (pre-Tier-3, only 26 hospitals in the price
index). This script merges the fresh compliance fields from the local build
into the LIVE Tier-3 summary from R2, preserving all price-related fields.

Usage: merge_summary.py <fresh_local_summary.json> <live_r2_summary.json> <output.json>

Fields taken from FRESH (compliance counts the refresh actually updates):
  generated_at, total_facilities, cms_required_total, compliant,
  compliance_pct, enforcement_actions_total

Fields PRESERVED from LIVE (Tier-3 price data):
  standardized_price_hospitals, standardized_price_index_hospitals,
  standardized_price_rows, cpt_indexed_hospitals, cpt_indexed_rows,
  zero_price_index_entries

All other fields: taken from fresh (compliance context).
"""
import json
import sys

# Fields the counts refresh owns (compliance data)
FRESH_FIELDS = {
    "generated_at",
    "total_facilities",
    "cms_required_total",
    "compliant",
    "compliance_pct",
    "enforcement_actions_total",
}

# Fields that belong to the Tier-3 pipeline (price data) — never overwrite
PRESERVE_FIELDS = {
    "standardized_price_hospitals",
    "standardized_price_index_hospitals",
    "standardized_price_rows",
    "cpt_indexed_hospitals",
    "cpt_indexed_rows",
    "zero_price_index_entries",
}


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <fresh.json> <live.json> <output.json>", file=sys.stderr)
        sys.exit(2)

    fresh = json.load(open(sys.argv[1]))
    live = json.load(open(sys.argv[2]))

    merged = {}
    # Start with fresh (compliance context)
    merged.update(fresh)
    # Overlay preserved Tier-3 price fields from live
    for field in PRESERVE_FIELDS:
        if field in live:
            merged[field] = live[field]
        elif field in merged:
            del merged[field]

    # Safety: if live has price data but fresh doesn't, ensure we didn't lose it
    # If live is missing price data (shouldn't happen), warn but proceed
    missing = [f for f in PRESERVE_FIELDS if f not in merged]
    if missing:
        print(f"WARNING: live summary missing Tier-3 fields: {missing}", file=sys.stderr)

    # Safety: the merged standardized count must never be the stale 26
    # If live had it and fresh overwrote, that's a bug
    if merged.get("standardized_price_hospitals") == 26 and live.get("standardized_price_hospitals", 0) > 100:
        print("ERROR: merge would regress standardized_price_hospitals to 26", file=sys.stderr)
        sys.exit(1)

    json.dump(merged, open(sys.argv[3], "w"), indent=2)
    print(f"Merged: fresh compliance + live Tier-3 price fields -> {sys.argv[3]}")
    print(f"  standardized_price_hospitals: {merged.get('standardized_price_hospitals')}")
    print(f"  compliant: {merged.get('compliant')}/{merged.get('cms_required_total')}")


if __name__ == "__main__":
    main()
