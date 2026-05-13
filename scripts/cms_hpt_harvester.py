#!/usr/bin/env python3
"""Stage 2.10: CMS-HPT file harvester.

Per CMS Hospital Price Transparency rule, hospitals must publish a /cms-hpt.txt
file at the root of their website containing the canonical MRF URL.

Format (per CMS specification):
  location-name: <hospital name>
  source-page-url: <hub page URL>
  mrf-url: <direct MRF URL>
  contact-name: <name>
  contact-email: <email>

  <blank line>

  location-name: <next hospital>
  ...

This file is NOT typically bot-walled (CMS requires it be publicly accessible
without auth, without bot detection). So /cms-hpt.txt bypasses the Akamai walls
that block the regular HTML pages.

Strategy:
  1. Build domain list from existing mrf_seed (hosts of mrf_page URLs) +
     hardcoded top health-system chains
  2. For each domain: HEAD /cms-hpt.txt, then GET if 200
  3. Parse all location-name + mrf-url blocks
  4. Fuzzy-match each location-name to a CCN in `hospitals`
  5. HEAD-verify each MRF URL
  6. Write to mrf_rediscovered with source 'cms-hpt'
"""
import asyncio, sqlite3, re, sys, os, datetime
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

try:
    import httpx
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'httpx'])
    import httpx


# Known major health system chains (seed list)
SEED_CHAINS = [
    "https://www.mercy.com", "https://www.mercy.net",
    "https://corewellhealth.org", "https://www.spectrumhealth.org",
    "https://www.promedica.org", "https://www.fairview.org",
    "https://www.ascension.org", "https://www.trinity-health.org",
    "https://trinityhealth.org", "https://www.trinityhealthma.org",
    "https://www.trinityhealthmichigan.org",
    "https://www.providence.org", "https://www.kp.org",
    "https://healthy.kaiserpermanente.org", "https://www.sutterhealth.org",
    "https://www.dignityhealth.org", "https://www.commonspirit.org",
    "https://www.hcahealthcare.com", "https://www.hcahealthcare.org",
    "https://www.tenethealth.com", "https://www.adventhealth.com",
    "https://www.kindredhospitals.com", "https://www.encompasshealth.com",
    "https://www.bswhealth.com", "https://www.steward.org",
    "https://www.memorialcare.org", "https://www.bannerhealth.com",
    "https://www.northwell.edu", "https://www.intermountainhealthcare.org",
    "https://www.sclhealth.org", "https://www.adventisthealth.org",
    "https://www.lifebridgehealth.org", "https://uhhospitals.org",
    "https://www.uhhospitals.org", "https://www.clevelandclinic.org",
    "https://www.mayoclinic.org", "https://www.northern lighthealth.org",
    "https://northernlighthealth.org",
    "https://www.bjc.org", "https://hcahealthcare.com",
    "https://www.beaumont.org", "https://www.northshore.org",
    "https://www.atriumhealth.org", "https://www.advocatehealth.com",
    "https://www.aurorahealthcare.org",
    # CHI / Catholic Health Initiatives
    "https://www.commonspirit.org", "https://www.chihealth.com",
    "https://www.dignity-health.com",
    # Steward & spinoffs
    "https://www.steward.org", "https://www.stewardhealthcare.com",
    # CHS / Lifepoint
    "https://www.chs.net", "https://www.lifepointhealth.net",
    # WellSpan, OSF, etc.
    "https://www.wellspan.org", "https://www.osfhealthcare.org",
    "https://www.geisinger.org", "https://www.uchealth.org",
    "https://www.bmc.org", "https://www.umms.org",
    "https://www.upmc.com",
]


def parse_cms_hpt(text):
    """Parse cms-hpt.txt content into list of dicts."""
    blocks = re.split(r'\n\s*\n', text)
    entries = []
    for block in blocks:
        entry = {}
        for line in block.splitlines():
            m = re.match(r'^([a-z\-]+)\s*:\s*(.+)$', line.strip(), re.IGNORECASE)
            if m:
                key = m.group(1).lower().strip()
                val = m.group(2).strip()
                entry[key] = val
        if entry.get('mrf-url') and entry.get('location-name'):
            entries.append(entry)
    return entries


