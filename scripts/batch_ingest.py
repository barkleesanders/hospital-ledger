#!/usr/bin/env python3
"""Batch ingest hospitals via mrf_parse → data/parsed/<ccn>.json.

Picks BEST live MRF URL per CCN (preferring direct file URLs over hub pages).
Supports resumable full-corpus runs by skipping already-parsed CCNs.
"""
import sqlite3, os, sys, time, json, argparse, concurrent.futures, datetime, subprocess, re, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mrf_parse import ingest_one, normalize_source_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, 'scripts')
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
PARSED_DIR = os.path.join(ROOT, 'data', 'parsed')
SITE_PRICE_DIR = os.path.join(ROOT, 'site', 'data', 'prices')
PRICED_INDEX = os.path.join(ROOT, 'public', 'data', 'prices', 'index.json')
TERMINAL_EXCEPTIONS = os.path.join(ROOT, 'data', 'coverage_terminal_exceptions.json')
STATUS_FILE = os.path.join(ROOT, 'data', 'full_standardize_status.json')
FAILURES_FILE = os.path.join(ROOT, 'data', 'full_standardize_failures.jsonl')
PARSE_ERRORS_DIR = os.path.join(ROOT, 'data', 'parse_errors')


DIRECTISH_URL_HINTS = (
    'mrfdownload',
    'export=download',
    '.ashx',
)

HTML_WRAPPER_HINTS = (
    'download.aspx',
    '/download',
    'mrfdownload',
    'export=download',
    '.ashx',
)

EXPLICIT_FILE_SUFFIXES = ('.csv', '.json', '.xlsx', '.xls', '.zip')

MAX_CANDIDATE_TRIES = 6
ROW_COUNT_RE = re.compile(rb'"row_count":(\d+)')
LIVE_PROBE_BYTES = 2048
LIVE_PROBE_TIMEOUT_SECONDS = 20

GENERIC_NAME_TOKENS = {
    'hospital', 'hosp', 'medical', 'center', 'centre', 'health', 'system',
    'memorial', 'regional', 'community', 'county', 'district', 'saint',
    'st', 'clinic', 'clinics', 'inc', 'llc', 'the', 'and', 'for', 'of',
    'essentia',
}

RANK_STOP_TOKENS = {
    'hospital', 'health', 'system', 'inc', 'llc', 'the', 'and', 'for', 'of',
    'essentia',
}


def is_direct_mrf_url(url):
    lower = normalize_source_url(url).lower()
    if any(token in lower for token in EXPLICIT_FILE_SUFFIXES):
        return True
    return any(token in lower for token in DIRECTISH_URL_HINTS)


def is_html_wrapper_url(url):
    lower = normalize_source_url(url).lower()
    return any(token in lower for token in HTML_WRAPPER_HINTS)


def has_explicit_file_suffix(url):
    lower = normalize_source_url(url).lower()
    return any(token in lower for token in EXPLICIT_FILE_SUFFIXES)


def is_protected_wrapper_url(url):
    lower = (url or '').lower()
    return 'urldefense.com/' in lower


def hospital_specific_tokens(name):
    tokens = []
    for raw in (name or '').lower().replace("'", ' ').split():
        token = ''.join(ch for ch in raw if ch.isalnum())
        if len(token) < 4 or token in GENERIC_NAME_TOKENS:
            continue
        tokens.append(token)
    return tokens


def hospital_rank_tokens(name):
    tokens = []
    for raw in (name or '').lower().replace("'", ' ').split():
        token = ''.join(ch for ch in raw if ch.isalnum())
        if len(token) < 4 or token in RANK_STOP_TOKENS:
            continue
        tokens.append(token)
    return tokens


def candidate_identity(candidate):
    return f"{candidate.get('name') or ''} {candidate.get('city') or ''}".strip()


def candidate_match_count(candidate):
    lower_url = normalize_source_url(candidate.get('url') or '').lower()
    rank_tokens = hospital_rank_tokens(candidate_identity(candidate))
    return sum(1 for token in rank_tokens if token in lower_url)


