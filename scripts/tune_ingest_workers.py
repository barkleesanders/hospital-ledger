#!/usr/bin/env python3
"""Benchmark ingest worker counts against live closeout targets.

The tuner intentionally runs real ingest work so the coverage closeout keeps
moving while it measures the fastest stable parallelism for the current cloud
runner.  Results are written to data/worker_tune_results.json and can be used
by full_standardize.py via --auto-workers.
"""

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from batch_ingest import DB, ROOT, get_target_ccns, parsed_ok

DATA_DIR = os.path.join(ROOT, "data")
RESULTS_FILE = os.path.join(DATA_DIR, "worker_tune_results.json")
STATUS_FILE = os.path.join(DATA_DIR, "worker_tune.status.json")
FAILURES_FILE = os.path.join(DATA_DIR, "worker_tune.failures.jsonl")


def cpu_default_max():
    cpus = os.cpu_count() or 4
    return max(16, min(128, cpus * 8))


def parse_worker_steps(raw, max_workers):
    if raw:
        steps = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            value = int(token)
            if value < 1:
                raise argparse.ArgumentTypeError("worker steps must be >= 1")
            if value <= max_workers:
                steps.append(value)
        return sorted(set(steps))

    base = [4, 8, 12, 16, 24, 32, 48, 64, 96, 128]
    steps = [value for value in base if value <= max_workers]
    if max_workers not in steps:
        steps.append(max_workers)
    return sorted(set(steps))


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def load_candidates(args):
    if args.ccns_file:
        with open(args.ccns_file) as handle:
            ccns = [line.strip() for line in handle if line.strip()]
    else:
        conn = sqlite3.connect(DB)
        try:
            ccns = get_target_ccns(conn, state=args.state, limit=None, offset=args.offset)
        finally:
            conn.close()
    if args.resume:
        ccns = [ccn for ccn in ccns if not parsed_ok(ccn)]
    if args.limit:
        ccns = ccns[: args.limit]
    return ccns


def run_probe(worker_count, ccns, args):
    with tempfile.NamedTemporaryFile("w", delete=False, dir=DATA_DIR, prefix="worker-tune-", suffix=".ccns") as handle:
        for ccn in ccns:
            handle.write(ccn + "\n")
        ccn_file = handle.name

    for stale_path in (STATUS_FILE,):
        try:
            os.unlink(stale_path)
        except FileNotFoundError:
            pass

    started = time.time()
    cmd = [
        sys.executable,
        os.path.join(ROOT, "scripts", "batch_ingest.py"),
        "--ccns-file",
        ccn_file,
        "--workers",
        str(worker_count),
        "--resume",
        "--progress-every",
        str(max(1, len(ccns))),
        "--status-every-seconds",
        "5",
        "--item-timeout-seconds",
        str(args.item_timeout_seconds),
        "--status-file",
        STATUS_FILE,
        "--failures-file",
        FAILURES_FILE,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    elapsed = max(time.time() - started, 0.001)
    status = read_json(STATUS_FILE)
    attempted = status.get("done", 0) or 0
    succeeded = status.get("success", 0) or 0
    failed = status.get("failed", 0) or 0
    skipped = status.get("skipped_existing", 0) or 0
    # Prefer the child status rate because it excludes process startup overhead,
    # but fall back to wall time if the status was not written.
    rate = status.get("rate_per_second") or (attempted / elapsed if attempted else 0)
    result = {
        "workers": worker_count,
        "ccns": ccns,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_existing": skipped,
        "elapsed_seconds": round(elapsed, 3),
        "rate_per_second": round(rate, 3),
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }
    try:
        os.unlink(ccn_file)
    except OSError:
        pass
    return result


def is_stable(result, args):
    if result["returncode"] != 0 and not args.allow_parser_failures:
        return False
    attempted = result["attempted"]
    if attempted <= 0:
        return False
    fail_rate = result["failed"] / attempted
    return fail_rate <= args.max_failure_rate


def choose_recommendation(results, args):
    stable = [result for result in results if is_stable(result, args)]
    pool = stable or [result for result in results if result["attempted"] > 0]
    if not pool:
        return args.fallback_workers
    best = max(pool, key=lambda item: (item["rate_per_second"], item["workers"]))
    return best["workers"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find the fastest stable batch_ingest worker count for this cloud runner.")
    parser.add_argument("--ccns-file", help="Optional newline-delimited CCN list to benchmark instead of all live-MRF gaps")
    parser.add_argument("--state", help="Optional state filter when discovering live-MRF benchmark targets")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many discovered targets before sampling")
    parser.add_argument("--limit", type=int, default=0, help="Maximum discovered targets to consider before sampling; 0 means no cap")
    parser.add_argument("--sample-size", type=int, default=48, help="CCNs tested per worker step")
    parser.add_argument("--max-workers", type=int, default=int(os.environ.get("HL_MAX_WORKERS", cpu_default_max())))
    parser.add_argument("--worker-steps", help="Comma-delimited worker counts to test; defaults to 4,8,12,16,24,32,48,64,96,128 up to --max-workers")
    parser.add_argument("--item-timeout-seconds", type=int, default=600, help="Per-CCN timeout passed through to batch_ingest")
    parser.add_argument("--max-failure-rate", type=float, default=0.35, help="Highest acceptable failure rate for a worker step")
    parser.add_argument("--fallback-workers", type=int, default=int(os.environ.get("HL_INGEST_WORKERS", "16")))
    parser.add_argument("--resume", action="store_true", default=True, help="Skip already parsed CCNs during tuning")
    parser.add_argument("--allow-parser-failures", action="store_true", help="Treat parser failure return codes as benchmark data instead of instability")
    parser.add_argument("--output", default=RESULTS_FILE)
    args = parser.parse_args(argv)

    if args.sample_size < 1:
        parser.error("--sample-size must be >= 1")
    if args.max_workers < 1:
        parser.error("--max-workers must be >= 1")
    if not 0 <= args.max_failure_rate <= 1:
        parser.error("--max-failure-rate must be between 0 and 1")

    steps = parse_worker_steps(args.worker_steps, args.max_workers)
    candidates = load_candidates(args)
    if not candidates:
        payload = {
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "candidate_count": 0,
            "worker_steps": steps,
            "recommended_workers": args.fallback_workers,
            "reason": "no_unparsed_candidates",
            "results": [],
        }
        write_json(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    results = []
    cursor = 0
    for workers in steps:
        if cursor >= len(candidates):
            break
        sample = candidates[cursor : cursor + args.sample_size]
        cursor += len(sample)
        print(f"benchmark workers={workers} sample={len(sample)}", flush=True)
        result = run_probe(workers, sample, args)
        results.append(result)
        payload = {
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "candidate_count": len(candidates),
            "sample_size": args.sample_size,
            "max_workers": args.max_workers,
            "worker_steps": steps,
            "recommended_workers": choose_recommendation(results, args),
            "results": results,
        }
        write_json(args.output, payload)
        if not is_stable(result, args) and workers >= 16:
            print(f"stopping benchmark after unstable workers={workers}", flush=True)
            break

    payload = read_json(args.output)
    payload["recommended_workers"] = choose_recommendation(results, args)
    payload["completed_at"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    write_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
