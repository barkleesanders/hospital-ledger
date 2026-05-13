#!/usr/bin/env python3
"""Stage 2.7: Second-pass MRF discovery for hospitals Stage 2.6 missed.

Improvements over v1:
  - Exa search uses `category: "company"` to bias toward hospital websites,
    not aggregators or news articles.
  - Simpler query phrasing (no "price transparency" wording — that biases
    toward aggregator listings).
  - Deep sitemap walking: follows sitemap-index files two levels, scans up to
    100 URLs per sub-sitemap for MRF patterns.
  - Trims TRANSPARENCY_PATHS to the 6 highest-hit-rate paths.
  - Tighter timeouts (10s page fetch, 10s HEAD).

Usage:
  python3 scripts/find_missing_v2.py [--state CA] [--limit N]
                                      [--exa-concurrency 16]
                                      [--http-concurrency 60] [--top-n 2]
"""
import argparse, asyncio, sqlite3, os, sys, re, datetime
from urllib.parse import urljoin, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')

EXA_KEY = os.environ.get('EXA_KEY') or os.environ.get('EXA_API_KEY')
EXA_URL = "https://api.exa.ai/search"

UA = ("HospitalLedgerBot/0.2 (+https://github.com/hospital-ledger; "
      "open-source public-good price transparency crawler)")

BLOCKLIST_DOMAINS = {
    'medrates.fyi', 'data.medrates.fyi', 'hospitalpricetransparency.com',
    'turquoise.health', 'serifhealth.com', 'payerprice.com', 'sidecarhealth.com',
    'dolthub.com', 'github.com', 'wikipedia.org', 'wikiwand.com',
    'yelp.com', 'glassdoor.com', 'indeed.com', 'linkedin.com', 'facebook.com',
    'instagram.com', 'twitter.com', 'x.com', 'youtube.com', 'tiktok.com',
    'cms.gov', 'data.cms.gov', 'hhs.gov', 'medicare.gov', 'usnews.com',
    'healthgrades.com', 'vitals.com', 'sharecare.com',
    'beckershospitalreview.com', 'beckers.com',
    'modernhealthcare.com', 'fiercehealthcare.com',
    'medicalnewstoday.com', 'webmd.com',
    'goodbill.com', 'hospitals.goodbill.com', 'hospital-data.com',
    'definitivehc.com', 'carecarta.com', 'goodrx.com', 'hospitalcostdata.com',
}

EXA_EXCLUDE_DOMAINS = sorted(BLOCKLIST_DOMAINS)

TRANSPARENCY_PATHS = (
    '/price-transparency', '/standard-charges',
    '/machine-readable-files', '/pricing',
    '/patients-visitors/billing/price-transparency',
    '/billing/price-transparency',
)

try:
    import httpx
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'httpx'])
    import httpx

