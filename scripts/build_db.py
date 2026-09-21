#!/usr/bin/env python3
"""Build hospital_ledger.db from CMS Hospital General Info + TPAFS MRF URL index."""
import sqlite3, csv, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
CMS_CSV = os.path.join(ROOT, 'seed', 'hospitals.csv')
TPAFS_CSV = os.path.join(ROOT, 'seed', 'tpafs_hospital_mrf_links.csv')
SCOREBOARD_CSV = os.path.join(ROOT, 'data', 'hospital_ledger_scoreboard.csv')

REQUIRED_TYPES = (
    'Acute Care Hospitals',
    'Critical Access Hospitals',
    'Childrens',
    'Rural Emergency Hospital',
)

conn = sqlite3.connect(DB)
c = conn.cursor()
c.executescript("""
DROP TABLE IF EXISTS hospitals;
DROP TABLE IF EXISTS mrf_seed;
DROP TABLE IF EXISTS mrf_probe;

CREATE TABLE hospitals(
  ccn TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  address TEXT, city TEXT, state TEXT, zip TEXT, county TEXT,
  phone TEXT, hospital_type TEXT, ownership TEXT,
  emergency TEXT, cms_rating TEXT
);

CREATE TABLE mrf_seed(
  ccn TEXT,
  entity_name_legal TEXT,
  entity_name_common TEXT,
  entity_type TEXT,
  mrf_url TEXT,
  mrf_url_status TEXT,
  mrf_page TEXT,
  file_name TEXT,
  file_format TEXT,
  state TEXT,
  last_updated_date TEXT,
  entry_date TEXT,
  PRIMARY KEY(ccn, mrf_url)
);

CREATE TABLE mrf_probe(
  ccn TEXT,
  mrf_url TEXT,
  probed_at TEXT,
  http_status INTEGER,
  content_type TEXT,
  content_length INTEGER,
  final_url TEXT,
  alive INTEGER,
  PRIMARY KEY(ccn, mrf_url, probed_at)
);

CREATE INDEX idx_hosp_state ON hospitals(state);
CREATE INDEX idx_seed_state ON mrf_seed(state);

-- Rediscovery is durable enrichment state. Do not drop it when rebuilding
-- CMS and TPAFS seeds, but create an empty table for a clean cloud bootstrap.
CREATE TABLE IF NOT EXISTS mrf_rediscovered (
  ccn TEXT,
  source_page TEXT,
  candidate_url TEXT,
  anchor_text TEXT,
  score INTEGER,
  discovered_at TEXT,
  head_status INTEGER,
  head_content_type TEXT,
  head_content_length INTEGER,
  alive INTEGER,
  rank INTEGER,
  PRIMARY KEY (ccn, candidate_url)
);
CREATE INDEX IF NOT EXISTS idx_redisc_ccn ON mrf_rediscovered(ccn);
CREATE INDEX IF NOT EXISTS idx_redisc_alive ON mrf_rediscovered(alive);
""")

with open(CMS_CSV) as f:
    r = csv.DictReader(f)
    rows = [(x['facility_id'], x['facility_name'], x['address'], x['citytown'],
             x['state'], x['zip_code'], x['countyparish'], x['telephone_number'],
             x['hospital_type'], x['hospital_ownership'], x['emergency_services'],
             x.get('hospital_overall_rating', '')) for x in r]