def transient_candidate_matches_hospital(name, url):
    lower_url = normalize_source_url(url).lower()
    tokens = hospital_specific_tokens(name)
    if not tokens:
        return False
    return any(token in lower_url for token in tokens)


def filelike_content_type(content_type):
    lower = (content_type or '').lower()
    return any(token in lower for token in (
        'text/csv',
        'application/csv',
        'application/json',
        'text/json',
        'application/octet-stream',
        'application/x-unknown',
        'spreadsheetml',
        'ms-excel',
        'application/zip',
        'application/x-zip',
    ))


def reject_candidate(url, content_type):
    lower_url = normalize_source_url(url).lower()
    lower_type = (content_type or '').lower()
    if 'cms.gov/hospital-price-transparency' in lower_url:
        return True
    if '.pdf' in lower_url or 'application/pdf' in lower_type:
        return True
    if '/innetwork/' in lower_url or 'cms_in-network-rates' in lower_url or 'in-network-rates' in lower_url:
        return True
    if lower_url.endswith('.xml') or '/feed.xml' in lower_url:
        return True
    if 'chargemaster-submission-guide' in lower_url:
        return True
    if 'text/html' in lower_type and is_direct_mrf_url(lower_url) and not is_html_wrapper_url(lower_url):
        return True
    if (
        'xml' in lower_type
        and 'application/json' not in lower_type
        and 'text/html' not in lower_type
        and 'spreadsheetml' not in lower_type
        and 'openxmlformats' not in lower_type
        and 'ms-excel' not in lower_type
    ):
        return True
    if 'search.hospitalpriceindex.com/hpi2/machinereadable/' in lower_url and 'text/html' in lower_type:
        return True
    return False


def probe_candidate_url(url):
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'HospitalLedgerBot/0.1 (+https://hospital-ledger.pages.dev)',
            'Range': f'bytes=0-{LIVE_PROBE_BYTES - 1}',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=LIVE_PROBE_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            content_type = response.headers.get('Content-Type', '')
            status = getattr(response, 'status', 200) or 200
            body = response.read(LIVE_PROBE_BYTES)
    except urllib.error.HTTPError as exc:
        return False, f'probe_http:{exc.code}'
    except Exception as exc:
        return False, f'probe:{type(exc).__name__}'

    if reject_candidate(final_url, content_type):
        return False, f'probe_reject:{status}:{content_type[:40]}'

    lower_type = (content_type or '').lower()
    body_prefix = body[:512].lstrip().lower()
    if 'text/html' in lower_type and not (is_html_wrapper_url(final_url) or is_protected_wrapper_url(final_url)):
        return False, f'probe_html:{status}:{content_type[:40]}'
    if (
        b'<html' in body_prefix
        or b'<!doctype html' in body_prefix
        or b'<meta http-equiv="refresh"' in body_prefix
    ) and not (is_html_wrapper_url(final_url) or is_protected_wrapper_url(final_url)):
        return False, f'probe_html_body:{status}:{content_type[:40]}'

    return True, f'probe_ok:{status}:{content_type[:40]}'


def candidate_priority(candidate):
    url = candidate['url']
    content_type = candidate.get('content_type') or ''
    if reject_candidate(url, content_type):
        return None
    normalized_url = normalize_source_url(url)
    explicit_rank = 0 if has_explicit_file_suffix(normalized_url) else 1 if is_direct_mrf_url(normalized_url) else 2
    wrapper_rank = 1 if is_protected_wrapper_url(url) or is_html_wrapper_url(normalized_url) else 0
    content_lower = content_type.lower()
    if any(token in content_lower for token in ('application/json', 'text/json', 'text/csv', 'application/csv', 'spreadsheetml', 'ms-excel', 'application/zip', 'application/x-zip')):
        content_rank = 0
    elif any(token in content_lower for token in ('application/octet-stream', 'application/x-unknown')):
        content_rank = 1
    elif 'text/html' in content_lower:
        content_rank = 3
    else:
        content_rank = 2
    name_match_count = candidate_match_count(candidate)
    name_rank = -name_match_count
    availability_rank = int(candidate.get('availability') or 0)
    source_rank = 0 if candidate['source'] == 'rediscovered' else 1
    score_rank = -int(candidate.get('score') or 0)
    return (
        explicit_rank,
        wrapper_rank,
        content_rank,
        name_rank,
        availability_rank,
        source_rank,
        score_rank,
        normalized_url,
    )


