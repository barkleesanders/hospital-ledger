#!/usr/bin/env python3
"""Stage 2.5: Rediscover MRF URLs by crawling each hospital's transparency
landing page (mrf_page) for current MRF links.

Target population:
  - hospitals with mrf_page set AND (current mrf_url is dead OR empty)
  - skips hospitals with already-live mrf_url

For each candidate page, GET → parse HTML → find anchor hrefs that look like
machine-readable charge files (CMS naming convention, file extension, anchor
text keywords). Score and rank candidates. HEAD-verify top-N. Write results
to mrf_rediscovered table.

Usage:
  python3 scripts/rediscover_mrf_urls.py [--state CA] [--ccn 050228]
                                          [--limit N] [--concurrency 6]
                                          [--top-n 3] [--all]
"""
import argparse, asyncio, sqlite3, os, sys, re, datetime, html as html_lib
from urllib.parse import urljoin, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')

UA = ("HospitalLedgerBot/0.1 (+https://github.com/hospital-ledger; "
      "open-source public-good price transparency crawler)")

try:
    import httpx
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'httpx'])
    import httpx

# Regexes
ANCHOR_RE = re.compile(
    r'<a\s+[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_STRIP = re.compile(r'<[^>]+>')
FILE_EXT_RE = re.compile(r'\.(csv|json|xlsx?|zip|xml)(\?|$)', re.IGNORECASE)
# CMS Hospital Price Transparency file naming convention:
# <ein>_<hospital-name>_standardcharges.json|csv
CMS_NAMING_RE = re.compile(r'standard[\-_]?charges?', re.IGNORECASE)
TEXT_KEYWORDS_RE = re.compile(
    r'machine[\s\-_]?readable|standard[\s\-_]?charges?|chargemaster|'
    r'price[\s\-_]?transparency|negotiated[\s\-_]?rates?',
    re.IGNORECASE,
)
PATH_KEYWORDS_RE = re.compile(
    r'machine[\-_]?readable|standard[\-_]?charges?|chargemaster|transparency',
    re.IGNORECASE,
)
# Sitemap parsing — <loc> only (works for both regular and index sitemaps)
LOC_RE = re.compile(r'<loc>\s*([^<\s]+)\s*</loc>', re.IGNORECASE)
SITEMAP_CANDIDATES = (
    '/sitemap.xml', '/sitemap_index.xml', '/sitemap-index.xml',
    '/sitemap-pages.xml', '/en.sitemap.xml',
)


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mrf_rediscovered (
      ccn TEXT,
      source_page TEXT,
      candidate_url TEXT,
      anchor_text TEXT,
      score INTEGER,
      discovered_at TEXT,
      head_status INTEGER,
      head_content_type TEXT,
      head_content_length INTEGER,
      alive INTEGER,
      rank INTEGER,
      PRIMARY KEY (ccn, candidate_url)
    );
    CREATE INDEX IF NOT EXISTS idx_redisc_ccn ON mrf_rediscovered(ccn);
    CREATE INDEX IF NOT EXISTS idx_redisc_alive ON mrf_rediscovered(alive);
    """)
    conn.commit()


def score_candidate(href, anchor_text):
    """Score 0–10 likelihood that href is a hospital MRF."""
    path = urlparse(href).path.lower()
    filename = path.rsplit('/', 1)[-1]
    clean_text = TAG_STRIP.sub(' ', anchor_text).strip().lower()

    score = 0
    if FILE_EXT_RE.search(path):
        score += 3
    if CMS_NAMING_RE.search(filename):
        score += 5  # gold — CMS regulatory naming
    if PATH_KEYWORDS_RE.search(path):
        score += 2
    if TEXT_KEYWORDS_RE.search(clean_text):
        score += 2
    # bonus for short clean filenames vs marketing PDFs
    if FILE_EXT_RE.search(path) and len(filename) > 12 and '_' in filename:
        score += 1
    return score, clean_text[:80]


def extract_candidates(html, base_url):
    candidates = []
    for m in ANCHOR_RE.finditer(html):
        href, anchor_text = html_lib.unescape(m.group(1)), m.group(2)
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        try:
            full = urljoin(base_url, href)
        except Exception:
            continue
        if not full.startswith(('http://', 'https://')):
            continue
        score, text = score_candidate(full, anchor_text)
        if score >= 3:
            candidates.append({'url': full, 'text': text, 'score': score})

    # dedupe by URL, keep highest score
    seen = {}
    for c in candidates:
        if c['url'] not in seen or seen[c['url']]['score'] < c['score']:
            seen[c['url']] = c
    # sort by score desc
    return sorted(seen.values(), key=lambda x: -x['score'])


async def fetch_page(client, url):
    try:
        r = await client.get(url, follow_redirects=True, timeout=30.0)
        return r.status_code, str(r.url), r.text if 200 <= r.status_code < 400 else ''
    except Exception:
        return -1, url, ''


async def harvest_sitemap(client, host_url, depth=0, max_depth=2, seen=None):
    """Walk sitemap(s) rooted at host_url, return MRF-candidate URLs.

    Tries common sitemap paths, follows sitemap index files one level deep.
    Filters <loc> entries for MRF naming + keywords + filetype.
    """
    if seen is None:
        seen = set()
    if depth > max_depth:
        return []

    base = f"{urlparse(host_url).scheme}://{urlparse(host_url).netloc}"
    paths = [host_url] if host_url.endswith(('.xml', '.gz')) else [base + p for p in SITEMAP_CANDIDATES]

    candidates = []
    for sm_url in paths:
        if sm_url in seen:
            continue
        seen.add(sm_url)
        try:
            r = await client.get(sm_url, follow_redirects=True, timeout=20.0)
            if r.status_code >= 400 or not r.text:
                continue
            locs = LOC_RE.findall(r.text)
        except Exception:
            continue
        # If this is a sitemap-index, recurse one level
        if '<sitemapindex' in r.text.lower() and depth < max_depth:
            for loc in locs[:50]:
                if loc in seen:
                    continue
                if loc.endswith(('.xml', '.gz')):
                    candidates.extend(await harvest_sitemap(client, loc, depth + 1, max_depth, seen))
            continue
        # Regular sitemap — filter <loc>s for MRF signals
        for loc in locs:
            path = urlparse(loc).path.lower()
            filename = path.rsplit('/', 1)[-1]
            if CMS_NAMING_RE.search(filename) or FILE_EXT_RE.search(path) and PATH_KEYWORDS_RE.search(path):
                score, _ = score_candidate(loc, '')
                if score >= 3:
                    candidates.append({'url': loc, 'text': 'sitemap', 'score': score})
        # First sitemap that returns hits is usually enough
        if candidates:
            break
    return candidates


async def head_verify(client, url):
    try:
        r = await client.head(url, follow_redirects=True, timeout=20.0)
        if r.status_code == 405 or r.status_code >= 400:
            r = await client.get(url, follow_redirects=True, timeout=20.0,
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


async def process_hospital(client, sem, ccn, mrf_page, top_n):
    async with sem:
        cands = []
        final_url = mrf_page
        page_status = 0
        # Strategy 1: parse transparency landing page
        if mrf_page:
            page_status, final_url, html = await fetch_page(client, mrf_page)
            if html:
                cands.extend(extract_candidates(html, final_url))
        # Strategy 2: sitemap.xml of the same host (fallback if no high-score hits)
        if mrf_page and (not cands or max(c['score'] for c in cands) < 5):
            sm_cands = await harvest_sitemap(client, mrf_page)
            cands.extend(sm_cands)
        # dedupe by URL keep max score, sort by score desc
        seen = {}
        for c in cands:
            if c['url'] not in seen or seen[c['url']]['score'] < c['score']:
                seen[c['url']] = c
        cands = sorted(seen.values(), key=lambda x: -x['score'])[:top_n]
        results = []
        for rank, c in enumerate(cands, 1):
            v = await head_verify(client, c['url'])
            results.append({
                'ccn': ccn,
                'source_page': final_url,
                'candidate_url': c['url'],
                'anchor_text': c['text'],
                'score': c['score'],
                'rank': rank,
                **v,
            })
        return ccn, mrf_page, results, page_status


async def run(rows, concurrency, top_n):
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_keepalive_connections=concurrency,
                          max_connections=concurrency * 2)
    async with httpx.AsyncClient(
        headers={'User-Agent': UA, 'Accept': 'text/html,*/*;q=0.5'},
        limits=limits, verify=False,
    ) as client:
        tasks = [process_hospital(client, sem, ccn, page, top_n) for ccn, page in rows]
        done = 0
        all_results = []
        new_finds = 0
        page_dead = 0
        for fut in asyncio.as_completed(tasks):
            ccn, page, results, page_status = await fut
            done += 1
            if page_status >= 400 or page_status == -1:
                page_dead += 1
            for r in results:
                if r.get('alive'):
                    new_finds += 1
            all_results.extend(results)
            if done % 20 == 0 or done == len(tasks):
                print(f"  pages {done}/{len(tasks)} | live finds={new_finds} | dead pages={page_dead}",
                      file=sys.stderr)
        return all_results


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--state'); p.add_argument('--ccn'); p.add_argument('--limit', type=int)
    p.add_argument('--concurrency', type=int, default=6)
    p.add_argument('--top-n', type=int, default=3, help='HEAD-verify top N candidates per page')
    p.add_argument('--all', action='store_true')
    p.add_argument('--skip-done', action='store_true', help='skip CCNs already in mrf_rediscovered')
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    ensure_schema(conn)

    # Target: hospitals with mrf_page set AND (mrf_url empty OR mrf_url dead)
    # Limit to one mrf_page per CCN (dedupe)
    q = """
    WITH lp AS (SELECT ccn, mrf_url, MAX(probed_at) AS pa FROM mrf_probe GROUP BY ccn, mrf_url)
    SELECT DISTINCT s.ccn, s.mrf_page
    FROM mrf_seed s
    LEFT JOIN lp ON lp.ccn = s.ccn AND lp.mrf_url = s.mrf_url
    LEFT JOIN mrf_probe p ON p.ccn = lp.ccn AND p.mrf_url = lp.mrf_url AND p.probed_at = lp.pa
    WHERE s.mrf_page != ''
    """
    params = []
    if args.ccn:
        q += " AND s.ccn = ?"
        params.append(args.ccn)
    else:
        q += " AND (s.mrf_url = '' OR (p.alive IS NOT NULL AND p.alive = 0))"
    if args.state: q += " AND s.state = ?"; params.append(args.state)
    if args.skip_done:
        q += " AND s.ccn NOT IN (SELECT DISTINCT ccn FROM mrf_rediscovered)"
    q += " ORDER BY s.ccn"
    if args.limit:
        q += f" LIMIT {args.limit}"
    elif not args.all and not args.state and not args.ccn:
        q += " LIMIT 20"

    rows = conn.execute(q, params).fetchall()
    # Dedupe (ccn, mrf_page) — keep one per CCN
    seen, deduped = set(), []
    for ccn, page in rows:
        if ccn in seen:
            continue
        seen.add(ccn)
        deduped.append((ccn, page))

    print(f"rediscovering MRFs for {len(deduped)} hospitals (concurrency={args.concurrency}, top_n={args.top_n})",
          file=sys.stderr)
    import time
    t0 = time.time()
    results = asyncio.run(run(deduped, args.concurrency, args.top_n))
    elapsed = time.time() - t0

    now = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
    c = conn.cursor()
    for r in results:
        c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
            (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
             head_status, head_content_type, head_content_length, alive, rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r['ccn'], r['source_page'], r['candidate_url'], r['anchor_text'],
             r['score'], now, r['status'], r['content_type'],
             r['content_length'], r['alive'], r['rank']))
    conn.commit()

    live = sum(1 for r in results if r.get('alive'))
    hospitals_with_finds = len({r['ccn'] for r in results if r.get('alive')})
    print(f"\n=== rediscovery complete in {elapsed:.1f}s ===", file=sys.stderr)
    print(f"  total candidates verified: {len(results)}", file=sys.stderr)
    print(f"  live candidates: {live}", file=sys.stderr)
    print(f"  hospitals with >=1 live find: {hospitals_with_finds}/{len(deduped)} "
          f"({100*hospitals_with_finds/max(1,len(deduped)):.1f}%)", file=sys.stderr)


if __name__ == '__main__':
    main()
