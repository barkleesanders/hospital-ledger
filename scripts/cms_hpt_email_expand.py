#!/usr/bin/env python3
"""Stage 2.12: Email-domain expansion + Wayback fallback.

Two new sources for the remaining 640 missing hospitals:

  A) **Contact-email domains** from every cms-hpt.txt we already harvested.
     Hospitals often list `contact-email: PriceTransparency@<corporate-domain>.org`.
     The corporate domain is usually NOT the same as the public hospital site —
     and that corporate site often hosts a system-wide /cms-hpt.txt listing more
     hospitals we haven't found yet.

  B) **Wayback Machine** for each still-missing hospital. For the 640 hospitals
     where automated discovery has failed, query the IA CDX API:
       https://web.archive.org/cdx/search/cdx?url=*.<guessed-domain>/*standardcharges*&output=json
     If Wayback has any historical MRF snapshot, fetch it via web.archive.org's
     timegate (https://web.archive.org/web/<timestamp>/<original>) and verify.
"""
import asyncio, sqlite3, re, sys, os, datetime
from urllib.parse import urlparse, quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from cms_hpt_harvester import (parse_cms_hpt, fetch_cms_hpt, head_verify,
                                build_hospital_index, match_to_ccn, UA)
from cms_hpt_expand import name_to_domains, COMMON_TLDS

try:
    import httpx
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'httpx'])
    import httpx


EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')


async def fetch_email_domains(client, sem, hpt_url):
    """Re-fetch a /cms-hpt.txt and extract all email-domain parts."""
    async with sem:
        try:
            r = await client.get(hpt_url, follow_redirects=True, timeout=15.0)
            if r.status_code != 200:
                return set()
            doms = set()
            for m in EMAIL_RE.finditer(r.text):
                d = m.group(1).lower().strip().rstrip('.')
                if d and '.' in d:
                    doms.add(d)
            return doms
        except Exception:
            return set()


async def wayback_search(client, sem, domain):
    """Query Wayback CDX for any standardcharges URL on this domain."""
    async with sem:
        url = (f"https://web.archive.org/cdx/search/cdx?"
               f"url={quote(domain)}/*standardcharges*&output=json&limit=10&filter=statuscode:200")
        try:
            r = await client.get(url, timeout=20.0)
            if r.status_code != 200:
                return domain, []
            rows = r.json()
            if not rows or len(rows) < 2:
                return domain, []
            # row format: [urlkey, timestamp, original, mimetype, statuscode, digest, length]
            urls = []
            for row in rows[1:]:
                ts = row[1]
                orig = row[2]
                # Build replay URL (id_ = identity, raw content)
                replay = f"https://web.archive.org/web/{ts}id_/{orig}"
                urls.append((replay, orig))
            return domain, urls
        except Exception:
            return domain, []


