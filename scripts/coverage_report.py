#!/usr/bin/env python3
"""Final coverage report: before/after rediscovery."""
import sqlite3, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

def q(sql): return conn.execute(sql).fetchall()
def q1(sql): return conn.execute(sql).fetchone()[0]

cms_total = q1("SELECT COUNT(*) FROM hospitals")
cms_req = q1("""SELECT COUNT(*) FROM hospitals
  WHERE hospital_type IN ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')""")
seed_ccns = q1("SELECT COUNT(DISTINCT ccn) FROM mrf_seed")

# alive from original probe (CCNs with >=1 live seed URL)
alive_seed_ccns = q1("""
  SELECT COUNT(DISTINCT s.ccn) FROM mrf_seed s
  JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url
  WHERE p.alive=1
""")
# alive from rediscovery (CCNs with >=1 live rediscovered URL)
alive_redisc_ccns = q1("SELECT COUNT(DISTINCT ccn) FROM mrf_rediscovered WHERE alive=1")
# combined
combined = q1("""
  SELECT COUNT(DISTINCT ccn) FROM (
    SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p
      ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1
    UNION
    SELECT ccn FROM mrf_rediscovered WHERE alive=1
  )
""")

# CMS-required hospitals with at least one live MRF (anywhere)
cms_req_with_live = q1("""
  SELECT COUNT(DISTINCT h.ccn) FROM hospitals h
  WHERE h.hospital_type IN ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
    AND (
      h.ccn IN (SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
      OR h.ccn IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
    )
""")

print("===== HOSPITAL LEDGER COVERAGE REPORT =====\n")
print(f"CMS hospitals (all types):                  {cms_total:>6,}")
print(f"CMS MRF-required (Acute/CAH/Child/Rural):   {cms_req:>6,}")
print(f"Hospitals with MRF seed records (TPAFS):    {seed_ccns:>6,}\n")

print("--- ALIVE COUNTS (distinct CCNs with >=1 live MRF) ---")
print(f"From original TPAFS seed only:              {alive_seed_ccns:>6,}  ({100*alive_seed_ccns/cms_req:5.1f}% of required)")
print(f"From rediscovery only:                      {alive_redisc_ccns:>6,}")
print(f"Combined (seed OR rediscovered):            {combined:>6,}  ({100*combined/cms_req:5.1f}% of required)")
print(f"CMS-required hospitals with >=1 live MRF:   {cms_req_with_live:>6,}  ({100*cms_req_with_live/cms_req:5.1f}% of required)\n")

print("--- GAP ANALYSIS ---")
gap = cms_req - cms_req_with_live
print(f"Required hospitals still missing live MRF:  {gap:>6,}  ({100*gap/cms_req:5.1f}% of required)")

print("\n--- BY HOSPITAL TYPE (CMS-required) ---")
for r in q("""
  SELECT h.hospital_type, COUNT(*) AS total,
    SUM(CASE WHEN h.ccn IN (SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
              OR h.ccn IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1) THEN 1 ELSE 0 END) AS with_live
  FROM hospitals h
  WHERE h.hospital_type IN ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
  GROUP BY h.hospital_type ORDER BY total DESC
"""):
    pct = 100 * r['with_live'] / r['total']
    print(f"  {r['hospital_type']:<32} {r['total']:>5}  live={r['with_live']:>4} ({pct:5.1f}%)")

print("\n--- WORST 10 STATES (CMS-required, % missing) ---")
for r in q("""
  WITH t AS (
    SELECT h.state, COUNT(*) AS n,
      SUM(CASE WHEN h.ccn IN (SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
                OR h.ccn IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1) THEN 1 ELSE 0 END) AS live
    FROM hospitals h
    WHERE h.hospital_type IN ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
    GROUP BY h.state HAVING n >= 10
  )
  SELECT state, n, live, 100.0*(n-live)/n AS pct_missing FROM t ORDER BY pct_missing DESC LIMIT 10
"""):
    print(f"  {r['state']}  total={r['n']:>3}  live={r['live']:>3}  missing={r['pct_missing']:5.1f}%")

print("\n--- SF FLAGSHIP STATUS ---")
for ccn in ('050228', '050454', '050076', '050152', '050457', '050008'):
    h = q1(f"SELECT name FROM hospitals WHERE ccn='{ccn}'") if conn.execute(f"SELECT 1 FROM hospitals WHERE ccn='{ccn}'").fetchone() else 'unknown'
    seed_alive = q1(f"SELECT COUNT(*) FROM mrf_seed s JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE s.ccn='{ccn}' AND p.alive=1")
    redisc_alive = q1(f"SELECT COUNT(*) FROM mrf_rediscovered WHERE ccn='{ccn}' AND alive=1")
    print(f"  {ccn} {h:<48} seed_live={seed_alive} redisc_live={redisc_alive}")
