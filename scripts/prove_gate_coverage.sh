#!/usr/bin/env bash
# Sabotage proofs for INVENTORY G0 ("one gate, no orphans").
#
# A check without a proof does not count: each new check below must FAIL at the shape of
# the real past mistake (a real commit) and PASS on the current tree.
#
#   P1  scripts/check_gate_coverage.py FAILS on the tree at each past-mistake commit
#       (d930135, 3bc2a10, 94bddbd) and names the orphaned regression tests, the missing
#       csp_audit.py gate and the split predeploy battery.
#   P2  it PASSES on the current tree.
#   P3  mutation proofs on the current tree: delete one wired test / the csp gate /
#       the observability lint / the fingerprint / the predeploy->verify link, or
#       swallow a test with `|| true` -> it FAILS, naming the removed thing.
#   P4  the tracked-tree fingerprint (verify.sh's hermeticity guard) DIFFERS after the three
#       tests that polluted the repo at 94bddbd (test_mrf_parse_field_limit,
#       test_mrf_parse_v3_tab_sniff, test_hl_wave_chunk) run against their 94bddbd
#       versions, and is IDENTICAL after the same three run from the current tree.
#
# Needs full git history (the commits above). Usage:
#   bash scripts/prove_gate_coverage.sh [proof-dir]
# Exit 0 only if every proof holds. Logs land in proof-dir (default ./.ship/g0-proof).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
PROOF_DIR="${1:-$REPO/.ship/g0-proof}"
mkdir -p "$PROOF_DIR"
PROOF_DIR="$(cd "$PROOF_DIR" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
FAILS=0
SUMMARY="$PROOF_DIR/SUMMARY.txt"
: > "$SUMMARY"

say() { echo "$*" | tee -a "$SUMMARY"; }
verdict() { # name expected-ok(0/1) message
  if [ "$2" = 1 ]; then say "PASS  $1 - $3"; else say "FAIL  $1 - $3"; FAILS=$((FAILS + 1)); fi
}

# Build the minimal skeleton the checker reads (verify.sh, package.json, vitest configs,
# and an empty placeholder for every test-shaped path) from a commit or from the working tree.
skeleton_from_commit() { # sha dest
  local sha="$1" dest="$2"
  mkdir -p "$dest"
  git -C "$REPO" ls-tree -r --name-only "$sha" | while IFS= read -r f; do
    case "$f" in
      scripts/verify.sh | package.json | vitest.*.config.mjs)
        mkdir -p "$dest/$(dirname "$f")"
        git -C "$REPO" show "$sha:$f" > "$dest/$f"
        ;;
      */test_*.py | test_*.py | *.test.ts | *.test.tsx | *.test.js | *.test.mjs)
        mkdir -p "$dest/$(dirname "$f")"
        : > "$dest/$f"
        ;;
    esac
  done
}
skeleton_from_worktree() { # dest
  local dest="$1"
  mkdir -p "$dest"
  git -C "$REPO" ls-files -- scripts/verify.sh package.json 'vitest.*.config.mjs' | while IFS= read -r f; do
    mkdir -p "$dest/$(dirname "$f")"
    cp "$REPO/$f" "$dest/$f"
  done
  git -C "$REPO" ls-files | while IFS= read -r f; do
    case "$f" in
      */test_*.py | test_*.py | *.test.ts | *.test.tsx | *.test.js | *.test.mjs)
        mkdir -p "$dest/$(dirname "$f")"
        : > "$dest/$f"
        ;;
    esac
  done
  # Tracked-but-new files (this PR's own) are in ls-files only once added; include untracked tests too.
  git -C "$REPO" ls-files --others --exclude-standard | while IFS= read -r f; do
    case "$f" in
      */test_*.py | test_*.py | *.test.ts | *.test.tsx | *.test.js | *.test.mjs)
        mkdir -p "$dest/$(dirname "$f")"
        : > "$dest/$f"
        ;;
    esac
  done
}
# The skeleton must carry the real verify.sh/package.json of the working tree (not the index copy).
overlay_worktree_gate_files() { # dest
  cp "$REPO/scripts/verify.sh" "$1/scripts/verify.sh"
  cp "$REPO/package.json" "$1/package.json"
}

run_checker() { # root logfile -> sets RC
  python3 "$REPO/scripts/check_gate_coverage.py" --root "$1" > "$2" 2>&1
  RC=$?
}

say "== G0 sabotage proofs $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(git rev-parse --short HEAD)+worktree =="

# ---- P1: the checker fails on every past-mistake tree --------------------------------------
for sha in d930135 3bc2a10 94bddbd; do
  if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
    verdict "P1[$sha]" 0 "commit not present in this clone (UNMEASURED, counted as failure)"
    continue
  fi
  D="$SCRATCH/p1-$sha"
  skeleton_from_commit "$sha" "$D"
  run_checker "$D" "$PROOF_DIR/p1-$sha.log"
  ok=1
  [ "$RC" = 1 ] || ok=0
  for needle in "ORPHANED TEST: scripts/test_mrf_parse_guard.py" \
    "ORPHANED TEST: scripts/test_r2_put_get.py" \
    "ORPHANED TEST: ops/wave/test_lessons_gate.py" \
    "MISSING GATE: verify.sh does not run csp_audit.py" \
    "SPLIT BATTERY"; do
    grep -qF "$needle" "$PROOF_DIR/p1-$sha.log" || { ok=0; say "      missing in output: $needle"; }
  done
  n=$(grep -c "ORPHANED TEST" "$PROOF_DIR/p1-$sha.log")
  verdict "P1[$sha]" "$ok" "exit=$RC, $n orphaned tests named, csp gate + split battery named"
done