async def fetch_cms_hpt(client, sem, domain):
    """Try /cms-hpt.txt on a domain. Returns (domain, entries)."""
    async with sem:
        url = domain.rstrip('/') + '/cms-hpt.txt'
        try:
            r = await client.get(url, follow_redirects=True, timeout=15.0)
            if r.status_code == 200 and r.text:
                entries = parse_cms_hpt(r.text)
                return domain, entries
        except Exception:
            pass
        return domain, []


async def head_verify(client, sem, url):
    async with sem:
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


STOP_WORDS = {'the', 'of', 'and', 'inc', 'llc', 'corp', 'hospital', 'hospitals',
              'medical', 'center', 'health', 'system', 'care', 'campus',
              'memorial', 'community', 'regional', 'st', 'saint'}


def name_sig(name):
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or '').lower())
    return frozenset(t for t in s.split() if t and t not in STOP_WORDS and len(t) >= 3)


_hospital_index = None


def build_hospital_index(conn):
    """Pre-index hospitals as {state: [(ccn, name, sig)]} for fast matching."""
    global _hospital_index
    if _hospital_index is not None:
        return _hospital_index
    idx = {}
    for ccn, name, state in conn.execute("SELECT ccn, name, state FROM hospitals"):
        sig = name_sig(name)
        if not sig:
            continue
        idx.setdefault(state, []).append((ccn, name, sig))
    _hospital_index = idx
    return idx


def match_to_ccn(conn, location_name, state_hint=None):
    """Fuzzy match a CMS-HPT location-name to a CCN. state_hint narrows search."""
    sig = name_sig(location_name)
    if not sig:
        return None
    idx = build_hospital_index(conn)
    # Try state-hint first if provided, else scan all states
    if state_hint and state_hint in idx:
        candidates = idx[state_hint]
    else:
        candidates = []
        for st_list in idx.values():
            candidates.extend(st_list)
    best = None
    best_score = 0
    for ccn, name, cand_sig in candidates:
        inter = sig & cand_sig
        if not inter:
            continue
        union = sig | cand_sig
        score = len(inter) / len(union)
        if score > best_score:
            best_score = score
            best = (ccn, name, score)
    return best if best and best_score >= 0.5 else None


