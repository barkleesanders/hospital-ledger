#!/usr/bin/env python3
"""Stage 2.6: Find MRFs for hospitals with no live URL yet, via Exa search.

Strategy per hospital:
  1. Exa search for "<facility_name>" <city> <state> price transparency
  2. Filter results: drop aggregator/blocklist domains, prefer hospital's own
     domain (heuristic: domain words overlap with hospital name)
  3. For each candidate URL: fetch HTML, extract MRF candidates with existing
     scoring logic; also try /sitemap.xml of the same host
  4. HEAD-verify top candidates
  5. Insert into mrf_rediscovered (source 'exa' implicit by ccn presence)

Usage:
  python3 scripts/find_missing_mrfs.py [--state CA] [--limit N]
                                        [--exa-concurrency 30]
                                        [--http-concurrency 100]
                                        [--top-n 3]
"""
import argparse, asyncio, sqlite3, os, sys, re, datetime, json
from urllib.parse import urljoin, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')

EXA_KEY = os.environ.get('EXA_KEY') or os.environ.get('EXA_API_KEY')
EXA_URL = "https://api.exa.ai/search"

UA = ("HospitalLedgerBot/0.1 (+https://github.com/hospital-ledger; "
      "open-source public-good price transparency crawler)")

# Domains to skip in Exa results — known aggregators / not source-of-truth
BLOCKLIST_DOMAINS = {
    'medrates.fyi', 'data.medrates.fyi', 'hospitalpricetransparency.com',
    'turquoise.health', 'serifhealth.com', 'payerprice.com', 'sidecarhealth.com',
    'dolthub.com', 'github.com', 'wikipedia.org', 'wikiwand.com',
    'yelp.com', 'glassdoor.com', 'indeed.com', 'linkedin.com', 'facebook.com',
    'instagram.com', 'twitter.com', 'x.com', 'youtube.com', 'tiktok.com',
    'cms.gov', 'data.cms.gov', 'hhs.gov', 'medicare.gov', 'usnews.com',
    'healthgrades.com', 'vitals.com', 'sharecare.com', 'beckers.com',
    'beckershospitalreview.com', 'modernhealthcare.com', 'fiercehealthcare.com',
    'medicalnewstoday.com', 'webmd.com', 'mayoclinic.org',  # mayoclinic.org IS a hospital but rarely the target
}

