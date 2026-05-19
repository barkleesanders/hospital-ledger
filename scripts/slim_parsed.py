#!/usr/bin/env python3
"""Slim parsed MRF JSONs for site bundling.

For each parsed/<ccn>.json:
 - Keep only displayable items with a code (CPT/HCPCS/DRG/MS-DRG/REV/CDM)
 - Dedupe to (code, billing_class/setting) keys
 - Truncate descriptions to 100 chars
 - Output to public/data/prices/<ccn>.json (compact)

Also builds:
 - public/data/prices/index.json — CCN -> {n_items, top_codes}
 - public/data/cpt-index.json    — CPT code -> [{ccn, gross, cash, payers_count}]
"""
import json, os, sys, glob, re, gzip, subprocess
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'data', 'parsed')
OUT_DIR = os.path.join(ROOT, 'public', 'data', 'prices')
INDEX = os.path.join(OUT_DIR, 'index.json')
CPT_INDEX = os.path.join(ROOT, 'public', 'data', 'cpt-index.json')
# Side-car accumulators consumed by scripts/build_aggregates.py.
# JSONL = one record per (ccn, raw_payer) tuple; rebuilt every full slim run.
PAYER_RAW_JSONL = os.path.join(ROOT, 'data', '_payer_raw.jsonl')
COMPLIANCE_JSONL = os.path.join(ROOT, 'data', '_compliance_per_hospital.jsonl')

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(PAYER_RAW_JSONL), exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 45 CFR § 180 compliance scoring
#
# Six required elements per the regulation. We weight them so the compliance
# grade reflects what's actually useful to a patient: presence of MRF, the four
# price types (gross / cash / payer-specific / min-max), and free public access.
# ─────────────────────────────────────────────────────────────────────────────
COMPLIANCE_WEIGHTS = {
    'mrf':         25,  # MRF file exists and is machine-readable
    'gross':       15,  # Standard charges (gross) present on >= 80% of items
    'cash':        15,  # Discounted cash price present on >= 50% of items
    'payer_rates': 20,  # Payer-specific negotiated rates present on >= 50% of items
    'min_max':     15,  # De-identified min AND max negotiated charges on >= 50% of items
    'free_access': 10,  # Source URL responded 200 without auth/PII
}


def grade_for(score):
    if score >= 90: return 'A'
    if score >= 80: return 'B'
    if score >= 70: return 'C'
    if score >= 60: return 'D'
    return 'F'


def compute_compliance(slim_items, mrf_alive=True, free_access=True):
    """Return a compliance dict per 45 CFR § 180."""
    n = len(slim_items)
    if n == 0:
        return {
            'score': 0,
            'grade': 'F',
            'elements': {k: False for k in COMPLIANCE_WEIGHTS},
            'item_count': 0,
        }
    gross_n = sum(1 for it in slim_items if it.get('gross') is not None)
    cash_n = sum(1 for it in slim_items if it.get('cash') is not None)
    payers_n = sum(1 for it in slim_items if (it.get('pc') or 0) > 0)
    minmax_n = sum(1 for it in slim_items if it.get('min') is not None and it.get('max') is not None)

    elements = {
        'mrf': bool(mrf_alive) and n > 0,
        'gross': gross_n / n >= 0.80,
        'cash': cash_n / n >= 0.50,
        'payer_rates': payers_n / n >= 0.50,
        'min_max': minmax_n / n >= 0.50,
        'free_access': bool(free_access),
    }
    score = sum(w for k, w in COMPLIANCE_WEIGHTS.items() if elements.get(k))
    return {
        'score': score,
        'grade': grade_for(score),
        'elements': elements,
        'coverage': {
            'gross_pct':  round(100 * gross_n / n, 1),
            'cash_pct':   round(100 * cash_n / n, 1),
            'payer_pct':  round(100 * payers_n / n, 1),
            'minmax_pct': round(100 * minmax_n / n, 1),
        },
        'item_count': n,
    }

# Codes worth surfacing in per-hospital display files. Blank code_type rows
# from current parsers are hospital charge-master lines, so expose them as CDM.
DISPLAY_TYPES = {'CPT', 'HCPCS', 'DRG', 'MS-DRG', 'REV', 'CDM'}
CPT_INDEX_TYPES = {'CPT', 'HCPCS'}


