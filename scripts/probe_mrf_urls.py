#!/usr/bin/env python3
"""Probe MRF URLs from mrf_seed for liveness. Writes results to mrf_probe table.

Usage:
  python3 scripts/probe_mrf_urls.py [--state CA] [--ccn 050228] [--limit N] [--concurrency 8] [--all]
"""
import argparse, asyncio, sqlite3, os, sys, time, datetime
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')

UA = "HospitalLedgerBot/0.1 (+https://github.com/hospital-ledger; open-source public-good price transparency crawler)"

try:
    import httpx
except ImportError:
    print("Installing httpx...", file=sys.stderr)
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'httpx'])
    import httpx


async def probe_one(client, sem, ccn, url):
    async with sem:
        result = {
            'ccn': ccn, 'mrf_url': url,
            'probed_at': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'http_status': None, 'content_type': None, 'content_length': None,
            'final_url': None, 'alive': 0,
        }
        if not url:
            return result
        try:
            r = await client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code == 405 or (r.status_code >= 400 and r.status_code < 500):
                r = await client.get(url, follow_redirects=True, timeout=30.0,
                                     headers={'Range': 'bytes=0-1023'})
            result['http_status'] = r.status_code
            result['content_type'] = r.headers.get('content-type', '')[:200]
            cl = r.headers.get('content-length')
            result['content_length'] = int(cl) if cl and cl.isdigit() else None
            result['final_url'] = str(r.url)[:500]
            result['alive'] = 1 if 200 <= r.status_code < 400 else 0
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            result['http_status'] = -1
            result['content_type'] = f"err:{type(e).__name__}"
        except Exception as e:
            result['http_status'] = -2
            result['content_type'] = f"err:{type(e).__name__}:{str(e)[:80]}"
        return result


async def run(rows, concurrency):
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency * 2)
    async with httpx.AsyncClient(headers={'User-Agent': UA, 'Accept': '*/*'},
                                 limits=limits, verify=False) as client:
        tasks = [probe_one(client, sem, ccn, url) for ccn, url in rows]
        done = 0
        results = []
        for fut in asyncio.as_completed(tasks):
            r = await fut
            results.append(r)
            done += 1
            if done % 25 == 0 or done == len(tasks):
                alive = sum(1 for x in results if x['alive'])
                print(f"  probed {done}/{len(tasks)} | alive={alive} ({100*alive/done:.1f}%)", file=sys.stderr)
        return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--state'); p.add_argument('--ccn'); p.add_argument('--limit', type=int)
    p.add_argument('--concurrency', type=int, default=8)
    p.add_argument('--all', action='store_true', help='probe all seed URLs')
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    q = "SELECT ccn, mrf_url FROM mrf_seed WHERE mrf_url != ''"
    params = []
    if args.state: q += " AND state = ?"; params.append(args.state)
    if args.ccn:   q += " AND ccn = ?";   params.append(args.ccn)
    if args.limit: q += f" LIMIT {args.limit}"
    elif not args.all and not args.state and not args.ccn:
        q += " LIMIT 25"
    rows = c.execute(q, params).fetchall()
    print(f"probing {len(rows)} URLs (concurrency={args.concurrency})...", file=sys.stderr)
    t0 = time.time()
    results = asyncio.run(run(rows, args.concurrency))
    elapsed = time.time() - t0

    c.executemany("""INSERT OR REPLACE INTO mrf_probe
        (ccn, mrf_url, probed_at, http_status, content_type, content_length, final_url, alive)
        VALUES (:ccn, :mrf_url, :probed_at, :http_status, :content_type, :content_length, :final_url, :alive)""",
        results)
    conn.commit()

    alive = sum(1 for x in results if x['alive'])
    print(f"\n=== probe complete in {elapsed:.1f}s ===", file=sys.stderr)
    print(f"alive: {alive}/{len(results)} ({100*alive/max(1,len(results)):.1f}%)", file=sys.stderr)
    print(f"dead:  {len(results)-alive}/{len(results)}", file=sys.stderr)
    by_status = {}
    for r in results:
        by_status[r['http_status']] = by_status.get(r['http_status'], 0) + 1
    print("by http_status:", sorted(by_status.items(), key=lambda x: -x[1]), file=sys.stderr)


if __name__ == '__main__':
    main()