# Common transparency paths to try on a hospital domain
TRANSPARENCY_PATHS = (
    '/price-transparency', '/price-transparency/',
    '/standard-charges', '/standard-charges/',
    '/machine-readable', '/machine-readable-files',
    '/pricing', '/pricing/', '/about/pricing',
    '/patients/billing/price-transparency',
    '/patients-visitors/billing/price-transparency',
    '/billing/price-transparency',
    '/transparency', '/transparency/',
    '/financial-assistance/price-transparency',
    '/about-us/price-transparency',
    '/hospital-charges', '/charges',
    '/sitemap.xml',
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


def name_words(name):
    """Extract significant words from hospital name (lowercased, alpha-only)."""
    words = re.findall(r'[a-z]+', name.lower())
    stop = {'hospital', 'medical', 'center', 'health', 'care', 'system', 'the',
            'of', 'and', 'for', 'inc', 'llc', 'corp', 'corporation', 'st',
            'saint', 'memorial', 'community', 'regional', 'general'}
    return [w for w in words if w not in stop and len(w) >= 4]


def is_blocklisted(url):
    host = urlparse(url).netloc.lower()
    if not host:
        return True
    return any(host == d or host.endswith('.' + d) for d in BLOCKLIST_DOMAINS)


def pick_hospital_domain(results, hospital_name):
    """From Exa results, pick the candidate URL most likely the hospital's own site.

    Strategy: skip blocklist; prefer the URL whose host contains the most
    distinctive hospital-name words.
    """
    sig = name_words(hospital_name)
    scored = []
    for r in results:
        url = r.get('url') or r.get('id', '')
        if not url or is_blocklisted(url):
            continue
        host = urlparse(url).netloc.lower()
        # score by name-word overlap
        overlap = sum(1 for w in sig if w in host)
        scored.append((overlap, url, r))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    # If any has overlap > 0 prefer it, else take first non-blocklisted
    return scored[0][1] if scored[0][0] > 0 else scored[0][1]


EXA_EXCLUDE_DOMAINS = [
    'payerprice.com', 'turquoise.health', 'medrates.fyi', 'data.medrates.fyi',
    'hospitalcostdata.com', 'hospitals.goodbill.com', 'goodbill.com',
    'sidecarhealth.com', 'wikipedia.org', 'cms.gov', 'data.cms.gov',
    'medicare.gov', 'hospital-data.com', 'usnews.com', 'healthgrades.com',
    'vitals.com', 'sharecare.com', 'beckershospitalreview.com',
    'modernhealthcare.com', 'fiercehealthcare.com', 'goodrx.com',
    'definitivehc.com', 'turquoiseuna.com', 'carecarta.com',
]


async def exa_search(client, sem, ccn, name, city, state):
    async with sem:
        # Avoid 'price transparency' wording — that biases Exa toward
        # aggregator sites (turquoise, payerprice, medrates). Instead, target
        # the hospital's own site or its parent system.
        query = f'{name} {city} {state} hospital chargemaster standard charges'
        body = {
            'query': query,
            'numResults': 8,
            'type': 'auto',
            'excludeDomains': EXA_EXCLUDE_DOMAINS,
        }
        try:
            r = await client.post(EXA_URL, json=body,
                                  headers={'x-api-key': EXA_KEY,
                                           'Content-Type': 'application/json'},
                                  timeout=30.0)
            if r.status_code != 200:
                return ccn, None, []
            data = r.json()
            return ccn, data, data.get('results', [])
        except Exception:
            return ccn, None, []


async def harvest_sitemap(client, host_url, seen=None):
    if seen is None:
        seen = set()
    if host_url in seen:
        return []
    seen.add(host_url)
    candidates = []
    try:
        r = await client.get(host_url, follow_redirects=True, timeout=20.0)
        if r.status_code >= 400 or not r.text:
            return []
        text = r.text
        locs = LOC_RE.findall(text)
    except Exception:
        return []
    if '<sitemapindex' in text.lower():
        for loc in locs[:30]:
            if loc.endswith(('.xml', '.gz')):
                candidates.extend(await harvest_sitemap(client, loc, seen))
        return candidates
    for loc in locs:
        path = urlparse(loc).path.lower()
        filename = path.rsplit('/', 1)[-1]
        if (CMS_NAMING_RE.search(filename)
            or (FILE_EXT_RE.search(path) and PATH_KEYWORDS_RE.search(path))):
            score, _ = score_candidate(loc, '')
            if score >= 3:
                candidates.append({'url': loc, 'text': 'sitemap', 'score': score})
    return candidates


async def fetch_page(client, url):
    try:
        r = await client.get(url, follow_redirects=True, timeout=15.0)
        return r.status_code, str(r.url), r.text if 200 <= r.status_code < 400 else ''
    except Exception:
        return -1, url, ''


async def head_verify(client, url):
    try:
        r = await client.head(url, follow_redirects=True, timeout=15.0)
        if r.status_code == 405 or r.status_code >= 400:
            r = await client.get(url, follow_redirects=True, timeout=15.0,
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


async def probe_hospital_domain(client, ccn, source_url, hospital_name, top_n):
    """Given a hospital's domain seed URL, find MRF candidates and verify."""
    base = f"{urlparse(source_url).scheme}://{urlparse(source_url).netloc}"
    candidates_all = []

    # Strategy A: GET the seed URL directly (Exa-returned page) — often the
    # billing/price-transparency page itself
    status, final_url, html = await fetch_page(client, source_url)
    if html:
        candidates_all.extend(extract_candidates(html, final_url))

    # Strategy B: try common transparency paths on the same domain in parallel
    path_tasks = [fetch_page(client, base + p) for p in TRANSPARENCY_PATHS[:-1]]
    sm_task = harvest_sitemap(client, base + '/sitemap.xml')
    fetches = await asyncio.gather(*path_tasks, sm_task, return_exceptions=True)
    for fres in fetches[:-1]:
        if isinstance(fres, Exception):
            continue
        st, furl, fhtml = fres
        if fhtml:
            candidates_all.extend(extract_candidates(fhtml, furl))
    if not isinstance(fetches[-1], Exception):
        candidates_all.extend(fetches[-1])

    # Dedupe, sort by score
    seen = {}
    for c in candidates_all:
        if c['url'] not in seen or seen[c['url']]['score'] < c['score']:
            seen[c['url']] = c
    top = sorted(seen.values(), key=lambda x: -x['score'])[:top_n]

    # HEAD-verify
    verified = []
    for rank, c in enumerate(top, 1):
        v = await head_verify(client, c['url'])
        verified.append({
            'ccn': ccn,
            'source_page': source_url,
            'candidate_url': c['url'],
            'anchor_text': c.get('text', '')[:80],
            'score': c['score'],
            'rank': rank,
            **v,
        })
    return verified


async def process_one(client, exa_sem, http_sem, ccn, name, city, state, top_n):
    # 1) Exa search
    _, exa_data, results = await exa_search(client, exa_sem, ccn, name, city, state)
    if not results:
        return ccn, []

    # 2) Pre-score Exa result URLs. Some Exa results ARE the MRF file directly.
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
    exa_top_url = results[0].get('url', '')

    async with http_sem:
        # Strategy 1: HEAD-verify direct MRF hits from Exa results
        for rank, c in enumerate(direct_hits[:top_n], 1):
            v = await head_verify(client, c['url'])
            verified.append({
                'ccn': ccn, 'source_page': 'exa:' + exa_top_url,
                'candidate_url': c['url'], 'anchor_text': c['text'],
                'score': c['score'], 'rank': rank, **v,
            })

        # Strategy 2: probe up to 3 hub pages if we don't yet have enough live hits
        live_so_far = sum(1 for v in verified if v.get('alive'))
        if live_so_far < top_n:
            for hub in hub_pages[:3]:
                if sum(1 for v in verified if v.get('alive')) >= top_n:
                    break
                try:
                    hub_results = await probe_hospital_domain(
                        client, ccn, hub, name,
                        top_n - sum(1 for v in verified if v.get('alive')))
                except Exception:
                    continue
                for hr in hub_results:
                    hr['source_page'] = 'exa-hub:' + hub
                    hr['rank'] = len(verified) + 1
                    verified.append(hr)

    return ccn, verified


def get_missing_hospitals(conn, state=None, limit=None):
    sql = """
    SELECT h.ccn, h.name, h.city, h.state, h.hospital_type
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
        total_verified = 0
        live_count = 0
        results = []
        for fut in asyncio.as_completed(tasks):
            ccn, vlist = await fut
            done += 1
            total_verified += len(vlist)
            live_count += sum(1 for v in vlist if v.get('alive'))
            results.extend(vlist)
            if done % 25 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} done | verified={total_verified} | live={live_count}",
                      file=sys.stderr)
        return results


def main():
    if not EXA_KEY:
        sys.exit("Set EXA_KEY or EXA_API_KEY in the environment before running this Exa discovery script.")
    p = argparse.ArgumentParser()
    p.add_argument('--state'); p.add_argument('--limit', type=int)
    p.add_argument('--exa-concurrency', type=int, default=30)
    p.add_argument('--http-concurrency', type=int, default=80)
    p.add_argument('--top-n', type=int, default=2)
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    rows = get_missing_hospitals(conn, args.state, args.limit)
    print(f"missing hospitals targeted: {len(rows)}", file=sys.stderr)
    print(f"  exa concurrency: {args.exa_concurrency}", file=sys.stderr)
    print(f"  http concurrency: {args.http_concurrency}", file=sys.stderr)
    print(f"  top_n: {args.top_n}", file=sys.stderr)

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
