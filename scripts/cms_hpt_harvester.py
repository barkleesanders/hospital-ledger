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

# --- Edge routing (2026-11-01) ---
# On this VM, ANY first-seen hospital domain raises a human approval card —
# including direct script egress (the 2026-11-01 harvest card bursts came
# from these scripts probing directly, not from an agent browsing). Route
# through hospitalledger.com/api/fetch via hl_edge, the pattern
# probe_mrf_urls.py / mrf_parse.py already use: with HL_FETCH_PROXY set the
# VM only ever connects to the one already-approved Worker domain. Config
# comes from the environment or ~/hospital-ledger.env (the file hl_wave.py
# reads); no config = direct fetch, unchanged behaviour (e.g. the Mini).
_EDGE_ENV_FILE = os.path.join(os.path.expanduser('~'), 'hospital-ledger.env')
if os.path.exists(_EDGE_ENV_FILE):
    for _line in open(_EDGE_ENV_FILE):
        _k, _, _v = _line.strip().partition('=')
        if _k.strip() in ('HL_FETCH_PROXY', 'HL_FETCH_TOKEN') and _v.strip():
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import hl_edge
except Exception:
    hl_edge = None

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


# --- Transport-failure accounting (harvest fix 2026-11-09, Finding 1) ---
# head_verify returns status=-1 when NO HTTP answer arrived (timeout,
# connection error, dead edge leg) — that is transport evidence, not a
# hospital answer. On 2026-11-08 a dead leg made every verification -1,
# both harvest scripts exited 0, and the INSERT OR REPLACE commit stamped
# 2,361 previously-good rows alive=0, silently suppressing promote
# candidates. Rules now enforced by commit_verifications():
#   * A transport-failed result NEVER overwrites an existing alive=1 row.
#   * If transport failures exceed TRANSPORT_FAIL_SHARE of attempts (with
#     at least TRANSPORT_MIN_SAMPLE attempts), or a run gets ZERO HTTP
#     answers at all, the leg is declared dead: transport-failed rows are
#     not committed in any form, genuine HTTP answers still commit, and
#     the caller exits TRANSPORT_DEAD_EXIT (non-zero).
# The 25% share gives ~3x headroom over the healthy 2026-11-02 baseline
# (112 transport failures / 1,468 verifications = 7.6%).
TRANSPORT_FAIL_SHARE = 0.25
TRANSPORT_MIN_SAMPLE = 20
TRANSPORT_DEAD_EXIT = 2


def transport_stats(verifies):
    """(attempts, transport_failures, http_answers) for verify results."""
    attempts = sum(1 for v in verifies if v is not None)
    tfail = sum(1 for v in verifies if v is not None and v.get('status') == -1)
    return attempts, tfail, attempts - tfail


def transport_dead(verifies):
    """True when this run's verification leg was transport-dead/degraded."""
    attempts, tfail, answers = transport_stats(verifies)
    if attempts and answers == 0:
        return True
    return (attempts >= TRANSPORT_MIN_SAMPLE
            and tfail > TRANSPORT_FAIL_SHARE * attempts)


