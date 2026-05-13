#!/usr/bin/env python3
"""Stage 2.8: Ingest CMS Hospital Price Transparency Enforcement dataset.

CMS publishes a public CSV of every enforcement action they have taken against
hospitals for MRF non-compliance. Source:
  https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/
    hospital-price-transparency-enforcement-activities-and-outcomes

Schema: Case_ID, Hosp_Name, Hosp_Address, City, State, Action, Date_of_Action

This script:
  1. Loads CSV into table `cms_enforcement` (raw)
  2. Fuzzy-matches each enforcement record to a CCN in `hospitals` by
     (normalized_name, city, state)
  3. Writes `cms_enforcement_match` linking case_id ↔ ccn
  4. Produces summary: hospitals with enforcement, action types, % of
     still-missing hospitals that have at least one CMS enforcement action

Why this matters: even when we can't find a live MRF for a hospital, CMS's
enforcement record IS the policy artifact. A CCN with enforcement action +
no live MRF in our dataset = federally-documented non-compliance.
"""
import csv, sqlite3, os, re, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'db', 'hospital_ledger.db')
ENF_CSV = os.path.join(ROOT, 'seed', 'cms_enforcement.csv')

STOP = {'the', 'of', 'and', 'inc', 'llc', 'corp', 'corporation', 'co'}


def normalize(s):
    """Aggressive normalization for fuzzy matching."""
    s = (s or '').lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t not in STOP]
    return ' '.join(tokens)


def name_signature(name):
    """Distinctive subset of name words (drops stopwords + 'hospital' family).
    Used to do near-match across naming variants (e.g. "Mercy Hospital" vs
    "Mercy Medical Center")."""
    SOFT_STOP = STOP | {
        'hospital', 'hospitals', 'medical', 'center', 'health',
        'system', 'systems', 'campus', 'care',
    }
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or '').lower())
    tokens = [t for t in s.split() if t and t not in SOFT_STOP and len(t) >= 3]
    return frozenset(tokens)


