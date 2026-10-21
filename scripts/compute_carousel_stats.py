#!/usr/bin/env python3
"""Recompute ProcedureCarousel min/median/max from production cpt-index.

Reads a cpt-index.json blob from stdin (e.g. piped from
`curl https://hospitalledger.com/api/cpt-index`) and prints the p5/median/p95
of gross charge plus distinct-hospital count for every code currently used in
src/components/ProcedureCarousel.tsx. Edit the CODES list below to add/remove
slides.

Usage:
    curl -sS https://hospitalledger.com/api/cpt-index \
      | python3 scripts/compute_carousel_stats.py

Output is a markdown table you can paste into a follow-up PR description, plus
the exact `min/median/max/hospitals` field values to drop into the TypeScript
slide list.
"""
from __future__ import annotations

import json
import sys

# Keep in sync with src/components/ProcedureCarousel.tsx PROCEDURE_SLIDES.
CODES: list[tuple[str, str]] = [
    ("27130", "TOTAL HIP REPLACEMENT"),
    ("27447", "TOTAL KNEE REPLACEMENT"),
    ("59409", "VAGINAL DELIVERY (DELIVERY ONLY)"),
    ("47562", "GALLBLADDER REMOVAL (LAPAROSCOPIC)"),
    ("45378", "DIAGNOSTIC COLONOSCOPY"),
    ("66984", "CATARACT SURGERY"),
    ("70553", "MRI BRAIN W/ CONTRAST"),
    ("93458", "CARDIAC CATHETERIZATION"),
]


def robust(rows: list[dict]) -> dict | None:
    """p5 / median / p95 of `gross` over rows with gross > $10 (skip junk)."""
    grosses = sorted(r["gross"] for r in rows if r.get("gross") and r["gross"] > 10)
    if not grosses:
        return None
    n = len(grosses)
    return {
        "n": len(rows),
        "p5": int(round(grosses[max(0, int(n * 0.05))])),
        "median": int(round(grosses[n // 2])),
        "p95": int(round(grosses[min(n - 1, int(n * 0.95))])),
    }


def main() -> int:
    data = json.load(sys.stdin)
    print("| Code | Procedure | n | p5 | median | p95 |")
    print("|------|-----------|---|----|--------|-----|")
    ts_blocks: list[str] = []
    for code, name in CODES:
        rows = data.get(code) or []
        s = robust(rows)
        if s is None:
            print(f"| {code} | {name} | **0** | — | — | — |  ← NOT IN INDEX, swap this slide")
            continue
        print(f"| {code} | {name} | {s['n']:,} | ${s['p5']:,} | ${s['median']:,} | ${s['p95']:,} |")
        ts_blocks.append(
            f'  {{ code: "{code}", name: "{name}", min: {s["p5"]}, '
            f'median: {s["median"]}, max: {s["p95"]}, hospitals: {s["n"]} }},'
        )
    print()
    print("// Paste into PROCEDURE_SLIDES:")
    for block in ts_blocks:
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
