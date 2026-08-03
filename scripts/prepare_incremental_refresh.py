#!/usr/bin/env python3
"""Identify changed hospital MRFs and invalidate only their cached parses.

The old weekly job used ``batch_ingest --resume`` without checking existing
hospitals, so it was a gap retry, not a refresh. This probe stores stable HTTP
validators in data/cloud_refresh_state.json. Hospitals whose URL, ETag,
Last-Modified, or Content-Length changes have their local parsed cache removed;
the existing resumable ingester then processes them again.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "hospital_ledger.db"
STATE = ROOT / "data" / "cloud_refresh_state.json"
PARSED = ROOT / "data" / "parsed"


def candidates() -> dict[str, str]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        WITH all_candidates AS (
          SELECT s.ccn, s.mrf_url AS url, COALESCE(p.probed_at, '') AS seen, 0 AS score
          FROM mrf_seed s
          LEFT JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url AND p.alive=1
          UNION ALL
          SELECT ccn, candidate_url, COALESCE(discovered_at, ''), COALESCE(score, 0)+1000
          FROM mrf_rediscovered WHERE alive=1
        ), ranked AS (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY ccn ORDER BY score DESC, seen DESC) AS n
          FROM all_candidates WHERE url IS NOT NULL AND url != ''
        )
        SELECT ccn, url FROM ranked WHERE n=1
        """
    ).fetchall()
    con.close()
    return {str(r["ccn"]): str(r["url"]) for r in rows}


def probe(item: tuple[str, str]) -> tuple[str, dict[str, object]]:
    ccn, url = item
    headers = {"User-Agent": "HospitalLedgerBot/1.0 (+https://hospitalledger.com)"}
    result: dict[str, object] = {"url": url}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers=headers)
        if method == "GET":
            request.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                h = response.headers
                result.update({
                    "status": int(getattr(response, "status", 200) or 200),
                    "final_url": response.geturl(),
                    "etag": h.get("ETag"),
                    "last_modified": h.get("Last-Modified"),
                    "content_length": h.get("Content-Length"),
                })
                return ccn, result
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue
            result.update({"status": exc.code, "error": f"HTTP {exc.code}"})
            return ccn, result
        except Exception as exc:
            if method == "HEAD":
                continue
            result.update({"status": 0, "error": type(exc).__name__})
    return ccn, result


def invalidate(ccn: str) -> None:
    for suffix in (".json", ".json.gz"):
        try:
            (PARSED / f"{ccn}{suffix}").unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=min(32, (os.cpu_count() or 4) * 4))
    parser.add_argument("--commit", action="store_true", help="persist state after a successful pipeline")
    args = parser.parse_args()
    previous = json.loads(STATE.read_text()) if STATE.exists() else {"hospitals": {}}
    current: dict[str, dict[str, object]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for ccn, metadata in pool.map(probe, candidates().items()):
            current[ccn] = metadata

    if args.commit:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "hospitals": current,
        }, sort_keys=True, separators=(",", ":")) + "\n")
        print(f"saved validators for {len(current)} hospitals")
        return 0

    old = previous.get("hospitals", {})
    keys = ("url", "final_url", "etag", "last_modified", "content_length")
    changed = [ccn for ccn, meta in current.items() if ccn not in old or any(meta.get(k) != old[ccn].get(k) for k in keys)]
    for ccn in changed:
        invalidate(ccn)
    plan = ROOT / "data" / "cloud_refresh_plan.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({"changed": changed, "probed": len(current)}, separators=(",", ":")) + "\n")
    print(f"probed={len(current)} changed={len(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
