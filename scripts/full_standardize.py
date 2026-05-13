#!/usr/bin/env python3
"""Run the full live-MRF standardization pass end to end.

This is the resumable nationwide runner:
1. Ingest every live-MRF hospital into data/parsed/<ccn>.json
2. Rebuild compact site/data/prices and site/data/cpt-index.json
3. Optionally mirror artifacts to R2

Rerunning is safe with --resume because parsed hospitals are skipped.

For full-corpus runs on multi-core machines, use shard orchestration to split the
ordered live-MRF hospital list into disjoint ranges and run batch_ingest.py in
parallel subprocesses. That gives real process-level parallelism without changing
the ingest logic itself.
"""
import argparse
import datetime
import json
import math
import os
import subprocess
import sys
import time
from contextlib import ExitStack

import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from batch_ingest import DB, get_target_ccns


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, 'scripts')
DATA_DIR = os.path.join(ROOT, 'data')
SHARD_DIR = os.path.join(DATA_DIR, 'full_standardize_shards')
AGG_STATUS_FILE = os.path.join(DATA_DIR, 'full_standardize_parallel_status.json')


def run(cmd):
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def read_status(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def resolve_targets(args):
    conn = sqlite3.connect(DB)
    try:
        if args.state:
            limit = args.limit if args.limit and args.limit > 0 else None
            return get_target_ccns(conn, state=args.state, limit=limit, offset=args.offset)
        if args.limit and args.limit > 0:
            return get_target_ccns(conn, limit=args.limit, offset=args.offset)
        return get_target_ccns(conn, offset=args.offset)
    finally:
        conn.close()


def shard_ranges(count, shards):
    chunk = int(math.ceil(count / max(1, shards)))
    ranges = []
    start = 0
    while start < count:
        limit = min(chunk, count - start)
        ranges.append((start, limit))
        start += limit
    return ranges


def aggregate_status(shard_specs, started_at):
    done = success = failed = pending = total_items = skipped_existing = 0
    rate = 0.0
    shard_statuses = []
    for spec in shard_specs:
        status = read_status(spec['status_file'])
        if not status:
            shard_statuses.append({
                'shard': spec['index'],
                'offset': spec['offset'],
                'limit': spec['limit'],
                'state': 'starting',
            })
            pending += spec['limit']
            continue
        shard_statuses.append({
            'shard': spec['index'],
            'offset': spec['offset'],
            'limit': spec['limit'],
            'done': status.get('done', 0),
            'pending': status.get('pending', 0),
            'success': status.get('success', 0),
            'failed': status.get('failed', 0),
            'skipped_existing': status.get('skipped_existing', 0),
            'rate_per_second': status.get('rate_per_second', 0),
        })
        shard_skipped = status.get('skipped_existing', 0) or 0
        done += status.get('done', 0) + shard_skipped
        success += status.get('success', 0)
        failed += status.get('failed', 0)
        skipped_existing += shard_skipped
        pending += status.get('pending', 0)
        total_items += status.get('total_items', 0)
        rate += status.get('rate_per_second', 0) or 0
    eta = int(pending / rate) if rate > 0 and pending > 0 else 0 if pending == 0 else None
    return {
        'started_at': started_at,
        'updated_at': datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
        'done': done,
        'success': success,
        'failed': failed,
        'skipped_existing': skipped_existing,
        'pending': pending,
        'total_items': total_items,
        'rate_per_second': round(rate, 3),
        'eta_seconds': eta,
        'shards': shard_statuses,
    }


def run_sharded_ingest(args):
    targets = resolve_targets(args)
    total = len(targets)
    if total == 0:
        print("no live-MRF hospitals matched the requested slice", flush=True)
        return

    ranges = shard_ranges(total, args.shards)
    os.makedirs(SHARD_DIR, exist_ok=True)
    started_at = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
    print(f"launching {len(ranges)} shards over {total} target hospitals "
          f"(workers per shard={args.workers})", flush=True)

    shard_specs = []
    with ExitStack() as stack:
        for index, (offset, limit) in enumerate(ranges, 1):
            log_path = os.path.join(SHARD_DIR, f'shard-{index:02d}.log')
            status_path = os.path.join(SHARD_DIR, f'shard-{index:02d}.status.json')
            failures_path = os.path.join(SHARD_DIR, f'shard-{index:02d}.failures.jsonl')
            log_file = stack.enter_context(open(log_path, 'a'))
            cmd = [
                sys.executable,
                os.path.join(SCRIPTS, 'batch_ingest.py'),
                '--workers', str(args.workers),
                '--progress-every', str(args.progress_every),
                '--item-timeout-seconds', str(args.item_timeout_seconds),
                '--offset', str(args.offset + offset),
                '--limit', str(limit),
                '--status-file', status_path,
                '--failures-file', failures_path,
            ]
            if args.resume:
                cmd.append('--resume')
            if args.state:
                cmd.extend(['--state', args.state])
            print("$ " + " ".join(cmd), flush=True)
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            shard_specs.append({
                'index': index,
                'offset': args.offset + offset,
                'limit': limit,
                'status_file': status_path,
                'failures_file': failures_path,
                'log_file': log_path,
                'proc': proc,
            })

        while True:
            states = [spec['proc'].poll() for spec in shard_specs]
            agg = aggregate_status(shard_specs, started_at)
            write_json(AGG_STATUS_FILE, agg)
            print(f"    aggregate {agg['done']}/{total} | ok={agg['success']} fail={agg['failed']} "
                  f"| {agg['rate_per_second']:.2f}/s | eta={agg['eta_seconds']}s", flush=True)
            if all(code is not None for code in states):
                bad = [spec for spec in shard_specs if spec['proc'].returncode != 0]
                if bad:
                    details = ', '.join(
                        f"shard {spec['index']} rc={spec['proc'].returncode} log={spec['log_file']}"
                        for spec in bad
                    )
                    raise subprocess.CalledProcessError(1, f'sharded ingest failed: {details}')
                break
            time.sleep(10)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--workers', type=int, default=6)
    p.add_argument('--shards', type=int, default=1,
                   help='Process-level shards; each shard runs batch_ingest.py over a disjoint CCN slice')
    p.add_argument('--progress-every', type=int, default=25)
    p.add_argument('--item-timeout-seconds', type=int, default=0,
                   help='When >0, cap each hospital parse in a subprocess and continue on timeout')
    p.add_argument('--limit', type=int, default=0, help='0 means all live-MRF hospitals')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--state')
    p.add_argument('--resume', action='store_true', default=True)
    p.add_argument('--upload-r2', action='store_true')
    args = p.parse_args()

    started = time.time()
    if args.shards > 1:
        run_sharded_ingest(args)
    else:
        ingest = [
            sys.executable,
            os.path.join(SCRIPTS, 'batch_ingest.py'),
            '--workers', str(args.workers),
            '--progress-every', str(args.progress_every),
            '--item-timeout-seconds', str(args.item_timeout_seconds),
        ]
        if args.resume:
            ingest.append('--resume')
        if args.state:
            ingest.extend(['--state', args.state])
        if args.offset:
            ingest.extend(['--offset', str(args.offset)])
        if args.limit and args.limit > 0:
            ingest.extend(['--limit', str(args.limit)])
        else:
            ingest.append('--all')
        run(ingest)
    run([sys.executable, os.path.join(SCRIPTS, 'slim_parsed.py')])
    if args.upload_r2:
        env = os.environ.copy()
        env['SKIP_INGEST'] = '1'
        env['SKIP_SLIM'] = '1'
        env['UPLOAD_R2'] = '1'
        subprocess.run([sys.executable, os.path.join(SCRIPTS, 'stage4_refresh.py')], cwd=ROOT, check=True, env=env)
    print(f"full standardize pass finished in {time.time() - started:.1f}s", flush=True)


if __name__ == '__main__':
    main()
