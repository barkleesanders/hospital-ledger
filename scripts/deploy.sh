#!/usr/bin/env bash
# Production build + deploy for hospital-ledger (invoked by `npm run deploy`).
#
# WHY THIS WRAPPER EXISTS (2026-06-08 ENOSPC incident):
#   public/data/prices is ~18 GB — 3,800 per-hospital priced JSONs (largest
#   ~85 MiB). They are served from R2 (HL_MRF_PARSED/prices/{ccn}.json) and are
#   EXCLUDED from the Worker via public/.assetsignore, so they never ship as
#   assets. But `vite build` copies ALL of public/ into dist/ BEFORE wrangler
#   applies .assetsignore — so it tried to duplicate 18 GB into a disk-tight box
#   and died with `ENOSPC: no space left on device`.
#
#   Fix: stash the per-hospital price files aside for the duration of the build
#   (a same-filesystem `mv` is instant and uses no extra disk), keeping only the
#   small index.json (which IS a needed asset), then ALWAYS restore them. dist/
#   then only has to hold cpt-detail (~2 GB) + the small assets, which fits.
#
#   This wrapper is what the weekly refresh (scripts/refresh.sh Step 8) and any
#   manual `npm run deploy` both run, so the fix is durable, not a one-off.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# STASH must live OUTSIDE public/ — vite copies the ENTIRE public/ tree, so a
# stash inside it (even a dot-dir) gets copied too. A repo-root dir is on the
# same filesystem, so the mv is still instant and uses no extra disk.
PRICES="public/data/prices"
STASH=".prices_build_stash"

restore_prices() {
  # Always put the price files back, whatever happened to the build/deploy.
  if [ -d "$STASH" ]; then
    rm -rf "$PRICES"
    mv "$STASH" "$PRICES"
  fi
}
trap restore_prices EXIT

# Stash everything in prices/ except the small index.json the bundle needs.
if [ -d "$PRICES" ] && [ ! -d "$STASH" ]; then
  mv "$PRICES" "$STASH"                 # instant, same filesystem, no extra disk
  mkdir -p "$PRICES"
  [ -f "$STASH/index.json" ] && cp "$STASH/index.json" "$PRICES/index.json"
fi

rm -rf dist
npx --no-install vite build
npx --no-install wrangler deploy -c wrangler.jsonc
# trap restore_prices runs here on normal exit too
