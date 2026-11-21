#!/usr/bin/env python3
"""hl_self_heal.py — Hospital Ledger self-healing pipeline (gen-20261006).

Detect -> repair-ladder -> escalate for three failure classes:

  1. DEAD MRF URLS      (probe says 404/410, or content vanished)
  2. PARSE FAILURES     (quarantined CCNs from wave results / known-quarantined.json)
  3. SCHEMA DRIFT       (content-type or size anomalies vs probe history)

Repair ladder per dead URL (cheapest first):
  r1. <host>/cms-hpt.txt           (CMS 2024 rule: root txt names the MRF)
  r2. transparency-page re-crawl   (mrf_page from mrf_seed, anchor scoring)
  r3. Wayback CDX lookup          (web.archive.org exact-URL history)
  else -> ESCALATE (needs Barklee / browser leg / CMS track)

Transport failures (http_status=-1, our egress flaked) are NEVER treated as
dead URLs. They are marked for re-probe via an alternate route instead of
burning the repair ladder on a URL that was never proven broken.

Blocked (403) URLs are categorized, not repaired: per the ledger's standing
doctrine, 403 is "site blocks automated access", never "no MRF". These need
the real-browser byte-verification leg.

Modes:
  --dry-run (DEFAULT): detect + run the read-only repair probes (cms-hpt.txt
      GET, Wayback CDX, page crawl are all plain GETs). Writes NOTHING to the
      DB or R2. Reports what WOULD be repaired.
  --live: persist successful repairs to mrf_rediscovered. Requires explicit
      flag. Never publishes, never deletes R2 objects, never touches
      prices/index.json.

Exit codes: 0 = healthy, or every detection auto-repaired;
            1 = operational failure (DB unreachable, bad args);
            2 = escalations pending (needs Barklee).

Writes (live mode only): mrf_rediscovered rows. Nothing else.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
from urllib.parse import urljoin, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "db", "hospital_ledger.db")
KNOWN_QUARANTINED = os.path.join(
    os.path.expanduser("~"),
    "workspace/hospital-ledger-push/pipeline/known-quarantined.json",
)

UA = ("HospitalLedgerBot/0.1 (+https://github.com/hospital-ledger; "
      "open-source public-good price transparency crawler)")

try:
    import httpx
except ImportError:  # pragma: no cover
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "httpx"])
    import httpx

# ---------------------------------------------------------------------------
# Failure classification


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds") + "Z"


def classify_probe(http_status, alive) -> str:
    """Map a probe row to a failure class.

    Returns one of: ok | transport-failure | genuinely-dead | blocked |
    server-error | unknown.
    """
    if alive == 1:
        return "ok"
    if http_status in (-1, None):
        # Our side failed to complete the probe (egress flap, timeout,
        # DNS). The URL was never proven dead.
        return "transport-failure"
    if http_status in (404, 410):
        return "genuinely-dead"
    if http_status == 403:
        return "blocked"
    if isinstance(http_status, int) and 500 <= http_status <= 599:
        return "server-error"
    if isinstance(http_status, int) and 400 <= http_status <= 499:
        return "genuinely-dead"
    return "unknown"


# ---------------------------------------------------------------------------
# Phase 1: detection (read-only)


def latest_probes(conn: sqlite3.Connection):
    """Latest probe row per (ccn, mrf_url)."""
    cur = conn.execute(
        """
        SELECT p.ccn, p.mrf_url, p.http_status, p.content_type,
               p.content_length, p.final_url, p.alive, p.probed_at
        FROM mrf_probe p
        INNER JOIN (
            SELECT ccn, mrf_url, MAX(probed_at) AS mx
            FROM mrf_probe GROUP BY ccn, mrf_url
        ) latest
          ON p.ccn = latest.ccn AND p.mrf_url = latest.mrf_url
         AND p.probed_at = latest.mx
        """
    )
    return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]


def detect_dead_urls(conn: sqlite3.Connection) -> dict:
    """Bucket every probed URL by failure class."""
    buckets: dict[str, list] = {
        "genuinely-dead": [], "transport-failure": [],
        "blocked": [], "server-error": [], "unknown": [],
    }
    for row in latest_probes(conn):
        cls = classify_probe(row["http_status"], row["alive"])
        if cls != "ok":
            buckets[cls].append(row)
    return buckets


def detect_parse_failures(conn: sqlite3.Connection) -> list:
    """Quarantined CCNs: known-quarantined.json + recent wave result files."""
    out: list[dict] = []
    if os.path.isfile(KNOWN_QUARANTINED):
        try:
            data = json.load(open(KNOWN_QUARANTINED))
            ccns = data if isinstance(data, list) else data.get("ccns", [])
            for ccn in ccns:
                out.append({"ccn": ccn, "source": "known-quarantined.json",
                            "class": "parse-failure"})
        except (OSError, json.JSONDecodeError):
            pass
    # Latest wave results under ~/hl-vm-waves/results (if present).
    results_dir = os.path.expanduser("~/hl-vm-waves/results")
    if os.path.isdir(results_dir):
        for fn in sorted(os.listdir(results_dir)):
            if not fn.endswith(".json"):
                continue
            try:
                res = json.load(open(os.path.join(results_dir, fn)))
                for ccn in res.get("quarantined", []) or []:
                    if not any(e["ccn"] == ccn for e in out):
                        out.append({"ccn": ccn, "source": fn, "class": "parse-failure"})
            except (OSError, json.JSONDecodeError):
                continue
    return out


def detect_schema_drift(conn: sqlite3.Connection) -> list:
    """Content-type / size anomalies vs each URL's own probe history.

    Flags: a URL that served a data content-type and now serves text/html
    (blocking/interstitial page), or whose content-length collapsed by >90%.
    """
    out: list[dict] = []
    cur = conn.execute(
        """
        SELECT ccn, mrf_url,
               MAX(CASE WHEN rn = 1 THEN content_type END) AS latest_ct,
               MAX(CASE WHEN rn = 1 THEN content_length END) AS latest_len,
               MAX(CASE WHEN rn = 1 THEN http_status END) AS latest_status,
               MAX(CASE WHEN rn = 2 THEN content_type END) AS prev_ct,
               MAX(CASE WHEN rn = 2 THEN content_length END) AS prev_len
        FROM (
            SELECT ccn, mrf_url, content_type, content_length, http_status,
                   ROW_NUMBER() OVER (PARTITION BY ccn, mrf_url
                                      ORDER BY probed_at DESC) AS rn
            FROM mrf_probe
        )
        WHERE rn <= 2
        GROUP BY ccn, mrf_url
        HAVING latest_status BETWEEN 200 AND 299
        """
    )
    data_ct = re.compile(r"(csv|json|excel|spreadsheet|zip|octet-stream)", re.I)
    html_ct = re.compile(r"text/html", re.I)
    for ccn, url, lct, llen, _, pct, plen in cur.fetchall():
        lct, pct = lct or "", pct or ""
        if pct and data_ct.search(pct) and html_ct.search(lct):
            out.append({"ccn": ccn, "mrf_url": url, "class": "schema-drift",
                        "detail": f"content-type flipped {pct[:40]} -> {lct[:40]} "
                                  "(likely blocking page)"})
        elif plen and llen and plen > 1024 and llen < plen * 0.1:
            out.append({"ccn": ccn, "mrf_url": url, "class": "schema-drift",
                        "detail": f"content-length collapsed {plen} -> {llen} bytes"})
    return out


# ---------------------------------------------------------------------------
# Phase 2: repair ladder (read-only probes; persistence only in live mode)


def _client_for(url: str, timeout: float) -> "httpx.Client":
    """httpx client for a URL. Loopback/private hosts bypass the egress
    proxy (proxy env vars mangle localhost); external hosts use it."""
    host = (urlparse(url).hostname or "")
    loopback = host in ("localhost", "127.0.0.1", "::1") or host.startswith("10.") \
        or host.startswith("192.168.") or host.startswith("172.16.")
    return httpx.Client(headers={"User-Agent": UA}, follow_redirects=True,
                        timeout=timeout, trust_env=not loopback)


def head_verify(url: str, timeout: float = 30.0) -> dict:
    """HEAD (fall back to ranged GET) to verify a candidate URL serves a file."""
    try:
        with _client_for(url, timeout) as c:
            r = c.head(url)
            if r.status_code == 405 or 400 <= r.status_code < 500:
                r = c.get(url, headers={"Range": "bytes=0-1023"})
            ct = r.headers.get("content-type", "")
            return {"ok": 200 <= r.status_code < 300,
                    "http_status": r.status_code,
                    "content_type": ct[:120],
                    "final_url": str(r.url)}
    except Exception as e:  # transport failure on OUR side
        return {"ok": False, "http_status": -1, "content_type": "",
                "final_url": url, "error": str(e)[:120]}


def repair_r1_cmshpt(dead_url: str, timeout: float = 30.0) -> dict | None:
    """Rung 1: fetch <host>/cms-hpt.txt, parse the mrf-url field."""
    try:
        host = urlparse(dead_url)
        txt_url = f"{host.scheme}://{host.netloc}/cms-hpt.txt"
        with _client_for(txt_url, timeout) as c:
            r = c.get(txt_url)
        if r.status_code != 200:
            return None
        m = re.search(r"^mrf-url:\s*(\S+)", r.text, re.M | re.I)
        if not m:
            return None
        cand = m.group(1).strip()
        v = head_verify(cand)
        if v["ok"]:
            return {"rung": "cms-hpt.txt", "candidate_url": cand,
                    "source_page": txt_url, "verify": v}
        return None
    except Exception:
        return None


ANCHOR_RE = re.compile(
    r'<a\s+[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL)
TAG_STRIP = re.compile(r"<[^>]+>")
FILE_EXT_RE = re.compile(r"\.(csv|json|xlsx?|zip|xml)(\?|$)", re.IGNORECASE)
CMS_NAMING_RE = re.compile(r"standard[\s\-_]?charges?", re.IGNORECASE)


def repair_r2_page_crawl(dead_url: str, mrf_page: str | None,
                         timeout: float = 30.0) -> dict | None:
    """Rung 2: crawl the hospital's transparency page for MRF anchors."""
    if not mrf_page:
        return None
    try:
        with _client_for(mrf_page, timeout) as c:
            r = c.get(mrf_page)
        if r.status_code != 200:
            return None
        best = None
        for href, inner in ANCHOR_RE.findall(r.text):
            text = TAG_STRIP.sub("", inner).strip()
            score = 0
            if FILE_EXT_RE.search(href):
                score += 2
            if CMS_NAMING_RE.search(href):
                score += 3
            if CMS_NAMING_RE.search(text):
                score += 2
            if score >= 3:
                cand = urljoin(mrf_page, href)
                if best is None or score > best[0]:
                    best = (score, cand, text[:80])
        if not best:
            return None
        _, cand, anchor = best
        v = head_verify(cand)
        if v["ok"]:
            return {"rung": "page-crawl", "candidate_url": cand,
                    "source_page": mrf_page, "anchor_text": anchor, "verify": v}
        return None
    except Exception:
        return None


