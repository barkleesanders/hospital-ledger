#!/usr/bin/env python3
"""Pre-deploy copy-truth audit for hospitalledger.com.

Catches stale numeric claims drifting between README.md / src/routes/ and the
canonical ground truth. Ground truth is R2, not local files (2026-10-13 fix —
local copies go stale: the laptop's payer data was 2026-09-11 vintage and the
VM bundle ships a stub _payer_raw.jsonl, so local-truth mode false-failed):

  * Summary claims read R2 meta/summary.json via the public route
    https://hospitalledger.com/data/summary.json (local public/data/summary.json
    is only an offline fallback; force it with --offline).
  * Payer claims read R2 /data/payer-stats.json (shape
    {"rows": int, "ccns": int, "rates": int}) once the payer pipeline publishes
    it. Until then they fall back to a FULL local data/_payer_raw.jsonl
    (>=10k rows); on partial copies (VM bundle, fresh clones) they SKIP
    instead of false-failing.
  * DB claims (CMS enforcement / hospital universe) read the local SQLite db
    and SKIP when no db is present.

  python3 scripts/predeploy_audit.py              # source vs canonical truth (pre-deploy)
  python3 scripts/predeploy_audit.py --check-live # + live serving layer vs R2 (post-deploy)
  python3 scripts/predeploy_audit.py --fix        # auto-apply proposed edits
  python3 scripts/predeploy_audit.py --offline    # force local-file truth (no network)

Exit 0 = all verifiable claims match ground truth. Exit 1 = stale claims found.
Skipped claims are reported as SKIP lines and never fail the audit.

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

OFFLINE = False  # set by main() from --offline
_SUMMARY_CACHE: dict | None = None


class SkipClaim(Exception):
    """Ground truth for this claim is unavailable here; skip, never fail."""


def _fetch_json(url: str):
    # CF bot-blocks the default Python-urllib UA; use a real one.
    req = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "hospital-ledger-predeploy-audit/1.0 (curl-compatible)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _summary() -> dict:
    """Canonical summary: R2 meta/summary.json via the public route.

    The local public/data/summary.json is only an offline fallback — on any
    deployed/partial copy it lags the live R2 object (the "old bundled file"
    failure this replaced).
    """
    global _SUMMARY_CACHE
    if _SUMMARY_CACHE is not None:
        return _SUMMARY_CACHE
    if not OFFLINE:
        try:
            _SUMMARY_CACHE = _fetch_json(f"{LIVE_BASE}/data/summary.json")
            return _SUMMARY_CACHE
        except Exception:
            pass  # fall through to the local copy
    _SUMMARY_CACHE = json.load(open(SUMMARY))
    return _SUMMARY_CACHE


# A full-sized local file can still be stale: the laptop's 2026-09-11 extract
# has 68,528 rows (past the 10k floor) while the README's 72,951 came from the
# 2026-09-27 VM run. Trust it only if it is no older than the canonical
# summary's generated_at minus this window.
PAYER_RAW_MAX_LAG_DAYS = 7


def _payer_raw_is_current() -> bool:
    import datetime as dt

    try:
        gen = dt.datetime.fromisoformat(_summary()["generated_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return False  # cannot date the canonical data -> cannot vouch for the file
    mtime = dt.datetime.fromtimestamp(PAYER_RAW.stat().st_mtime, dt.timezone.utc)
    return mtime >= gen - dt.timedelta(days=PAYER_RAW_MAX_LAG_DAYS)


def _payer_stats() -> dict:
    """Payer truth: R2 /data/payer-stats.json, else a FULL local raw file.

    A partial local _payer_raw.jsonl (VM bundle stub, stale laptop snapshot)
    must never fail the audit — with no canonical source the claim SKIPs.
    """
    if not OFFLINE:
        try:
            s = _fetch_json(f"{LIVE_BASE}/data/payer-stats.json")
            return {"rows": int(s["rows"]), "ccns": int(s["ccns"]), "rates": int(s["rates"])}
        except Exception:
            pass  # not published yet / unreachable — try local
    if PAYER_RAW.exists() and _payer_raw_is_current():
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
        if rows >= 10_000:  # full dataset only; stubs/stale extracts skip
            return {"rows": rows, "ccns": len(ccns), "rates": rates}
    raise SkipClaim(
        "payer truth unavailable (no R2 payer-stats.json; local _payer_raw.jsonl "
        f"missing, partial, or >{PAYER_RAW_MAX_LAG_DAYS}d older than summary generated_at)"
    )


def _db_count(table: str) -> int:
    if not DB.exists():
        raise SkipClaim(f"no local db at {DB}")
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


SKIPPED: list[tuple[Claim, str]] = []


def audit_source() -> list[tuple[Claim, str, str, int]]:
    """Return list of (claim, claimed, expected, line_no) for STALE entries.

    Claims whose ground truth is unavailable here land in SKIPPED instead —
    a partial copy must never false-fail the gate.
    """
    stale = []
    SKIPPED.clear()
    for claim in CLAIMS:
        path = ROOT / claim.where
        if not path.exists():
            continue
        text = path.read_text()
        m = re.search(claim.pattern, text)
        try:
            expected = claim.truth()
        except SkipClaim as e:
            SKIPPED.append((claim, str(e)))
            continue
        if not m:
            stale.append((claim, "(pattern not matched)", expected, -1))
            continue
        claimed = m.group(1)
        if claimed != expected:
            line_no = text[: m.start()].count("\n") + 1
            stale.append((claim, claimed, expected, line_no))
    return stale


def audit_live() -> list[str]:
    """Verify the live serving layer against the canonical R2 summary.

    _summary() IS the R2 object, so this checks that the site's routes serve
    the R2 truth (the old version compared the live site to a possibly-stale
    local bundled summary.json — the check that could not pass off the
    full-data machine).
    """
    failures = []
    s = _summary()
    expected_priced = s["standardized_price_index_hospitals"]

    def fetch_json(path: str):
        return _fetch_json(f"{LIVE_BASE}{path}?cb={int(__import__('time').time())}")

    try:
        live_summary = fetch_json("/data/summary.json")
        if live_summary["standardized_price_index_hospitals"] != expected_priced:
            failures.append(
                f"live summary.json shows {live_summary['standardized_price_index_hospitals']}, "
                f"R2 canonical says {expected_priced}"
            )
    except Exception as e:
        failures.append(f"failed to fetch /data/summary.json: {e}")

    prices_idx = None
    try:
        prices_idx = fetch_json("/api/prices-index")
        live_n = len(prices_idx.get("hospitals", prices_idx))
        if live_n != expected_priced:
            failures.append(
                f"/api/prices-index returns {live_n} hospitals, R2 summary says {expected_priced}"
            )
    except Exception as e:
        failures.append(f"failed to fetch /api/prices-index: {e}")

    try:
        cpt = fetch_json("/api/cpt-index")
        if len(cpt) != 5000:
            failures.append(f"/api/cpt-index returns {len(cpt)} codes (expected 5000)")
    except Exception as e:
        failures.append(f"failed to fetch /api/cpt-index: {e}")

    # Random-priced-CCN smoke: pick 3 priced CCNs from the LIVE prices index,
    # curl each /api/prices/{ccn}, assert 200 + n_slim > 0. Catches the "R2
    # missing file" regression class we hit twice (Tier 1 + Phase B forgot
    # stage4_refresh.py). (Was: sampled from the local bundled index.json.)
    import random
    try:
        priced = []
        if isinstance(prices_idx, dict):
            priced = [h["ccn"] for h in prices_idx.get("hospitals", []) if h.get("n", 0) > 0]
        if not priced:
            idx = json.load(open(SUMMARY.parent / "prices" / "index.json"))
            priced = [h["ccn"] for h in idx.get("hospitals", []) if h.get("n", 0) > 0]
        sample = random.sample(priced, min(3, len(priced))) if priced else []
        for ccn in sample:
            try:
                d = fetch_json(f"/api/prices/{ccn}")
                if not isinstance(d, dict) or d.get("n_slim", 0) <= 0:
                    failures.append(f"/api/prices/{ccn} returns no n_slim (got {d.get('n_slim') if isinstance(d, dict) else type(d).__name__})")
            except Exception as e:
                failures.append(f"/api/prices/{ccn} fetch failed: {e}")
    except Exception as e:
        failures.append(f"random-priced-CCN smoke setup failed: {e}")

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
    global OFFLINE
    p = argparse.ArgumentParser(description="hospital-ledger copy-truth audit")
    p.add_argument("--fix", action="store_true", help="auto-apply proposed edits")
    p.add_argument("--check-live", action="store_true", help="also verify against live site")
    p.add_argument("--offline", action="store_true",
                   help="force local-file ground truth (default: R2 canonical via public routes)")
    args = p.parse_args()
    OFFLINE = args.offline

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

    for claim, why in SKIPPED:
        print(f"SKIP {claim.label}: {why}")

    if stale:
        print(f"FAIL: {len(stale)} stale claim(s) found:")
        for claim, claimed, expected, line_no in stale:
            loc = f"{claim.where}:{line_no}" if line_no > 0 else claim.where
            print(f"  {loc} | {claim.label}: {claimed} → {expected}")
        return 1

    print(f"PASS: {len(CLAIMS) - len(SKIPPED)} claim(s) verified against ground truth"
          f" ({len(SKIPPED)} skipped)")

    if args.check_live:
        live_failures = audit_live()
        if live_failures:
            print(f"FAIL (live): {len(live_failures)} mismatch(es):")
            for f in live_failures:
                print(f"  {f}")
            return 1
        print("PASS (live): live site numbers match the R2 canonical summary")

    return 0


if __name__ == "__main__":
    sys.exit(main())
