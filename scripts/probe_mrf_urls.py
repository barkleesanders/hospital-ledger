#!/usr/bin/env python3
"""Probe MRF URLs from mrf_seed for liveness. Writes results to mrf_probe table.

Persistence rewrite (2026-11-15, Barklee's self-completing-refresh directive):
the probe is resumable and never all-or-nothing.

  * A per-run ledger (table mrf_probe_run) tracks every selected item:
    state pending|answered|skipped, the methods tried, and the answer.
  * Results commit in chunks (--chunk, default 50): a SIGKILL loses at
    most one in-flight chunk, and re-running with the same --run-id
    resumes where the ledger left off (answered items are never
    re-probed).
  * The by_status breakdown prints periodically (--status-every),
    at the end of every pass, and on any catchable exit — never only
    on success.
  * An item is answered by the first method that returns a real HTTP
    status. Transport-class failures (status -1/-2: no HTTP answer)
    leave the item pending for the NEXT method. An item still
    unanswered after all three methods (edge -> mini -> retry) is
    SKIPPED: recorded in the ledger as skipped, never written to
    mrf_probe as dead (its prior probe row stands), and reported in
    the run accounting. Skip = exhausted three different ways, never
    a silent drop and never a hospital death.
  * The three methods: edge = the hl_edge Worker fetch (HL_FETCH_PROXY;
    the ONLY primary lane — a bare direct probe from the VM is
    forbidden since the 2026-11-05 approval-card incident and this
    script fails closed without HL_FETCH_PROXY in edge mode);
    mini = the Mac mini's egress (SSH curl); retry = a second edge
    pass, time-separated by the whole mini pass, eligible only for
    items both edge and mini already attempted — transient WAF /
    rate-limit windows clear, so a delayed re-ask is a genuinely
    different attempt.
  * REMOVED 2026-11-15 (approval-card bug, Barklee report): the
    resilient-egress proxy at 127.0.0.1:8899 as a probe method. It is
    a CONNECT proxy: the VM process names each hospital domain to
    the gateway, and the approval gate cards EVERY first-seen
    hospital domain a VM process touches, whatever proxy carries it
    (3 cards in one burst, 2026-11-15 11:03 PT). A lane is card-free
    only when the VM process's peer is one fixed already-granted
    domain (the hl_edge Worker) or the fetch executes on another
    machine (the Mini). Never re-add a CONNECT-proxy or direct
    method for hospital domains.

Usage:
  python3 scripts/probe_mrf_urls.py [--state CA] [--ccn 050228] [--limit N]
      [--concurrency 8] [--all] [--run-id ID] [--method edge|mini|retry]
      [--chunk 50] [--status-every 200]

Exit codes: 0 = the pass processed its work set (coverage accounting is
printed; the caller gates on it). Nonzero = operational failure only
(DB error, refused edge proxy config, mini-leg batch failure).
"""
import argparse, asyncio, sqlite3, os, sys, time, datetime, subprocess, tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get('HL_PROBE_DB', os.path.join(ROOT, 'db', 'hospital_ledger.db'))

UA = "HospitalLedgerBot/0.1 (+https://github.com/hospital-ledger; open-source public-good price transparency crawler)"

METHODS = ('edge', 'mini', 'retry')
# No CONNECT-proxy method exists by design: see the module docstring
# (2026-11-15 approval-card removal note). 'proxy' rows in old ledgers
# are historical attempts by the retired method; they still count as
# attempts, but the skip rule below keys on the CURRENT METHODS set.
TRANSPORT_STATUSES = (-1, -2)

try:
    import httpx
except ImportError:
    print("Installing httpx...", file=sys.stderr)
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'httpx'])
    import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hl_edge  # noqa: E402  (HL_FETCH_PROXY set = fetch via hospitalledger.com/api/fetch)


def now_iso():
    return datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'


LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS mrf_probe_run (
  run_id TEXT NOT NULL,
  ccn TEXT NOT NULL,
  mrf_url TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  methods TEXT NOT NULL DEFAULT '',
  http_status INTEGER,
  alive INTEGER,
  method TEXT,
  updated_at TEXT,
  PRIMARY KEY (run_id, ccn, mrf_url)
)
"""


class Ledger:
    def __init__(self, conn, run_id):
        self.conn = conn
        self.run_id = run_id
        conn.execute(LEDGER_DDL)
        conn.commit()

    def seed(self, rows):
        self.conn.executemany(
            "INSERT OR IGNORE INTO mrf_probe_run (run_id, ccn, mrf_url, updated_at) VALUES (?,?,?,?)",
            [(self.run_id, ccn, url, now_iso()) for ccn, url in rows])
        self.conn.commit()

    def workset(self, method):
        """Pending items this method has not tried yet. The retry pass is
        eligible only for items BOTH authoritative lanes (edge + mini)
        already attempted — it is the third way, never a first ask."""
        out = []
        for ccn, url, methods in self.conn.execute(
                "SELECT ccn, mrf_url, methods FROM mrf_probe_run WHERE run_id=? AND state='pending'",
                (self.run_id,)):
            tried = [m for m in methods.split(',') if m]
            if method in tried:
                continue
            if method == 'retry' and not ('edge' in tried and 'mini' in tried):
                continue
            out.append((ccn, url))
        return out

    def counts(self):
        c = {'pending': 0, 'answered': 0, 'skipped': 0}
        for state, n in self.conn.execute(
                "SELECT state, COUNT(*) FROM mrf_probe_run WHERE run_id=? GROUP BY state",
                (self.run_id,)):
            c[state] = n
        c['total'] = sum(c.values())
        return c

    def record(self, results, method):
        """Apply one chunk of probe results: answered -> mrf_probe + ledger;
        transport-failed -> ledger attempt; exhausted -> skipped."""
        cur = self.conn.cursor()
        answered = [r for r in results if r['http_status'] not in TRANSPORT_STATUSES]
        if answered:
            cur.executemany("""INSERT OR REPLACE INTO mrf_probe
                (ccn, mrf_url, probed_at, http_status, content_type, content_length, final_url, alive)
                VALUES (:ccn, :mrf_url, :probed_at, :http_status, :content_type, :content_length, :final_url, :alive)""",
                answered)
        for r in results:
            row = cur.execute(
                "SELECT methods FROM mrf_probe_run WHERE run_id=? AND ccn=? AND mrf_url=?",
                (self.run_id, r['ccn'], r['mrf_url'])).fetchone()
            tried = [m for m in (row[0].split(',') if row and row[0] else []) if m]
            if method not in tried:
                tried.append(method)
            if r['http_status'] not in TRANSPORT_STATUSES:
                state = 'answered'
            elif all(m in tried for m in METHODS):
                state = 'skipped'
            else:
                state = 'pending'
            cur.execute("""UPDATE mrf_probe_run
                SET state=?, attempts=?, methods=?, http_status=?, alive=?, method=?, updated_at=?
                WHERE run_id=? AND ccn=? AND mrf_url=?""",
                (state, len(tried), ','.join(tried),
                 r['http_status'] if state == 'answered' else None,
                 r['alive'] if state == 'answered' else None,
                 method if state == 'answered' else None,
                 now_iso(), self.run_id, r['ccn'], r['mrf_url']))
        self.conn.commit()


async def probe_one(client, sem, ccn, url):
    async with sem:
        result = {
            'ccn': ccn, 'mrf_url': url,
            'probed_at': now_iso(),
            'http_status': None, 'content_type': None, 'content_length': None,
            'final_url': None, 'alive': 0,
        }
        if not url:
            result['http_status'] = -2
            result['content_type'] = 'err:EmptyURL'
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
        except hl_edge.EdgeConfigError:
            raise  # a refused proxy must not be recorded as a dead hospital
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            result['http_status'] = -1
            result['content_type'] = f"err:{type(e).__name__}"
        except httpx.HTTPError as e:
            # Any other httpx-class failure (transport internals, edge-leg
            # wrappers) is still a per-item transport outcome, never a
            # pass-killing exception (smoke catch, 2026-11-15: a ReadTimeout
            # escaping the edge transport killed a whole pass pre-ledger).
            result['http_status'] = -2
            result['content_type'] = f"err:{type(e).__name__}:{str(e)[:80]}"
        except Exception as e:
            result['http_status'] = -2
            result['content_type'] = f"err:{type(e).__name__}:{str(e)[:80]}"
        return result


async def guarded(client, sem, ccn, url):
    """probe_one with a final containment net: any exception that escapes
    its own handling becomes this item's -2 transport result, so one bad
    item can never kill the pass. EdgeConfigError still propagates."""
    try:
        return await probe_one(client, sem, ccn, url)
    except hl_edge.EdgeConfigError:
        raise
    except Exception as e:
        return {'ccn': ccn, 'mrf_url': url, 'probed_at': now_iso(),
                'http_status': -2, 'content_type': f"err:{type(e).__name__}:{str(e)[:80]}",
                'content_length': None, 'final_url': None, 'alive': 0}


def print_status(counts, by_status, label):
    print(f"  [{label}] coverage: answered={counts['answered']} skipped={counts['skipped']} "
          f"pending={counts['pending']} total={counts['total']}", file=sys.stderr)
    print(f"  [{label}] by http_status: {sorted(by_status.items(), key=lambda x: -x[1])}",
          file=sys.stderr)


async def run_http(ledger, rows, method, concurrency, chunk, status_every):
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency * 2)
    if method in ('edge', 'retry'):
        if not hl_edge.enabled():
            raise RuntimeError(
                f"method={method} but HL_FETCH_PROXY is unset — fail closed "
                "(direct-mode probe from the VM is forbidden, 2026-11-05 guard)")
        try:
            print(f"  {hl_edge.preflight()}", file=sys.stderr)
        except Exception as e:
            # Preflight is advisory: its timeout must not kill the pass
            # before a single item is tried (smoke catch, 2026-11-15 — a
            # preflight ReadTimeout aborted the edge pass at startup and
            # would have starved the whole method ladder). The items
            # themselves produce the leg's true verdict per-item.
            print(f"  preflight failed ({type(e).__name__}) — continuing; per-item outcomes decide", file=sys.stderr)
        client_kwargs = hl_edge.httpx_kwargs(async_=True, limits=limits)
    else:
        client_kwargs = {}
    by_status = {}
    done = 0
    buf = []

    def flush():
        nonlocal buf
        if buf:
            ledger.record(buf, method)
            buf = []

    async with httpx.AsyncClient(headers={'User-Agent': UA, 'Accept': '*/*'},
                                 limits=limits, verify=False,
                                 **client_kwargs) as client:
        tasks = [guarded(client, sem, ccn, url) for ccn, url in rows]
        try:
            for fut in asyncio.as_completed(tasks):
                r = await fut
                buf.append(r)
                by_status[r['http_status']] = by_status.get(r['http_status'], 0) + 1
                done += 1
                if len(buf) >= chunk:
                    flush()
                if done % 25 == 0 or done == len(tasks):
                    print(f"  probed {done}/{len(tasks)} (method={method})", file=sys.stderr)
                if done % status_every == 0:
                    print_status(ledger.counts(), by_status, f"pass {method} @{done}")
        finally:
            flush()
    return by_status


MINI_SSH = ['ssh', '-F', '/home/hatch/.ssh/mini_ssh_config', 'mini']
MINI_BATCH = 100
MINI_BATCH_TIMEOUT = 420  # seconds per batch; a timed-out batch consumes no attempts


def run_mini(ledger, rows, chunk, status_every):
    """Third method: probe via the Mac mini's egress (curl over SSH, batched).
    Items in a batch that times out stay pending and consume no attempt."""
    by_status = {}
    done = 0
    for start in range(0, len(rows), MINI_BATCH):
        batch = rows[start:start + MINI_BATCH]
        payload = ''.join(f"{ccn}\t{url}\n" for ccn, url in batch if url)
        empty = [(ccn, url) for ccn, url in batch if not url]
        results = []
        for ccn, url in empty:
            results.append({'ccn': ccn, 'mrf_url': url, 'probed_at': now_iso(),
                            'http_status': -2, 'content_type': 'err:EmptyURL',
                            'content_length': None, 'final_url': None, 'alive': 0})
        if payload:
            with tempfile.NamedTemporaryFile('w', suffix='.tsv', delete=False) as tf:
                tf.write(payload)
                local = tf.name
            remote = f"/tmp/hl-probe-{os.getpid()}-{start}.tsv"
            try:
                subprocess.run(['scp', '-F', '/home/hatch/.ssh/mini_ssh_config', local, f'mini:{remote}'],
                               check=True, timeout=120, capture_output=True)
                # xargs splits each "ccn<TAB>url" line into $0=ccn, $1=url.
                script = (
                    f"cat {remote} | xargs -P 8 -L 1 sh -c '"
                    f"code=$(curl -sI -m 20 -o /dev/null -w \"%{{http_code}}\" -L \"$1\" 2>/dev/null); "
                    f"if [ \"$code\" = \"405\" ] || {{ [ \"$code\" -ge 400 ] 2>/dev/null && [ \"$code\" -lt 500 ] 2>/dev/null; }}; then "
                    f"code=$(curl -s -m 20 -o /dev/null -w \"%{{http_code}}\" -L -r 0-1023 \"$1\" 2>/dev/null); fi; "
                    f"printf \"%s\\t%s\\n\" \"$0\" \"$code\"' ; rm -f {remote}"
                )
                proc = subprocess.run(MINI_SSH + [script], timeout=MINI_BATCH_TIMEOUT,
                                      capture_output=True, text=True)
                seen = set()
                for line in proc.stdout.splitlines():
                    parts = line.split('\t')
                    if len(parts) != 2:
                        continue
                    ccn, code = parts[0].strip(), parts[1].strip()
                    url = next((u for c, u in batch if c == ccn), None)
                    if url is None or ccn in seen:
                        continue
                    seen.add(ccn)
                    if code in ('000', '', '0'):
                        status = -1
                    else:
                        try:
                            status = int(code)
                        except ValueError:
                            status = -1
                    results.append({'ccn': ccn, 'mrf_url': url, 'probed_at': now_iso(),
                                    'http_status': status,
                                    'content_type': 'via-mini-curl' if status not in TRANSPORT_STATUSES else 'err:MiniNoAnswer',
                                    'content_length': None, 'final_url': None,
                                    'alive': 1 if 200 <= status < 400 else 0})
                missing = [(c, u) for c, u in batch if u and c not in seen]
                if missing and not results:
                    raise RuntimeError(f"mini batch @{start}: no results returned (leg failure)")
                # Items missing from a partial batch consume no attempt: leave pending
                # by simply not recording them.
            finally:
                os.unlink(local)
        for r in results:
            by_status[r['http_status']] = by_status.get(r['http_status'], 0) + 1
        if results:
            ledger.record(results, 'mini')
        done += len(batch)
        print(f"  probed {done}/{len(rows)} (method=mini)", file=sys.stderr)
        if done % status_every < MINI_BATCH or done == len(rows):
            print_status(ledger.counts(), by_status, f"pass mini @{done}")
    return by_status


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--state'); p.add_argument('--ccn'); p.add_argument('--limit', type=int)
    p.add_argument('--concurrency', type=int, default=8)
    p.add_argument('--all', action='store_true', help='probe all seed URLs')
    p.add_argument('--run-id', default=None, help='run ledger id (runner supplies; default: date+selection)')
    p.add_argument('--method', choices=METHODS, default='edge')
    p.add_argument('--chunk', type=int, default=50)
    p.add_argument('--status-every', type=int, default=200)
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

    run_id = args.run_id or ('adhoc-' + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'))
    ledger = Ledger(conn, run_id)
    ledger.seed(rows)
    work = ledger.workset(args.method)
    counts0 = ledger.counts()
    print(f"run_id={run_id} method={args.method}: seeded={counts0['total']} "
          f"already answered={counts0['answered']} skipped={counts0['skipped']} "
          f"workset(this method)={len(work)} (concurrency={args.concurrency})", file=sys.stderr)

    t0 = time.time()
    by_status = {}
    if work:
        if args.method == 'mini':
            by_status = run_mini(ledger, work, args.chunk, args.status_every)
        else:
            by_status = asyncio.run(run_http(ledger, work, args.method, args.concurrency,
                                             args.chunk, args.status_every))
    elapsed = time.time() - t0
    counts = ledger.counts()
    print_status(counts, by_status, f"pass {args.method} end")
    print(f"coverage: answered={counts['answered']} skipped={counts['skipped']} "
          f"pending={counts['pending']} total={counts['total']}", file=sys.stderr)
    if counts['pending'] == 0:
        alive = conn.execute(
            "SELECT COUNT(*) FROM mrf_probe_run WHERE run_id=? AND state='answered' AND alive=1",
            (run_id,)).fetchone()[0]
        print(f"\n=== probe complete in {elapsed:.1f}s ===", file=sys.stderr)
        print(f"alive: {alive}/{counts['answered']} answered "
              f"({100*alive/max(1,counts['answered']):.1f}%); skipped: {counts['skipped']}", file=sys.stderr)
    else:
        print(f"\n=== probe pass complete (method={args.method}) in {elapsed:.1f}s: "
              f"{counts['pending']} item(s) still pending another method ===", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
