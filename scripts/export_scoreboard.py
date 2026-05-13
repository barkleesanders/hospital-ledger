#!/usr/bin/env python3
"""Export Hospital Ledger scoreboard as CC0 CSV.

Joins hospitals + mrf_seed + latest mrf_probe per (ccn, mrf_url). Each row =
one hospital × one MRF URL. The fields are kept minimal and public-domain-safe.

Output schema:
  ccn, hospital_name, city, state, hospital_type, ownership,
  entity_name, mrf_url, mrf_format, alive, http_status, content_type, bytes,
  last_probed_utc

License: CC0 1.0 Universal. Compiled from CMS public data (public domain) and
TPAFS transparency-data (CC BY-SA 4.0 — see attribution in README).
"""
import sqlite3, csv, os, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
OUT_CSV = os.path.join(ROOT, 'data', 'hospital_ledger_scoreboard.csv')

SQL = """
WITH latest_probe AS (
  SELECT ccn, mrf_url, MAX(probed_at) AS probed_at
  FROM mrf_probe GROUP BY ccn, mrf_url
),
seed_rows AS (
  SELECT 'seed' AS source, s.ccn, s.mrf_url, s.entity_name_common, s.entity_name_legal,
         s.file_format, s.mrf_page, COALESCE(p.alive, -1) AS alive,
         COALESCE(p.http_status, 0) AS http_status,
         COALESCE(p.content_type, '') AS content_type,
         COALESCE(p.content_length, 0) AS bytes,
         COALESCE(p.probed_at, '') AS verified_utc,
         '' AS source_page, 0 AS score, 0 AS rank
  FROM mrf_seed s
  LEFT JOIN latest_probe lp ON lp.ccn=s.ccn AND lp.mrf_url=s.mrf_url
  LEFT JOIN mrf_probe p ON p.ccn=lp.ccn AND p.mrf_url=lp.mrf_url AND p.probed_at=lp.probed_at
),
redisc_rows AS (
  SELECT 'rediscovered' AS source, r.ccn, r.candidate_url AS mrf_url,
         '' AS entity_name_common, '' AS entity_name_legal,
         '' AS file_format, '' AS mrf_page, r.alive,
         r.head_status AS http_status, r.head_content_type AS content_type,
         COALESCE(r.head_content_length, 0) AS bytes,
         r.discovered_at AS verified_utc,
         r.source_page, r.score, r.rank
  FROM mrf_rediscovered r
)
SELECT
  u.source,
  u.ccn,
  COALESCE(h.name, '') AS hospital_name,
  COALESCE(h.city, '') AS city,
  COALESCE(h.state, '') AS state,
  COALESCE(h.hospital_type, '') AS hospital_type,
  COALESCE(h.ownership, '') AS ownership,
  COALESCE(NULLIF(u.entity_name_common, ''), u.entity_name_legal, '') AS entity_name,
  u.mrf_url,
  u.file_format,
  u.alive, u.http_status, u.content_type, u.bytes,
  u.source_page,
  u.score AS rediscovery_score,
  u.verified_utc,
  COALESCE(enf.has_enf, 0) AS cms_enforcement_actions
FROM (SELECT * FROM seed_rows UNION ALL SELECT * FROM redisc_rows) u
LEFT JOIN hospitals h ON h.ccn = u.ccn
LEFT JOIN (
  SELECT ccn, COUNT(DISTINCT case_id) AS has_enf
  FROM cms_enforcement_match GROUP BY ccn
) enf ON enf.ccn = u.ccn
ORDER BY state, hospital_name, source, mrf_url;
"""

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = list(conn.execute(SQL))
fields = rows[0].keys() if rows else []

os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
with open(OUT_CSV, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(fields)
    for r in rows:
        w.writerow([r[k] for k in fields])

# stats
probed = sum(1 for r in rows if r['alive'] in (0, 1))
alive  = sum(1 for r in rows if r['alive'] == 1)
dead   = sum(1 for r in rows if r['alive'] == 0)
unkn   = sum(1 for r in rows if r['alive'] == -1)
distinct_ccns = len({r['ccn'] for r in rows if r['ccn']})
print(f"wrote {OUT_CSV}")
print(f"  total rows: {len(rows)}")
print(f"  distinct CCNs: {distinct_ccns}")
print(f"  probed: {probed}  alive: {alive}  dead: {dead}  unprobed: {unkn}")
if probed:
    print(f"  alive rate: {100*alive/probed:.1f}%")
