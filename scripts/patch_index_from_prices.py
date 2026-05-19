#!/usr/bin/env python3
"""Surgical MERGE patch for public/data/prices/index.json.

Reads the existing index, then for each CCN in CCNS (env var, comma/space-sep)
or CCNS_FILE, reads public/data/prices/{ccn}.json, computes its summary, and
adds/updates that CCN's entry. Does NOT walk all per-CCN files — only the
specified ones. This avoids the bug where a partial set of per-CCN files on
disk could cause a full-rebuild to drop hospitals that exist in the index but
whose per-CCN files live only in R2.

Does NOT touch cpt-index.json or side-cars — those need the full slim run on a
machine with enough RAM.

Usage:
  CCNS="110087,330023,360134,360179,390164" .venv/bin/python scripts/patch_index_from_prices.py
"""
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRICES_DIR = ROOT / "public" / "data" / "prices"
INDEX_PATH = PRICES_DIR / "index.json"
CPT_INDEX_TYPES = {"CPT", "HCPCS"}


def selected_ccns() -> set[str]:
    ccns: set[str] = set()
    for token in os.environ.get("CCNS", "").replace(",", " ").split():
        if token.strip():
            ccns.add(token.strip())
    ccns_file = os.environ.get("CCNS_FILE", "").strip()
    if ccns_file and Path(ccns_file).exists():
        with open(ccns_file) as f:
            for line in f:
                if line.strip():
                    ccns.add(line.strip())
    return ccns


def summary_from_prices_file(path: Path):
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    ccn = path.stem
    if ccn == "index":
        return None
    items = data.get("items") or []
    counts = data.get("counts") or {}
    by_type = counts.get("by_type") or {}
    cpt_indexed = sum(1 for it in items if it.get("type") in CPT_INDEX_TYPES)
    summary = {
        "ccn": ccn,
        "n": len(items),
        "name": data.get("hospital_name", ""),
        "counts": by_type,
        "cpt_indexed": cpt_indexed,
    }
    compliance = data.get("compliance")
    if compliance:
        summary["compliance"] = compliance
    return summary


def main() -> int:
    if not INDEX_PATH.exists():
        print(f"ERROR: {INDEX_PATH} does not exist", file=sys.stderr)
        return 1
    target = selected_ccns()
    if not target:
        print("ERROR: no CCNS or CCNS_FILE specified", file=sys.stderr)
        print("Usage: CCNS='110087,330023' .venv/bin/python scripts/patch_index_from_prices.py", file=sys.stderr)
        return 2

    with INDEX_PATH.open() as f:
        existing = json.load(f)
    existing_by_ccn = {h["ccn"]: h for h in existing.get("hospitals", [])}

    n_added = 0
    n_updated_with_rows = 0
    n_updated_to_zero = 0
    n_missing_file = 0
    n_no_change = 0
    diffs = []
    for ccn in sorted(target):
        path = PRICES_DIR / f"{ccn}.json"
        if not path.exists():
            n_missing_file += 1
            print(f"  {ccn}: SKIP (no per-CCN file at {path})")
            continue
        summary = summary_from_prices_file(path)
        if not summary:
            n_missing_file += 1
            print(f"  {ccn}: SKIP (file unreadable)")
            continue
        prev = existing_by_ccn.get(ccn)
        new_n = summary.get("n", 0)
        prev_n = (prev or {}).get("n", 0)
        if prev is None:
            n_added += 1
            diffs.append(f"  + {ccn} ADDED  n={new_n} {summary['name'][:40]}")
        elif prev_n == 0 and new_n > 0:
            n_updated_with_rows += 1
            diffs.append(f"  ↑ {ccn} 0 → n={new_n} {summary['name'][:40]}")
        elif prev_n > 0 and new_n == 0:
            n_updated_to_zero += 1
            diffs.append(f"  ↓ {ccn} n={prev_n} → 0 {summary['name'][:40]}")
        elif prev != summary:
            diffs.append(f"  ~ {ccn} n={prev_n}→{new_n} {summary['name'][:40]}")
        else:
            n_no_change += 1
            continue
        existing_by_ccn[ccn] = summary

    out = {"hospitals": sorted(existing_by_ccn.values(), key=lambda h: h["ccn"])}
    tmp_path = INDEX_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w") as f:
        json.dump(out, f, separators=(",", ":"))
    tmp_path.replace(INDEX_PATH)

    summaries = list(existing_by_ccn.values())
    with_rows = sum(1 for h in summaries if h.get("n", 0) > 0)
    zero_rows = sum(1 for h in summaries if h.get("n", 0) == 0)
    for line in diffs:
        print(line)
    print()
    print(f"wrote {INDEX_PATH}")
    print(f"  target CCNs:    {len(target)}")
    print(f"  added new:      {n_added}")
    print(f"  0 → n>0:        {n_updated_with_rows}")
    print(f"  n>0 → 0:        {n_updated_to_zero}")
    print(f"  no change:      {n_no_change}")
    print(f"  missing file:   {n_missing_file}")
    print()
    print(f"  index total:    {len(summaries)} ({with_rows} with n>0, {zero_rows} zero-row)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