async def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # ---- Phase 1: collect email-domains from existing cms-hpt sources ----
    known_hpt_sources = list({row[0] for row in c.execute(
        "SELECT DISTINCT source_page FROM mrf_rediscovered "
        "WHERE source_page LIKE 'cms-hpt%' OR source_page LIKE 'cms-hpt-v2:%'")})
    # source_page format: "cms-hpt:https://www.foo.org" or "cms-hpt-v2:https://..."
    cms_hpt_urls = set()
    for sp in known_hpt_sources:
        # strip the "cms-hpt:" or "cms-hpt-v2:" prefix
        for prefix in ('cms-hpt-v2:', 'cms-hpt:'):
            if sp.startswith(prefix):
                root = sp[len(prefix):]
                if root.startswith('http'):
                    cms_hpt_urls.add(root.rstrip('/') + '/cms-hpt.txt')
                break
    print(f"known cms-hpt URLs to mine for emails: {len(cms_hpt_urls)}", file=sys.stderr)

    sem = asyncio.Semaphore(40)
    head_sem = asyncio.Semaphore(40)
    wayback_sem = asyncio.Semaphore(8)  # IA rate-limits
    limits = httpx.Limits(max_keepalive_connections=80, max_connections=160)

    async with httpx.AsyncClient(headers={'User-Agent': UA, 'Accept': '*/*'},
                                 limits=limits, verify=False) as client:
        # Fetch each known cms-hpt to extract email domains
        tasks = [fetch_email_domains(client, sem, u) for u in cms_hpt_urls]
        email_doms = set()
        done = 0
        for fut in asyncio.as_completed(tasks):
            doms = await fut
            email_doms.update(doms)
            done += 1
            if done % 100 == 0:
                print(f"  scanned {done}/{len(tasks)} | email-domains so far: {len(email_doms)}",
                      file=sys.stderr)
        print(f"  total email-domains harvested: {len(email_doms)}", file=sys.stderr)

        # Build candidate URL list (https:// + www variant) from email domains
        candidate_domains = set()
        for d in email_doms:
            # skip obvious non-hospital domains
            if d in {'gmail.com', 'yahoo.com', 'aol.com', 'outlook.com', 'hotmail.com',
                     'icloud.com', 'live.com', 'protonmail.com', 'msn.com'}:
                continue
            candidate_domains.add(f"https://{d}")
            candidate_domains.add(f"https://www.{d}")
        print(f"  candidate cms-hpt URLs to probe: {len(candidate_domains)}", file=sys.stderr)

        # ---- Phase 2: probe /cms-hpt.txt on each ----
        new_domains_to_check = list(candidate_domains)
        probe_tasks = [fetch_cms_hpt(client, sem, d) for d in new_domains_to_check]
        all_entries = []
        new_hits = 0
        done = 0
        for fut in asyncio.as_completed(probe_tasks):
            d, entries = await fut
            done += 1
            if entries:
                new_hits += 1
                for e in entries:
                    all_entries.append((d, e))
            if done % 100 == 0 or done == len(probe_tasks):
                print(f"  probed {done}/{len(probe_tasks)} | cms-hpt hits={new_hits} | entries={len(all_entries)}",
                      file=sys.stderr)

        # ---- Phase 3: match + verify ----
        print(f"\nmatching {len(all_entries)} entries...", file=sys.stderr)
        build_hospital_index(conn)
        matched = []
        for dom, e in all_entries:
            m = match_to_ccn(conn, e['location-name'])
            if m:
                matched.append({'ccn': m[0], 'matched_location': e['location-name'],
                                'mrf_url': e['mrf-url'], 'source_domain': dom})
        print(f"  matched: {len(matched)} | distinct CCNs: {len({m['ccn'] for m in matched})}",
              file=sys.stderr)

        async def verify_idx(idx, url):
            return idx, await head_verify(client, head_sem, url)
        vtasks = [verify_idx(i, m['mrf_url']) for i, m in enumerate(matched)]
        verifies = [None] * len(matched)
        done = live = 0
        for fut in asyncio.as_completed(vtasks):
            idx, v = await fut
            verifies[idx] = v
            done += 1
            if v.get('alive'): live += 1
            if done % 200 == 0 or done == len(vtasks):
                print(f"  verified {done}/{len(vtasks)} | live={live}", file=sys.stderr)

        now = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
        for m, v in zip(matched, verifies):
            if v is None: continue
            c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
                (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
                 head_status, head_content_type, head_content_length, alive, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (m['ccn'], 'cms-hpt-email:' + m['source_domain'], m['mrf_url'],
                 f"cms-hpt-email:{m['matched_location'][:60]}", 10, now,
                 v['status'], v['content_type'], v['content_length'], v['alive'], 1))
        conn.commit()
        print(f"\n=== email-domain phase complete ===", file=sys.stderr)
        new_ccns = {m['ccn'] for m, v in zip(matched, verifies) if v and v.get('alive')}
        print(f"  alive: {sum(1 for v in verifies if v and v.get('alive'))}", file=sys.stderr)
        print(f"  distinct hospitals: {len(new_ccns)}", file=sys.stderr)

        # ---- Phase 4: Wayback fallback for still-missing ----
        still_missing = c.execute("""
            SELECT h.ccn, h.name, h.state FROM hospitals h
            WHERE h.hospital_type IN
              ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
              AND h.ccn NOT IN (
                SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p
                  ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
              AND h.ccn NOT IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
        """).fetchall()
        print(f"\nWayback fallback on {len(still_missing)} still-missing hospitals...", file=sys.stderr)

        # For each missing hospital: build candidate hosts, ask Wayback CDX
        ccn_to_doms = {}
        for ccn, name, state in still_missing:
            doms = []
            for cand in name_to_domains(name):
                p = urlparse(cand)
                if p.netloc:
                    doms.append(p.netloc.replace('www.', ''))
            ccn_to_doms[ccn] = list(dict.fromkeys(doms))[:6]

        # Query CDX (one query per (ccn, domain))
        wb_tasks = []
        wb_map = {}  # idx -> (ccn, dom)
        idx = 0
        for ccn, doms in ccn_to_doms.items():
            for d in doms:
                wb_map[idx] = (ccn, d)
                wb_tasks.append(wayback_search(client, wayback_sem, d))
                idx += 1
        print(f"  Wayback CDX queries: {len(wb_tasks)}", file=sys.stderr)

        wb_results = []  # (ccn, dom, url_pairs)
        done = 0
        hit_ccns = set()
        for i, fut in enumerate(asyncio.as_completed(wb_tasks)):
            dom, url_pairs = await fut
            # map back via the order of iteration — we can't recover, so just take what we get
            # actually we need ordered execution. Re-do in order:
            pass
        # Switch to ordered gather for correctness
        wb_results = await asyncio.gather(*wb_tasks, return_exceptions=True)
        wb_candidates = []  # (ccn, replay_url, original_url)
        for i, res in enumerate(wb_results):
            if isinstance(res, Exception): continue
            dom, url_pairs = res
            ccn, _ = wb_map[i]
            for replay, orig in url_pairs[:2]:  # max 2 per (ccn, dom)
                wb_candidates.append((ccn, replay, orig))
        print(f"  Wayback URL candidates found: {len(wb_candidates)}", file=sys.stderr)

        # Verify each Wayback replay URL — also try the LIVE original URL
        async def verify_pair(i, replay, original):
            v_live = await head_verify(client, head_sem, original)
            v_wb = await head_verify(client, head_sem, replay) if not v_live.get('alive') else None
            return i, v_live, v_wb
        ptasks = [verify_pair(i, c[1], c[2]) for i, c in enumerate(wb_candidates)]
        wb_verifies = await asyncio.gather(*ptasks, return_exceptions=True)
        wb_new = 0
        for i, c_tuple in enumerate(wb_candidates):
            ccn, replay, original = c_tuple
            res = wb_verifies[i]
            if isinstance(res, Exception): continue
            _, v_live, v_wb = res
            # Prefer live; if dead, use wayback replay
            if v_live.get('alive'):
                final_url = original; v = v_live
            elif v_wb and v_wb.get('alive'):
                final_url = replay; v = v_wb
            else:
                continue
            c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
                (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
                 head_status, head_content_type, head_content_length, alive, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ccn, 'wayback:' + (replay if v_wb and v_wb.get('alive') else original),
                 final_url, 'wayback', 8, now,
                 v['status'], v['content_type'], v['content_length'], v['alive'], 1))
            wb_new += 1
        conn.commit()
        print(f"\n=== wayback phase complete ===", file=sys.stderr)
        print(f"  new alive hospitals via wayback: {wb_new}", file=sys.stderr)


if __name__ == '__main__':
    asyncio.run(main())
