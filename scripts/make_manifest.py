#!/usr/bin/env python3
"""make_manifest.py — write meta/manifest.json for a site-data artifact dir.

The manifest is what /api/manifest serves and what a verifier compares against
(contract_version, generated_at, producer, refresh_tier, per-artifact sha256).

    python3 scripts/make_manifest.py <dir> --producer "muse.ai nova" --tier counts

generated_at is copied from meta/summary.json so the two can never disagree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

CONTRACT_VERSION = "1"
# Per-hospital price files are thousands of large objects; hashing all of them
# on every run is minutes of I/O for no gain — the index lists them, and the
# validator checks each file's own ccn/shape. Everything else is hashed.
SKIP_PREFIXES = ("prices/", "parsed/")
ALWAYS_HASH = ("prices/index.json",)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--producer", required=True, help='who ran this, e.g. "muse.ai nova" or "mac-mini launchd"')
    ap.add_argument("--tier", choices=("counts", "full"), required=True)
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    root = Path(args.root)
    summary_p = root / "meta" / "summary.json"
    if not summary_p.is_file():
        print("meta/summary.json missing — build it first", file=sys.stderr)
        return 1
    summary = json.loads(summary_p.read_text())
    artifacts: dict[str, dict] = {}
    for p in sorted(root.rglob("*.json")):
        rel = p.relative_to(root).as_posix()
        if rel == "meta/manifest.json":
            continue
        if rel.startswith(SKIP_PREFIXES) and rel not in ALWAYS_HASH:
            continue
        artifacts[rel] = {"bytes": p.stat().st_size, "sha256": sha256(p)}
    counts = {
        "prices_files": sum(1 for p in (root / "prices").glob("*.json") if p.name != "index.json")
        if (root / "prices").is_dir() else 0,
        "cpt_detail_files": len(list((root / "aggregates" / "cpt-detail").glob("*.json")))
        if (root / "aggregates" / "cpt-detail").is_dir() else 0,
        "payer_files": len(list((root / "aggregates" / "payer").glob("*.json")))
        if (root / "aggregates" / "payer").is_dir() else 0,
    }
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "generated_at": summary["generated_at"],
        "producer": args.producer,
        "refresh_tier": args.tier,
        "note": args.note,
        "summary": {k: summary.get(k) for k in (
            "total_facilities", "cms_required_total", "compliant", "compliance_pct",
            "standardized_price_hospitals", "standardized_price_rows")},
        "unhashed_counts": counts,
        "artifacts": artifacts,
    }
    out = root / "meta" / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}: {len(artifacts)} hashed artifact(s), tier={args.tier}, generated_at={manifest['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