async def main():
    conn = sqlite3.connect(DB)

    # Build complete domain list: seed chains + hosts from mrf_seed mrf_page
    domains = set(SEED_CHAINS)
    for row in conn.execute("SELECT DISTINCT mrf_page FROM mrf_seed WHERE mrf_page != ''"):
        url = row[0]
        try:
            p = urlparse(url)
            if p.scheme and p.netloc:
                # use scheme://netloc as the domain root
                domains.add(f"{p.scheme}://{p.netloc}")
        except Exception:
            pass
    for row in conn.execute("SELECT DISTINCT source_page FROM mrf_rediscovered WHERE source_page LIKE 'http%'"):
        url = row[0]
        try:
            p = urlparse(url)
            if p.scheme and p.netloc:
                domains.add(f"{p.scheme}://{p.netloc}")
        except Exception:
            pass

    print(f"probing /cms-hpt.txt on {len(domains)} domains...", file=sys.stderr)

    sem = asyncio.Semaphore(40)
    head_sem = asyncio.Semaphore(60)
    limits = httpx.Limits(max_keepalive_connections=60, max_connections=120)

    async with httpx.AsyncClient(headers={'User-Agent': UA, 'Accept': '*/*'},
                                 limits=limits, verify=False) as client:
        # Phase 1: fetch all /cms-hpt.txt
        tasks = [fetch_cms_hpt(client, sem, d) for d in domains]
        all_entries = []  # (domain, entries...)
        domain_hits = 0
        for i, fut in enumerate(asyncio.as_completed(tasks), 1):
            d, entries = await fut
            if entries:
                domain_hits += 1
                for e in entries:
                    all_entries.append((d, e))
            if i % 25 == 0 or i == len(tasks):
                print(f"  probed {i}/{len(tasks)} | domains with cms-hpt={domain_hits} | total entries={len(all_entries)}",
                      file=sys.stderr)

        # Phase 2: match each entry to a CCN (uses pre-built state-keyed index)
        print(f"\nmatching {len(all_entries)} entries to CCNs...", file=sys.stderr)
        build_hospital_index(conn)
        matched = []
        for domain, e in all_entries:
            m = match_to_ccn(conn, e['location-name'])
            if m:
                matched.append({
                    'ccn': m[0],
                    'hospital_name': m[1],
                    'matched_location': e['location-name'],
                    'mrf_url': e['mrf-url'],
                    'source_domain': domain,
                    'source_page': e.get('source-page-url', ''),
                    'match_score': m[2],
                })
        print(f"  matched: {len(matched)} entries", file=sys.stderr)
        print(f"  distinct CCNs covered: {len({m['ccn'] for m in matched})}", file=sys.stderr)

        # Phase 3: HEAD-verify each MRF URL once (parallel, indexed for ordered results)
        print(f"\nHEAD-verifying {len(matched)} MRF URLs...", file=sys.stderr)
        async def verify_indexed(idx, url):
            return idx, await head_verify(client, head_sem, url)
        verify_tasks = [verify_indexed(i, m['mrf_url']) for i, m in enumerate(matched)]
        verifies_ordered = [None] * len(matched)
        done = 0
        live_n = 0
        for fut in asyncio.as_completed(verify_tasks):
            idx, v = await fut
            verifies_ordered[idx] = v
            done += 1
            if v.get('alive'):
                live_n += 1
            if done % 100 == 0 or done == len(matched):
                print(f"  verified {done}/{len(matched)} | live={live_n}", file=sys.stderr)

        # Commit incrementally so kills don't lose work
        c = conn.cursor()
        now = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
        for m, v in zip(matched, verifies_ordered):
            if v is None:
                continue
            c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
                (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
                 head_status, head_content_type, head_content_length, alive, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (m['ccn'], 'cms-hpt:' + m['source_domain'], m['mrf_url'],
                 f"cms-hpt:{m['matched_location'][:60]}", 10, now,
                 v['status'], v['content_type'], v['content_length'], v['alive'], 1))
        conn.commit()
        live_count = sum(1 for v in verifies_ordered if v and v.get('alive'))
        print(f"\n=== complete ===", file=sys.stderr)
        print(f"  matched + verified: {len(matched)}", file=sys.stderr)
        print(f"  alive: {live_count}", file=sys.stderr)
        print(f"  distinct hospitals now found via cms-hpt: "
              f"{len({m['ccn'] for m, v in zip(matched, verifies_ordered) if v and v.get('alive')})}",
              file=sys.stderr)
        return  # short-circuit — old commit block below is now dead code

    # Insert into mrf_rediscovered
    c = conn.cursor()
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
    inserted = 0
    alive_count = 0
    for m, v in zip(matched, verifies_ordered):
        c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
            (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
             head_status, head_content_type, head_content_length, alive, rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m['ccn'], 'cms-hpt:' + m['source_domain'],
             m['mrf_url'],
             f"cms-hpt:{m['matched_location'][:60]}",
             10, now, v['status'], v['content_type'], v['content_length'],
             v['alive'], 1))
        inserted += 1
        if v['alive']:
            alive_count += 1
    conn.commit()

    print(f"\n=== complete ===", file=sys.stderr)
    print(f"  matched + verified: {inserted}", file=sys.stderr)
    print(f"  alive: {alive_count}", file=sys.stderr)
    print(f"  distinct hospitals now found via cms-hpt: "
          f"{len({m['ccn'] for m, v in zip(matched, verifies_ordered) if v.get('alive')})}",
          file=sys.stderr)


if __name__ == '__main__':
    asyncio.run(main())
