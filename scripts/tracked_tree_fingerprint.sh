#!/usr/bin/env bash
# Fingerprint of every tracked file that differs from HEAD (path + content hash).
#   tracked_tree_fingerprint.sh          -> one sha256 line
#   tracked_tree_fingerprint.sh --files  -> the "<blob-hash> <path>" listing it is computed from
# verify.sh takes one before and one after the battery: a test suite that rewrites tracked
# data (data/header_aliases.json, ops/wave/hl_lessons_debt.json) leaves the tree changed,
# and the next `git add -A` commits fixture junk into a real registry. Measured
# 2026-11-22 at 94bddbd: three tests did exactly that (the two mrf_parse suites and
# test_hl_wave_chunk). Comparing before/after (not against HEAD alone) means a developer's
# own uncommitted edits never trip it, only what the battery itself changed.
# Prints `UNMEASURED` (never a hash) outside a git work tree so callers cannot mistake
# "could not measure" for "unchanged".
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo UNMEASURED
  exit 0
fi
listing() {
  git diff --name-only HEAD | LC_ALL=C sort | while IFS= read -r path; do
    if [ -e "$path" ]; then
      echo "$(git hash-object -- "$path") $path"
    else
      echo "DELETED $path"
    fi
  done
}
if [ "${1:-}" = "--files" ]; then
  listing
else
  listing | shasum -a 256 | cut -d' ' -f1
fi