def detect_code_type(code):
    value = str(code or '').strip().upper()
    if re.fullmatch(r'\d{5}', value):
        return 'CPT'
    if re.fullmatch(r'[A-Z]\d{4}', value):
        return 'HCPCS'
    if re.fullmatch(r'\d{3}', value):
        return 'DRG'
    if re.fullmatch(r'\d{1,4}-\d{1,4}', value):
        return 'MS-DRG'
    return 'CDM'


def normalize_display_type(code, code_type):
    normalized = str(code_type or '').strip().upper().replace(' ', '').replace('_', '-')
    aliases = {
        'CPT': 'CPT',
        'HCPCS': 'HCPCS',
        'DRG': 'DRG',
        'MS-DRG': 'MS-DRG',
        'MSDRG': 'MS-DRG',
        'REV': 'REV',
        'REVCODE': 'REV',
        'RC': 'REV',
        'CDM': 'CDM',
        'CHARGECODE': 'CDM',
    }
    if normalized in aliases:
        return aliases[normalized]
    if normalized in {'', 'CODE', 'BILLINGCODE', 'PROCEDURECODE', 'CPT-HCPCS', 'CPTHCPCS', 'CPTHCPCSCODE'}:
        return detect_code_type(code)
    return code_type


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def selected_ccns():
    ccns = {
        ccn.strip()
        for ccn in os.environ.get('CCNS', '').replace(',', ' ').split()
        if ccn.strip()
    }
    ccns_file = os.environ.get('CCNS_FILE', '').strip()
    if ccns_file and os.path.exists(ccns_file):
        with open(ccns_file) as handle:
            for line in handle:
                ccn = line.strip()
                if ccn:
                    ccns.add(ccn)
    return ccns


def display_code_and_type(item):
    code = (item.get('code') or '').strip()
    code_type = (item.get('code_type') or '').strip().upper() or 'CDM'
    desc = (item.get('description') or '').strip()
    payer_rates = item.get('payer_rates') or []
    gross = item.get('gross_charge')
    cash = item.get('cash_discount')
    code_type = normalize_display_type(code, code_type)
    if not code and desc and (gross is not None or cash is not None or payer_rates):
        code = desc[:48]
        code_type = 'CDM'
    # Vendor-specific code_types (LOCAL, CHRGCD, PERDIEM, PERCASE, STANDARD,
    # PHARMACY, single-letter codes like 'C'/'H', etc.) carry real CDM data
    # with valid gross_charge / cash_discount / payer_rates. Normalize them to
    # 'CDM' so they survive the DISPLAY_TYPES filter at the slim step. Without
    # this, ~10 hospitals (200K+ items each) get dropped at the filter despite
    # having complete price data. See Tier 1 of the 2026-05-19 coverage-gap fix.
    if (
        code
        and code_type not in DISPLAY_TYPES
        and (gross is not None or cash is not None or payer_rates)
    ):
        code_type = 'CDM'
    return code, code_type

requested_ccns = selected_ccns()
keep_stale = env_bool('SLIM_KEEP_STALE', bool(requested_ccns))
merge_index = env_bool('SLIM_MERGE_INDEX', bool(requested_ccns))
index_from_prices = env_bool('SLIM_INDEX_FROM_PRICES', False)

summary_by_ccn = {}
if merge_index and os.path.exists(INDEX):
    try:
        with open(INDEX) as handle:
            existing_index = json.load(handle)
        for summary in existing_index.get('hospitals', []):
            ccn = summary.get('ccn')
            if ccn:
                summary_by_ccn[ccn] = summary
    except (OSError, json.JSONDecodeError):
        summary_by_ccn = {}

cpt_index = defaultdict(list)
generated_ccns = set()

# Side-car accumulators. Truncate on full runs (no CCN filter) so we don't
# carry stale records across rebuilds. On targeted runs (CCNS=...), append.
_full_run = not requested_ccns
if _full_run:
    open(PAYER_RAW_JSONL, 'w').close()
    open(COMPLIANCE_JSONL, 'w').close()
payer_raw_handle = open(PAYER_RAW_JSONL, 'a')
compliance_handle = open(COMPLIANCE_JSONL, 'a')

