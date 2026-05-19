#!/usr/bin/env python3
"""Compute standardized preview coverage closeout artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
PARSED_DIR = DATA_DIR / "parsed"
SITE_PRICE_INDEX = ROOT / "site" / "data" / "prices" / "index.json"
DB_PATH = ROOT / "db" / "hospital_ledger.db"
STATUS_FILE = DATA_DIR / "coverage_closeout_status.json"
GAP_FILE = DATA_DIR / "coverage_gap_ccns.txt"
PARSE_GAP_FILE = DATA_DIR / "coverage_parse_gap_ccns.txt"
PREVIEW_GAP_FILE = DATA_DIR / "coverage_preview_gap_ccns.txt"
BATCH_FILE = DATA_DIR / "coverage_closeout_batch_ccns.txt"
INGEST_STATUS_FILE = DATA_DIR / "coverage_closeout_ingest.status.json"
FAILURES_FILE = DATA_DIR / "coverage_closeout_failures.jsonl"
EXCEPTIONS_FILE = DATA_DIR / "coverage_terminal_exceptions.json"
WORKER_TUNE_FILE = DATA_DIR / "worker_tune_results.json"
ROW_COUNT_RE = re.compile(rb'"row_count":(\d+)')
FAILURE_GLOBS = (
    str(DATA_DIR / "full_standardize_failures.jsonl"),
    str(DATA_DIR / "full_standardize_shards" / "*.failures.jsonl"),
    str(DATA_DIR / "gap_shards" / "*.failures.jsonl"),
    str(DATA_DIR / "recovery_shard_*.failures.jsonl"),
)
SKIP_FAILURE_TOKENS = ("sample", "test", "benchmark", "_candidate_retry_")


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def read_json(path: Path) -> object | None:
    try:
        with path.open() as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def write_ccns(path: Path, ccns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{ccn}\n" for ccn in ccns))


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_target_rows() -> list[tuple[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT h.ccn, h.name
            FROM hospitals h
            WHERE h.hospital_type IN (
                'Acute Care Hospitals',
                'Critical Access Hospitals',
                'Childrens',
                'Rural Emergency Hospital'
            )
              AND (
                h.ccn IN (SELECT ccn FROM mrf_rediscovered WHERE alive = 1)
                OR h.ccn IN (
                    SELECT s.ccn
                    FROM mrf_seed s
                    JOIN mrf_probe p ON p.ccn = s.ccn AND p.mrf_url = s.mrf_url
                    WHERE p.alive = 1
                )
              )
            ORDER BY h.name
            """
        ).fetchall()
    finally:
        conn.close()
    return [(str(ccn), str(name or "")) for ccn, name in rows]


def parsed_row_count(ccn: str) -> int:
    path = PARSED_DIR / f"{ccn}.json"
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return 0
    match = ROW_COUNT_RE.search(head)
    return int(match.group(1)) if match else 0


def load_preview_rows() -> list[dict[str, object]]:
    raw = read_json(SITE_PRICE_INDEX)
    if isinstance(raw, dict):
        hospitals = raw.get("hospitals")
        if isinstance(hospitals, list):
            return [row for row in hospitals if isinstance(row, dict)]
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    return []


def load_preview_positive_ccns() -> set[str]:
    ccns: set[str] = set()
    for row in load_preview_rows():
        ccn = str(row.get("ccn") or "").strip()
        count = row.get("n")
        if ccn and isinstance(count, int) and count > 0:
            ccns.add(ccn)
    return ccns


def preview_row_total(rows: list[dict[str, object]]) -> int:
    total = 0
    for row in rows:
        count = row.get("n")
        if isinstance(count, int) and count > 0:
            total += count
    return total


