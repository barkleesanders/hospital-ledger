#!/usr/bin/env python3
"""Refuse publication when a refresh output is incomplete or regresses sharply."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "public" / "data" / "summary.json"
PRICES_INDEX = ROOT / "public" / "data" / "prices" / "index.json"
CPT_INDEX = ROOT / "public" / "data" / "cpt-index.json"
PUBLIC_DATA = ROOT / "public" / "data"

GUARDED_METRICS = (
    "standardized_price_index_hospitals",
    "standardized_price_hospitals",
    "standardized_price_rows",
    "cpt_indexed_hospitals",
    "cpt_indexed_rows",
)

AGGREGATE_METRICS = (
    "compliance_hospitals",
    "featured_payers",
    "total_payers",
    "payer_files",
    "procedure_files",
)


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"invalid or missing JSON: {path}: {exc}") from exc


def load_ccns(path: Path | None) -> set[str]:
    if path is None:
        return set()
    try:
        return {line.strip() for line in path.read_text().splitlines() if line.strip()}
    except OSError as exc:
        raise SystemExit(f"cannot read CCN list {path}: {exc}") from exc


def aggregate_metrics(root: Path) -> dict[str, int]:
    compliance = load_json(root / "compliance-ranking.json")
    payers = load_json(root / "payers-index.json")
    if not isinstance(compliance, dict) or not isinstance(compliance.get("hospitals"), list):
        raise SystemExit("compliance-ranking.json must contain a hospitals list")
    if not isinstance(payers, dict) or not isinstance(payers.get("featured"), list):
        raise SystemExit("payers-index.json must contain a featured list")
    return {
        "compliance_hospitals": len(compliance["hospitals"]),
        "featured_payers": len(payers["featured"]),
        "total_payers": int(payers.get("total") or 0),
        "payer_files": sum(1 for path in (root / "payer").glob("*.json") if path.is_file()),
        "procedure_files": sum(1 for path in (root / "cpt-detail").glob("*.json") if path.is_file()),
    }


def reject_regression(*, name: str, old: int, new: int, minimum_ratio: float, pct: float) -> None:
    if old > 0 and new < int(old * minimum_ratio):
        raise SystemExit(f"{name} regressed from {old} to {new}, beyond {pct:.1f}% guardrail")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-summary", type=Path)
    parser.add_argument("--baseline-data-dir", type=Path)
    parser.add_argument("--processed-file", type=Path)
    parser.add_argument("--max-regression-pct", type=float, default=5.0)
    args = parser.parse_args()

    summary = load_json(SUMMARY)
    prices_index = load_json(PRICES_INDEX)
    cpt_index = load_json(CPT_INDEX)
    if not isinstance(summary, dict):
        raise SystemExit("summary.json must be an object")
    if not isinstance(prices_index, dict) or not isinstance(prices_index.get("hospitals"), list):
        raise SystemExit("prices/index.json must contain a hospitals list")
    if not isinstance(cpt_index, dict) or len(cpt_index) != 5000:
        count = len(cpt_index) if isinstance(cpt_index, dict) else "invalid"
        raise SystemExit(f"cpt-index must contain exactly 5000 codes, got {count}")
    if CPT_INDEX.stat().st_size > 15 * 1024 * 1024:
        raise SystemExit("cpt-index exceeds the 15 MiB serving limit")

    indexed = {
        str(entry.get("ccn"))
        for entry in prices_index["hospitals"]
        if isinstance(entry, dict) and entry.get("ccn")
    }
    if len(indexed) != len(prices_index["hospitals"]):
        raise SystemExit("prices index contains missing or duplicate CCNs")
    declared_indexed = int(summary.get("standardized_price_index_hospitals") or 0)
    if declared_indexed != len(indexed):
        raise SystemExit(
            "summary/index mismatch: "
            f"summary={declared_indexed} prices_index={len(indexed)}"
        )
    processed = load_ccns(args.processed_file)
    for ccn in sorted(processed):
        price_path = PRICES_INDEX.parent / f"{ccn}.json"
        price = load_json(price_path)
        if not isinstance(price, dict) or int(price.get("n_slim") or 0) <= 0:
            raise SystemExit(f"processed hospital has no displayable compact output: {ccn}")
        if ccn not in indexed:
            raise SystemExit(f"processed hospital is absent from prices index: {ccn}")

    minimum_ratio = max(0.0, 1.0 - args.max_regression_pct / 100.0)
    if args.baseline_summary and args.baseline_summary.exists():
        baseline = load_json(args.baseline_summary)
        if not isinstance(baseline, dict):
            raise SystemExit("baseline summary must be an object")
        for key in GUARDED_METRICS:
            old = int(baseline.get(key) or 0)
            new = int(summary.get(key) or 0)
            reject_regression(
                name=key,
                old=old,
                new=new,
                minimum_ratio=minimum_ratio,
                pct=args.max_regression_pct,
            )

    current_aggregates = aggregate_metrics(PUBLIC_DATA)
    if args.baseline_data_dir and args.baseline_data_dir.exists():
        baseline_aggregates = aggregate_metrics(args.baseline_data_dir)
        for key in AGGREGATE_METRICS:
            reject_regression(
                name=key,
                old=baseline_aggregates[key],
                new=current_aggregates[key],
                minimum_ratio=minimum_ratio,
                pct=args.max_regression_pct,
            )

    print(
        "refresh output validated: "
        f"priced={summary.get('standardized_price_index_hospitals')} "
        f"rows={summary.get('standardized_price_rows')} "
        f"cpt_codes={len(cpt_index)} processed={len(processed)} "
        f"procedures={current_aggregates['procedure_files']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
