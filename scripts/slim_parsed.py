#!/usr/bin/env python3
"""Slim parsed MRF JSONs for site bundling.

For each parsed/<ccn>.json:
 - Keep only displayable items with a code (CPT/HCPCS/DRG/MS-DRG/REV/CDM)
 - Dedupe to (code, billing_class/setting) keys
 - Truncate descriptions to 100 chars
 - Output to site/data/prices/<ccn>.json (compact)

Also builds:
 - site/data/prices/index.json   — CCN -> {n_items, top_codes}
 - site/data/cpt-index.json      — CPT code -> [{ccn, gross, cash, payers_count}]
"""
import json, os, sys, glob
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'data', 'parsed')
OUT_DIR = os.path.join(ROOT, 'site', 'data', 'prices')
INDEX = os.path.join(OUT_DIR, 'index.json')
CPT_INDEX = os.path.join(ROOT, 'site', 'data', 'cpt-index.json')

os.makedirs(OUT_DIR, exist_ok=True)

# Codes worth surfacing in per-hospital display files. Blank code_type rows
# from current parsers are hospital charge-master lines, so expose them as CDM.
DISPLAY_TYPES = {'CPT', 'HCPCS', 'DRG', 'MS-DRG', 'REV', 'CDM'}
CPT_INDEX_TYPES = {'CPT', 'HCPCS'}


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
    if not code and desc and (gross is not None or cash is not None or payer_rates):
        code = desc[:48]
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

parsed_paths = sorted(glob.glob(os.path.join(SRC, '*.json')))
if requested_ccns:
    parsed_paths = [
        path for path in parsed_paths
        if os.path.basename(path).replace('.json', '') in requested_ccns
    ]

for path in parsed_paths:
    ccn = os.path.basename(path).replace('.json', '')
    try:
        data = json.load(open(path))
    except json.JSONDecodeError:
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
    out = {
        'ccn': ccn,
        'hospital_name': data.get('hospital_name', ''),
        'source_url': data.get('source_url', ''),
        'fetched_at': data.get('fetched_at', ''),
        'format': data.get('format_detected', ''),
        'n_total_raw': data.get('row_count', 0),
        'n_slim': len(slim),
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
    print(f"  {ccn}: {len(slim):>6} items, {sz/1024:.1f} KB")

    summary_by_ccn[ccn] = {
        'ccn': ccn,
        'n': len(slim),
        'name': out['hospital_name'],
        'counts': out['counts']['by_type'],
        'cpt_indexed': cpt_indexed,
    }

    # Add to CPT index
    for it in slim:
        if it['type'] not in CPT_INDEX_TYPES:
            continue
        payer_max = None
        if it.get('payers'):
            payer_max = max((p.get('r') for p in it['payers'] if p.get('r') is not None), default=None)
        cpt_index[it['code']].append({
            'ccn': ccn,
            'gross': it.get('gross'),
            'cash': it.get('cash'),
            'min': it.get('min'),
            'max': it.get('max'),
            'type': it.get('type'),
            'pc': it.get('pc', 0),
            'payer_max': payer_max,
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
        summary_by_ccn[ccn] = {
            'ccn': ccn,
            'n': len(items),
            'name': price_data.get('hospital_name', ''),
            'counts': by_type,
            'cpt_indexed': cpt_indexed,
        }
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

# Trim CPT index to top codes by coverage
cpt_arr = sorted(cpt_index.items(), key=lambda kv: -len(kv[1]))
trimmed = {c: vs for c, vs in cpt_arr[:5000]}
with open(CPT_INDEX, 'w') as f:
    json.dump(trimmed, f, separators=(',', ':'))

print(f"\nindex: {INDEX} ({os.path.getsize(INDEX)/1024:.1f} KB)")
print(f"cpt-index: {CPT_INDEX} ({os.path.getsize(CPT_INDEX)/1024:.1f} KB, {len(trimmed)} CPTs)")