def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.executescript("""
    DROP TABLE IF EXISTS cms_enforcement;
    DROP TABLE IF EXISTS cms_enforcement_match;
    CREATE TABLE cms_enforcement (
      row_id INTEGER PRIMARY KEY,
      case_id TEXT, hosp_name TEXT, hosp_address TEXT,
      city TEXT, state TEXT, action TEXT, date_of_action TEXT
    );
    CREATE TABLE cms_enforcement_match (
      case_id TEXT, ccn TEXT, match_strength INTEGER,
      PRIMARY KEY (case_id, ccn)
    );
    CREATE INDEX idx_enf_case ON cms_enforcement(case_id);
    CREATE INDEX idx_enf_namecity ON cms_enforcement(state, city);
    """)

    # 1) Load CSV
    with open(ENF_CSV, encoding='utf-8-sig') as f:
        rd = csv.DictReader(f)
        rows = []
        for row in rd:
            rows.append((row['Case_ID'], row['Hosp_Name'], row['Hosp_Address'],
                         row['City'], row['State'], row['Action'],
                         row['Date_of_Action']))
    c.executemany("INSERT INTO cms_enforcement (case_id, hosp_name, hosp_address, city, state, action, date_of_action) VALUES (?,?,?,?,?,?,?)", rows)
    print(f"loaded {len(rows)} enforcement actions")
    case_count = c.execute("SELECT COUNT(DISTINCT case_id) FROM cms_enforcement").fetchone()[0]
    print(f"  distinct cases: {case_count}")

    # 2) Build lookup index of our hospitals by (state, normalized name signature)
    hospitals = list(c.execute("SELECT ccn, name, city, state FROM hospitals").fetchall())
    by_state = defaultdict(list)
    for ccn, name, city, state in hospitals:
        by_state[state].append({
            'ccn': ccn, 'name': name, 'norm': normalize(name),
            'sig': name_signature(name), 'city_norm': normalize(city or ''),
        })

    # 3) Fuzzy match each unique enforcement case to a CCN
    cases = list(c.execute("""
        SELECT DISTINCT case_id, hosp_name, city, state FROM cms_enforcement
    """).fetchall())

    matched = []
    unmatched = []
    for case_id, hname, hcity, hstate in cases:
        if not hstate or hstate not in by_state:
            unmatched.append((case_id, hname, hstate))
            continue
        h_sig = name_signature(hname)
        h_norm = normalize(hname)
        h_city = normalize(hcity or '')
        best = None
        best_score = 0
        for cand in by_state[hstate]:
            score = 0
            # Exact normalized name match → 100
            if h_norm == cand['norm']:
                score = 100
            else:
                # Token-set overlap (Jaccard-ish)
                if h_sig and cand['sig']:
                    inter = h_sig & cand['sig']
                    union = h_sig | cand['sig']
                    if inter:
                        score = int(80 * len(inter) / len(union))
                # Bonus for same city
                if h_city and cand['city_norm'] == h_city:
                    score += 15
            if score > best_score:
                best_score = score
                best = cand
        if best and best_score >= 40:
            matched.append((case_id, best['ccn'], best_score))
        else:
            unmatched.append((case_id, hname, hstate))

    c.executemany("INSERT OR REPLACE INTO cms_enforcement_match VALUES (?,?,?)", matched)
    conn.commit()

    print(f"\n=== MATCHING ===")
    print(f"cases matched to CCN: {len(matched)}/{len(cases)} ({100*len(matched)/len(cases):.1f}%)")
    print(f"cases unmatched: {len(unmatched)}")

    # 4) Summary stats
    print(f"\n=== HOSPITALS UNDER CMS ENFORCEMENT ===")
    print(f"Distinct hospitals (CCN) with enforcement: "
          f"{c.execute('SELECT COUNT(DISTINCT ccn) FROM cms_enforcement_match').fetchone()[0]}")

    print(f"\n=== ACTION TYPES (top 12) ===")
    for r in c.execute("""
        SELECT action, COUNT(*) n FROM cms_enforcement
        GROUP BY action ORDER BY n DESC LIMIT 12
    """):
        print(f"  {r[1]:>5}  {r[0]}")

    print(f"\n=== INTERSECTION WITH OUR 'MISSING' COHORT ===")
    n_missing_w_enf = c.execute("""
        SELECT COUNT(DISTINCT h.ccn) FROM hospitals h
        JOIN cms_enforcement_match m ON m.ccn = h.ccn
        WHERE h.hospital_type IN
          ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
          AND h.ccn NOT IN (
            SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p
              ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
          AND h.ccn NOT IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
    """).fetchone()[0]
    n_missing_total = c.execute("""
        SELECT COUNT(*) FROM hospitals h
        WHERE h.hospital_type IN
          ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
          AND h.ccn NOT IN (
            SELECT s.ccn FROM mrf_seed s JOIN mrf_probe p
              ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url WHERE p.alive=1)
          AND h.ccn NOT IN (SELECT ccn FROM mrf_rediscovered WHERE alive=1)
    """).fetchone()[0]
    print(f"  Still-missing hospitals: {n_missing_total}")
    print(f"  Of those, with CMS enforcement record: {n_missing_w_enf}  ({100*n_missing_w_enf/max(1,n_missing_total):.1f}%)")

    print(f"\n=== TIMELINE OF ACTIONS ===")
    for r in c.execute("""
        SELECT substr(date_of_action, -4) AS yr, COUNT(*) n
        FROM cms_enforcement WHERE date_of_action != ''
        GROUP BY yr ORDER BY yr
    """):
        print(f"  {r[0]}  {r[1]:>5} actions")

    print(f"\n=== TOP 10 STATES BY ENFORCEMENT COUNT ===")
    for r in c.execute("""
        SELECT state, COUNT(DISTINCT case_id) n
        FROM cms_enforcement GROUP BY state ORDER BY n DESC LIMIT 10
    """):
        print(f"  {r[0]}  {r[1]:>4} cases")

    conn.close()


if __name__ == '__main__':
    main()
