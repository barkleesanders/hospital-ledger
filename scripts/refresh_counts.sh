#!/usr/bin/env bash
# scripts/refresh_counts.sh — the "counts" refresh tier (ops/data-contract.md).
#
# Re-probes every seeded MRF URL, rebuilds the hospital registry + summary
# counts, assembles a contract-shaped output tree, writes its manifest,
# validates it, and (optionally) publishes meta/* to R2.
#
# This is the tier a producer with no local parsed corpus can run — it never
# touches prices/, indexes/, aggregates/ (those carry forward), and
# summary.standardized_price_* are recomputed from prices/index.json exactly as
# served, so the counts stay consistent with the price data on the site.
#
# Usage:
#   bash scripts/refresh_counts.sh                 # build + validate to $OUT (default data/site-out)
#   bash scripts/refresh_counts.sh --publish       # …then upload meta/* to R2 (needs S3 creds, below)
#   bash scripts/refresh_counts.sh --limit 200     # quick smoke test: probe only 200 URLs (never --publish)
#
# Env:
#   HL_PRODUCER   who is running this (goes into meta/manifest.json), e.g. "muse.ai nova"
#   OUT           output dir (default: data/site-out)
#   CONCURRENCY   probe concurrency (default 12)
#   For --publish, S3-compatible R2 credentials scoped to bucket hl-mrf-parsed:
#   AWS_ACCESS_KEY_ID  AWS_SECRET_ACCESS_KEY  R2_ENDPOINT (https://<account>.r2.cloudflarestorage.com)
#
# Needs only python3 (>=3.9) + outbound HTTPS. httpx is the one third-party
# dependency (the probe); it is installed into ./.venv on first run, which also
# works on PEP-668 "externally managed" Pythons where `pip install --user` is
# refused (the muse.ai VM, measured 2026-09-18). R2 uploads use scripts/r2_put.py
# (stdlib SigV4) — no aws/rclone required.
#
# Exit codes: 0 ok · 1 build/validate failed · 3 publish failed · 4 post-publish live check failed
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ ! -x ".venv/bin/python3" ]; then
  python3 -m venv .venv || { echo "python3 -m venv failed (install python3-venv)"; exit 1; }
fi
export PATH="$ROOT/.venv/bin:$PATH"

PUBLISH=0; LIMIT=""; CONCURRENCY="${CONCURRENCY:-12}"
while [ $# -gt 0 ]; do
  case "$1" in
    --publish) PUBLISH=1 ;;
    --limit) LIMIT="$2"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done
if [ -n "$LIMIT" ] && [ "$PUBLISH" = 1 ]; then
  echo "refusing: --limit is a smoke test; a partial probe must never be published" >&2; exit 1
fi

OUT="${OUT:-$ROOT/data/site-out}"
PRODUCER="${HL_PRODUCER:-$(whoami)@$(hostname -s)}"
BUCKET="${PARSED_R2_BUCKET:-hl-mrf-parsed}"
LOG_DIR="$ROOT/data/refresh_logs"; mkdir -p "$LOG_DIR"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
exec > >(tee -a "$LOG_DIR/counts-$TS.log") 2>&1
echo "== refresh_counts $TS producer=$PRODUCER out=$OUT limit=${LIMIT:-all}"

python3 -c "import httpx" 2>/dev/null || python3 -m pip install --quiet "httpx==0.28.1"

# httpx 0.28.1 crashes at Client() on any IPv6 literal in NO_PROXY/no_proxy
# ("httpx.InvalidURL: Invalid port: ':1]'") — the muse.ai VM sets
# no_proxy=…,[::1],[fd8b:…]/64 (measured 2026-09-18, first Nova run exit 1).
# Keep hostnames/IPv4 entries and the egress proxy itself; drop IPv6 entries.
strip_ipv6() { printf '%s' "$1" | tr ',' '\n' | grep -v ':' | paste -sd, - ; }
[ -n "${NO_PROXY:-}" ] && export NO_PROXY="$(strip_ipv6 "$NO_PROXY")"
[ -n "${no_proxy:-}" ] && export no_proxy="$(strip_ipv6 "$no_proxy")"

# Step 1: re-probe seed URLs (INSERT OR REPLACE into mrf_probe; keeps history).
# Deliberately NOT build_db.py — it DROPs mrf_probe and would erase every probe
# ever made. The DB ships in the repo; the seed tables are already loaded.
if [ -n "$LIMIT" ]; then
  python3 scripts/probe_mrf_urls.py --limit "$LIMIT" --concurrency "$CONCURRENCY"
else
  python3 scripts/probe_mrf_urls.py --all --concurrency "$CONCURRENCY"
fi

# Step 2: enforcement match (idempotent; reads seed/cms_enforcement.csv).
python3 scripts/ingest_cms_enforcement.py

# Step 3: registry + summary. Reads public/data/prices/index.json for the
# standardized_price_* counts, so those track what the site serves.
python3 scripts/build_site_data.py

# Step 4: assemble the contract tree.
rm -rf "$OUT"; mkdir -p "$OUT/meta" "$OUT/prices"
cp public/data/summary.json   "$OUT/meta/summary.json"
cp public/data/hospitals.json "$OUT/meta/hospitals.json"
cp public/data/prices/index.json "$OUT/prices/index.json"
python3 scripts/make_manifest.py "$OUT" --producer "$PRODUCER" --tier counts \
  --note "refresh_counts.sh $TS probe_limit=${LIMIT:-all}"

# Step 5: gate.
python3 scripts/validate_site_data.py "$OUT" --tier counts --quiet
echo "generated_at: $(python3 -c "import json;print(json.load(open('$OUT/meta/summary.json'))['generated_at'])")"
echo "compliant:    $(python3 -c "import json;d=json.load(open('$OUT/meta/summary.json'));print(d['compliant'],'/',d['cms_required_total'],'=',d['compliance_pct'],'%')")"

[ "$PUBLISH" = 1 ] || { echo "== built + validated (no --publish)"; exit 0; }

# Step 6: publish. meta/summary.json + manifest.json go LAST: they flip the
# live generated_at, so everything they describe must already be uploaded.
: "${AWS_ACCESS_KEY_ID:?set R2 access key}" "${AWS_SECRET_ACCESS_KEY:?set R2 secret}" "${R2_ENDPOINT:?set https://<account>.r2.cloudflarestorage.com}"
export R2_BUCKET="$BUCKET"
python3 scripts/r2_put.py --check >/dev/null || { echo "publish failed: R2 credential rejected (HEAD bucket != 200)"; exit 3; }
for k in prices/index.json meta/hospitals.json meta/summary.json meta/manifest.json; do
  python3 scripts/r2_put.py "$OUT/$k" "$k" || { echo "publish failed at $k"; exit 3; }
done

# Step 7: prove the live site picked it up (manifest cache is 60 s).
WANT="$(python3 -c "import json;print(json.load(open('$OUT/meta/summary.json'))['generated_at'])")"
for i in $(seq 1 20); do
  GOT="$(curl -s "https://hospitalledger.com/api/manifest?cb=$(date +%s)$i" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("generated_at",""), d.get("summary_source",""))' 2>/dev/null || true)"
  case "$GOT" in "$WANT r2") echo "== LIVE: /api/manifest generated_at=$WANT summary_source=r2"; exit 0 ;; esac
  sleep 10
done
echo "live check FAILED: wanted generated_at=$WANT from r2, last saw: $GOT"; exit 4
