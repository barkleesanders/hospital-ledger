# hospitalledger.com — site data contract (v1)

**Purpose.** Everything the live site renders is a JSON object in the R2 bucket
`hl-mrf-parsed`. A producer that writes conforming objects to these keys updates
the live site — no Worker deploy, no git commit, no Cloudflare Worker credentials.
This is the interface the muse.ai refresh task (Nova) produces against, and the
interface `scripts/validate_site_data.py` gates.

Contract version: **1** (`meta/manifest.json#contract_version`). Bump it only when
a key is renamed/removed or a required field changes meaning; adding optional
fields is not a version bump.

## The key tree

Lay artifacts out on disk exactly as they are keyed in R2, then upload the tree.

| R2 key | Served at | Required for tier | Shape |
|---|---|---|---|
| `meta/manifest.json` | `/api/manifest` | counts, full | `{contract_version:"1", generated_at, producer, refresh_tier:"counts"\|"full", artifacts:{key:{bytes,sha256}}, …}` — write with `scripts/make_manifest.py` |
| `meta/summary.json` | `/data/summary.json`, home page + `/about-the-numbers` counts, `sitemap.xml` lastmod | counts, full | see **summary** below |
| `meta/hospitals.json` | `/data/hospitals.json`, home page hospital list | counts, full | `[{ccn, name, city, state, address, zip, type, ownership, emergency, rating, required, has_live_mrf, mrf_url?, mrf_format?, mrf_bytes?, mrf_verified?, mrf_source?, enforcement_count?, enforcement_actions?[]}]` — one row per CMS facility |
| `prices/index.json` | `/api/prices-index`, home page, sitemap hospital list | full (optional on counts) | `{hospitals:[{ccn, n, name, counts:{CDM,CPT,DRG,HCPCS}, cpt_indexed, compliance:{score,grade,elements{},coverage{},item_count}}]}` |
| `prices/{ccn}.json` | `/api/prices/:ccn`, `/hospital/:ccn` | full | `{ccn, hospital_name, source_url, fetched_at, format, n_total_raw, n_slim, compliance{}, counts{}, items:[{code, type, desc, gross, cash, min, max, pc?, payers?:[{p,r}], bc?, s?}]}`; `type ∈ {CPT,HCPCS,DRG,MS-DRG,REV,CDM}` |
| `parsed/{ccn}.json` | fallback for `/api/prices/:ccn` (slimmed on the fly) | full (optional) | raw parser output; only needed when `prices/{ccn}.json` is absent |
| `indexes/cpt-index.json` | `/api/cpt-index`, home page CPT search | full | `{ "<code>": [{ccn, gross, type, cash?, min?, max?, pc?, payer_max?}] }` — **exactly 5,000 codes, ≤ 15 MB** |
| `aggregates/cpt-detail/{CODE}.json` | `/api/procedure/:code`, `/procedure/:code` | full | `{code, desc, type, stats:{hospital_count, cash_p50, cash_min, cash_max, gross_p50, flagged_low, flagged_high}, hospitals:[{ccn,name,state,city,gross,cash,min,max,payer_count,payers[],quality}]}` — `code` == filename |
| `aggregates/payer/{slug}.json` | `/api/payer/:slug`, `/payer/:slug` | full | `{payer:{slug,display,category,hospital_count,raw_aliases[],median_rate,min_rate,max_rate}, hospital_count, hospitals:[{ccn,name,state,city,n_items_with_payer,median_rate,compliance_grade,compliance_score}]}` — `payer.slug` == filename |
| `aggregates/payers-index.json` | `/api/payers-index` | full | `{featured:[{slug,display,category,hospital_count,raw_aliases[],median_rate,min_rate,max_rate}], long_tail_count, total}` |
| `aggregates/compliance-ranking.json` | `/api/compliance-ranking` | full | `{hospitals:[{ccn,name,state,city,type,compliance{},n_items,cpt_indexed}], grade_distribution:{A,B,C,D,F}, total}` |

Static fallbacks: `public/data/{summary,hospitals}.json` and `public/data/prices/index.json`
are baked into the Worker at build time and are used **only** when the R2 key is
absent or fails `isValidSummary` (src/lib/site-data.ts). `/api/manifest` reports
`summary_source: "r2" | "bundled"` and every page sets `x-hl-source` accordingly.

### summary (`meta/summary.json`)

Required (validator enforces type + these invariants):