def ranked_mrf_candidates(conn, ccn):
    """Return ranked MRF candidates from seed + rediscovery by parseability."""
    rows = conn.execute("""
        SELECT h.name, h.city, r.candidate_url AS url, r.head_content_type AS content_type,
               r.score AS score, 'rediscovered' AS source
        FROM mrf_rediscovered r
        JOIN hospitals h ON h.ccn = r.ccn
        WHERE r.ccn = ? AND r.alive = 1
        UNION ALL
        SELECT h.name, h.city, s.mrf_url AS url, p.content_type AS content_type,
               0 AS score, 'seed' AS source
        FROM mrf_seed s
        JOIN mrf_probe p ON p.ccn = s.ccn AND p.mrf_url = s.mrf_url
        JOIN hospitals h ON h.ccn = s.ccn
        WHERE s.ccn = ? AND p.alive = 1
    """, (ccn, ccn)).fetchall()
    candidates = {}

    def add_candidate(candidate):
        url = candidate['url']
        normalized_url = normalize_source_url(url)
        if not url:
            return
        existing = candidates.get(normalized_url)
        if existing is None:
            candidates[normalized_url] = candidate
            return
        current = candidate_priority(candidate)
        previous = candidate_priority(existing)
        if current is None:
            return
        if previous is None or current < previous:
            candidates[normalized_url] = candidate

    for row in rows:
        add_candidate({
            'name': row[0],
            'city': row[1],
            'url': row[2],
            'content_type': row[3] or '',
            'score': row[4] or 0,
            'source': row[5],
            'availability': 0,
        })

    # If the only "alive" URLs are obvious junk wrappers, add direct file URLs
    # that merely failed a transient HEAD probe to the candidate pool.
    fallback_rows = conn.execute("""
        SELECT h.name, h.city, r.candidate_url AS url, r.head_content_type AS content_type,
               r.score AS score, 'rediscovered' AS source
        FROM mrf_rediscovered r
        JOIN hospitals h ON h.ccn = r.ccn
        WHERE r.ccn = ?
          AND r.alive = 0
          AND r.head_status = -1
        UNION ALL
        SELECT h.name, h.city, s.mrf_url AS url, p.content_type AS content_type,
               0 AS score, 'seed' AS source
        FROM mrf_seed s
        JOIN mrf_probe p ON p.ccn = s.ccn AND p.mrf_url = s.mrf_url
        JOIN hospitals h ON h.ccn = s.ccn
        WHERE s.ccn = ?
          AND p.alive = 0
          AND p.http_status = -1
    """, (ccn, ccn)).fetchall()
    for row in fallback_rows:
        candidate = {
            'name': row[0],
            'city': row[1],
            'url': row[2],
            'content_type': row[3] or '',
            'score': row[4] or 0,
            'source': row[5],
            'availability': 1,
        }
        if not is_direct_mrf_url(candidate['url']):
            continue
        if not transient_candidate_matches_hospital(candidate_identity(candidate), candidate['url']):
            continue
        add_candidate(candidate)

    if not candidates:
        return []

    ranked = []
    for candidate in candidates.values():
        priority = candidate_priority(candidate)
        if priority is None:
            continue
        ranked.append((priority, candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked]


def pick_best_mrf(conn, ccn):
    """Return (mrf_url, name) chosen from seed + rediscovery by parseability."""
    candidates = ranked_mrf_candidates(conn, ccn)
    if not candidates:
        return None, None
    best = candidates[0]
    return best['url'], best['name']


def load_parsed_record(ccn):
    path = parsed_path(ccn)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_price_preview_record(ccn):
    path = os.path.join(SITE_PRICE_DIR, f'{ccn}.json')
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def parsed_row_count(ccn):
    try:
        with open(parsed_path(ccn), 'rb') as f:
            head = f.read(512)
    except OSError:
        return 0
    match = ROW_COUNT_RE.search(head)
    return int(match.group(1)) if match else 0


def parsed_source_candidate(ccn):
    record = load_parsed_record(ccn) or load_price_preview_record(ccn)
    if not record:
        return None
    url = str(record.get('source_url') or '').strip()
    if not url:
        return None
    return {
        'name': str(record.get('hospital_name') or ccn),
        'city': '',
        'url': url,
        'content_type': '',
        'score': 0,
        'source': 'parsed-cache',
        'availability': 2,
    }


def clear_parsed_record(ccn):
    """Remove both .json and .json.gz so a re-ingest can't be shadowed by a stale .gz."""
    for path in (parsed_path(ccn), parsed_path(ccn) + '.gz'):
        try:
            os.remove(path)
        except FileNotFoundError:
            continue


def write_parse_error_log(ccn, candidate, returncode, stderr, stdout, note=''):
    """Persist the FULL stderr+stdout of a failed parse subprocess.

    The inline progress UI / failures jsonl only keep an ~80-char summary, which
    is undiagnosable. This writes everything to data/parse_errors/{ccn}.log so a
    later pass can read the real traceback. Returns the log path on success, or
    a short fallback string if the write itself fails (logging must never crash
    the ingest worker).
    """
    try:
        os.makedirs(PARSE_ERRORS_DIR, exist_ok=True)
        log_path = os.path.join(PARSE_ERRORS_DIR, f'{ccn}.log')
        url = (candidate or {}).get('url') or ''
        source = (candidate or {}).get('source') or ''
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        with open(log_path, 'w') as f:
            f.write(f"# parse failure log for CCN {ccn}\n")
            f.write(f"# timestamp:  {ts}\n")
            f.write(f"# returncode: {returncode}\n")
            if note:
                f.write(f"# note:       {note}\n")
            f.write(f"# source:     {source}\n")
            f.write(f"# url:        {url}\n")
            f.write("\n===== STDERR =====\n")
            f.write(stderr or '(empty)\n')
            f.write("\n===== STDOUT =====\n")
            f.write(stdout or '(empty)\n')
        return log_path
    except OSError as exc:
        return f'(log_write_failed:{exc})'


def ingest_candidate_subprocess(ccn, candidate, timeout_seconds):
    clear_parsed_record(ccn)
    url = candidate['url']
    name = candidate.get('name') or ccn
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS_DIR, 'mrf_parse.py'),
        '--ccn', ccn,
        '--name', name,
        '--url', url,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, 0, '', f'timeout:{int(timeout_seconds)}s'

    elapsed = time.time() - t0
    record = load_parsed_record(ccn)
    if proc.returncode != 0:
        log_path = write_parse_error_log(ccn, candidate, proc.returncode, proc.stderr, proc.stdout)
        detail = (proc.stderr or proc.stdout or '').strip().replace('\n', ' ')
        summary = detail[:80] or f'{elapsed:.1f}s'
        return False, 0, '', f"subprocess:{proc.returncode}:{summary} [log:{log_path}]"
    if not record:
        log_path = write_parse_error_log(ccn, candidate, 0, proc.stderr, proc.stdout, note='no_output')
        return False, 0, '', f'no_output:{elapsed:.1f}s [log:{log_path}]'

    items = record.get('items') or []
    row_count = int(record.get('row_count') or len(items) or 0)
    fmt = str(record.get('format_detected') or '')
    detail = f"{elapsed:.1f}s" if row_count > 0 else f"zero_items:{elapsed:.1f}s"
    return row_count > 0, row_count, fmt, detail


