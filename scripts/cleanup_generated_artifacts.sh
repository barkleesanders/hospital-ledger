#!/usr/bin/env bash
# Remove large generated deployment output after a successful live release.
# This keeps the Mac mini's internal disk lean. The raw source data and all
# tracked files stay in place.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

for sentinel in package.json wrangler.jsonc scripts/refresh.sh; do
  if [ ! -e "$sentinel" ]; then
    printf 'FATAL: refusing cleanup; missing project sentinel: %s/%s\n' "$ROOT" "$sentinel" >&2
    exit 1
  fi
done

removed_kib=0
for rel in dist public/data/cpt-detail; do
  if [ -L "$rel" ]; then
    printf 'FATAL: refusing cleanup of symlinked generated path: %s/%s\n' "$ROOT" "$rel" >&2
    exit 1
  fi
  if [ -n "$(git ls-files -- "$rel")" ]; then
    printf 'FATAL: refusing cleanup because Git tracks files under %s\n' "$rel" >&2
    exit 1
  fi
  if ! git check-ignore -q "$rel"; then
    printf 'FATAL: refusing cleanup because Git does not ignore %s\n' "$rel" >&2
    exit 1
  fi
  if [ -d "$rel" ]; then
    kib="$(du -sk "$rel" | cut -f1)"
    removed_kib=$((removed_kib + kib))
    rm -rf -- "$rel"
    printf 'removed_generated=%s kib=%s\n' "$rel" "$kib"
  else
    printf 'already_absent=%s\n' "$rel"
  fi
done

printf 'removed_generated_total_kib=%s\n' "$removed_kib"
