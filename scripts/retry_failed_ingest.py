#!/usr/bin/env python3
"""Retry failed full-corpus standardization ingests.

Reads the existing JSONL failure log, de-duplicates CCNs, optionally filters
by state and limit, skips hospitals that now have valid parsed output, and
re-runs the established ingest flow in parallel.
"""

import argparse
import concurrent.futures
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from batch_ingest import DB, FAILURES_FILE, ingest_ccn, parsed_ok


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--failures-file",
        nargs="+",
        default=[FAILURES_FILE],
        help="One or more JSONL files produced by the full standardize run",
    )
    parser.add_argument(
        "--state",
        help="Case-insensitive hospitals.state filter applied before retries",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of queued retries after filters and skip checks",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel retry workers",
    )
    parser.add_argument(
        "--item-timeout-seconds",
        type=int,
        default=0,
        help="When >0, run each CCN parse in a subprocess capped at this timeout",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    return args


def load_failed_ccns(paths):
    seen = set()
    ccns = []
    for path in paths:
        with open(path) as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"invalid JSON in {path} line {line_no}: {exc}") from exc
                ccn = str(payload.get("ccn") or "").strip()
                if ccn and ccn not in seen:
                    seen.add(ccn)
                    ccns.append(ccn)
    return ccns


def chunked(values, size):
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def filter_ccns_by_state(conn, ccns, state):
    if not ccns:
        return []
    matches = set()
    for group in chunked(ccns, 500):
        placeholders = ",".join("?" for _ in group)
        rows = conn.execute(
            f"""
            SELECT ccn
            FROM hospitals
            WHERE UPPER(TRIM(state)) = UPPER(TRIM(?))
              AND ccn IN ({placeholders})
            """,
            [state, *group],
        )
        matches.update(row[0] for row in rows)
    return [ccn for ccn in ccns if ccn in matches]


def classify_result(ccn, ok, items, fmt, message):
    if ok and items > 0:
        return True, ccn, items, fmt or "", message or ""
    if ok:
        detail = f"zero_items ({message})" if message else "zero_items"
        return False, ccn, items, fmt or "", detail
    return False, ccn, items, fmt or "", message or "unknown_error"


def main(argv=None):
    args = parse_args(argv)
    failed_ccns = load_failed_ccns(args.failures_file)
    unique_failed = len(failed_ccns)

    if args.state:
        conn = sqlite3.connect(DB)
        try:
            candidate_ccns = filter_ccns_by_state(conn, failed_ccns, args.state)
        finally:
            conn.close()
    else:
        candidate_ccns = failed_ccns

    state_matched = len(candidate_ccns)
    queued_ccns = [ccn for ccn in candidate_ccns if not parsed_ok(ccn)]
    skipped_existing = state_matched - len(queued_ccns)

    if args.limit is not None:
        queued_ccns = queued_ccns[: args.limit]

    print(
        "retry targets:"
        f" unique_failed={unique_failed}"
        f" state_match={state_matched}"
        f" skipped_existing={skipped_existing}"
        f" queued={len(queued_ccns)}"
        f" workers={args.workers}"
    )

    if not queued_ccns:
        print(
            "summary:"
            f" attempted=0 succeeded=0 failed=0 skipped_existing={skipped_existing}"
        )
        return 0

    succeeded = 0
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(ingest_ccn, ccn, args.item_timeout_seconds): ccn
            for ccn in queued_ccns
        }
        for future in concurrent.futures.as_completed(futures):
            fallback_ccn = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                failed.append((fallback_ccn, "", 0, f"worker:{type(exc).__name__}:{exc}"))
                print(f"FAIL {fallback_ccn} fmt=- items=0 worker:{type(exc).__name__}:{exc}")
                continue

            ok, ccn, items, fmt, detail = classify_result(*result)
            if ok:
                succeeded += 1
                print(f"OK   {ccn} fmt={fmt or '-'} items={items} {detail}".rstrip())
            else:
                failed.append((ccn, fmt, items, detail))
                print(f"FAIL {ccn} fmt={fmt or '-'} items={items} {detail}")

    print(
        "summary:"
        f" attempted={len(queued_ccns)}"
        f" succeeded={succeeded}"
        f" failed={len(failed)}"
        f" skipped_existing={skipped_existing}"
    )
    if failed:
        print("failed_ccns: " + ", ".join(item[0] for item in failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