def repair_r3_wayback(dead_url: str, timeout: float = 45.0) -> dict | None:
    """Rung 3: Wayback CDX for archived captures of the exact URL."""
    try:
        cdx = ("https://web.archive.org/cdx/search/cdx"
               f"?url={dead_url}&output=json&filter=statuscode:200"
               "&fl=timestamp,original,statuscode&limit=1")
        with _client_for(cdx, timeout) as c:
            r = c.get(cdx)
        if r.status_code != 200:
            return None
        rows = r.json()
        if len(rows) < 2:  # header row only
            return None
        ts, original, _ = rows[1][0], rows[1][1], rows[1][2]
        archived = f"https://web.archive.org/web/{ts}id_/{original}"
        v = head_verify(archived)
        if v["ok"]:
            return {"rung": "wayback", "candidate_url": archived,
                    "source_page": cdx, "verify": v}
        return None
    except Exception:
        return None


REPAIR_RUNGS = (
    ("cms-hpt.txt", repair_r1_cmshpt),
    ("page-crawl", repair_r2_page_crawl),
    ("wayback", repair_r3_wayback),
)


def repair_one(ccn: str, dead_url: str, mrf_page: str | None,
               live: bool, conn: sqlite3.Connection) -> dict:
    """Run the ladder for one dead URL. Returns the outcome record."""
    outcome: dict = {"ccn": ccn, "dead_url": dead_url, "repaired": False,
                     "rung": None, "candidate_url": None, "persisted": False}
    for rung_name, rung_fn in REPAIR_RUNGS:
        try:
            hit = (rung_fn(dead_url, mrf_page)
                   if rung_name == "page-crawl" else rung_fn(dead_url))
        except Exception as e:  # a rung must never kill the ladder
            outcome.setdefault("rung_errors", {})[rung_name] = str(e)[:120]
            continue
        if hit:
            outcome.update({"repaired": True, "rung": rung_name,
                            "candidate_url": hit["candidate_url"],
                            "source_page": hit.get("source_page"),
                            "verify": hit.get("verify")})
            if live:
                conn.execute(
                    """INSERT OR REPLACE INTO mrf_rediscovered
                       (ccn, source_page, candidate_url, anchor_text, score,
                        discovered_at, head_status, head_content_type,
                        head_content_length, alive, rank)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ccn, hit.get("source_page"), hit["candidate_url"],
                     hit.get("anchor_text", ""), 100, utcnow(),
                     hit["verify"]["http_status"],
                     hit["verify"].get("content_type", ""), 0, 1, 1))
                conn.commit()
                outcome["persisted"] = True
            return outcome
    outcome["escalated"] = True
    outcome["escalation_reason"] = (
        "all three repair rungs failed (cms-hpt.txt, page-crawl, wayback)")
    return outcome


# ---------------------------------------------------------------------------
# Phase 3: orchestrate + report


def seed_page_for(conn: sqlite3.Connection, ccn: str) -> str | None:
    cur = conn.execute("SELECT mrf_page FROM mrf_seed WHERE ccn = ? LIMIT 1", (ccn,))
    row = cur.fetchone()
    return row[0] if row else None


def run(db_path: str, live: bool, limit: int | None = None,
        max_repairs: int = 50) -> dict:
    """Full detect -> repair -> report pass. Returns the report dict."""
    report: dict = {"ts": utcnow(), "mode": "live" if live else "dry-run",
                    "detected": {}, "repairs": [], "escalations": [],
                    "parse_failures": [], "schema_drift": [],
                    "reprobe_queue": []}
    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        report["operational_error"] = f"DB unreachable: {e}"
        return report

    buckets = detect_dead_urls(conn)
    report["detected"] = {k: len(v) for k, v in buckets.items()}

    # Transport failures are NOT dead: queue for re-probe via alternate egress.
    for row in buckets["transport-failure"]:
        report["reprobe_queue"].append(
            {"ccn": row["ccn"], "mrf_url": row["mrf_url"],
             "reason": "probe transport failed on our side; re-probe, do not repair"})

    # Blocked (403): categorize, never "repair".
    for row in buckets["blocked"]:
        report["escalations"].append(
            {"ccn": row["ccn"], "mrf_url": row["mrf_url"],
             "class": "blocked-403",
             "reason": "site blocks automated access; needs real-browser "
                       "byte-verification leg (not a repair)"})

    # Genuinely dead: run the ladder (bounded).
    dead = buckets["genuinely-dead"]
    if limit:
        dead = dead[:limit]
    for i, row in enumerate(dead):
        if i >= max_repairs:
            report["truncated"] = f"repair ladder capped at {max_repairs}"
            break
        page = seed_page_for(conn, row["ccn"])
        outcome = repair_one(row["ccn"], row["mrf_url"], page, live, conn)
        if outcome["repaired"]:
            report["repairs"].append(outcome)
        else:
            report["escalations"].append(
                {"ccn": row["ccn"], "mrf_url": row["mrf_url"],
                 "class": "unrepairable-url",
                 "reason": outcome.get("escalation_reason")})

    report["parse_failures"] = detect_parse_failures(conn)
    for pf in report["parse_failures"]:
        report["escalations"].append(
            {"ccn": pf["ccn"], "class": "parse-failure",
             "reason": f"quarantined ({pf['source']}); parser adaptation "
                       "needs human review"})
    report["schema_drift"] = detect_schema_drift(conn)
    for sd in report["schema_drift"]:
        report["escalations"].append(
            {"ccn": sd["ccn"], "mrf_url": sd["mrf_url"],
             "class": "schema-drift", "reason": sd["detail"]})

    conn.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Hospital Ledger self-healing pass")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--live", action="store_true",
                    help="persist repairs to mrf_rediscovered (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run (default); detect + plan, persist nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap dead-URL detections processed (testing)")
    ap.add_argument("--max-repairs", type=int, default=50)
    ap.add_argument("--report", default=None, help="write JSON report to path")
    args = ap.parse_args()

    report = run(args.db, live=args.live, limit=args.limit,
                 max_repairs=args.max_repairs)
    if report.get("operational_error"):
        print(json.dumps(report, indent=1))
        return 1
    if args.report:
        with open(args.report, "w") as f:
            json.dump(report, f, indent=1)

    n_esc = len(report["escalations"])
    n_rep = len(report["repairs"])
    det = report["detected"]
    print(f"[self-heal] mode={report['mode']} "
          f"dead={det.get('genuinely-dead', 0)} "
          f"transport-fail={det.get('transport-failure', 0)} "
          f"blocked={det.get('blocked', 0)} "
          f"repaired={n_rep} escalated={n_esc} "
          f"parse-failures={len(report['parse_failures'])} "
          f"schema-drift={len(report['schema_drift'])}")
    if n_esc and not args.live:
        print("[self-heal] dry-run: repairs NOT persisted; "
              "re-run with --live to persist (needs Barklee's approval)")
    # Quiet-on-healthy is the caller's job; exit code carries the signal.
    return 2 if n_esc else 0


if __name__ == "__main__":
    sys.exit(main())