```
generated_at                        ISO-8601 UTC; ≤ now, ≤ 400 days old; == manifest.generated_at
total_facilities                    == len(meta/hospitals.json)                       (5,426 on 2026-06-14)
cms_required_total                  == count(hospitals.required)                     (4,625)
compliant                           == count(hospitals.required ∧ has_live_mrf)       (3,986)
compliance_pct                      == round(100·compliant/cms_required_total, 1)     (86.2)
missing                             == cms_required_total − compliant
live_mrf_total                      int
enforcement_actions_total           int
standardized_price_index_hospitals  == len(prices/index.json.hospitals)               (3,800)
standardized_price_hospitals        == count(index.n > 0) ≤ index_hospitals ≤ compliant (3,692)
standardized_price_rows             == Σ index.n                                       (67,615,249)
cpt_indexed_hospitals               == count(index.cpt_indexed > 0)
cpt_indexed_rows                    == Σ index.cpt_indexed
states[], types[], worst_offenders[]  lists (rendered by the home page tables)
```
Optional: `under_enforcement`, `missing_with_enforcement`, `zero_price_index_entries`,
`count_definitions{}`. Producer: `scripts/build_site_data.py` (reads `db/hospital_ledger.db`
+ `public/data/prices/index.json`).

## Refresh tiers

| Tier | What changes | Producer steps | Cost | Cadence |
|---|---|---|---|---|
| **counts** | compliance numbers, hospital registry, enforcement | `build_db.py` (CMS seed) → `probe_mrf_urls.py --all` → `ingest_cms_enforcement.py` → `build_site_data.py` → copy to `meta/` → `make_manifest.py --tier counts` → validate → upload `meta/*` | ~15–30 min, ~2 GB disk, network-bound (7,200 HEAD/GET probes) | weekly |
| **full** | everything above + re-parsed prices for hospitals whose MRF changed, rebuilt CPT index and aggregates | counts steps, then `batch_ingest.py --resume --all` → `slim_parsed.py` → `promote_terminal_exceptions.py` → `build_aggregates.py` → `build_site_data.py` → `make_manifest.py --tier full` → validate → upload | 4–6 h on a 10-core Mac; needs the parsed corpus (~5.8 GB gz in R2 `parsed/`) locally for `slim_parsed.py` | monthly, or when counts shows ≥ 50 newly-live MRFs |

A counts run **must not** overwrite `prices/`, `indexes/`, `aggregates/` — it carries
those forward, and `summary.standardized_price_*` are recomputed from the
`prices/index.json` it did not change (so they stay consistent with what is served).

## Publishing (producer side)

```
python3 scripts/validate_site_data.py <outdir> --tier <counts|full>   # must exit 0
# S3-compatible R2 endpoint, credentials scoped to bucket hl-mrf-parsed (write):
export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=…
ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
# meta/ LAST — summary+manifest flip the site's generated_at, so the data they
# describe must already be in place.
aws s3 sync <outdir>/prices     s3://hl-mrf-parsed/prices     --endpoint-url $ENDPOINT --size-only   # full tier only
aws s3 sync <outdir>/indexes    s3://hl-mrf-parsed/indexes    --endpoint-url $ENDPOINT               # full tier only
aws s3 sync <outdir>/aggregates s3://hl-mrf-parsed/aggregates --endpoint-url $ENDPOINT               # full tier only
aws s3 cp   <outdir>/meta/hospitals.json s3://hl-mrf-parsed/meta/hospitals.json --endpoint-url $ENDPOINT --content-type application/json
aws s3 cp   <outdir>/meta/summary.json   s3://hl-mrf-parsed/meta/summary.json   --endpoint-url $ENDPOINT --content-type application/json
aws s3 cp   <outdir>/meta/manifest.json  s3://hl-mrf-parsed/meta/manifest.json  --endpoint-url $ENDPOINT --content-type application/json
```
(`rclone` with an `s3` remote of `provider = Cloudflare` works identically. For the
counts tier, `scripts/r2_put.py` does the four `cp` lines with only the Python stdlib —
that is what `scripts/refresh_counts.sh --publish` uses.)

Never `--delete`. Never write to `hl-mrf-raw` (the raw MRF archive) from a refresh.

## Verifying (consumer side — what "it updated the site" means)

```
curl -s "https://hospitalledger.com/api/manifest?cb=$(date +%s)"
#  → generated_at advanced, producer names the run, summary_source == "r2"
mkdir -p /tmp/hl-check/meta && for f in manifest summary hospitals; do
  curl -s "https://hospitalledger.com/$( [ $f = manifest ] && echo api/manifest || echo data/$f.json )?cb=$(date +%s)" -o /tmp/hl-check/meta/$f.json; done
python3 scripts/validate_site_data.py /tmp/hl-check --tier counts        # exit 0
curl -s "https://hospitalledger.com/?cb=$(date +%s)" | grep -o 'on 2026-[0-9-]*'   # home page footer date == generated_at date
```
Edge cache is 300 s on pages, 60 s on `/api/manifest`; allow 5 minutes after upload.

## Why R2-first (2026-09-18)

Before this contract the numbers on the home page were `import`ed from
`public/data/summary.json` at build time, so *any* data change required a `vite
build && wrangler deploy` with Cloudflare credentials — which is exactly the step the
mac-mini refresh job had been failing on since June (`Failed to fetch auth token:
400` in a non-interactive launchd context). Making the Worker read `meta/*` from R2
removes the deploy from the update loop entirely: a producer needs one scoped R2
write credential and nothing else.