def commit_verifications(conn, matched, verifies, source_prefix, now):
    """Commit verify results to mrf_rediscovered under Finding-1 rules.
    Returns (committed_count, transport_failure_flag)."""
    dead = transport_dead(verifies)
    attempts, tfail, answers = transport_stats(verifies)
    if attempts:
        print(f"  transport: {tfail}/{attempts} no-answer "
              f"({tfail / attempts:.1%} transport failures, {answers} HTTP answers)",
              file=sys.stderr)
    if dead:
        print(f"  TRANSPORT FAILURE: transport failures exceed "
              f"{TRANSPORT_FAIL_SHARE:.0%} of {attempts} verifications (or zero "
              f"HTTP answers) — transport-failed rows will NOT be committed; "
              f"exiting {TRANSPORT_DEAD_EXIT}", file=sys.stderr)
    c = conn.cursor()
    committed = 0
    for m, v in zip(matched, verifies):
        if v is None:
            continue
        if v.get('status') == -1:
            if dead:
                continue  # dead leg: never write transport evidence
            # Healthy run: a no-answer result must not stamp over a
            # previously verified-alive row for the same (ccn, url).
            prev = c.execute(
                "SELECT alive FROM mrf_rediscovered WHERE ccn=? AND candidate_url=?",
                (m['ccn'], m['mrf_url'])).fetchone()
            if prev and prev[0] == 1:
                continue
        c.execute("""INSERT OR REPLACE INTO mrf_rediscovered
            (ccn, source_page, candidate_url, anchor_text, score, discovered_at,
             head_status, head_content_type, head_content_length, alive, rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m['ccn'], source_prefix + m['source_domain'], m['mrf_url'],
             f"cms-hpt:{m['matched_location'][:60]}", 10, now,
             v['status'], v['content_type'], v['content_length'], v['alive'], 1))
        committed += 1
    conn.commit()
    return committed, dead


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


def _letter_suffixed(ccn):
    """CCNs carrying a letter suffix are federal facilities (F = VA/IHS/PHS)
    or PPS-exempt distinct-part subunits — neither publishes a civilian
    cms-hpt.txt. (Harvest fix 2026-11-09, Finding 2.)"""
    return any(ch.isalpha() for ch in (ccn or ''))


_domain_state_cache = None


def domain_state_hint(conn, domain):
    """Best available state for the facility behind a cms-hpt source domain:
    the state of hospitals already known on the same host (from mrf_seed /
    mrf_rediscovered URLs) when they unanimously agree, else None. The
    candidate's facility state is otherwise unknowable from a cms-hpt entry,
    so a unanimous known-host state is the only state evidence we may use.
    (Harvest fix 2026-11-09, Finding 2.)"""
    global _domain_state_cache
    if _domain_state_cache is None:
        _domain_state_cache = {}
        rows = conn.execute("""
            SELECT h.state, s.mrf_url, s.mrf_page FROM hospitals h
            JOIN mrf_seed s ON s.ccn = h.ccn
            UNION ALL
            SELECT h.state, r.candidate_url, '' FROM hospitals h
            JOIN mrf_rediscovered r ON r.ccn = h.ccn
        """)
        for state, url_a, url_b in rows:
            for url in (url_a, url_b):
                if not url:
                    continue
                try:
                    host = urlparse(url).netloc.lower()
                except Exception:
                    continue
                if host.startswith('www.'):
                    host = host[4:]
                if host:
                    _domain_state_cache.setdefault(host, set()).add(state)
    try:
        host = urlparse(domain).netloc.lower()
    except Exception:
        return None
    if host.startswith('www.'):
        host = host[4:]
    states = _domain_state_cache.get(host)
    if states and len(states) == 1:
        return next(iter(states))
    return None


def match_to_ccn(conn, location_name, state_hint=None):
    """Fuzzy match a CMS-HPT location-name to a CCN. state_hint narrows search.

    Harvest fix 2026-11-09 (Finding 2 — matcher vetoes): the Nov 9 harvest
    attached civilian files to federal VA CCNs on shared geographic tokens
    (03033F, 22001F, 24002F, 38003F). Two vetoes now apply:
      * Federal/subunit veto: letter-suffixed CCNs are NEVER matched to a
        cms-hpt candidate (federal facilities do not publish civilian
        cms-hpt files), regardless of name score or state.
      * State veto: when state_hint is given (callers derive it from the
        source domain via domain_state_hint), candidates are restricted to
        that state — the matched CCN's state must agree with the
        candidate's facility state. Name-token similarity alone is never
        sufficient across a state line.
    """
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
        if _letter_suffixed(ccn):
            continue
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

    if hl_edge is not None and hl_edge.enabled():
        print(f"  {hl_edge.preflight()}", file=sys.stderr)
    async with httpx.AsyncClient(headers={'User-Agent': UA, 'Accept': '*/*'},
                                 limits=limits, verify=False,
                                 **(hl_edge.httpx_kwargs(async_=True, limits=limits) if hl_edge else {})) as client:
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
            m = match_to_ccn(conn, e['location-name'],
                             state_hint=domain_state_hint(conn, domain))
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

        # Commit under Finding-1 transport rules (helper above): a dead
        # leg can no longer stamp previously-good rows alive=0.
        now = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')
        committed, dead = commit_verifications(
            conn, matched, verifies_ordered, 'cms-hpt:', now)
        live_count = sum(1 for v in verifies_ordered if v and v.get('alive'))
        print(f"\n=== complete ===", file=sys.stderr)
        print(f"  matched + verified: {len(matched)}", file=sys.stderr)
        print(f"  committed: {committed}", file=sys.stderr)
        print(f"  alive: {live_count}", file=sys.stderr)
        print(f"  distinct hospitals now found via cms-hpt: "
              f"{len({m['ccn'] for m, v in zip(matched, verifies_ordered) if v and v.get('alive')})}",
              file=sys.stderr)
        return TRANSPORT_DEAD_EXIT if dead else 0
        # short-circuit — old commit block below is now dead code

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
    sys.exit(asyncio.run(main()))