def ingest_candidate(ccn, candidate):
    clear_parsed_record(ccn)
    url = candidate['url']
    name = candidate.get('name') or ccn
    t0 = time.time()
    ok, n, fmt, err = ingest_one(ccn, name, url)
    elapsed = time.time() - t0
    if ok and n > 0:
        return True, n, fmt, err or f"{elapsed:.1f}s"
    detail = err or f"zero_items:{elapsed:.1f}s"
    return False, n, fmt, detail


def ingest_ccn(ccn, timeout_seconds=0, selected_candidate=None):
    """Ingest a CCN from either an authoritative plan or legacy ranking.

    When ``selected_candidate`` is supplied, its URL is the complete candidate
    list. The weekly planner already probed and selected that exact URL, so
    re-ranking here would make change detection and ingestion refer to different
    source files. Calls that omit it retain the historical fallback behavior.
    """
    if selected_candidate is not None:
        url = selected_candidate.get('url') if isinstance(selected_candidate, dict) else None
        if not isinstance(url, str) or not url.strip():
            clear_parsed_record(ccn)
            return ccn, False, 0, '', 'planned_url_missing'
        candidate = dict(selected_candidate)
        candidate['url'] = url
        candidate.setdefault('name', ccn)
        candidate.setdefault('source', 'refresh-plan')
        candidates = [candidate]
    else:
        conn = sqlite3.connect(DB)
        candidates = ranked_mrf_candidates(conn, ccn)
        conn.close()
        fallback = parsed_source_candidate(ccn)
        if fallback:
            fallback_url = normalize_source_url(fallback['url'])
            seen_urls = {normalize_source_url(candidate.get('url') or '') for candidate in candidates}
            if fallback_url and fallback_url not in seen_urls:
                candidates.append(fallback)
    if not candidates:
        clear_parsed_record(ccn)
        return ccn, False, 0, '', 'no_live_mrf'

    attempts = []
    max_tries = min(len(candidates), MAX_CANDIDATE_TRIES)
    for index, candidate in enumerate(candidates[:MAX_CANDIDATE_TRIES], start=1):
        live_ok, probe_detail = probe_candidate_url(candidate['url'])
        if not live_ok:
            attempts.append(f"{candidate.get('source') or 'candidate'}:-:{probe_detail}")
            continue
        if timeout_seconds and timeout_seconds > 0:
            ok, n, fmt, detail = ingest_candidate_subprocess(ccn, candidate, timeout_seconds)
        else:
            ok, n, fmt, detail = ingest_candidate(ccn, candidate)
        if ok and n > 0:
            source = candidate.get('source') or 'candidate'
            msg = detail if index == 1 else f"{detail} via {source} candidate {index}/{max_tries}"
            return ccn, True, n, fmt, msg
        attempts.append(f"{candidate.get('source') or 'candidate'}:{fmt or '-'}:{detail}")

    clear_parsed_record(ccn)
    return ccn, False, 0, '', attempts[-1][:200] if attempts else 'no_live_mrf'