c.executemany("INSERT OR REPLACE INTO hospitals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
print(f"hospitals loaded: {c.execute('SELECT COUNT(*) FROM hospitals').fetchone()[0]}")

with open(TPAFS_CSV) as f:
    r = csv.DictReader(f)
    rows = []
    for x in r:
        if not x.get('ccn'):
            continue
        rows.append((x['ccn'], x['reporting_entity_name_legal'],
                     x['reporting_entity_name_common'], x['reporting_entity_type'],
                     x['machine_readable_url'], x['machine_readable_url_status'],
                     x['machine_readable_page'], x['file_name'], x['file_format'],
                     x['state_or_region'], x['last_updated_date'], x['entry_date']))
c.executemany("INSERT OR IGNORE INTO mrf_seed VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
print(f"mrf_seed loaded: {c.execute('SELECT COUNT(*) FROM mrf_seed').fetchone()[0]}")

# The tracked scoreboard is a portable snapshot of URL rediscovery and probe
# history. It lets a clean cloud worker recover the full live-URL corpus even
# when the prior SQLite checkpoint is unavailable.
if os.path.exists(SCOREBOARD_CSV):
    probe_rows = []
    rediscovered_rows = []
    with open(SCOREBOARD_CSV, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            ccn = (row.get('ccn') or '').strip()
            url = (row.get('mrf_url') or '').strip()
            if not ccn or not url:
                continue
            verified = (row.get('verified_utc') or '').strip() or 'scoreboard'
            status_text = (row.get('http_status') or '').strip()
            length_text = (row.get('bytes') or '').strip()
            alive = 1 if (row.get('alive') or '').strip() in {'1', 'true', 'True'} else 0
            status = int(status_text) if status_text.lstrip('-').isdigit() else None
            length = int(length_text) if length_text.isdigit() else None
            probe_rows.append((
                ccn, url, verified, status, (row.get('content_type') or '').strip(),
                length, url, alive,
            ))
            if (row.get('source') or '').strip() == 'rediscovered':
                score_text = (row.get('rediscovery_score') or '').strip()
                score = int(score_text) if score_text.lstrip('-').isdigit() else 0
                rediscovered_rows.append((
                    ccn, (row.get('source_page') or '').strip(), url, '', score,
                    verified, status, (row.get('content_type') or '').strip(),
                    length, alive, 0,
                ))
    c.executemany(
        """INSERT OR IGNORE INTO mrf_probe
           (ccn,mrf_url,probed_at,http_status,content_type,content_length,final_url,alive)
           VALUES (?,?,?,?,?,?,?,?)""",
        probe_rows,
    )
    c.executemany(
        """INSERT OR IGNORE INTO mrf_rediscovered
           (ccn,source_page,candidate_url,anchor_text,score,discovered_at,
            head_status,head_content_type,head_content_length,alive,rank)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rediscovered_rows,
    )
    print(f"scoreboard probes loaded: {len(probe_rows)}")
    print(f"scoreboard rediscovered URLs loaded: {len(rediscovered_rows)}")

# coverage
ph = ','.join('?' * len(REQUIRED_TYPES))
print("\n=== COVERAGE ===")
total = c.execute('SELECT COUNT(*) FROM hospitals').fetchone()[0]
req = c.execute(f"SELECT COUNT(*) FROM hospitals WHERE hospital_type IN ({ph})", REQUIRED_TYPES).fetchone()[0]
seed_total = c.execute('SELECT COUNT(*) FROM mrf_seed').fetchone()[0]
seed_ccns = c.execute('SELECT COUNT(DISTINCT ccn) FROM mrf_seed').fetchone()[0]
matched = c.execute("SELECT COUNT(DISTINCT h.ccn) FROM hospitals h JOIN mrf_seed s ON h.ccn = s.ccn").fetchone()[0]
missing = c.execute(
    f"SELECT COUNT(*) FROM hospitals WHERE hospital_type IN ({ph}) "
    f"AND ccn NOT IN (SELECT ccn FROM mrf_seed)", REQUIRED_TYPES).fetchone()[0]

print(f"CMS hospitals (all types): {total}")
print(f"  CMS MRF-required (Acute/CAH/Children/Rural-EH): {req}")
print(f"TPAFS seed rows: {seed_total}")
print(f"  TPAFS unique CCNs: {seed_ccns}")
print(f"CCN overlap (CMS ↔ TPAFS): {matched}")
print(f"CMS-required hospitals MISSING from TPAFS: {missing}  ({100*missing/req:.1f}% gap)")

print("\n=== SF hospitals (CMS) ===")
for row in c.execute("SELECT ccn, name, hospital_type FROM hospitals WHERE city = 'SAN FRANCISCO' ORDER BY name"):
    print(f"  {row[0]} | {row[1]} | {row[2]}")

print("\n=== ZSFG MRF entries in TPAFS ===")
zsfg = list(c.execute("""SELECT ccn, entity_name_common, mrf_url, mrf_url_status, last_updated_date
  FROM mrf_seed
  WHERE entity_name_common LIKE '%Zuckerberg%' OR entity_name_common LIKE '%San Francisco General%'
     OR entity_name_legal  LIKE '%Zuckerberg%' OR entity_name_legal  LIKE '%San Francisco General%'"""))
if not zsfg:
    print("  (none)")
for row in zsfg:
    print(f"  CCN={row[0]} {row[1]}")
    print(f"    url={row[2][:130]}")
    print(f"    status={row[3]} updated={row[4]}")

print("\n=== UCSF MRF entries in TPAFS ===")
ucsf = list(c.execute("""SELECT ccn, entity_name_common, mrf_url, mrf_url_status, last_updated_date
  FROM mrf_seed
  WHERE entity_name_common LIKE '%UCSF%' OR entity_name_legal LIKE '%UCSF%'
     OR entity_name_common LIKE '%University of California, San Francisco%'
     OR entity_name_legal  LIKE '%University of California, San Francisco%'"""))
if not ucsf:
    print("  (none)")
for row in ucsf:
    print(f"  CCN={row[0]} {row[1]}")
    print(f"    url={row[2][:130]}")
    print(f"    status={row[3]} updated={row[4]}")

conn.commit()
conn.close()
print(f"\ndb at {DB}")