ANCHOR_RE = re.compile(
    r'<a\s+[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_STRIP = re.compile(r'<[^>]+>')
FILE_EXT_RE = re.compile(r'\.(csv|json|xlsx?|zip|xml)(\?|$)', re.IGNORECASE)
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
LOC_RE = re.compile(r'<loc>\s*([^<\s]+)\s*</loc>', re.IGNORECASE)


def score_candidate(href, anchor_text):
    path = urlparse(href).path.lower()
    filename = path.rsplit('/', 1)[-1]
    clean_text = TAG_STRIP.sub(' ', anchor_text).strip().lower()
    score = 0
    if FILE_EXT_RE.search(path):
        score += 3
    if CMS_NAMING_RE.search(filename):
        score += 5
    if PATH_KEYWORDS_RE.search(path):
        score += 2
    if TEXT_KEYWORDS_RE.search(clean_text):
        score += 2
    if FILE_EXT_RE.search(path) and len(filename) > 12 and '_' in filename:
        score += 1
    return score, clean_text[:80]


def is_blocklisted(url):
    host = urlparse(url).netloc.lower()
    if not host:
        return True
    return any(host == d or host.endswith('.' + d) for d in BLOCKLIST_DOMAINS)


def extract_candidates(html, base_url):
    candidates = []
    for m in ANCHOR_RE.finditer(html):
        href, anchor_text = m.group(1), m.group(2)
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        try:
            full = urljoin(base_url, href)
        except Exception:
            continue
        if not full.startswith(('http://', 'https://')):
            continue
        if is_blocklisted(full):
            continue
        score, text = score_candidate(full, anchor_text)
        if score >= 3:
            candidates.append({'url': full, 'text': text, 'score': score})
    seen = {}
    for c in candidates:
        if c['url'] not in seen or seen[c['url']]['score'] < c['score']:
            seen[c['url']] = c
    return sorted(seen.values(), key=lambda x: -x['score'])


async def exa_search(client, sem, ccn, name, city, state):
    async with sem:
        # Simpler query + category=company for hospital-website bias
        query = f"{name} {city} {state}"
        body = {
            'query': query,
            'numResults': 6,
            'type': 'auto',
            'category': 'company',
            'excludeDomains': EXA_EXCLUDE_DOMAINS,
        }
        try:
            r = await client.post(EXA_URL, json=body,
                                  headers={'x-api-key': EXA_KEY,
                                           'Content-Type': 'application/json'},
                                  timeout=25.0)
            if r.status_code != 200:
                return ccn, []
            data = r.json()
            return ccn, data.get('results', [])
        except Exception:
            return ccn, []


async def fetch_text(client, url):
    try:
        r = await client.get(url, follow_redirects=True, timeout=10.0)
        if 200 <= r.status_code < 400:
            return r.text, str(r.url)
        return '', url
    except Exception:
        return '', url


async def deep_sitemap(client, base_url, max_depth=2, max_urls_per_sitemap=200,
                      seen=None):
    """Walk sitemap (recursively follow indices) and return MRF-pattern URLs."""
    if seen is None:
        seen = set()
    if base_url in seen:
        return []
    seen.add(base_url)

    candidates = []
    text, final_url = await fetch_text(client, base_url)
    if not text:
        return []
    locs = LOC_RE.findall(text)
    is_index = '<sitemapindex' in text.lower()

    if is_index and max_depth > 0:
        # Recurse — but only fetch up to 30 sub-sitemaps to avoid explosion
        for loc in locs[:30]:
            if loc.endswith(('.xml', '.gz')) and loc not in seen:
                candidates.extend(await deep_sitemap(
                    client, loc, max_depth - 1, max_urls_per_sitemap, seen))
        return candidates

    # Leaf sitemap: filter <loc>s for MRF patterns
    for loc in locs[:max_urls_per_sitemap]:
        if is_blocklisted(loc):
            continue
        path = urlparse(loc).path.lower()
        filename = path.rsplit('/', 1)[-1]
        if (CMS_NAMING_RE.search(filename)
            or (FILE_EXT_RE.search(path) and PATH_KEYWORDS_RE.search(path))):
            score, _ = score_candidate(loc, '')
            if score >= 3:
                candidates.append({'url': loc, 'text': 'sitemap', 'score': score})
    return candidates


async def head_verify(client, url):
    try:
        r = await client.head(url, follow_redirects=True, timeout=10.0)
        if r.status_code == 405 or r.status_code >= 400:
            r = await client.get(url, follow_redirects=True, timeout=10.0,
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


async def deep_probe_domain(client, base_url):
    """For a base domain URL, harvest candidates from sitemap + common paths."""
    base = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    candidates_all = []

    # 1) Fetch the seed URL itself — may be the transparency page
    text, final_url = await fetch_text(client, base_url)
    if text and '<html' in text.lower():
        candidates_all.extend(extract_candidates(text, final_url))

    # 2) Common transparency paths on the host
    path_urls = [base + p for p in TRANSPARENCY_PATHS]
    path_fetches = await asyncio.gather(
        *[fetch_text(client, u) for u in path_urls], return_exceptions=True)
    for fr in path_fetches:
        if isinstance(fr, Exception):
            continue
        txt, furl = fr
        if txt:
            candidates_all.extend(extract_candidates(txt, furl))

    # 3) Sitemap walk
    sm_cands = await deep_sitemap(client, base + '/sitemap.xml')
    if not sm_cands:
        sm_cands = await deep_sitemap(client, base + '/sitemap_index.xml')
    candidates_all.extend(sm_cands)

    # Dedupe + sort
    seen = {}
    for c in candidates_all:
        if c['url'] not in seen or seen[c['url']]['score'] < c['score']:
            seen[c['url']] = c
    return sorted(seen.values(), key=lambda x: -x['score'])


async def process_one(client, exa_sem, http_sem, ccn, name, city, state, top_n):
    # 1) Exa search
    _, results = await exa_search(client, exa_sem, ccn, name, city, state)
    if not results:
        return ccn, []

    # 2) Pre-score Exa URLs. Direct hits go straight to verify.
    direct_hits = []
    hub_pages = []
    seen_hosts = set()
    for r in results[:6]:
        url = r.get('url') or r.get('id', '')
        if not url or is_blocklisted(url):
            continue
        score, text = score_candidate(url, r.get('title', ''))
        if score >= 5:
            direct_hits.append({'url': url, 'text': text or r.get('title', '')[:80],
                                'score': score})
        else:
            host = urlparse(url).netloc.lower()
            if host and host not in seen_hosts:
                seen_hosts.add(host)
                hub_pages.append(url)

    verified = []
    exa_top = (results[0].get('url') or '') if results else ''

    async with http_sem:
        # Direct hits first
        for rank, c in enumerate(direct_hits[:top_n], 1):
            v = await head_verify(client, c['url'])
            verified.append({
                'ccn': ccn, 'source_page': 'exa-v2:' + exa_top,
                'candidate_url': c['url'], 'anchor_text': c['text'],
                'score': c['score'], 'rank': rank, **v,
            })

        # If we still need more live finds, deep-probe each hub domain
        for hub in hub_pages[:3]:
            if sum(1 for v in verified if v.get('alive')) >= top_n:
                break
            try:
                domain_cands = await deep_probe_domain(client, hub)
            except Exception:
                continue
            for c in domain_cands[:top_n - sum(1 for v in verified if v.get('alive'))]:
                v = await head_verify(client, c['url'])
                verified.append({
                    'ccn': ccn, 'source_page': 'exa-v2-hub:' + hub,
                    'candidate_url': c['url'], 'anchor_text': c.get('text', ''),
                    'score': c['score'],
                    'rank': len(verified) + 1, **v,
                })
                if sum(1 for vv in verified if vv.get('alive')) >= top_n:
                    break
    return ccn, verified


def get_still_missing(conn, state=None, limit=None):
    sql = """
    SELECT h.ccn, h.name, h.city, h.state
    FROM hospitals h
    WHERE h.hospital_type IN
      ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
      AND h.ccn NOT IN (
        SELECT s.ccn FROM mrf_seed s
        JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url
        WHERE p.alive=1
      )
      AND h.ccn NOT IN (
        SELECT ccn FROM mrf_rediscovered WHERE alive=1
      )
    """
    params = []
    if state:
        sql += " AND h.state = ?"
        params.append(state)
    sql += " ORDER BY h.ccn"
    if limit:
        sql += f" LIMIT {limit}"
    return conn.execute(sql, params).fetchall()


async def run_all(rows, exa_conc, http_conc, top_n):
    exa_sem = asyncio.Semaphore(exa_conc)
    http_sem = asyncio.Semaphore(http_conc)
    limits = httpx.Limits(max_keepalive_connections=http_conc,
                          max_connections=http_conc * 2)
    async with httpx.AsyncClient(
        headers={'User-Agent': UA, 'Accept': 'text/html,*/*;q=0.5'},
        limits=limits, verify=False,
    ) as client:
        tasks = [process_one(client, exa_sem, http_sem,
                             r[0], r[1], r[2], r[3], top_n) for r in rows]
        done = 0
        total_v = 0
        live_count = 0
        all_results = []
        for fut in asyncio.as_completed(tasks):
            ccn, vlist = await fut
            done += 1
            total_v += len(vlist)
            live_count += sum(1 for v in vlist if v.get('alive'))
            all_results.extend(vlist)
            if done % 25 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} done | verified={total_v} | live={live_count}",
                      file=sys.stderr)
        return all_results