# ---- P2: the current tree passes --------------------------------------------------------------
python3 "$REPO/scripts/check_gate_coverage.py" > "$PROOF_DIR/p2-current.log" 2>&1
RC=$?
verdict "P2[current tree]" "$([ "$RC" = 0 ] && echo 1 || echo 0)" "exit=$RC: $(tail -1 "$PROOF_DIR/p2-current.log")"

# ---- P3: mutations of the current tree each fail, naming what was removed -----------------------
mutate() { # name sed-expr-on-verify expected-needle  [package-mutation-python]
  local name="$1" expr="$2" needle="$3" pkgmut="${4:-}"
  local D="$SCRATCH/p3-$name"
  skeleton_from_worktree "$D"
  overlay_worktree_gate_files "$D"
  if [ -n "$expr" ]; then
    sed -E "$expr" "$REPO/scripts/verify.sh" > "$D/scripts/verify.sh"
    if cmp -s "$REPO/scripts/verify.sh" "$D/scripts/verify.sh"; then
      verdict "P3[$name]" 0 "mutation did not change verify.sh (instrument broken)"
      return
    fi
  fi
  if [ -n "$pkgmut" ]; then
    python3 - "$D/package.json" <<PYEOF
import json, sys
p = sys.argv[1]
d = json.load(open(p))
$pkgmut
json.dump(d, open(p, "w"))
PYEOF
  fi
  run_checker "$D" "$PROOF_DIR/p3-$name.log"
  if [ "$RC" = 1 ] && grep -qF "$needle" "$PROOF_DIR/p3-$name.log"; then
    verdict "P3[$name]" 1 "exit=1 and output names: $needle"
  else
    verdict "P3[$name]" 0 "exit=$RC; expected failure naming: $needle"
  fi
}
mutate drop-orphan-r2 '/scripts\/test_r2_put_get\.py/d' "ORPHANED TEST: scripts/test_r2_put_get.py"
mutate drop-orphan-lessons '/ops\/wave\/test_lessons_gate\.py/d' "ORPHANED TEST: ops/wave/test_lessons_gate.py"
mutate drop-orphan-concurrency '/test_hl_wave_concurrency\.py/d' "ORPHANED TEST: ops/wave/test_hl_wave_concurrency.py"
mutate drop-csp '/^"\$VERIFY_PYTHON" scripts\/csp_audit\.py$/d' "csp_audit.py"
mutate drop-observability '/^npm run lint:observability$/d' "lint:observability"
mutate drop-fingerprint-after '/^TREE_AFTER=/d' "after-battery tracked-tree fingerprint"
mutate drop-self-check '/scripts\/check_gate_coverage\.py/d' "check_gate_coverage.py"
mutate swallow-test 's#^("\$VERIFY_PYTHON" scripts/test_hl_edge\.py)$#\1 || true#' "swallows failure"
mutate split-predeploy '' "SPLIT BATTERY" \
  'd["scripts"]["predeploy"] = "python3 scripts/predeploy_audit.py && python3 scripts/csp_audit.py && npm run build"'

# ---- P4: the hermeticity fingerprint catches the 94bddbd test pollution -------------------------
POLLUTERS="tests/test_mrf_parse_field_limit.py tests/test_mrf_parse_v3_tab_sniff.py ops/wave/test_hl_wave_chunk.py"
pollute_run() { # label dir -> prints "same|DIFFERENT" and files changed, logs
  local label="$1" dir="$2"
  (
    cd "$dir" || exit 9
    git init -q . && git add -A && git -c user.email=g0@example.com -c user.name=g0 commit -q -m seed || exit 9
    before="$(bash scripts/tracked_tree_fingerprint.sh)"
    for t in $POLLUTERS; do python3 "$t" > /dev/null 2>&1 || { echo "polluter $t exited non-zero"; exit 8; }; done
    after="$(bash scripts/tracked_tree_fingerprint.sh)"
    echo "before=$before"
    echo "after=$after"
    git diff --stat HEAD
    [ "$before" = "$after" ] && echo RESULT=same || echo RESULT=DIFFERENT
  ) > "$PROOF_DIR/p4-$label.log" 2>&1
  tail -1 "$PROOF_DIR/p4-$label.log"
}
if git cat-file -e 94bddbd^{commit} 2>/dev/null; then
  OLD="$SCRATCH/p4-old"
  mkdir -p "$OLD"
  git archive 94bddbd tests scripts ops data/header_aliases.json | tar -x -C "$OLD"
  # the current fingerprint script is the instrument under test
  cp "$REPO/scripts/tracked_tree_fingerprint.sh" "$OLD/scripts/tracked_tree_fingerprint.sh"
  r="$(pollute_run old "$OLD")"
  verdict "P4[94bddbd tests]" "$([ "$r" = "RESULT=DIFFERENT" ] && echo 1 || echo 0)" "fingerprint after the three polluting tests: $r (expected DIFFERENT)"
else
  verdict "P4[94bddbd tests]" 0 "94bddbd not present (UNMEASURED, counted as failure)"
fi
NEW="$SCRATCH/p4-new"
mkdir -p "$NEW"
for p in tests scripts ops data/header_aliases.json; do
  mkdir -p "$NEW/$(dirname "$p")"
  cp -R "$REPO/$p" "$NEW/$(dirname "$p")/" 2>/dev/null
done
# drop caches so they cannot differ between before/after
find "$NEW" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
r="$(pollute_run new "$NEW")"
verdict "P4[current tests]" "$([ "$r" = "RESULT=same" ] && echo 1 || echo 0)" "fingerprint after the same three tests: $r (expected same)"

say "== $FAILS proof(s) failed =="
[ "$FAILS" = 0 ]
