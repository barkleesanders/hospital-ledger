#!/usr/bin/env python3
"""Stage 2.7: BVA-scraper-style domain sitemap walker for still-missing hospitals.

Pattern adapted from ~/va_bva_scraper_sitemap_official.py:
  - ThreadPoolExecutor with 100 workers
  - No rate limiter (RPM=0)
  - requests.Session with HTTPAdapter retry
  - Regex-based <loc> parsing (faster than ET, handles namespaces)
  - Checkpoint every 100 hospitals

For each missing hospital:
  1. Generate candidate domains from name heuristics
  2. For each candidate, HEAD probe /sitemap.xml
  3. If alive, regex-parse <loc> URLs, filter for MRF patterns (CMS naming,
     .csv/.json/.xlsx + transparency keywords)
  4. HEAD-verify top candidates, write to mrf_rediscovered

NO Exa. NO third-party search. Pure name-to-domain heuristics + sitemap.
"""
import re
import sys
import json
import sqlite3
import os
import datetime
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
CHECKPOINT_FILE = os.path.join(ROOT, 'data', 'sitemap_walker_checkpoint.json')
LOG_FILE = os.path.join(ROOT, 'data', 'sitemap_walker.log')

WORKERS = 100   # matches BVA scraper
TIMEOUT = 8     # short timeout — we're fan-out probing
GLOBAL_RPM = 0  # no rate limit

UA = ("HospitalLedgerBot/0.3 sitemap-walker "
      "(+https://github.com/hospital-ledger)")

# Regexes (adapted from BVA scraper line 161-162)
LOC_RE = re.compile(r'<loc>(.*?)</loc>', re.IGNORECASE | re.DOTALL)
FILE_EXT_RE = re.compile(r'\.(csv|json|xlsx?|zip|xml)(\?|$)', re.IGNORECASE)
CMS_NAMING_RE = re.compile(r'standard[\-_]?charges?', re.IGNORECASE)
PATH_KEYWORDS_RE = re.compile(
    r'machine[\-_]?readable|standard[\-_]?charges?|chargemaster|transparency',
    re.IGNORECASE)

# Domain-shape signals
COMMON_TLDS = ('.org', '.com', '.health', '.net', '.us')
STOP_WORDS = {
    'hospital', 'hospitals', 'medical', 'center', 'health', 'system',
    'systems', 'care', 'inc', 'llc', 'corp', 'corporation', 'the', 'of',
    'and', 'for', 'st', 'saint', 'memorial', 'community', 'regional',
    'foundation', 'campus', 'group', 'partners', 'associates',
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, mode='w'), logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


def make_session():
    s = requests.Session()
    retries = Retry(total=1, backoff_factor=0.3, status_forcelist=(500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retries, pool_connections=200, pool_maxsize=200)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    s.headers.update({'User-Agent': UA, 'Accept': 'text/xml,text/html,*/*;q=0.5'})
    return s


_session_local = threading.local()
def session():
    if not hasattr(_session_local, 's'):
        _session_local.s = make_session()
    return _session_local.s


def normalize_name(name):
    """Strip punctuation, split on non-alpha, filter stop words."""
    tokens = re.findall(r'[a-z]+', name.lower())
    sig = [t for t in tokens if t not in STOP_WORDS and len(t) >= 3]
    return tokens, sig


def candidate_domains(name):
    """Generate plausible domain names for a hospital."""
    tokens, sig = normalize_name(name)
    if not tokens:
        return []
    # Variants
    full_concat = ''.join(tokens)                                   # shelbybaptistmedicalcenter
    sig_concat = ''.join(sig)                                       # shelbybaptist
    sig_with_hosp = ''.join(sig) + 'hospital'                       # shelbybaptisthospital
    sig_with_health = ''.join(sig) + 'health'                       # shelbybaptisthealth
    first_sig = sig[0] if sig else ''                               # shelby
    first_two = ''.join(sig[:2]) if len(sig) >= 2 else first_sig    # shelbybaptist

    bases = list({full_concat, sig_concat, sig_with_hosp,
                  sig_with_health, first_sig, first_two})
    bases = [b for b in bases if 4 <= len(b) <= 60]

    candidates = []
    for base in bases:
        for tld in COMMON_TLDS:
            candidates.append(f"https://www.{base}{tld}")
            candidates.append(f"https://{base}{tld}")
    # Dedupe preserving order
    seen, out = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c); out.append(c)
    return out[:18]  # cap