# Read both uncompressed and gzipped parsed files. After ingest, files may be
# transparently gzipped to .json.gz to keep data/parsed/ from accumulating.
# When both .json and .json.gz exist for the same CCN, the .json is the fresh
# parse (mrf_parse writes uncompressed); prefer it over the stale .gz so a
# re-ingest doesn't get shadowed.


def _ccn_from_path(p):
    base = os.path.basename(p)
    if base.endswith('.json.gz'):
        return base[:-len('.json.gz')]
    if base.endswith('.json'):
        return base[:-len('.json')]
    return base


_by_ccn = {}
for _p in glob.glob(os.path.join(SRC, '*.json')) + glob.glob(os.path.join(SRC, '*.json.gz')):
    _ccn = _ccn_from_path(_p)
    # Prefer .json (fresh) over .json.gz (stale) when both exist.
    if _ccn not in _by_ccn or not _p.endswith('.gz'):
        _by_ccn[_ccn] = _p
parsed_paths = sorted(_by_ccn.values())

if requested_ccns:
    parsed_paths = [p for p in parsed_paths if _ccn_from_path(p) in requested_ccns]

# Auto-gzip raw parsed files after a successful slim, unless disabled. This
# turns data/parsed/ into a transient scratch dir instead of a 100+ GB
# accumulator. Disable with SLIM_NO_GZIP=1 for debug runs.
_auto_gzip = not env_bool('SLIM_NO_GZIP', False)