def parsed_path(ccn):
    return os.path.join(PARSED_DIR, f'{ccn}.json')


def parsed_ok(ccn):
    return parsed_row_count(ccn) > 0


def load_done_set():
    """CCNs that should be skipped on --resume.

    Sources (both survive a `rm -rf data/parsed/`):
      - public/data/prices/index.json : every successfully-slimmed hospital
      - data/coverage_terminal_exceptions.json : known-dead, don't re-fetch

    This replaces the old `parsed_ok()` check, which used the existence of
    data/parsed/{ccn}.json as a resume marker — that file is now a transient
    gzipped scratch artifact, not the source of truth.
    """
    done = set()
    try:
        with open(PRICED_INDEX) as f:
            idx = json.load(f)
        for h in idx.get('hospitals', []):
            ccn = h.get('ccn')
            if ccn:
                done.add(ccn)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        with open(TERMINAL_EXCEPTIONS) as f:
            exc = json.load(f)
        for e in exc.get('exceptions', []):
            ccn = e.get('ccn')
            if ccn:
                done.add(ccn)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return done


def load_ccns_file(path):
    ccns = []
    with open(path) as handle:
        for raw_line in handle:
            ccn = raw_line.strip()
            if ccn:
                ccns.append(ccn)
    return ccns


def load_worklist(path):
    """Load a planner-generated CCN-to-candidate map.

    A direct ``{ccn: url}`` map is also accepted for small ad hoc runs. Invalid
    or absent URLs remain represented so ingestion reports a deterministic
    ``planned_url_missing`` failure instead of silently choosing another URL.
    """
    with open(path) as handle:
        payload = json.load(handle)
    entries = payload.get('hospitals') if isinstance(payload, dict) else None
    if entries is None and isinstance(payload, dict):
        entries = payload
    if not isinstance(entries, dict):
        raise ValueError('worklist must contain a hospitals object')

    worklist = {}
    for raw_ccn, raw_entry in entries.items():
        ccn = str(raw_ccn).strip()
        if not ccn:
            raise ValueError('worklist contains an empty CCN')
        if isinstance(raw_entry, str):
            entry = {'url': raw_entry}
        elif isinstance(raw_entry, dict):
            entry = dict(raw_entry)
        else:
            raise ValueError(f'worklist entry for {ccn} must be an object or URL string')
        worklist[ccn] = entry
    return worklist


