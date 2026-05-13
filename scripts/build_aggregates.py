#!/usr/bin/env python3
"""Build the patient-facing aggregate JSON files served by the new API endpoints.

Inputs (produced by scripts/slim_parsed.py + scripts/build_site_data.py):
  data/_payer_raw.jsonl          per (ccn, raw_payer) raw stats
  data/_compliance_per_hospital.jsonl   per-hospital § 180 score
  data/_cpt_detail_raw.jsonl     per (CPT code) hospital + top-5 payers
  site/data/prices/index.json    per-hospital slim summary (with compliance)
  site/data/hospitals.json       per-hospital metadata (state, name, etc.)

Outputs (uploaded to R2 by stage4_refresh.py):
  site/data/compliance-ranking.json     — sortable hospital ranking
  site/data/payers-index.json           — top ~100 payers patient-facing
  site/data/payer/{slug}.json           — per-payer page data
  site/data/cpt-detail/{code}.json      — per-procedure cross-hospital comparison

Each output file is bounded in size (≤500 KB for index files, ≤300 KB per
payer/code) so the API endpoints can serve them with a single R2 GET.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

# Make payer_canonical importable when run from repo root or scripts/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from payer_canonical import canonicalize, category_for, PATIENT_FACING_PAYERS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data'
SITE_DATA_DIR = ROOT / 'site' / 'data'

PAYER_RAW = DATA_DIR / '_payer_raw.jsonl'
COMPLIANCE_PER_HOSP = DATA_DIR / '_compliance_per_hospital.jsonl'
CPT_DETAIL_RAW = DATA_DIR / '_cpt_detail_raw.jsonl'
PRICES_INDEX = SITE_DATA_DIR / 'prices' / 'index.json'
HOSPITALS_JSON = SITE_DATA_DIR / 'hospitals.json'

OUT_COMPLIANCE = SITE_DATA_DIR / 'compliance-ranking.json'
OUT_PAYERS_INDEX = SITE_DATA_DIR / 'payers-index.json'
OUT_PAYER_DIR = SITE_DATA_DIR / 'payer'
OUT_CPT_DETAIL_DIR = SITE_DATA_DIR / 'cpt-detail'

# Caps to keep file sizes bounded
MAX_PAYERS_IN_INDEX = 200
MAX_HOSPITALS_PER_PAYER = 500
MAX_PROCEDURES_PER_PAYER = 100
MAX_HOSPITALS_PER_PROCEDURE = 500

# A "patient-relevant" payer needs to appear at >= this many hospitals to land
# on the curated patient list (anything below is still searchable via raw slug
# but doesn't get a featured page).
MIN_HOSPITALS_FOR_FEATURED_PAYER = 5


def load_jsonl(path):
    if not path.exists():
        return
    with path.open() as h:
        for line in h:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_json(path):
    if not path.exists():
        return None
    try:
        with path.open() as h:
            return json.load(h)
    except (OSError, json.JSONDecodeError):
        return None


def main():
    OUT_PAYER_DIR.mkdir(parents=True, exist_ok=True)
    OUT_CPT_DETAIL_DIR.mkdir(parents=True, exist_ok=True)

    # ── Load hospital metadata for state/name lookup ──────────────────────
    hospitals_meta = {}
    hospitals_data = load_json(HOSPITALS_JSON) or []
    for h in hospitals_data:
        ccn = h.get('ccn')
        if ccn:
            hospitals_meta[ccn] = {
                'name': h.get('name', ''),
                'state': h.get('state', ''),
                'city': h.get('city', ''),
                'type': h.get('type', ''),
                'has_live_mrf': h.get('has_live_mrf', False),
                'mrf_url': h.get('mrf_url', ''),
            }

    # ── Load per-hospital compliance ──────────────────────────────────────
    compliance_by_ccn = {}
    for rec in load_jsonl(COMPLIANCE_PER_HOSP):
        ccn = rec.get('ccn')
        if ccn:
            compliance_by_ccn[ccn] = rec.get('compliance', {})

    # ── Load slim index for n_items + cpt_indexed counts ──────────────────
    slim_index = load_json(PRICES_INDEX) or {}
    slim_by_ccn = {h.get('ccn'): h for h in slim_index.get('hospitals', []) if h.get('ccn')}

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║ 1. compliance-ranking.json                                          ║
    # ╚══════════════════════════════════════════════════════════════════════╝
    ranking = []
    for ccn, comp in compliance_by_ccn.items():
        meta = hospitals_meta.get(ccn, {})
        slim = slim_by_ccn.get(ccn, {})
        ranking.append({
            'ccn': ccn,
            'name': meta.get('name') or slim.get('name', ''),
            'state': meta.get('state', ''),
            'city': meta.get('city', ''),
            'type': meta.get('type', ''),
            'compliance': {
                'score': comp.get('score', 0),
                'grade': comp.get('grade', 'F'),
                'elements': comp.get('elements', {}),
                'coverage': comp.get('coverage', {}),
            },
            'n_items': slim.get('n', 0),
            'cpt_indexed': slim.get('cpt_indexed', 0),
        })
    # Sort by score desc, then by item count desc
    ranking.sort(key=lambda r: (-r['compliance']['score'], -r['n_items']))
    with OUT_COMPLIANCE.open('w') as f:
        json.dump({
            'hospitals': ranking,
            'total': len(ranking),
            'grade_distribution': _grade_distribution(ranking),
        }, f, separators=(',', ':'))
    print(f"compliance-ranking: {OUT_COMPLIANCE.name} ({OUT_COMPLIANCE.stat().st_size/1024:.1f} KB, {len(ranking)} hospitals)")

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║ 2. payers-index.json + per-payer files                              ║
    # ╚══════════════════════════════════════════════════════════════════════╝
    # Aggregate: slug -> { display, hospitals: {ccn: stats}, raw_aliases: set, all_rates: list }
    payer_acc = defaultdict(lambda: {
        'display': None,
        'category': 'other',
        'hospitals': defaultdict(lambda: {'n_items': 0, 'rates': []}),
        'raw_aliases': set(),
    })
    for rec in load_jsonl(PAYER_RAW):
        ccn = rec.get('ccn')
        raw = rec.get('raw_payer', '')
        if not ccn:
            continue
        slug, display = canonicalize(raw)
        bucket = payer_acc[slug]
        if bucket['display'] is None:
            bucket['display'] = display
            bucket['category'] = category_for(slug)
        bucket['raw_aliases'].add(raw[:60])  # cap raw alias length
        h = bucket['hospitals'][ccn]
        h['n_items'] += rec.get('n_items', 0)
        if rec.get('median') is not None:
            h['rates'].append(rec['median'])

    payer_summaries = []
    for slug, bucket in payer_acc.items():
        hosp_count = len(bucket['hospitals'])
        if hosp_count == 0:
            continue
        all_rates = []
        for h in bucket['hospitals'].values():
            all_rates.extend(h['rates'])
        all_rates.sort()
        summary = {
            'slug': slug,
            'display': bucket['display'] or slug,
            'category': bucket['category'],
            'hospital_count': hosp_count,
            'raw_aliases': sorted(bucket['raw_aliases'])[:10],  # cap
        }
        if all_rates:
            summary['median_rate'] = round(all_rates[len(all_rates) // 2], 2)
            summary['min_rate'] = round(all_rates[0], 2)
            summary['max_rate'] = round(all_rates[-1], 2)
        payer_summaries.append(summary)
    payer_summaries.sort(key=lambda p: -p['hospital_count'])

    # Featured = patient-curated brands (anything in PATIENT_FACING_PAYERS)
    # OR top long-tail payers above the floor.
    curated_slugs = {p['slug'] for p in PATIENT_FACING_PAYERS}
    featured = []
    other = []
    for p in payer_summaries:
        if p['slug'] in curated_slugs or p['hospital_count'] >= MIN_HOSPITALS_FOR_FEATURED_PAYER:
            featured.append(p)
        else:
            other.append(p)
    featured = featured[:MAX_PAYERS_IN_INDEX]

    with OUT_PAYERS_INDEX.open('w') as f:
        json.dump({
            'featured': featured,
            'long_tail_count': len(other),
            'total': len(payer_summaries),
        }, f, separators=(',', ':'))
    print(f"payers-index: {OUT_PAYERS_INDEX.name} ({OUT_PAYERS_INDEX.stat().st_size/1024:.1f} KB, {len(featured)} featured, {len(payer_summaries)} total)")

    # ── per-payer page files (only for featured) ──────────────────────────
    for summary in featured:
        slug = summary['slug']
        bucket = payer_acc[slug]
        hospitals_for_payer = []
        for ccn, stats in bucket['hospitals'].items():
            meta = hospitals_meta.get(ccn, {})
            hospitals_for_payer.append({
                'ccn': ccn,
                'name': meta.get('name') or slim_by_ccn.get(ccn, {}).get('name', ''),
                'state': meta.get('state', ''),
                'city': meta.get('city', ''),
                'n_items_with_payer': stats['n_items'],
                'median_rate': round(stats['rates'][len(stats['rates']) // 2], 2) if stats['rates'] else None,
                'compliance_grade': compliance_by_ccn.get(ccn, {}).get('grade', 'F'),
                'compliance_score': compliance_by_ccn.get(ccn, {}).get('score', 0),
            })
        hospitals_for_payer.sort(key=lambda h: -(h['n_items_with_payer']))
        out = {
            'payer': summary,
            'hospitals': hospitals_for_payer[:MAX_HOSPITALS_PER_PAYER],
            'hospital_count': len(hospitals_for_payer),
        }
        out_path = OUT_PAYER_DIR / f'{slug}.json'
        with out_path.open('w') as f:
            json.dump(out, f, separators=(',', ':'))
    print(f"payer-pages: wrote {len(featured)} files to {OUT_PAYER_DIR}/")

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║ 3. cpt-detail/{code}.json — per-procedure cross-hospital comparison ║
    # ╚══════════════════════════════════════════════════════════════════════╝
    detail_count = 0
    for rec in load_jsonl(CPT_DETAIL_RAW):
        code = rec.get('code', '')
        entries = rec.get('entries', [])
        if not code or not entries:
            continue
        # Enrich each entry with hospital metadata + canonical payer mapping
        enriched = []
        for e in entries:
            ccn = e.get('ccn')
            meta = hospitals_meta.get(ccn, {})
            payers_canonical = []
            for p in (e.get('payers_top5') or []):
                slug, display = canonicalize(p.get('p', ''))
                payers_canonical.append({
                    'slug': slug,
                    'display': display,
                    'rate': p.get('r'),
                })
            enriched.append({
                'ccn': ccn,
                'name': meta.get('name') or slim_by_ccn.get(ccn, {}).get('name', ''),
                'state': meta.get('state', ''),
                'city': meta.get('city', ''),
                'gross': e.get('gross'),
                'cash': e.get('cash'),
                'min': e.get('min'),
                'max': e.get('max'),
                'payer_count': e.get('pc', 0),
                'payers': payers_canonical,
            })
        # Sort by cash asc (cheapest cash first); fall back to gross
        enriched.sort(key=lambda h: (h['cash'] if h['cash'] is not None else float('inf')))
        # Compute headline stats
        cash_values = [h['cash'] for h in enriched if h['cash'] is not None]
        gross_values = [h['gross'] for h in enriched if h['gross'] is not None]
        out = {
            'code': code,
            'desc': entries[0].get('desc', '') if entries else '',
            'type': entries[0].get('type', '') if entries else '',
            'stats': {
                'hospital_count': len(enriched),
                'cash_p50': round(median(cash_values), 2) if cash_values else None,
                'cash_min': round(min(cash_values), 2) if cash_values else None,
                'cash_max': round(max(cash_values), 2) if cash_values else None,
                'gross_p50': round(median(gross_values), 2) if gross_values else None,
            },
            'hospitals': enriched[:MAX_HOSPITALS_PER_PROCEDURE],
        }
        out_path = OUT_CPT_DETAIL_DIR / f'{code}.json'
        with out_path.open('w') as f:
            json.dump(out, f, separators=(',', ':'))
        detail_count += 1
    print(f"cpt-detail: wrote {detail_count} files to {OUT_CPT_DETAIL_DIR}/")


def _grade_distribution(ranking):
    counts = defaultdict(int)
    for r in ranking:
        counts[r['compliance']['grade']] += 1
    return dict(sorted(counts.items()))


if __name__ == '__main__':
    main()
