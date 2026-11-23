#!/usr/bin/env bash
# Sabotage proofs for the tools/test-harness.mjs changes (INVENTORY G0 follow-through).
#
# Past mistake: PR #91 (94bddbd) removed the home page's #coverage-gap block. measure()
# silently widened its scope to the whole <main> and the gate went red on pristine main.
#
#   H1  the ORIGINAL harness on the 94bddbd tree FAILS ('Unmeasured filter/opacity'):
#       the red-on-main baseline this PR starts from.
#   H2  the NEW harness on the 94bddbd tree (anchor missing) FAILS BY NAME:
#       '#coverage-gap anchor missing', not a silent scope change.
#   H3  the NEW harness on the CURRENT tree PASSES (160 cells).
#
# (An animation wait was tried and dropped: with the scope restored the harness is green
# without it, so it had no proof of need. The opacity failure in H1 is a CONSEQUENCE of the
# widened scope, not a separate race.)
#
# Needs full git history and Chromium for playwright. Usage:
#   bash scripts/prove_harness_anchor.sh [proof-dir]
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
PROOF_DIR="${1:-$REPO/.ship/g0-proof}"
mkdir -p "$PROOF_DIR"
PROOF_DIR="$(cd "$PROOF_DIR" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
SUMMARY="$PROOF_DIR/harness-SUMMARY.txt"
: > "$SUMMARY"
FAILS=0
say() { echo "$*" | tee -a "$SUMMARY"; }
verdict() { if [ "$2" = 1 ]; then say "PASS  $1 - $3"; else say "FAIL  $1 - $3"; FAILS=$((FAILS + 1)); fi; }
has() { grep -qF -- "$1" "$2" && echo 1 || echo 0; }
failed_with() { # needle logfile -> 1 iff RC != 0 and the needle is in the log
  if [ "$RC" != 0 ] && [ "$(has "$1" "$2")" = 1 ]; then echo 1; else echo 0; fi
}

git cat-file -e 94bddbd^{commit} 2>/dev/null || { say "FAIL  94bddbd missing from this clone (UNMEASURED)"; exit 1; }
git archive 94bddbd | tar -x -C "$SCRATCH"
ln -s "$REPO/node_modules" "$SCRATCH/node_modules"
cp "$REPO/tools/test-harness.mjs" "$SCRATCH/new-harness.mjs.src"
cp "$SCRATCH/tools/test-harness.mjs" "$SCRATCH/old-harness.mjs.src"

build() { (cd "$SCRATCH" && node tools/build-faq-island.mjs > /dev/null 2>&1 && ./node_modules/.bin/vite build > /dev/null 2>&1); }
run_harness() { # source-file label -> sets RC
  cp "$1" "$SCRATCH/tools/test-harness.mjs"
  (cd "$SCRATCH" && node tools/test-harness.mjs > "$PROOF_DIR/harness-$2.log" 2>&1)
  RC=$?
}

say "== harness proofs $(date -u +%Y-%m-%dT%H:%M:%SZ) =="
build || { say "FAIL  build of the 94bddbd tree failed (UNMEASURED)"; exit 1; }

run_harness "$SCRATCH/old-harness.mjs.src" H1-old-harness-on-94bddbd
verdict "H1[original harness @94bddbd]" "$(failed_with 'Unmeasured filter/opacity' "$PROOF_DIR/harness-H1-old-harness-on-94bddbd.log")" "exit=$RC, expected failure naming Unmeasured filter/opacity"

run_harness "$SCRATCH/new-harness.mjs.src" H2-new-harness-on-94bddbd
verdict "H2[new harness @94bddbd]" "$(failed_with '#coverage-gap anchor missing' "$PROOF_DIR/harness-H2-new-harness-on-94bddbd.log")" "exit=$RC, expected failure naming #coverage-gap anchor missing"

# Current tree: overlay the working-tree home route, rebuild.
cp "$REPO/src/routes/home.tsx" "$SCRATCH/src/routes/home.tsx"
build || { say "FAIL  build of the current tree failed (UNMEASURED)"; exit 1; }
run_harness "$SCRATCH/new-harness.mjs.src" H3-new-harness-on-current
ok=0
if [ "$RC" = 0 ] && [ "$(has '"event":"harness.pass"' "$PROOF_DIR/harness-H3-new-harness-on-current.log")" = 1 ]; then ok=1; fi
verdict "H3[new harness @current]" "$ok" "exit=$RC: $(head -c 120 "$PROOF_DIR/harness-H3-new-harness-on-current.log")"

say "== $FAILS harness proof(s) failed =="
[ "$FAILS" = 0 ]
