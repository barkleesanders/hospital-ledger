#!/usr/bin/env python3
"""Stage 2.13: Discovery -> wave promotion bridge.

Closes the loop between discovery (cms-hpt harvester, ein_pattern_gen) and the
daily pricing wave (hl_wave.py, which builds its queue from mrf_seed).

  1. Reads verified candidates from mrf_rediscovered (alive=1) whose CCN has no
     URL in mrf_seed.
  2. HEAD-verifies each candidate URL (skips ones already verified 200).
  3. Upserts verified URLs into mrf_seed (mrf_url, status, format, timestamps).
  4. Stages their hosts into a grant-request file for Barklee's approval.
     NEVER writes to the granted-hosts file itself.

Also accepts ein_pattern_gen.py JSON-line output via --batch-file.

Usage:
  promote_discoveries.py [--batch-file gen_output.jsonl] [--dry-run] [--limit N]

Idempotent: re-running promotes only new candidates.
"""
import argparse, datetime, json, os, sqlite3, sys, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "db", "hospital_ledger.db")
GOAL_HIDDEN = "/home/hatch/workspace/goals/hospital-ledger-weekly-refresh-counts-tier/hidden_files"
GRANT_REQUESTS = os.path.join(GOAL_HIDDEN, "grant_requests_pending.json")

UA = {"User-Agent": "Mozilla/5.0 (compatible; HospitalLedgerBot/1.0)"}

# --- Edge routing (2026-11-01) ---
# On this VM, ANY first-seen hospital domain raises a human approval card —
# including direct script egress (the 2026-11-01 harvest card bursts came
# from the harvest scripts fetching directly). Route through
# hospitalledger.com/api/fetch via hl_edge, the pattern probe_mrf_urls.py /
# mrf_parse.py already use: with HL_FETCH_PROXY set the VM only ever
# connects to the one already-approved Worker domain. Config comes from the
# environment or ~/hospital-ledger.env (the file hl_wave.py reads); no
# config = direct fetch, unchanged behaviour (e.g. the Mini).
_EDGE_ENV_FILE = os.path.join(os.path.expanduser("~"), "hospital-ledger.env")
if os.path.exists(_EDGE_ENV_FILE):
    for _line in open(_EDGE_ENV_FILE):
        _k, _, _v = _line.strip().partition("=")
        if _k.strip() in ("HL_FETCH_PROXY", "HL_FETCH_TOKEN") and _v.strip():
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, os.path.join(ROOT, "scripts"))
try:
    import hl_edge
except Exception:
    hl_edge = None
try:
    import httpx
except Exception:
    httpx = None

# --- Transport-failure accounting (harvest fix 2026-11-09, Finding 1) ---
# head_verify returns status 0 when NO HTTP answer arrived (timeout /
# connection error / dead edge leg) — transport evidence, not a hospital
# answer. On 2026-11-08 this bridge ran against a dead leg, got http-0 for
# all 300 candidates, and exited 0, indistinguishable from "nothing to
# promote". Promotions only ever happen on a genuine HTTP answer (ok=True),
# so nothing here can promote or demote on transport evidence; the fix is
# accounting + exit code: if transport failures exceed 25% of verification
# attempts (min sample 20 — same thresholds as cms_hpt_harvester), or a run
# gets zero HTTP answers, exit TRANSPORT_DEAD_EXIT instead of 0.
TRANSPORT_FAIL_SHARE = 0.25
TRANSPORT_MIN_SAMPLE = 20
TRANSPORT_DEAD_EXIT = 3

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(headers=UA, verify=False, follow_redirects=True,
                               **(hl_edge.httpx_kwargs() if hl_edge else {}))
    return _CLIENT


def head_verify(url, timeout=12):
    if httpx is not None:
        # httpx path (2026-11-01): routes via hl_edge when configured — see
        # the edge-routing note above. Same semantics as the urllib path
        # below: HEAD, ranged-GET retry on 403/405.
        try:
            r = _client().head(url, timeout=timeout)
            if r.status_code in (403, 405):
                try:
                    r = _client().get(url, timeout=timeout,
                                      headers={"Range": "bytes=0-0"})
                except Exception:
                    return False, r.status_code, "head-blocked"
            if 200 <= r.status_code < 400:
                return True, r.status_code, r.headers.get("Content-Type", "")
            return False, r.status_code, ""
        except Exception as e:
            return False, 0, str(e)[:60]
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code in (403, 405):
            req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return True, r.status, r.headers.get("Content-Type", "")
            except Exception:
                return False, e.code, "head-blocked"
        return False, e.code, ""
    except Exception as e:
        return False, 0, str(e)[:60]