def probe_alive(url):
    """HEAD probe a URL — returns True if 2xx/3xx within timeout."""
    try:
        r = session().head(url, allow_redirects=True, timeout=TIMEOUT)
        if 200 <= r.status_code < 400:
            return True
        if r.status_code == 405:  # method not allowed → try GET
            r = session().get(url, allow_redirects=True, timeout=TIMEOUT,
                              stream=True)
            return 200 <= r.status_code < 400
    except Exception:
        pass
    return False


def find_live_domain(name):
    """Probe candidate domains; return first live one or None."""
    for cand in candidate_domains(name):
        if probe_alive(cand):
            return cand
    return None


def fetch_text(url, timeout=TIMEOUT):
    try:
        r = session().get(url, allow_redirects=True, timeout=timeout)
        if 200 <= r.status_code < 400:
            return r.text, r.url
    except Exception:
        pass
    return '', url


def score_url(url):
    path = urlparse(url).path.lower()
    filename = path.rsplit('/', 1)[-1]
    score = 0
    if FILE_EXT_RE.search(path): score += 3
    if CMS_NAMING_RE.search(filename): score += 5
    if PATH_KEYWORDS_RE.search(path): score += 2
    return score


def walk_sitemap(base, depth=0, max_depth=2, seen=None, max_sitemaps=40):
    """Walk sitemap.xml recursively, return MRF-candidate URLs with scores."""
    if seen is None:
        seen = set()
    if depth > max_depth:
        return []

    # Try common sitemap entry points at the base
    if depth == 0:
        sitemap_urls = [
            base + '/sitemap.xml',
            base + '/sitemap_index.xml',
            base + '/sitemap-index.xml',
        ]
    else:
        sitemap_urls = [base]

    candidates = []
    for sm in sitemap_urls:
        if sm in seen:
            continue
        seen.add(sm)
        text, _ = fetch_text(sm, timeout=TIMEOUT)
        if not text:
            continue
        locs = LOC_RE.findall(text)
        if not locs:
            continue

        # Sitemap index — recurse into child sitemaps
        if '<sitemapindex' in text.lower():
            for loc in locs[:max_sitemaps]:
                loc = loc.strip()
                if loc.endswith(('.xml', '.gz')) and loc not in seen:
                    candidates.extend(
                        walk_sitemap(loc, depth + 1, max_depth, seen, max_sitemaps))
            # Once we found an index here, stop probing other entry points
            return candidates

        # Leaf sitemap — filter for MRF patterns
        for loc in locs[:1000]:  # cap per sitemap
            loc = loc.strip()
            score = score_url(loc)
            if score >= 3:
                candidates.append({'url': loc, 'score': score})

        if candidates:
            return candidates  # first viable entry point wins
    return candidates


def head_verify(url):
    try:
        r = session().head(url, allow_redirects=True, timeout=TIMEOUT)
        if r.status_code == 405 or r.status_code >= 400:
            r = session().get(url, allow_redirects=True, timeout=TIMEOUT,
                              stream=True,
                              headers={'Range': 'bytes=0-1023'})
        cl = r.headers.get('content-length')
        return {
            'status': r.status_code,
            'content_type': r.headers.get('content-type', '')[:200],
            'content_length': int(cl) if cl and cl.isdigit() else None,
            'alive': 1 if 200 <= r.status_code < 400 else 0,
        }
    except Exception as e:
        return {'status': -1, 'content_type': f'err:{type(e).__name__}',
                'content_length': None, 'alive': 0}


