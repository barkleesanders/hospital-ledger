#!/usr/bin/env python3
"""Pre-deploy copy-truth audit for hospitalledger.com.

Catches stale numeric claims drifting between README.md / src/routes/ and the
actual ground-truth values derived from public/data/summary.json, the SQLite
db, and the side-car JSONLs. Runs in two modes:

  python3 scripts/predeploy_audit.py              # source vs local truth (pre-deploy)
  python3 scripts/predeploy_audit.py --check-live # source vs live site (post-deploy)
  python3 scripts/predeploy_audit.py --fix        # auto-apply proposed edits

Exit 0 = all claims match ground truth. Exit 1 = stale claims found.

Adding a new claim:
  1. Add a Claim(...) entry to CLAIMS below.
  2. Run the script; if it fires false-positive, refine the regex.

The script is intentionally dependency-free (stdlib + sqlite3) and idempotent.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "public" / "data" / "summary.json"
PAYER_RAW = ROOT / "data" / "_payer_raw.jsonl"
PARSED_DIR = ROOT / "data" / "parsed"
DB = ROOT / "db" / "hospital_ledger.db"
LIVE_BASE = "https://hospitalledger.com"


def _summary() -> dict:
    return json.load(open(SUMMARY))


def _payer_stats() -> dict:
    rows = 0
    ccns: set[str] = set()
    rates = 0
    with open(PAYER_RAW) as f:
        for line in f:
            rows += 1
            try:
                r = json.loads(line)
                if r.get("ccn"):
                    ccns.add(r["ccn"])
                rates += r.get("n_rates", 0)
            except json.JSONDecodeError:
                continue
    return {"rows": rows, "ccns": len(ccns), "rates": rates}


def _db_count(table: str) -> int:
    with sqlite3.connect(DB) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _parsed_dir_size() -> str:
    out = subprocess.check_output(["du", "-sh", str(PARSED_DIR)]).decode().split()[0]
    return out  # e.g. "5.8G"


def _millions(n: int, places: int = 1) -> str:
    """Format an integer as '65.3 M' (matches README style)."""
    return f"{n / 1_000_000:.{places}f} M"


@dataclass
class Claim:
    """One stale-number claim to verify.

    `pattern` is a regex with ONE capture group containing the displayed value.
    `truth` returns the expected displayed value as a STRING (exact match).
    `where` is the file path (for grep + report).
    `label` is a human-readable name for the report.
    """

    label: str
    where: str
    pattern: str
    truth: Callable[[], str]


# ─────────────────────────────────────────────────────────────────────────────
# Ground-truth claim table.
#
# Each row says: "in file <where>, the regex <pattern> should match a string
# equal to truth()." If it matches a DIFFERENT string, audit fails with a clear
# file:line | current → expected diff.
# ─────────────────────────────────────────────────────────────────────────────
CLAIMS: list[Claim] = [
    Claim(
        "README: priced index hospitals",
        "README.md",
        r"\| Hospitals with a standardized on-site price preview \| \*\*([\d,]+)\*\*",
        lambda: f"{_summary()['standardized_price_index_hospitals']:,}",
    ),
    Claim(
        "README: standardized price rows",
        "README.md",
        r"\| Standardized price rows across those hospitals \| \*\*([\d.]+ M)\*\*",
        lambda: _millions(_summary()["standardized_price_rows"]),
    ),
    Claim(
        "README: CPT/HCPCS-coded rows",
        "README.md",
        r"\| CPT- / HCPCS-coded rows \(patient-comparable\) \| \*\*([\d.]+ M)\*\*",
        lambda: _millions(_summary()["cpt_indexed_rows"]),
    ),
    Claim(
        "README: payer rate cells",
        "README.md",
        r"\| Payer-negotiated rate cells \| \*\*([\d.]+ M)\*\*",
        lambda: _millions(_payer_stats()["rates"]),
    ),
    Claim(
        "README: hospitals with payer-rate row",
        "README.md",
        r"\| Hospitals with at least one payer-rate row \| \*\*([\d,]+)\*\*",
        lambda: f"{_payer_stats()['ccns']:,}",
    ),
    Claim(
        "README: CCN x raw-payer-string rows",
        "README.md",
        r"\| CCN × raw-payer-string rows \| \*\*([\d,]+)\*\*",
        lambda: f"{_payer_stats()['rows']:,}",
    ),
    Claim(
        "README: CMS enforcement records",
        "README.md",
        r"\| CMS enforcement records loaded \| \*\*([\d,]+)\*\*",
        lambda: f"{_db_count('cms_enforcement'):,}",
    ),
    Claim(
        "README: hospitals in CMS universe",
        "README.md",
        r"\| Hospitals in the CMS universe \| \*\*([\d,]+)\*\*",
        lambda: f"{_db_count('hospitals'):,}",
    ),
    Claim(
        "README: Stage 4 coverage line",
        "README.md",
        r"live \((\d[\d,]*) / 3,986 = ",
        lambda: f"{_summary()['standardized_price_index_hospitals']:,}",
    ),
    Claim(
        "README: closing-paragraph parsed count",
        "README.md",
        r"Stage 4 is live, and ([\d,]+) hospitals' MRFs have been",
        lambda: f"{_summary()['standardized_price_index_hospitals']:,}",
    ),
    Claim(
        "home.tsx: payer-raw row count",
        "src/routes/home.tsx",
        r'<td class="py-2 pr-3">([\d,]+) raw rows</td>',
        lambda: f"{_payer_stats()['rows']:,}",
    ),
]


def audit_source() -> list[tuple[Claim, str, str, int]]:
    """Return list of (claim, claimed, expected, line_no) for STALE entries."""
    stale = []
    for claim in CLAIMS:
        path = ROOT / claim.where
        if not path.exists():
            continue
        text = path.read_text()
        m = re.search(claim.pattern, text)
        if not m:
            stale.append((claim, "(pattern not matched)", claim.truth(), -1))
            continue
        claimed = m.group(1)
        expected = claim.truth()
        if claimed != expected:
            line_no = text[: m.start()].count("\n") + 1
            stale.append((claim, claimed, expected, line_no))
    return stale


def audit_live() -> list[str]:
    """Verify live site numbers match local summary.json. Returns failure messages."""
    failures = []
    s = _summary()
    expected_priced = s["standardized_price_index_hospitals"]

    def fetch_json(path: str):
        url = f"{LIVE_BASE}{path}?cb={int(__import__('time').time())}"
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    try:
        live_summary = fetch_json("/data/summary.json")
        if live_summary["standardized_price_index_hospitals"] != expected_priced:
            failures.append(
                f"live summary.json shows {live_summary['standardized_price_index_hospitals']}, "
                f"local says {expected_priced}"
            )
    except Exception as e:
        failures.append(f"failed to fetch /data/summary.json: {e}")

    try:
        prices_idx = fetch_json("/api/prices-index")
        live_n = len(prices_idx.get("hospitals", prices_idx))
        if live_n != expected_priced:
            failures.append(
                f"/api/prices-index returns {live_n} hospitals, summary says {expected_priced}"
            )
    except Exception as e:
        failures.append(f"failed to fetch /api/prices-index: {e}")

    try:
        cpt = fetch_json("/api/cpt-index")
        if len(cpt) != 5000:
            failures.append(f"/api/cpt-index returns {len(cpt)} codes (expected 5000)")
    except Exception as e:
        failures.append(f"failed to fetch /api/cpt-index: {e}")

    return failures


def apply_fix(claim: Claim, claimed: str, expected: str) -> bool:
    """In-place replace `claimed` with `expected` in the file (one occurrence)."""
    path = ROOT / claim.where
    text = path.read_text()
    new_text = re.sub(
        claim.pattern,
        lambda m: m.group(0).replace(claimed, expected, 1),
        text,
        count=1,
    )
    if new_text == text:
        return False
    path.write_text(new_text)
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="hospital-ledger copy-truth audit")
    p.add_argument("--fix", action="store_true", help="auto-apply proposed edits")
    p.add_argument("--check-live", action="store_true", help="also verify against live site")
    args = p.parse_args()

    stale = audit_source()
    if stale and args.fix:
        applied = 0
        for claim, claimed, expected, _ in stale:
            if claimed == "(pattern not matched)":
                print(f"SKIP {claim.label}: pattern not found in {claim.where}")
                continue
            if apply_fix(claim, claimed, expected):
                print(f"FIXED {claim.where}: {claim.label}: {claimed} → {expected}")
                applied += 1
        # Re-audit after fixing
        stale = audit_source()
        print(f"applied {applied} fixes")

    if stale:
        print(f"FAIL: {len(stale)} stale claim(s) found:")
        for claim, claimed, expected, line_no in stale:
            loc = f"{claim.where}:{line_no}" if line_no > 0 else claim.where
            print(f"  {loc} | {claim.label}: {claimed} → {expected}")
        return 1

    print(f"PASS: {len(CLAIMS)} claim(s) verified against ground truth")

    if args.check_live:
        live_failures = audit_live()
        if live_failures:
            print(f"FAIL (live): {len(live_failures)} mismatch(es):")
            for f in live_failures:
                print(f"  {f}")
            return 1
        print("PASS (live): live site numbers match local summary.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