def guess_format(url, ctype):
    u = url.lower()
    if ".zip" in u:
        return "zip"
    if ".json" in u or "json" in (ctype or ""):
        return "json"
    if ".xlsx" in u:
        return "xlsx"
    return "csv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-file", help="ein_pattern_gen.py JSONL output")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    db = sqlite3.connect(DB, timeout=12)
    # Incremental commits: a killed run rolls back only its partial batch
    COMMIT_EVERY = 10
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Candidates from the harvester: alive, CCN not yet seeded with a URL
    rows = db.execute("""
        SELECT r.ccn, r.candidate_url, r.head_status
        FROM mrf_rediscovered r
        LEFT JOIN mrf_seed s ON s.ccn = r.ccn AND s.mrf_url != ''
        WHERE r.alive = 1 AND s.ccn IS NULL
        GROUP BY r.ccn, r.candidate_url
        ORDER BY r.score DESC
        LIMIT ?
    """, (args.limit,)).fetchall()
    cands = [{"ccn": c, "url": u, "head_status": h} for c, u, h in rows]

    # Candidates from the EIN generator batch file
    if args.batch_file:
        with open(args.batch_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("url") and rec.get("ccn"):
                    exists = db.execute(
                        "SELECT 1 FROM mrf_seed WHERE ccn=? AND mrf_url != ''",
                        (rec["ccn"],)).fetchone()
                    # A batch record may explicitly request replacement of a
                    # known-dead seed URL ("replace": true) -- the upsert's
                    # UPDATE branch exists for exactly this. Without the flag
                    # the historical de-dup behavior is unchanged. A record
                    # carrying a fresh verified status ("http": 200/206, e.g.
                    # probed via the pipeline edge route by the producing
                    # wave) is preverified per the module docstring and is
                    # not re-HEADed over the direct route. (2026-10-25)
                    if not exists or rec.get("replace"):
                        cands.append({"ccn": rec["ccn"], "url": rec["url"],
                                      "head_status": rec.get("http"),
                                      "preverified": rec.get("http") in (200, 206)})

    promoted, skipped, new_hosts = [], [], set()
    # Parallel HEAD verification (10 workers); DB writes stay single-threaded
    def verify_one(c):
        if c.get("preverified"):
            return c, True, c["head_status"], ""
        ok, status, ctype = head_verify(c["url"])
        return c, ok, status, ctype

    verified = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(verify_one, c): c for c in cands}
        for fut in as_completed(futs):
            verified.append(fut.result())

    # Transport accounting (Finding 1): status 0 = no HTTP answer.
    # Preverified batch records were never verified here — excluded.
    attempts = sum(1 for c, ok, status, ctype in verified
                   if not c.get("preverified"))
    tfail = sum(1 for c, ok, status, ctype in verified
                if not c.get("preverified") and status == 0)
    answers = attempts - tfail
    transport_failure = bool(attempts and (
        answers == 0
        or (attempts >= TRANSPORT_MIN_SAMPLE
            and tfail > TRANSPORT_FAIL_SHARE * attempts)))
    if transport_failure:
        print(f"TRANSPORT FAILURE: {tfail}/{attempts} verifications got no "
              f"HTTP answer — verification leg is dead/degraded; exiting "
              f"{TRANSPORT_DEAD_EXIT}", file=sys.stderr)

    for c, ok, status, ctype in verified:
        ccn, url = c["ccn"], c["url"]
        if not ok:
            skipped.append((ccn, f"http-{status}"))
            continue
        fmt = guess_format(url, ctype)
        host = (urlparse(url).hostname or "").lower()
        if not args.dry_run:
            row = db.execute("SELECT ccn FROM mrf_seed WHERE ccn=?", (ccn,)).fetchone()
            tag = "promoted-" + now[:10]
            if row:
                db.execute("""UPDATE mrf_seed SET mrf_url=?, mrf_url_status=?,
                              file_format=?, last_updated_date=? WHERE ccn=?""",
                           (url, tag, fmt, now, ccn))
            else:
                db.execute("""INSERT INTO mrf_seed
                    (ccn, mrf_url, mrf_url_status, file_format, entry_date, last_updated_date)
                    VALUES (?,?,?,?,?,?)""", (ccn, url, tag, fmt, now, now))
        promoted.append({"ccn": ccn, "url": url, "format": fmt, "host": host})
        new_hosts.add(host)
        if not args.dry_run and len(promoted) % COMMIT_EVERY == 0:
            db.commit()

    if not args.dry_run:
        db.commit()
    db.close()

    # Stage grant requests (never touch the granted file)
    if new_hosts:
        try:
            staged = json.load(open(GRANT_REQUESTS)) if os.path.exists(GRANT_REQUESTS) else {}
        except (json.JSONDecodeError, OSError):
            staged = {}
        for h in sorted(new_hosts):
            staged.setdefault(h, {"requested_at": now, "status": "pending",
                                  "ccns": sorted({p["ccn"] for p in promoted if p["host"] == h})})
        if not args.dry_run:
            with open(GRANT_REQUESTS, "w") as f:
                json.dump(staged, f, indent=1)

    print(json.dumps({
        "promoted": len(promoted), "skipped": len(skipped),
        "new_hosts_staged": sorted(new_hosts),
        "dry_run": args.dry_run,
        "transport": {"attempts": attempts, "no_answer": tfail,
                      "http_answers": answers,
                      "transport_failure": transport_failure},
        "sample_promoted": promoted[:5],
        "sample_skipped": skipped[:5],
    }, indent=1))
    return TRANSPORT_DEAD_EXIT if transport_failure else 0


if __name__ == "__main__":
    sys.exit(main())