def main():
    if not EXA_KEY:
        sys.exit("Set EXA_KEY or EXA_API_KEY in the environment before running this Exa discovery script.")
    p = argparse.ArgumentParser()
    p.add_argument('--state'); p.add_argument('--limit', type=int)
    p.add_argument('--exa-concurrency', type=int, default=16)
    p.add_argument('--http-concurrency', type=int, default=60)
    p.add_argument('--top-n', type=int, default=2)
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    rows = get_still_missing(conn, args.state, args.limit)
    print(f"still-missing hospitals targeted: {len(rows)}", file=sys.stderr)
    print(f"  exa conc: {args.exa_concurrency} | http conc: {args.http_concurrency} | top_n: {args.top_n}",
          file=sys.stderr)

    import time
    t0 = time.time()
    results = asyncio.run(run_all(rows, args.exa_concurrency,
                                  args.http_concurrency, args.top_n))
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
    ccns_with_live = len({r['ccn'] for r in results if r.get('alive')})
    print(f"\n=== complete in {elapsed:.1f}s ===", file=sys.stderr)
    print(f"  total candidates verified: {len(results)}", file=sys.stderr)
    print(f"  live: {live}", file=sys.stderr)
    print(f"  hospitals with >=1 live: {ccns_with_live}/{len(rows)} "
          f"({100*ccns_with_live/max(1,len(rows)):.1f}%)", file=sys.stderr)


if __name__ == '__main__':
    main()
