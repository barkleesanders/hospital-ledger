#!/usr/bin/env python3
"""Stage 2.11: Expanded CMS-HPT harvest.

Builds a much larger domain list to probe for /cms-hpt.txt:
  1. Every distinct host from every URL in our DB (mrf_seed, mrf_rediscovered)
  2. Name-derived domain candidates for each still-missing hospital
  3. Common health-system parent domains
"""
import asyncio, sqlite3, re, sys, os, datetime
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
# reuse parser/verifier helpers
from cms_hpt_harvester import (
    parse_cms_hpt, fetch_cms_hpt, head_verify,
    build_hospital_index, match_to_ccn, UA,
)
try:
    import httpx
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'httpx'])
    import httpx

STOP = {'hospital','hospitals','medical','center','health','system','systems',
        'care','inc','llc','corp','corporation','the','of','and','for','st',
        'saint','memorial','community','regional','foundation','campus',
        'group','partners','associates','centers'}
COMMON_TLDS = ('.org', '.com', '.health', '.net', '.us')


def name_to_domains(name):
    """Generate candidate hospital domains from name."""
    tokens = re.findall(r'[a-z]+', (name or '').lower())
    sig = [t for t in tokens if t not in STOP and len(t) >= 3]
    if not sig and tokens:
        sig = tokens[:3]
    if not sig:
        return []
    bases = set()
    bases.add(''.join(tokens))
    bases.add(''.join(sig))
    if len(sig) >= 2:
        bases.add(''.join(sig[:2]))
        bases.add(''.join(sig[:2]) + 'hospital')
        bases.add(''.join(sig[:2]) + 'health')
    bases.add(sig[0])
    bases.add(sig[0] + 'hospital')
    bases.add(sig[0] + 'health')
    if len(sig) >= 3:
        bases.add(''.join(sig[:3]))
    bases = {b for b in bases if 4 <= len(b) <= 60}
    out = []
    for b in bases:
        for tld in COMMON_TLDS:
            out.append(f"https://www.{b}{tld}")
            out.append(f"https://{b}{tld}")
    return out[:24]


async def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # Build domain set from existing DB URLs
    domains = set()
    for col in ['mrf_url', 'mrf_page']:
        for row in c.execute(f"SELECT DISTINCT {col} FROM mrf_seed WHERE {col} != ''"):
            try:
                p = urlparse(row[0])
                if p.scheme and p.netloc:
                    domains.add(f"{p.scheme}://{p.netloc}")
            except Exception: pass
    for col in ['candidate_url', 'source_page']:
        for row in c.execute(f"SELECT DISTINCT {col} FROM mrf_rediscovered WHERE {col} LIKE 'http%'"):
            try:
                p = urlparse(row[0])
                if p.scheme and p.netloc:
                    domains.add(f"{p.scheme}://{p.netloc}")
            except Exception: pass

    print(f"existing-URL domains: {len(domains)}", file=sys.stderr)
    pre_count = len(domains)

    # Add name-derived candidates for still-missing hospitals
    missing = c.execute("""
        SELECT h.name FROM hospitals h
        WHERE h.hospital_type IN
          ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
          AND h.ccn NOT IN (
            SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p
              ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
          AND h.ccn NOT IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
    """).fetchall()
    print(f"still-missing hospitals: {len(missing)}", file=sys.stderr)
    for (name,) in missing:
        for d in name_to_domains(name):
            domains.add(d)
    print(f"after name heuristics: {len(domains)} (+{len(domains)-pre_count})", file=sys.stderr)

    sem = asyncio.Semaphore(80)
    head_sem = asyncio.Semaphore(80)
    limits = httpx.Limits(max_keepalive_connections=80, max_connections=160)

    async with httpx.AsyncClient(headers={'User-Agent': UA, 'Accept':'*/*'},
                                 limits=limits, verify=False) as client:
        # Phase 1: fetch /cms-hpt.txt on each
        tasks = [fetch_cms_hpt(client, sem, d) for d in domains]
        all_entries = []
        domain_hits = 0
        new_seen_domains = set()
        # Existing domains where we already harvested in Stage 2.10 (skip-the-dup is harmless — INSERT OR REPLACE)
        for i, fut in enumerate(asyncio.as_completed(tasks), 1):
            d, entries = await fut
            if entries:
                domain_hits += 1
                new_seen_domains.add(d)
                for e in entries:
                    all_entries.append((d, e))
            if i % 100 == 0 or i == len(tasks):
                print(f"  probed {i}/{len(tasks)} | domains-with-hpt={domain_hits} | entries={len(all_entries)}",
                      file=sys.stderr)

        # Phase 2: match
        print(f"\nmatching {len(all_entries)} entries...", file=sys.stderr)
        build_hospital_index(conn)
        matched = []
        for d, e in all_entries:
            m = match_to_ccn(conn, e['location-name'])
            if m:
                matched.append({
                    'ccn': m[0], 'matched_location': e['location-name'],
                    'mrf_url': e['mrf-url'], 'source_domain': d,
                })
        print(f"  matched: {len(matched)} | distinct CCNs: {len({m['ccn'] for m in matched})}",
              file=sys.stderr)

        # Phase 3: HEAD-verify with indexed ordering
        async def verify_indexed(idx, url):
            return idx, await head_verify(client, head_sem, url)
        vtasks = [verify_indexed(i, m['mrf_url']) for i, m in enumerate(matched)]
        verifies = [None] * len(matched)
        done = 0; live_n = 0
        for fut in asyncio.as_completed(vtasks):
            idx, v = await fut
            verifies[idx] = v
            done += 1
            if v.get('alive'): live_n += 1
            if done % 200 == 0 or done == len(matched):
                print(f"  verified {done}/{len(matched)} | live={live_n}", file=sys.stderr)

        # Commit
        now = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
        for m, v in zip(matched, verifies):
            if v is None: continue
            c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
                (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
                 head_status, head_content_type, head_content_length, alive, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (m['ccn'], 'cms-hpt-v2:' + m['source_domain'], m['mrf_url'],
                 f"cms-hpt:{m['matched_location'][:60]}", 10, now,
                 v['status'], v['content_type'], v['content_length'], v['alive'], 1))
        conn.commit()
        print(f"\n=== complete ===", file=sys.stderr)
        print(f"  alive: {sum(1 for v in verifies if v and v.get('alive'))}", file=sys.stderr)
        print(f"  distinct hospitals: "
              f"{len({m['ccn'] for m, v in zip(matched, verifies) if v and v.get('alive')})}",
              file=sys.stderr)


if __name__ == '__main__':
    asyncio.run(main())
