#!/usr/bin/env python3
"""Build public/data/hospitals.json from the ledger DB.

Schema per hospital:
  ccn, name, city, state, type, ownership, rating,
  has_live_mrf, mrf_url, mrf_format, mrf_bytes, mrf_verified, mrf_source,
  enforcement_count, enforcement_actions[]
"""
import json, sqlite3, os, datetime
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
PUBLIC_DATA = os.path.join(ROOT, 'public', 'data')
OUT = os.path.join(PUBLIC_DATA, 'hospitals.json')
SUMMARY_OUT = os.path.join(PUBLIC_DATA, 'summary.json')
PRICE_INDEX = os.path.join(PUBLIC_DATA, 'prices', 'index.json')

REQUIRED_TYPES = (
    'Acute Care Hospitals', 'Critical Access Hospitals',
    'Childrens', 'Rural Emergency Hospital',
)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Get best (alive, highest-score, most-recent) MRF per CCN
best_mrf = {}
# Seed-side
for r in conn.execute("""
    SELECT s.ccn, s.mrf_url, s.file_format, p.alive, p.http_status, p.content_length, p.probed_at
    FROM mrf_seed s JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url
    WHERE p.alive = 1
"""):
    ccn = r['ccn']
    score = (1, r['probed_at'] or '')
    if ccn not in best_mrf or best_mrf[ccn]['_sort'] < score:
        best_mrf[ccn] = {
            'mrf_url': r['mrf_url'],
            'mrf_format': r['file_format'] or '',
            'mrf_bytes': r['content_length'] or 0,
            'mrf_status': r['http_status'],
            'mrf_verified': r['probed_at'],
            'mrf_source': 'tpafs-seed',
            '_sort': score,
        }
# Rediscovered (higher score wins)
for r in conn.execute("""
    SELECT ccn, candidate_url, score, alive, head_status, head_content_length,
           discovered_at, source_page
    FROM mrf_rediscovered WHERE alive = 1
"""):
    ccn = r['ccn']
    score = (r['score'] or 0, r['discovered_at'] or '')
    if ccn not in best_mrf or best_mrf[ccn]['_sort'] < score:
        src = (r['source_page'] or '').split(':', 1)[0] or 'rediscovery'
        best_mrf[ccn] = {
            'mrf_url': r['candidate_url'],
            'mrf_format': '',
            'mrf_bytes': r['head_content_length'] or 0,
            'mrf_status': r['head_status'],
            'mrf_verified': r['discovered_at'],
            'mrf_source': src,
            '_sort': score,
        }

# Enforcement actions per CCN
enf = defaultdict(list)
for r in conn.execute("""
    SELECT m.ccn, e.action, e.date_of_action
    FROM cms_enforcement_match m
    JOIN cms_enforcement e ON e.case_id = m.case_id
    ORDER BY e.date_of_action
"""):
    enf[r['ccn']].append({'action': r['action'], 'date': r['date_of_action']})

# Build hospital records
hospitals = []
for r in conn.execute("""
    SELECT ccn, name, address, city, state, zip, county, phone,
           hospital_type, ownership, emergency, cms_rating
    FROM hospitals
    ORDER BY state, name
"""):
    ccn = r['ccn']
    mrf = best_mrf.get(ccn)
    is_required = r['hospital_type'] in REQUIRED_TYPES
    rec = {
        'ccn': ccn,
        'name': r['name'],
        'city': r['city'],
        'state': r['state'],
        'address': r['address'],
        'zip': r['zip'],
        'type': r['hospital_type'],
        'ownership': r['ownership'],
        'emergency': r['emergency'] == 'Yes',
        'rating': r['cms_rating'] or '',
        'required': is_required,
        'has_live_mrf': mrf is not None,
    }
    if mrf:
        mrf_clean = {k: v for k, v in mrf.items() if not k.startswith('_')}
        rec.update(mrf_clean)
    if ccn in enf:
        rec['enforcement_count'] = len(enf[ccn])
        rec['enforcement_actions'] = enf[ccn]
    hospitals.append(rec)

# Compact JSON
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w') as f:
    json.dump(hospitals, f, separators=(',', ':'))
print(f"wrote {OUT}: {len(hospitals)} hospitals, {os.path.getsize(OUT)/1024:.1f} KB")

