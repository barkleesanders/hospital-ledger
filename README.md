# Hospital Ledger

**Open-source, public-good crawler for U.S. hospital price transparency
machine-readable files (MRFs).**

Status: v0 (Stages 1–2 complete).

## What this is

Every U.S. hospital is required by federal rule [45 CFR § 180](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180)
to publish a machine-readable file (MRF) listing standard charges and
payer-negotiated rates. CMS does not aggregate this data and does not
verify the files. Commercial aggregators (Turquoise, PayerPrice, Serif)
paywall their data behind NDAs.

This project produces a single CC0-licensed dataset of every U.S. hospital
MRF: where it is, whether it's live, and what's in it.

## Pipeline

| Stage | Description | Status |
|---|---|---|
| 1 | Seed CMS hospital universe (5,426 facilities) | done |
| 2 | Load MRF URL seeds (7,191 from TPAFS) + probe liveness | done (probe results in `mrf_probe`) |
| 3 | URL rediscovery crawler for dead URLs | pending |
| 4 | Fetch + parse alive MRFs (CSV-tall/wide, JSON v2/v3, XLSX) | pending |
| 5 | Public API + UI | pending |
| 6 | Compliance watchdog + auto-CMS-complaints | pending |

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
python3 scripts/build_db.py          # ~5 sec
python3 scripts/probe_mrf_urls.py --all --concurrency 12   # ~10–15 min
python3 scripts/export_scoreboard.py # ~1 sec → data/hospital_ledger_scoreboard.csv
```

## Cloud closeout speed tuning

For standardized-price closeout runs in Codex/Cloud, the ingestion defaults are
set for higher network-bound parallelism: `batch_ingest.py` and
`full_standardize.py` use `HL_INGEST_WORKERS` or 16 workers by default, while
`retry_failed_ingest.py` uses `HL_RETRY_WORKERS` or 12 workers by default.

To benchmark the current runner and automatically choose the fastest stable
worker count before a full pass, run:

```bash
python3 scripts/full_standardize.py --auto-workers --max-workers 128 --tune-sample-size 48 --resume
```

The tuner performs real ingest work on live-MRF gaps, tests worker steps up to
`--max-workers`, backs off when a worker step is unstable, and writes the
recommendation plus benchmark details to `data/worker_tune_results.json`.

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

- Not a chargemaster aggregator (yet — Stage 4).
- Not a payer (insurance-side) transparency tool (out of scope for v1).
- Not a clinical or quality dataset (CMS rating included but not the focus).
- Not affiliated with CMS, HHS, or any commercial transparency vendor.