def hospital_name(conn, ccn):
    row = conn.execute('SELECT name FROM hospitals WHERE ccn = ?', (ccn,)).fetchone()
    return str(row[0]) if row and row[0] else ccn


def select_worklist_candidates(conn, ccns, worklist):
    missing = [ccn for ccn in ccns if ccn not in worklist]
    if missing:
        preview = ', '.join(missing[:10])
        suffix = '' if len(missing) <= 10 else f' (+{len(missing) - 10} more)'
        raise ValueError(f'worklist is missing {len(missing)} requested CCN(s): {preview}{suffix}')

    selected = {}
    for ccn in ccns:
        candidate = dict(worklist[ccn])
        candidate.setdefault('name', hospital_name(conn, ccn))
        candidate.setdefault('source', 'refresh-plan')
        selected[ccn] = candidate
    return selected


def write_status(path, payload):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def append_failure(path, failure):
    with open(path, 'a') as f:
        f.write(json.dumps(failure, separators=(',', ':')) + '\n')


def get_target_ccns(conn, *, state=None, limit=None, offset=0):
    q = """
    SELECT DISTINCT h.ccn FROM hospitals h
    WHERE h.hospital_type IN ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
      AND (h.ccn IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
        OR h.ccn IN (SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1))
    """
    params = []
    if state:
        q += " AND h.state = ?"
        params.append(state)
    q += " ORDER BY h.name"
    if limit is not None:
        q += " LIMIT ?"
        params.append(limit)
    if offset:
        q += " OFFSET ?"
        params.append(offset)
    return [r[0] for r in conn.execute(q, params)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ccns', nargs='+', help='Explicit CCN list')
    p.add_argument('--ccns-file', help='Path to newline-delimited CCN list')
    p.add_argument(
        '--worklist',
        help='Planner JSON mapping each requested CCN to its authoritative selected URL',
    )
    p.add_argument('--state', help='Process all live-MRF hospitals in a state')
    p.add_argument('--limit', type=int, default=10)
    p.add_argument('--all', action='store_true', help='Ignore --limit and target every live-MRF hospital')
    p.add_argument('--resume', action='store_true', help='Skip CCNs already in public/data/prices/index.json or data/coverage_terminal_exceptions.json')
    p.add_argument('--offset', type=int, default=0, help='Skip the first N eligible CCNs after ordering')
    p.add_argument('--workers', type=int, default=4, help='Parallel ingestion workers (mind RAM — each parser holds the whole MRF in memory)')
    p.add_argument('--progress-every', type=int, default=25)
    p.add_argument(
        '--status-every-seconds',
        type=float,
        default=15.0,
        help='Refresh the status JSON at least this often even between progress checkpoints',
    )
    p.add_argument(
        '--item-timeout-seconds',
        type=int,
        default=0,
        help='When >0, run each CCN parse in a subprocess capped at this timeout',
    )
    p.add_argument('--status-file', default=STATUS_FILE)
    p.add_argument('--failures-file', default=FAILURES_FILE)
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    if args.ccns and args.ccns_file:
        p.error('--ccns and --ccns-file are mutually exclusive')

    if args.ccns:
        ccns = args.ccns
    elif args.ccns_file:
        ccns = load_ccns_file(args.ccns_file)
    else:
        limit = None if args.all else args.limit
        ccns = get_target_ccns(conn, state=args.state, limit=limit, offset=args.offset)
    selected_candidates = None
    if args.worklist:
        try:
            selected_candidates = select_worklist_candidates(
                conn,
                ccns,
                load_worklist(args.worklist),
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            conn.close()
            p.error(f'invalid --worklist: {exc}')
    conn.close()

    eligible = len(ccns)
    if args.resume:
        done = load_done_set()
        before = len(ccns)
        ccns = [c for c in ccns if c not in done]
        skipped = before - len(ccns)
        print(f"resume skip: priced+exceptions={len(done)} → skipped {skipped} of {before} eligible", flush=True)
    else:
        skipped = 0

    print(f"ingesting {len(ccns)} hospitals (workers={args.workers}, eligible={eligible}, skipped_existing={skipped})...", flush=True)
    os.makedirs(os.path.dirname(args.status_file), exist_ok=True)
    os.makedirs(os.path.dirname(args.failures_file), exist_ok=True)
    start_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    started = time.time()
    last_status_write = started
    done = succ = fail = total_items = 0
    recent_failures = []

    write_status(args.status_file, {
        'started_at': start_iso,
        'state': args.state or '',
        'all': args.all,
        'resume': args.resume,
        'workers': args.workers,
        'eligible': eligible,
        'skipped_existing': skipped,
        'pending': len(ccns),
        'done': 0,
        'success': 0,
        'failed': 0,
        'total_items': 0,
        'eta_seconds': None,
    })

    if not ccns:
        print("nothing to do", flush=True)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(
                ingest_ccn,
                c,
                args.item_timeout_seconds,
                selected_candidates[c] if selected_candidates is not None else None,
            ): c
            for c in ccns
        }
        for fut in concurrent.futures.as_completed(futs):
            ccn, ok, n, fmt, msg = fut.result()
            done += 1
            total_items += n
            if ok and n > 0:
                succ += 1
            else:
                fail += 1
                failure = {'ccn': ccn, 'fmt': fmt, 'items': n, 'error': msg}
                recent_failures.append(failure)
                append_failure(args.failures_file, failure)
            status = '✓' if ok and n > 0 else '✗'
            print(f"  {status} {ccn} fmt={fmt:10} items={n:>7} {msg[:50]}", flush=True)
            elapsed = max(time.time() - started, 0.001)
            rate = done / elapsed
            remaining = len(ccns) - done
            eta = int(remaining / rate) if rate > 0 else None
            now = time.time()
            should_print_progress = done % args.progress_every == 0 or done == len(ccns)
            should_write_status = (
                should_print_progress
                or done == len(ccns)
                or (now - last_status_write) >= max(args.status_every_seconds, 1.0)
            )
            if should_print_progress:
                print(f"    progress {done}/{len(ccns)} | ok={succ} fail={fail} | {rate:.2f}/s | eta={eta}s", flush=True)
            if should_write_status:
                write_status(args.status_file, {
                    'started_at': start_iso,
                    'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                    'state': args.state or '',
                    'all': args.all,
                    'resume': args.resume,
                    'workers': args.workers,
                    'eligible': eligible,
                    'skipped_existing': skipped,
                    'pending': len(ccns) - done,
                    'done': done,
                    'success': succ,
                    'failed': fail,
                    'total_items': total_items,
                    'rate_per_second': round(rate, 3),
                    'eta_seconds': eta,
                    'recent_failures': recent_failures[-10:],
                })
                last_status_write = now

    elapsed = time.time() - started
    print(f"\n{succ}/{len(ccns)} ingested, {total_items:,} total items in {elapsed:.1f}s", flush=True)


if __name__ == '__main__':
    main()