# Summary metrics
required = [h for h in hospitals if h['required']]
compliant = [h for h in required if h['has_live_mrf']]
under_enf = [h for h in required if h.get('enforcement_count', 0) > 0]
missing = [h for h in required if not h['has_live_mrf']]
missing_with_enf = [h for h in missing if h.get('enforcement_count', 0) > 0]

# Worst-offender hospitals: missing MRF + multiple CMS enforcement actions
worst = sorted(missing_with_enf, key=lambda h: -h.get('enforcement_count', 0))[:25]

# State stats
state_stats = defaultdict(lambda: {'total': 0, 'live': 0})
for h in required:
    s = h['state']
    state_stats[s]['total'] += 1
    if h['has_live_mrf']:
        state_stats[s]['live'] += 1
state_arr = []
for s, st in state_stats.items():
    if st['total'] >= 5:
        st['pct'] = round(100 * st['live'] / st['total'], 1)
        state_arr.append({'state': s, **st})
state_arr.sort(key=lambda x: x['pct'])

# By-type stats
type_stats = defaultdict(lambda: {'total': 0, 'live': 0})
for h in required:
    t = h['type']
    type_stats[t]['total'] += 1
    if h['has_live_mrf']:
        type_stats[t]['live'] += 1
type_arr = [{'type': t, **st, 'pct': round(100 * st['live'] / st['total'], 1)}
            for t, st in type_stats.items()]
type_arr.sort(key=lambda x: -x['total'])

def price_index_metrics():
    metrics = {
        'standardized_price_index_hospitals': 0,
        'standardized_price_hospitals': 0,
        'standardized_price_rows': 0,
        'cpt_indexed_hospitals': 0,
        'cpt_indexed_rows': 0,
        'zero_price_index_entries': 0,
    }
    try:
        with open(PRICE_INDEX) as f:
            entries = json.load(f).get('hospitals', [])
    except (OSError, json.JSONDecodeError):
        return metrics
    metrics['standardized_price_index_hospitals'] = len(entries)
    for h in entries:
        n = int(h.get('n') or h.get('count') or h.get('items') or 0)
        cpt = int(h.get('cpt_indexed') or 0)
        metrics['standardized_price_rows'] += n
        metrics['cpt_indexed_rows'] += cpt
        if n > 0:
            metrics['standardized_price_hospitals'] += 1
        else:
            metrics['zero_price_index_entries'] += 1
        if cpt > 0:
            metrics['cpt_indexed_hospitals'] += 1
    return metrics

summary = {
    'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
    'total_facilities': len(hospitals),
    'cms_required_total': len(required),
    'live_mrf_total': sum(1 for h in hospitals if h['has_live_mrf']),
    'compliant': len(compliant),
    'compliance_pct': round(100 * len(compliant) / len(required), 1),
    'missing': len(missing),
    'under_enforcement': len(under_enf),
    'missing_with_enforcement': len(missing_with_enf),
    'enforcement_actions_total': sum(h.get('enforcement_count', 0) for h in required),
    **price_index_metrics(),
    'count_definitions': {
        'total_facilities': 'Rows in public/data/hospitals.json from CMS Hospital General Information.',
        'cms_required_total': 'Hospitals whose type is in the CMS price-transparency-required set.',
        'compliant': 'CMS-required hospitals with a verified live machine-readable file URL.',
        'standardized_price_index_hospitals': 'Entries in public/data/prices/index.json, including zero-row outputs.',
        'standardized_price_hospitals': 'Entries in public/data/prices/index.json with n > 0 standardized rows.',
        'cpt_indexed_hospitals': 'Entries in public/data/prices/index.json with cpt_indexed > 0.',
    },
    'states': state_arr,
    'types': type_arr,
    'worst_offenders': [{
        'ccn': h['ccn'], 'name': h['name'], 'city': h['city'], 'state': h['state'],
        'enforcement_count': h.get('enforcement_count', 0),
    } for h in worst],
}
with open(SUMMARY_OUT, 'w') as f:
    json.dump(summary, f, separators=(',', ':'))
print(f"wrote {SUMMARY_OUT}: {os.path.getsize(SUMMARY_OUT)/1024:.1f} KB")
print(f"compliance: {summary['compliance_pct']}%  missing: {summary['missing']}  "
      f"under-enforcement: {summary['under_enforcement']}")