def sample_preview_ccns(rows: list[dict[str, object]]) -> list[str]:
    ranked = sorted(
        [row for row in rows if isinstance(row.get("n"), int) and int(row["n"]) > 0],
        key=lambda row: (int(row["n"]), str(row.get("ccn") or "")),
    )
    if not ranked:
        return []
    picks = [ranked[0], ranked[len(ranked) // 2], ranked[-1]]
    ccns: list[str] = []
    for row in picks:
        ccn = str(row.get("ccn") or "").strip()
        if ccn and ccn not in ccns:
            ccns.append(ccn)
    return ccns


def iter_failure_logs() -> list[Path]:
    paths: list[Path] = []
    for pattern in FAILURE_GLOBS:
        for match in sorted(glob.glob(pattern)):
            path = Path(match)
            if not path.exists() or path.stat().st_size == 0:
                continue
            lower = path.name.lower()
            if any(token in lower for token in SKIP_FAILURE_TOKENS):
                continue
            paths.append(path)
    return paths


def load_failure_buckets() -> dict[str, dict[str, object]]:
    buckets: dict[str, dict[str, object]] = {}
    for path in iter_failure_logs():
        with path.open() as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ccn = str(payload.get("ccn") or "").strip()
                if not ccn:
                    continue
                bucket = buckets.setdefault(
                    ccn,
                    {
                        "failure_logs": [],
                        "reasons": [],
                    },
                )
                if str(path) not in bucket["failure_logs"]:
                    bucket["failure_logs"].append(str(path.relative_to(ROOT)))
                reason = str(payload.get("error") or "unknown_error").strip()
                if reason and reason not in bucket["reasons"]:
                    bucket["reasons"].append(reason)
    return buckets


def seed_probe_rows(conn: sqlite3.Connection, ccn: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT s.mrf_url, p.http_status, p.content_type, p.content_length, p.alive, p.probed_at
        FROM mrf_seed s
        LEFT JOIN mrf_probe p ON p.ccn = s.ccn AND p.mrf_url = s.mrf_url
        WHERE s.ccn = ?
        ORDER BY COALESCE(p.alive, 0) DESC, COALESCE(p.probed_at, '') DESC, s.mrf_url
        """,
        (ccn,),
    ).fetchall()
    return [
        {
            "mrf_url": str(url or ""),
            "http_status": status,
            "content_type": str(content_type or ""),
            "content_length": length,
            "alive": alive,
            "probed_at": str(probed_at or ""),
        }
        for url, status, content_type, length, alive, probed_at in rows[:10]
    ]


def rediscovery_rows(conn: sqlite3.Connection, ccn: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT candidate_url, source_page, score, head_status, head_content_type,
               head_content_length, alive, discovered_at
        FROM mrf_rediscovered
        WHERE ccn = ?
        ORDER BY COALESCE(discovered_at, '') DESC, COALESCE(score, 0) DESC, candidate_url
        """,
        (ccn,),
    ).fetchall()
    return [
        {
            "candidate_url": str(url or ""),
            "source_page": str(source_page or ""),
            "score": score,
            "head_status": status,
            "head_content_type": str(content_type or ""),
            "head_content_length": length,
            "alive": alive,
            "discovered_at": str(discovered_at or ""),
        }
        for url, source_page, score, status, content_type, length, alive, discovered_at in rows[:10]
    ]


def exception_http_summary(seed_rows: list[dict[str, object]], rediscovered: list[dict[str, object]]) -> str:
    parts: list[str] = []
    if seed_rows:
        top = seed_rows[0]
        parts.append(
            "seed:"
            f"status={top.get('http_status')},"
            f"alive={top.get('alive')},"
            f"type={top.get('content_type') or '-'}"
        )
    if rediscovered:
        top = rediscovered[0]
        parts.append(
            "rediscovery:"
            f"status={top.get('head_status')},"
            f"alive={top.get('alive')},"
            f"type={top.get('head_content_type') or '-'}"
        )
    return "; ".join(parts) or "no seed or rediscovery probe rows"


def build_terminal_exceptions(
    target_gap: list[str],
    names_by_ccn: dict[str, str],
    failure_buckets: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        entries: list[dict[str, object]] = []
        for ccn in target_gap:
            bucket = failure_buckets.get(ccn)
            if not bucket:
                continue
            reasons = list(bucket.get("reasons") or [])
            if not any("no_live_mrf" in reason for reason in reasons):
                continue
            seed_rows = seed_probe_rows(conn, ccn)
            rediscovered = rediscovery_rows(conn, ccn)
            original_url = ""
            if seed_rows:
                original_url = str(seed_rows[0].get("mrf_url") or "")
            elif rediscovered:
                original_url = str(rediscovered[0].get("candidate_url") or "")
            entries.append(
                {
                    "ccn": ccn,
                    "hospital_name": names_by_ccn.get(ccn, ""),
                    "original_url": original_url,
                    "rediscovery_attempts": rediscovered,
                    "seed_probe_history": seed_rows,
                    "http_content_result": exception_http_summary(seed_rows, rediscovered),
                    "reason": reasons[0] if reasons else "no_live_mrf",
                    "failure_logs": list(bucket.get("failure_logs") or []),
                    "provisional": True,
                }
            )
    finally:
        conn.close()
    return entries


def candidate_host_clusters(ccns: list[str]) -> list[dict[str, object]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        clusters: dict[str, dict[str, object]] = {}
        for ccn in ccns:
            row = conn.execute(
                """
                SELECT candidate_url
                FROM mrf_rediscovered
                WHERE ccn = ? AND alive = 1
                ORDER BY COALESCE(score, 0) DESC, COALESCE(discovered_at, '') DESC, candidate_url
                LIMIT 1
                """,
                (ccn,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT s.mrf_url
                    FROM mrf_seed s
                    JOIN mrf_probe p ON p.ccn = s.ccn AND p.mrf_url = s.mrf_url
                    WHERE s.ccn = ? AND p.alive = 1
                    ORDER BY COALESCE(p.probed_at, '') DESC, s.mrf_url
                    LIMIT 1
                    """,
                    (ccn,),
                ).fetchone()
            host = "unknown"
            if row is not None and row[0]:
                host = urlparse(str(row[0])).netloc.lower() or "unknown"
            entry = clusters.setdefault(
                host,
                {
                    "host": host,
                    "count": 0,
                    "error": "unknown",
                    "fmt": "-",
                    "example_ccn": ccn,
                },
            )
            entry["count"] = int(entry["count"]) + 1
        return sorted(clusters.values(), key=lambda item: (-int(item["count"]), str(item["host"])))
    finally:
        conn.close()


def compute_closeout() -> dict[str, object]:
    target_rows = load_target_rows()
    target_ccns = [ccn for ccn, _ in target_rows]
    names_by_ccn = {ccn: name for ccn, name in target_rows}

    parsed_positive = {ccn for ccn in target_ccns if parsed_row_count(ccn) > 0}
    preview_rows = load_preview_rows()
    preview_positive = {
        str(row.get("ccn") or "").strip()
        for row in preview_rows
        if isinstance(row.get("n"), int) and int(row["n"]) > 0
    }
    target_set = set(target_ccns)
    parse_gap = sorted(target_set - parsed_positive)
    parsed_not_preview = sorted(parsed_positive - preview_positive)
    preview_not_parsed = sorted(preview_positive - parsed_positive)
    target_gap = sorted(target_set - preview_positive)
    failure_buckets = load_failure_buckets()
    terminal_exceptions = build_terminal_exceptions(target_gap, names_by_ccn, failure_buckets)

    return {
        "generated_at": now_utc(),
        "live_target_total": len(target_ccns),
        "parsed_positive_total": len(parsed_positive),
        "preview_positive_total": len(preview_positive),
        "preview_index_entries": len(preview_rows),
        "preview_rows_total": preview_row_total(preview_rows),
        "coverage_pct": round((len(preview_positive) / len(target_ccns)) * 100, 2) if target_ccns else 0.0,
        "effective_target_total": len(target_ccns),
        "missing_parsed_total": len(parse_gap),
        "parsed_not_preview_total": len(parsed_not_preview),
        "preview_not_parsed_total": len(preview_not_parsed),
        "target_gap_total": len(target_gap),
        "terminal_exception_candidate_total": len(terminal_exceptions),
        "failure_clusters": candidate_host_clusters(parse_gap)[:15],
        "target_ccns": target_ccns,
        "parsed_positive_ccns": sorted(parsed_positive),
        "parse_gap_ccns": parse_gap,
        "preview_positive_ccns": sorted(preview_positive),
        "parsed_not_preview_ccns": parsed_not_preview,
        "preview_not_parsed_ccns": preview_not_parsed,
        "target_gap_ccns": target_gap,
        "terminal_exceptions": terminal_exceptions,
        "sample_preview_ccns": sample_preview_ccns(preview_rows),
        "samples": {
            "parse_gap": parse_gap[:25],
            "parsed_not_preview": parsed_not_preview[:25],
            "preview_not_parsed": preview_not_parsed[:25],
            "target_gap": target_gap[:25],
        },
    }


def write_closeout_artifacts(
    *,
    status_file: Path = STATUS_FILE,
    gap_file: Path = GAP_FILE,
    parse_gap_file: Path = PARSE_GAP_FILE,
    preview_gap_file: Path = PREVIEW_GAP_FILE,
    batch_file: Path = BATCH_FILE,
    exceptions_file: Path = EXCEPTIONS_FILE,
    loop: int = 1,
    phase: str = "snapshot",
) -> dict[str, object]:
    closeout = compute_closeout()

    write_ccns(gap_file, closeout["target_gap_ccns"])
    write_ccns(parse_gap_file, closeout["parse_gap_ccns"])
    write_ccns(preview_gap_file, closeout["target_gap_ccns"])
    write_ccns(batch_file, closeout["parse_gap_ccns"])

    write_json(
        exceptions_file,
        {
            "exceptions": closeout["terminal_exceptions"],
        },
    )

    status_payload = {
        "updated_at": closeout["generated_at"],
        "phase": phase,
        "loop": loop,
        "live_target_total": closeout["live_target_total"],
        "effective_target_total": closeout["effective_target_total"],
        "parsed_positive_total": closeout["parsed_positive_total"],
        "preview_positive_total": closeout["preview_positive_total"],
        "preview_index_entries": closeout["preview_index_entries"],
        "preview_rows": closeout["preview_rows_total"],
        "coverage_pct": closeout["coverage_pct"],
        "missing_parsed": closeout["missing_parsed_total"],
        "missing_preview": closeout["target_gap_total"],
        "parsed_not_preview_total": closeout["parsed_not_preview_total"],
        "preview_not_parsed_total": closeout["preview_not_parsed_total"],
        "target_gap_total": closeout["target_gap_total"],
        "parsed_positive": closeout["parsed_positive_total"],
        "preview_positive": closeout["preview_positive_total"],
        "terminal_exception_candidate_total": closeout["terminal_exception_candidate_total"],
        "terminal_exceptions": closeout["terminal_exception_candidate_total"],
        "gap_file": display_path(gap_file),
        "parse_gap_file": display_path(parse_gap_file),
        "preview_gap_file": display_path(preview_gap_file),
        "terminal_exceptions_file": display_path(exceptions_file),
        "failure_clusters": closeout["failure_clusters"],
        "last_cluster_attacked": (closeout["failure_clusters"][0] if closeout["failure_clusters"] else None),
        "sample_preview_ccns": closeout["sample_preview_ccns"],
    }
    write_json(status_file, status_payload)
    return status_payload | {
        "parse_gap_ccns": closeout["parse_gap_ccns"],
        "preview_gap_ccns": closeout["target_gap_ccns"],
        "parsed_not_preview_ccns": closeout["parsed_not_preview_ccns"],
    }


def resolve_python() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(ROOT), text=True, check=check)


def auto_tune_batch_workers(batch_file: Path, args: argparse.Namespace) -> int:
    """Run the real-work cloud tuner against the current parse gap and return workers."""
    if not args.auto_workers:
        return args.batch_workers
    if not batch_file.exists() or batch_file.stat().st_size == 0:
        return args.batch_workers
    cmd = [
        resolve_python(),
        str(ROOT / "scripts" / "tune_ingest_workers.py"),
        "--ccns-file",
        str(batch_file),
        "--sample-size",
        str(args.tune_sample_size),
        "--max-workers",
        str(args.max_workers),
        "--item-timeout-seconds",
        str(args.batch_item_timeout_seconds),
        "--max-failure-rate",
        str(args.tune_max_failure_rate),
        "--fallback-workers",
        str(args.batch_workers),
        "--output",
        str(WORKER_TUNE_FILE),
        "--allow-parser-failures",
    ]
    run(cmd)
    result = read_json(WORKER_TUNE_FILE)
    if not isinstance(result, dict):
        return args.batch_workers
    recommended = int(result.get("recommended_workers") or args.batch_workers)
    return max(1, recommended)


def run_batch_ingest(batch_file: Path, workers: int, timeout_seconds: int) -> None:
    if not batch_file.exists() or batch_file.stat().st_size == 0:
        return
    run(
        [
            resolve_python(),
            str(ROOT / "scripts" / "batch_ingest.py"),
            "--ccns-file",
            str(batch_file),
            "--resume",
            "--workers",
            str(workers),
            "--item-timeout-seconds",
            str(timeout_seconds),
            "--progress-every",
            "25",
            "--status-file",
            str(INGEST_STATUS_FILE),
            "--failures-file",
            str(FAILURES_FILE),
        ]
    )


def run_retry_passes(pass_count: int, workers: int, timeout_seconds: int) -> None:
    if pass_count <= 0 or not FAILURES_FILE.exists() or FAILURES_FILE.stat().st_size == 0:
        return
    for _ in range(pass_count):
        run(
            [
                resolve_python(),
                str(ROOT / "scripts" / "retry_failed_ingest.py"),
                "--workers",
                str(workers),
                "--item-timeout-seconds",
                str(timeout_seconds),
                "--failures-file",
                str(FAILURES_FILE),
            ],
            check=False,
        )


def run_rediscovery(ccns: list[str], limit: int, top_n: int) -> None:
    if limit <= 0:
        return
    for ccn in ccns[:limit]:
        run(
            [
                resolve_python(),
                str(ROOT / "scripts" / "rediscover_mrf_urls.py"),
                "--ccn",
                ccn,
                "--top-n",
                str(top_n),
            ],
            check=False,
        )


def publish_local() -> None:
    run([resolve_python(), str(ROOT / "scripts" / "build_site_data.py")])
    run([resolve_python(), str(ROOT / "scripts" / "slim_parsed.py")])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", default=str(STATUS_FILE))
    parser.add_argument("--gap-file", default=str(GAP_FILE))
    parser.add_argument("--parse-gap-file", default=str(PARSE_GAP_FILE))
    parser.add_argument("--preview-gap-file", default=str(PREVIEW_GAP_FILE))
    parser.add_argument("--batch-file", default=str(BATCH_FILE))
    parser.add_argument("--exceptions-file", default=str(EXCEPTIONS_FILE))
    parser.add_argument("--loop-until-complete", action="store_true")
    parser.add_argument("--max-loops", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=int, default=0)
    parser.add_argument("--batch-workers", type=int, default=int(os.environ.get("HL_INGEST_WORKERS", "16")))
    parser.add_argument("--batch-item-timeout-seconds", type=int, default=2400)
    parser.add_argument("--retry-passes", type=int, default=0)
    parser.add_argument("--retry-workers", type=int, default=int(os.environ.get("HL_RETRY_WORKERS", "12")))
    parser.add_argument("--retry-item-timeout-seconds", type=int, default=1200)
    parser.add_argument("--rediscover-limit", type=int, default=0)
    parser.add_argument("--rediscover-top-n", type=int, default=5)
    parser.add_argument("--publish-each-loop", action="store_true")
    parser.add_argument("--auto-workers", action="store_true", help="Benchmark the current parse gap and use the fastest stable batch worker count")
    parser.add_argument("--max-workers", type=int, default=int(os.environ.get("HL_MAX_WORKERS", str(max(16, min(128, (os.cpu_count() or 4) * 8))))))
    parser.add_argument("--tune-sample-size", type=int, default=48)
    parser.add_argument("--tune-max-failure-rate", type=float, default=0.35)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    loop_count = args.max_loops if args.loop_until_complete else 1
    payload: dict[str, object] = {}
    for loop in range(1, max(1, loop_count) + 1):
        payload = write_closeout_artifacts(
            status_file=Path(args.status_file),
            gap_file=Path(args.gap_file),
            parse_gap_file=Path(args.parse_gap_file),
            preview_gap_file=Path(args.preview_gap_file),
            batch_file=Path(args.batch_file),
            exceptions_file=Path(args.exceptions_file),
            loop=loop,
            phase="snapshot",
        )
        print(
            f"coverage {payload['preview_positive']}/{payload['effective_target_total']} previews "
            f"({payload['coverage_pct']:.2f}%), parsed={payload['parsed_positive']}, "
            f"missing_parsed={payload['missing_parsed']}, missing_preview={payload['missing_preview']}",
            flush=True,
        )
        if not args.loop_until_complete:
            break
        if int(payload["missing_parsed"]) == 0 and int(payload["missing_preview"]) == 0:
            break
        if int(payload["missing_parsed"]) > 0:
            tuned_workers = auto_tune_batch_workers(Path(args.batch_file), args)
            print(f"closeout ingest workers={tuned_workers}", flush=True)
            run_batch_ingest(Path(args.batch_file), tuned_workers, args.batch_item_timeout_seconds)
            run_retry_passes(args.retry_passes, args.retry_workers, args.retry_item_timeout_seconds)
            refreshed = write_closeout_artifacts(
                status_file=Path(args.status_file),
                gap_file=Path(args.gap_file),
                parse_gap_file=Path(args.parse_gap_file),
                preview_gap_file=Path(args.preview_gap_file),
                batch_file=Path(args.batch_file),
                exceptions_file=Path(args.exceptions_file),
                loop=loop,
                phase="post_ingest",
            )
            if int(refreshed["missing_parsed"]) > 0 and args.rediscover_limit > 0:
                run_rediscovery(refreshed["parse_gap_ccns"], args.rediscover_limit, args.rediscover_top_n)
        if args.publish_each_loop:
            publish_local()
        payload = write_closeout_artifacts(
            status_file=Path(args.status_file),
            gap_file=Path(args.gap_file),
            parse_gap_file=Path(args.parse_gap_file),
            preview_gap_file=Path(args.preview_gap_file),
            batch_file=Path(args.batch_file),
            exceptions_file=Path(args.exceptions_file),
            loop=loop,
            phase="published" if args.publish_each_loop else "completed_loop",
        )
        if int(payload["missing_parsed"]) == 0 and int(payload["missing_preview"]) == 0:
            break
        if loop < max(1, loop_count) and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