def process_hospital(ccn, name, city, state, top_n=2):
    """End-to-end: name → domain → sitemap → MRF candidates → verify."""
    domain = find_live_domain(name)
    if not domain:
        return ccn, [], None

    candidates = walk_sitemap(domain)
    # Dedupe, keep top-N by score
    seen = {}
    for c in candidates:
        if c['url'] not in seen or seen[c['url']]['score'] < c['score']:
            seen[c['url']] = c
    top = sorted(seen.values(), key=lambda x: -x['score'])[:top_n]

    verified = []
    for rank, c in enumerate(top, 1):
        v = head_verify(c['url'])
        verified.append({
            'ccn': ccn,
            'source_page': 'sitemap-walker:' + domain,
            'candidate_url': c['url'],
            'anchor_text': 'sitemap',
            'score': c['score'],
            'rank': rank, **v,
        })
    return ccn, verified, domain


def get_still_missing(conn, state=None, limit=None):
    sql = """
    SELECT h.ccn, h.name, h.city, h.state FROM hospitals h
    WHERE h.hospital_type IN
      ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
      AND h.ccn NOT IN (
        SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p
          ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
      AND h.ccn NOT IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
    """
    params = []
    if state: sql += " AND h.state = ?"; params.append(state)
    sql += " ORDER BY h.ccn"
    if limit: sql += f" LIMIT {limit}"
    return conn.execute(sql, params).fetchall()


def commit_batch(conn, batch, now):
    c = conn.cursor()
    for r in batch:
        c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
          (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
           head_status, head_content_type, head_content_length, alive, rank)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
          (r['ccn'], r['source_page'], r['candidate_url'], r['anchor_text'],
           r['score'], now, r['status'], r['content_type'],
           r['content_length'], r['alive'], r['rank']))
    conn.commit()


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--state'); p.add_argument('--limit', type=int)
    p.add_argument('--workers', type=int, default=WORKERS)
    p.add_argument('--top-n', type=int, default=2)
    args = p.parse_args()

    conn = sqlite3.connect(DB, check_same_thread=False)
    rows = get_still_missing(conn, args.state, args.limit)
    logger.info(f"missing hospitals targeted: {len(rows)}")
    logger.info(f"workers: {args.workers} | top_n: {args.top_n}")

    import time
    t0 = time.time()
    db_lock = threading.Lock()
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')

    done = 0
    found_domain = 0
    live_candidates = 0
    hospitals_with_live = 0
    pending = []

    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix='sm') as ex:
        futures = {ex.submit(process_hospital, r[0], r[1], r[2], r[3], args.top_n): r[0]
                   for r in rows}
        for fut in as_completed(futures):
            try:
                ccn, verified, domain = fut.result()
            except Exception as e:
                logger.error(f"task failed: {e}")
                done += 1
                continue
            done += 1
            if domain: found_domain += 1
            if verified:
                pending.extend(verified)
                live_candidates += sum(1 for v in verified if v.get('alive'))
                if any(v.get('alive') for v in verified):
                    hospitals_with_live += 1
            # Flush DB every 100 hospitals
            if len(pending) >= 100:
                with db_lock:
                    commit_batch(conn, pending, now)
                pending = []
            if done % 25 == 0 or done == len(rows):
                logger.info(f"  {done}/{len(rows)} done | domains_found={found_domain} "
                            f"| live_candidates={live_candidates} | hospitals_recovered={hospitals_with_live}")
    # final flush
    if pending:
        with db_lock:
            commit_batch(conn, pending, now)

    elapsed = time.time() - t0
    logger.info(f"=== complete in {elapsed:.1f}s ===")
    logger.info(f"  domains found: {found_domain}/{len(rows)} ({100*found_domain/max(1,len(rows)):.1f}%)")
    logger.info(f"  hospitals recovered: {hospitals_with_live}/{len(rows)} "
                f"({100*hospitals_with_live/max(1,len(rows)):.1f}%)")


if __name__ == '__main__':
    main()