for path in parsed_paths:
    ccn = _ccn_from_path(path)
    try:
        opener = gzip.open if path.endswith('.gz') else open
        with opener(path, 'rt') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, gzip.BadGzipFile):
        continue
    items = data.get('items', [])
    # Filter + dedupe
    seen = {}
    skipped_missing_code = 0
    skipped_unpriced = 0
    skipped_type = 0
    for it in items:
        code, code_type = display_code_and_type(it)
        if not code:
            skipped_missing_code += 1
            continue
        # Need at least gross OR cash to be useful
        gross = it.get('gross_charge')
        cash = it.get('cash_discount')
        payer_rates = it.get('payer_rates') or []
        if gross is None and cash is None and not payer_rates:
            skipped_unpriced += 1
            continue
        if code_type not in DISPLAY_TYPES:
            skipped_type += 1
            continue
        key = (code, it.get('billing_class', ''), it.get('setting', ''))
        if key in seen:
            # Keep the one with more payer info
            if len(payer_rates) > len(seen[key].get('payer_rates') or []):
                seen[key] = it
            continue
        seen[key] = it

    slim = []
    counts_by_type = defaultdict(int)
    for it in seen.values():
        code, code_type = display_code_and_type(it)
        payer_rates = it.get('payer_rates') or []
        # Compact payer rates: keep top 5 by rate_dollar
        payers = sorted(
            [p for p in payer_rates if p.get('rate_dollar')],
            key=lambda p: p.get('rate_dollar', 0),
            reverse=True
        )[:5]
        rec = {
            'code': code,
            'type': code_type,
            'desc': (it.get('description') or '')[:100],
            'gross': it.get('gross_charge'),
            'cash': it.get('cash_discount'),
            'min': it.get('min_negotiated'),
            'max': it.get('max_negotiated'),
            'pc': len(payer_rates),
        }
        if payers:
            rec['payers'] = [{'p': p['payer'][:40], 'r': p['rate_dollar']} for p in payers]
        if it.get('billing_class'):
            rec['bc'] = it['billing_class'][:16]
        if it.get('setting'):
            rec['s'] = it['setting'][:3].lower()  # i/o
        slim.append(rec)
        counts_by_type[code_type] += 1

    # Sort by gross desc for "most expensive" view
    slim.sort(key=lambda x: -(x['gross'] or 0))
    cpt_indexed = sum(1 for it in slim if it['type'] in CPT_INDEX_TYPES)

    # 45 CFR § 180 compliance score for this hospital.
    # mrf_alive: we got here via parsed/<ccn>.json so the MRF was live at parse time.
    # free_access: assume true unless source HTTP recorded a challenge; refined later
    # by build_aggregates.py joining mrf_probe.alive flags.
    compliance = compute_compliance(slim, mrf_alive=True, free_access=True)

    out = {
        'ccn': ccn,
        'hospital_name': data.get('hospital_name', ''),
        'source_url': data.get('source_url', ''),
        'fetched_at': data.get('fetched_at', ''),
        'format': data.get('format_detected', ''),
        'n_total_raw': data.get('row_count', 0),
        'n_slim': len(slim),
        'compliance': compliance,
        'counts': {
            'raw': len(items),
            'source_rows': data.get('row_count', 0),
            'display': len(slim),
            'cpt_indexed': cpt_indexed,
            'by_type': dict(sorted(counts_by_type.items())),
            'skipped': {
                'missing_code': skipped_missing_code,
                'unpriced': skipped_unpriced,
                'unsupported_type': skipped_type,
            },
        },
        'items': slim,
    }
    out_path = os.path.join(OUT_DIR, f"{ccn}.json")
    with open(out_path, 'w') as f:
        json.dump(out, f, separators=(',', ':'))
    generated_ccns.add(ccn)
    sz = os.path.getsize(out_path)
    print(f"  {ccn}: {len(slim):>6} items {compliance['grade']}/{compliance['score']:>3}, {sz/1024:.1f} KB")

    # Raw parsed file has been consumed — gzip it so data/parsed/ stays bounded.
    # No-op if already .gz. Failure is non-fatal; the priced file is the artifact.
    if _auto_gzip and not path.endswith('.gz'):
        subprocess.run(['gzip', '-9', '-f', path], check=False)

    summary_by_ccn[ccn] = {
        'ccn': ccn,
        'n': len(slim),
        'name': out['hospital_name'],
        'counts': out['counts']['by_type'],
        'cpt_indexed': cpt_indexed,
        'compliance': compliance,
    }

    # Compliance side-car (one line per hospital), consumed by build_aggregates.py
    compliance_handle.write(json.dumps({
        'ccn': ccn,
        'name': out['hospital_name'],
        'compliance': compliance,
    }) + '\n')

    # Per-payer raw aggregate: emit one line per (ccn, raw_payer) pair with
    # rate stats. build_aggregates.py canonicalizes the raw_payer string.
    payer_acc = defaultdict(lambda: {'n_items': 0, 'rates': []})
    for it in slim:
        for p in (it.get('payers') or []):
            raw_name = p.get('p') or ''
            rate = p.get('r')
            agg = payer_acc[raw_name]
            agg['n_items'] += 1
            if isinstance(rate, (int, float)) and rate > 0:
                agg['rates'].append(rate)
    for raw_name, agg in payer_acc.items():
        rates = sorted(agg['rates'])
        rec = {
            'ccn': ccn,
            'raw_payer': raw_name,
            'n_items': agg['n_items'],
            'n_rates': len(rates),
        }
        if rates:
            rec['min'] = rates[0]
            rec['max'] = rates[-1]
            rec['median'] = rates[len(rates) // 2]
        payer_raw_handle.write(json.dumps(rec) + '\n')

    # Add to CPT index. Keep payer_max scalar for backwards compat with the
    # current cpt-index.json shape, AND attach top-5 raw payer rates so
    # build_aggregates.py can emit cpt-detail/{code}.json with payer breakdowns.
    for it in slim:
        if it['type'] not in CPT_INDEX_TYPES:
            continue
        payers_full = it.get('payers') or []
        payer_max = max((p.get('r') for p in payers_full if p.get('r') is not None), default=None)
        cpt_index[it['code']].append({
            'ccn': ccn,
            'gross': it.get('gross'),
            'cash': it.get('cash'),
            'min': it.get('min'),
            'max': it.get('max'),
            'type': it.get('type'),
            'pc': it.get('pc', 0),
            'payer_max': payer_max,
            'desc': it.get('desc', ''),
            'payers_top5': payers_full[:5],  # consumed by build_aggregates.py
        })

if index_from_prices:
    summary_by_ccn = {}
    cpt_index = defaultdict(list)
    for price_path in sorted(glob.glob(os.path.join(OUT_DIR, '*.json'))):
        ccn = os.path.basename(price_path).replace('.json', '')
        if ccn == 'index':
            continue
        try:
            with open(price_path) as handle:
                price_data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        items = price_data.get('items') or []
        counts = price_data.get('counts') or {}
        by_type = counts.get('by_type') or {}
        cpt_indexed = sum(1 for item in items if item.get('type') in CPT_INDEX_TYPES)
        summary = {
            'ccn': ccn,
            'n': len(items),
            'name': price_data.get('hospital_name', ''),
            'counts': by_type,
            'cpt_indexed': cpt_indexed,
        }
        # Surface compliance (added 2026-05-13). Authoritative source: per-hospital
        # slim file. Falls back to recomputing from items if the file was produced
        # by an older slim_parsed run that didn't emit it.
        compliance = price_data.get('compliance')
        if not compliance:
            compliance = compute_compliance(items, mrf_alive=True, free_access=True)
            # Backfill compliance into the slim file in place so future reads are cheap.
            try:
                price_data['compliance'] = compliance
                with open(price_path, 'w') as wh:
                    json.dump(price_data, wh, separators=(',', ':'))
            except OSError:
                pass
        summary['compliance'] = compliance
        # Also write to compliance side-car so build_aggregates.py sees it.
        compliance_handle.write(json.dumps({
            'ccn': ccn,
            'name': price_data.get('hospital_name', ''),
            'compliance': compliance,
        }) + '\n')
        summary_by_ccn[ccn] = summary
        for item in items:
            if item.get('type') not in CPT_INDEX_TYPES:
                continue
            payer_max = None
            if item.get('payers'):
                payer_max = max((payer.get('r') for payer in item['payers'] if payer.get('r') is not None), default=None)
            cpt_index[item.get('code', '')].append({
                'ccn': ccn,
                'gross': item.get('gross'),
                'cash': item.get('cash'),
                'min': item.get('min'),
                'max': item.get('max'),
                'type': item.get('type'),
                'pc': item.get('pc', 0),
                'payer_max': payer_max,
            })

ccn_summaries = [summary_by_ccn[ccn] for ccn in sorted(summary_by_ccn)]
with open(INDEX, 'w') as f:
    json.dump({'hospitals': ccn_summaries}, f, separators=(',', ':'))

if not keep_stale:
    for stale_path in glob.glob(os.path.join(OUT_DIR, '*.json')):
        stale_ccn = os.path.basename(stale_path).replace('.json', '')
        if stale_ccn == 'index' or stale_ccn in generated_ccns:
            continue
        os.remove(stale_path)
        print(f"  removed stale preview {stale_ccn}.json")

# Trim CPT index to top codes by coverage. Strip the heavy payers_top5/desc
# fields here — they live in cpt-detail/{code}.json (built by build_aggregates.py).
cpt_arr = sorted(cpt_index.items(), key=lambda kv: -len(kv[1]))
trimmed = {}
for code, entries in cpt_arr[:5000]:
    trimmed[code] = [
        {k: v for k, v in e.items() if k not in ('payers_top5', 'desc')}
        for e in entries
    ]
with open(CPT_INDEX, 'w') as f:
    json.dump(trimmed, f, separators=(',', ':'))

# Dump the FULL cpt_index (with payers_top5 + desc) to a side-car JSONL the
# build_aggregates.py script consumes to produce per-code detail files. Keeps
# the top 10,000 codes by coverage so the search-by-procedure feature has more
# than just the headline 5,000.
CPT_DETAIL_RAW = os.path.join(ROOT, 'data', '_cpt_detail_raw.jsonl')
with open(CPT_DETAIL_RAW, 'w') as f:
    for code, entries in cpt_arr[:10000]:
        f.write(json.dumps({'code': code, 'entries': entries}) + '\n')

# Close side-cars
payer_raw_handle.close()
compliance_handle.close()

print(f"\nindex: {INDEX} ({os.path.getsize(INDEX)/1024:.1f} KB)")
print(f"cpt-index: {CPT_INDEX} ({os.path.getsize(CPT_INDEX)/1024:.1f} KB, {len(trimmed)} CPTs)")
print(f"cpt-detail-raw: {CPT_DETAIL_RAW} ({os.path.getsize(CPT_DETAIL_RAW)/1024:.1f} KB)")
print(f"payer-raw:     {PAYER_RAW_JSONL} ({os.path.getsize(PAYER_RAW_JSONL)/1024:.1f} KB)")
print(f"compliance:    {COMPLIANCE_JSONL} ({os.path.getsize(COMPLIANCE_JSONL)/1024:.1f} KB)")
