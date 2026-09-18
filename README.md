# Hospital Ledger

**Open-source, public-good crawler for U.S. hospital price transparency
machine-readable files (MRFs).**

Live at **[hospitalledger.com](https://hospitalledger.com)**. Status: v1 — live,
public, indexed (all 11 pipeline stages running; the site & API are deployed).

## What this is

Every U.S. hospital is required by federal rule [45 CFR § 180](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180)
to publish a machine-readable file (MRF) listing standard charges and
payer-negotiated rates. CMS does not aggregate this data and does not
verify the files. Commercial aggregators (Turquoise, PayerPrice, Serif)
paywall their data behind NDAs.

This project produces a single CC0-licensed dataset of every U.S. hospital
MRF: where it is, whether it's live, and what's in it — searchable on the site
and queryable via JSON API.

## What's in the data right now

Numbers below are verified from the deployed site (`public/data/summary.json`,
`public/data/prices/index.json`, and the R2-backed `/api/cpt-index`) on
2026-05-19. Re-derive them anytime with the queries in [Verify](#verify).

| Metric | Value | Source |
|---|---|---|
| Hospitals in the CMS universe | **5,426** | `hospitals` table |
| CMS-required hospitals (the denominator for compliance) | **4,625** | `summary.json#cms_required_total` |
| CMS-required hospitals with a verified live MRF | **3,986 (86.2%)** | `summary.json#compliant` / `#compliance_pct` |
| Hospitals with a standardized on-site price preview | **3,800** | `public/data/prices/index.json` |
| Standardized price rows across those hospitals | **67.6 M** | sum of `n` in prices index |
| CPT- / HCPCS-coded rows (patient-comparable) | **14.7 M** | sum of `cpt_indexed` in prices index |
| Distinct CPT / HCPCS codes in the cross-hospital index | **5,000** | `/api/cpt-index` keys |
| Payer-negotiated rate cells | **148.0 M** | sum of `n_rates` in `data/_payer_raw.jsonl` |
| Hospitals with at least one payer-rate row | **2,745** | distinct CCN in `_payer_raw.jsonl` |
| CCN × raw-payer-string rows | **68,528** | line count of `_payer_raw.jsonl` |
| Canonical payer brands surfaced on site | **200 featured** (21,803 raw) | `/api/payers-index` |
| CMS enforcement records loaded | **11,440** | `cms_enforcement` table |
| Enforcement actions linked to required hospitals | **8,642** | `summary.json#enforcement_actions_total` |
| Raw MRF data downloaded & parsed | **~5.8 GB gzipped** (originally ~107 GB raw) | `data/parsed/*.json.gz` |

## Pipeline

The site is built from 11 stages running end-to-end; all 11 are running in
production. Stages 1–2 land in the SQLite mirror at `db/hospital_ledger.db`;
stages 3–11 land in JSONL gap files under `data/` and the deployed JSON bundles
under `public/data/`.

| Stage | Description | Status |
|---|---|---|
| 1 | Seed CMS hospital universe (5,426 facilities) | done |
| 2 | Load MRF URL seeds (7,191 from TPAFS) + probe liveness | done (in `mrf_probe`) |
| 3 | URL rediscovery crawler for dead URLs | done (in `mrf_rediscovered`) |
| 4 | Fetch + parse alive MRFs (CSV-tall/wide, JSON v2/v3, XLSX) | live (3,800 / 3,986 = 94.5% of required+live hospitals parsed; 163 terminal exceptions) |
| 5 | Public API + UI (SSR on Cloudflare Workers + R2) | live at hospitalledger.com |
| 6 | Compliance watchdog + CMS enforcement ingestion | live (`cms_enforcement` + `cms_enforcement_match`) |
| 7 | Cross-hospital CPT / HCPCS price index | live (5,000 codes, R2-backed `/api/cpt-index`) |
| 8 | Canonical payer normalization | live (200 featured brands, `/api/payers-index`) |
| 9 | Per-hospital standardized price files | live (R2 `parsed/{ccn}.json`, served via `/api/prices/{ccn}`) |
| 10 | Coverage-gap closeout & retry loop | running (`scripts/coverage_closeout.py`, `scripts/finalize_gap_backfill.py`) |
| 11 | CMS validation monitor (auto-CMS-complaints) | running (`scripts/cms_validation_monitor.py`) |

Coverage gap as of 2026-05-19 closeout snapshot: 218 preview-gap hospitals
remaining (out of 3,986 CMS-required+live targets); 163 terminal exceptions
documented in `data/coverage_terminal_exceptions.json`; full failure cluster
breakdown in `data/coverage_closeout_status.json`.

## Ask anything (site FAQ)

Every page except the methodology page carries an "Ask anything" row: the home
page directly under the hero's lead paragraph, and each hospital, procedure and
insurance page under its headline card. It POSTs to `/api/faq/ask` (a real form
with JavaScript off; a streaming island with it) and answers from the site's own
rendered pages — the home page, `/about-the-numbers`, the procedure-page price
caveats and the hospital-page grade key — plus, on a detail page, the record on
screen re-read from R2 by id. Workers AI (`AI` binding, llama-3.3-70b), 20
questions per minute per IP (`FAQ_RATE_LIMITER`), 8 KB body cap. The model is
told to copy numbers whole or say they are not listed, and that prices are as
published, never a quote. Code: `src/faq/`; tests: `npm run test:faq`; island
bundle: `npm run build:faq-island` (committed to `public/faq-island.js`).

## Files

| Path | Contents | Source |
|---|---|---|
| `seed/hospitals.csv` | 5,426 hospital records, keyed on CCN | [CMS Hospital General Info](https://data.cms.gov/provider-data/dataset/xubh-q36u) — public domain |
| `seed/tpafs_hospital_mrf_links.csv` | 7,191 MRF URL seed entries | [TPAFS/transparency-data](https://github.com/TPAFS/transparency-data) — CC BY-SA 4.0 |
| `db/hospital_ledger.db` | SQLite mirror + probe results | this project |
| `data/hospital_ledger_scoreboard.csv` | CC0 alive/dead/size scoreboard | this project |
| `scripts/build_db.py` | Stage 1: build SQLite from CSV seeds | this project |
| `scripts/probe_mrf_urls.py` | Stage 2: async HTTP probe | this project |
| `scripts/export_scoreboard.py` | Export CC0 scoreboard CSV | this project |

## Database schema

```sql
CREATE TABLE hospitals (
  ccn TEXT PRIMARY KEY,           -- CMS Certification Number
  name, address, city, state, zip, county,
  phone, hospital_type, ownership, emergency, cms_rating
);

CREATE TABLE mrf_seed (
  ccn TEXT, entity_name_legal TEXT, entity_name_common TEXT,
  entity_type TEXT, mrf_url TEXT, mrf_url_status TEXT,
  mrf_page TEXT, file_name TEXT, file_format TEXT,
  state TEXT, last_updated_date TEXT, entry_date TEXT,
  PRIMARY KEY (ccn, mrf_url)
);

CREATE TABLE mrf_probe (
  ccn TEXT, mrf_url TEXT, probed_at TEXT,
  http_status INTEGER, content_type TEXT, content_length INTEGER,
  final_url TEXT, alive INTEGER,
  PRIMARY KEY (ccn, mrf_url, probed_at)
);
```

## Reproduce

```bash
cd ~/projects/hospital-ledger
python3 scripts/build_db.py          # ~5 sec — Stage 1 (CMS universe)
python3 scripts/probe_mrf_urls.py --all --concurrency 12   # ~10–15 min — Stage 2
python3 scripts/export_scoreboard.py # ~1 sec → data/hospital_ledger_scoreboard.csv
python3 scripts/rediscover_mrf_urls.py # Stage 3
python3 scripts/batch_ingest.py --workers $(scripts/tune_ingest_workers.py) # Stage 4
python3 scripts/build_aggregates.py  # Stage 7 — cross-hospital CPT/HCPCS index
python3 scripts/build_site_data.py   # Stage 5 — emit public/data/{hospitals,summary,prices/index}.json
```

## Verify

To re-derive every number in the table above (don't trust the README, trust the
data):

```bash
sqlite3 db/hospital_ledger.db <<'SQL'
SELECT COUNT(*) AS hospitals FROM hospitals;                            -- 5426
SELECT COUNT(DISTINCT ccn) AS alive_probe FROM mrf_probe WHERE alive=1; -- 1840
SELECT COUNT(*) AS enf FROM cms_enforcement;                            -- 11440
SQL

python3 - <<'PY'
import json
d = json.load(open('public/data/prices/index.json'))
print("priced hospitals:", len(d['hospitals']))
print("standardized rows:", sum(h['n'] for h in d['hospitals']))
print("CPT-indexed rows:", sum(h['cpt_indexed'] for h in d['hospitals']))
PY

curl -sS https://hospitalledger.com/api/cpt-index | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print('distinct codes:', len(d))"
curl -sS https://hospitalledger.com/api/payers-index | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print('payers total/featured:', d['total'], len(d['featured']))"
```

## License

- **Code:** AGPLv3 (forces commercial aggregators who fork to publish back).
- **Data outputs** (`data/*.csv`): **CC0 1.0 Universal** (public domain dedication).
- **Upstream seed:** `tpafs_hospital_mrf_links.csv` is CC BY-SA 4.0 from
  [TPAFS/transparency-data](https://github.com/TPAFS/transparency-data) —
  attribution preserved per their license. Only the *seed URL list* is
  CC BY-SA; the probed scoreboard (alive/dead/size results) is original work
  released CC0.
- **CMS Hospital General Information** is U.S. federal public domain.

## What this is NOT

- Not a payer (insurance-side) transparency tool (out of scope for v1).
- Not a clinical or quality dataset (CMS rating included but not the focus).
- Not affiliated with CMS, HHS, or any commercial transparency vendor.

> Previously this section also said "Not a chargemaster aggregator (yet — Stage
> 4)." That is now false: Stage 4 is live, and 3,800 hospitals' MRFs have been
> parsed into a unified schema and indexed by CPT / HCPCS.
