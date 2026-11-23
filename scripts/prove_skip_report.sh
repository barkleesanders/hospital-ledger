#!/usr/bin/env bash
# Sabotage proofs for INVENTORY M2 / step 2d ("a publish run skips and reports refused hospitals
# instead of aborting on the first refusal; the gate policy is frozen").
#
# A check without a proof does not count: every property must FAIL at the shape of the real
# mistake and PASS on the current tree. Mutations are applied to a SCRATCH COPY, never the tracked
# tree.
#
#   S0   current tree passes: skip-report + cut-over + gate tests, and the lint (the control, first)
#   S1   decide_all stops at the first refusal                      -> mid-set tests fail
#   S1b  the publisher's main() returns before publishing when anything refused
#                                                                   -> planted-refused-mid-set test fails
#   S2   the refusal report is built from ALL decisions             -> accepted-in-report tests fail
#   S3   a raising guard propagates instead of refusing that item   -> raising-guard tests fail
#   S4   refused items are handed to the publish path               -> never-reach-publish test fails
#   S5   all-refused exits 0                                        -> exit-code tests fail
#   S5b  a run with refusals exits 0                                -> exit-code test fails
#   S6   a missing run module falls back to the old main            -> fails-closed test fails
#   S7   enumerate_store drops malformed keys                       -> full-set tests fail
#   S8   the quarantine guard is emptied                            -> quarantine-skip test fails
#   S9   the three frozen gate files differ from the merge base      -> frozen-policy check fails
#        (control first: byte-identical on the current tree)
#
# Usage: bash scripts/prove_skip_report.sh [proof-dir]
# Exit 0 only if every proof holds.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
PROOF_DIR="${1:-$REPO/.ship/m2d-proof}"
mkdir -p "$PROOF_DIR"
PROOF_DIR="$(cd "$PROOF_DIR" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
FAILS=0
SUMMARY="$PROOF_DIR/SUMMARY.txt"
: > "$SUMMARY"
say() { echo "$*" | tee -a "$SUMMARY"; }
verdict() { # name ok(0/1) message
  if [ "$2" = 1 ]; then say "PASS  $1 - $3"; else say "FAIL  $1 - $3"; FAILS=$((FAILS + 1)); fi
}

fresh_scratch() { # dest
  rm -rf "$1"; mkdir -p "$1"
  { git ls-files -z --cached --others --exclude-standard -- scripts tests; } \
    | tar --null -T - -cf - | tar -xf - -C "$1"
  ln -s "$REPO/db" "$1/db"
}

mutate_file() { # dest rel old new  (exact single replacement, or die)
  python3 -I - "$1/$2" "$3" "$4" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
if src.count(old) != 1:
    sys.exit(f"MUTATION DID NOT APPLY: {old!r} found {src.count(old)} times in {path}")
open(path, "w").write(src.replace(old, new))
PYEOF
}

run_tests() { (cd "$1" && python3 -m unittest discover -s tests -p 'test_publish_skip_report.py') > "$2" 2>&1; }

expect_fail() { # name log rc needle...
  local name="$1" log="$2" rc="$3"; shift 3
  if [ "$rc" = 0 ]; then verdict "$name" 0 "sabotaged code PASSED (the check is blind to this mistake)"; return; fi
  local miss=""
  for needle in "$@"; do grep -qF -- "$needle" "$log" || miss="$miss [$needle]"; done
  if [ -n "$miss" ]; then verdict "$name" 0 "failed, but the log does not name:$miss"; else
    verdict "$name" 1 "fails as required, naming: $*"; fi
}

# ---------------------------------------------------------------- S0 control
S="$SCRATCH/current"; fresh_scratch "$S"
run_tests "$S" "$PROOF_DIR/s0-current-tests.log"; RC=$?
verdict "S0[skip-report tests on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "test_publish_skip_report.py exit=$RC (expected 0)"
(cd "$S" && python3 -m unittest discover -s tests -p 'test_cutover_publisher_gate.py') > "$PROOF_DIR/s0-current-cutover-tests.log" 2>&1; RC=$?
verdict "S0[cut-over tests on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "test_cutover_publisher_gate.py exit=$RC (expected 0)"
python3 scripts/check_identity_invariants.py > "$PROOF_DIR/s0-current-lint.log" 2>&1; RC=$?
verdict "S0[lint on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "check_identity_invariants.py exit=$RC (expected 0)"

mut() { # name rel old new needle...
  local name="$1" rel="$2" old="$3" new="$4"; shift 4
  S="$SCRATCH/s-$name"; fresh_scratch "$S"
  if ! mutate_file "$S" "$rel" "$old" "$new" > "$PROOF_DIR/s-$name.apply.log" 2>&1; then
    say "UNMEASURED  S[$name] - mutation did not apply: $(cat "$PROOF_DIR/s-$name.apply.log")"; FAILS=$((FAILS + 1)); return
  fi
  run_tests "$S" "$PROOF_DIR/s-$name.log"; local rc=$?
  expect_fail "S[$name]" "$PROOF_DIR/s-$name.log" $rc "$@"
}

R=scripts/hl_publish_run.py
T=scripts/cutover_publisher_gate.py

mut abort-at-first-refusal-in-loop $R '        out.append(decide(ccn, guards, ids))' \
  $'        out.append(decide(ccn, guards, ids))\n        if not out[-1].accepted:  # SABOTAGE: stop at the first refusal\n            break' \
  "test_a_refused_item_mid_set_does_not_stop_later_items" \
  "test_a_planted_refused_item_mid_set_does_not_stop_later_accepted_items"
mut abort-in-publisher-main $T '    refused = len(decisions) - len(accepted)' \
  $'    refused = len(decisions) - len(accepted)\n    if refused:  # SABOTAGE: abort the run when anything was refused\n        return 2' \
  "test_a_planted_refused_item_mid_set_does_not_stop_later_accepted_items"
mut accepted-leak-into-refusals $R '    refused = [d for d in decisions if not d.accepted]' \
  '    refused = list(decisions)  # SABOTAGE: every decision lands in the refusal report' \
  "test_the_refusal_report_never_contains_an_accepted_item" \
  "test_a_planted_accepted_item_never_appears_in_the_refusal_report"
mut raising-guard-propagates $R '        except Exception as exc:  # noqa: BLE001 - fail closed per item, never abort the run' \
  '        except KeyError as exc:  # SABOTAGE: only one exception type is contained' \
  "test_a_guard_that_raises_refuses_only_that_item" \
  "test_a_malformed_guard_result_refuses_the_item"
mut refused-items-reach-publish $T '                publish_rc = _publish(accepted, dry_run)' \
  '                publish_rc = _publish(ccns, dry_run)  # SABOTAGE: every requested item, refused or not' \
  "test_refused_items_never_reach_the_publish_path"
mut all-refused-exits-zero $R $'    if accepted == 0:\n        return RC_NOTHING_ACCEPTED' \
  $'    if accepted == 0:\n        return RC_OK  # SABOTAGE' \
  "test_exit_code_contract" "test_every_refused_run_exits_nonzero_and_publishes_nothing"
mut refusals-exit-zero $R '    return RC_COMPLETE_WITH_REFUSALS if refused else RC_OK' \
  '    return RC_OK  # SABOTAGE: a run with refusals looks clean' \
  "test_exit_code_contract"
mut missing-run-module-falls-back $T $'        log(f"FATAL: skip-and-report module unavailable, failing closed ({type(exc).__name__}: {exc})")\n        return 2' \
  $'        log(f"FATAL: skip-and-report module unavailable ({type(exc).__name__}: {exc})")\n        return _legacy_main()  # SABOTAGE' \
  "test_a_missing_run_module_fails_closed_and_never_publishes"
mut enumerate-drops-malformed $R '                      if f.endswith(".json.gz") and os.path.isfile(os.path.join(store, f)))' \
  '                      if f.endswith(".json.gz") and f[:6].isdigit() and os.path.isfile(os.path.join(store, f)))  # SABOTAGE' \
  "test_enumerate_store_is_sorted_complete_and_drops_nothing_malformed" \
  "test_all_enumerates_the_whole_store_including_malformed_keys_and_reports_them"
mut quarantine-emptied $T 'QUARANTINED = ("150042",)' 'QUARANTINED = ()  # SABOTAGE' \
  "test_all_enumerates_the_whole_store_including_malformed_keys_and_reports_them"

# ---------------------------------------------------------------- S9: the gate policy is frozen
FROZEN="scripts/hl_publish_gate.py scripts/hl_admission.py scripts/hl_publish_gate_policy.json"
BASE="${FROZEN_BASE:-$(git merge-base HEAD origin/main 2>/dev/null || true)}"
if [ -z "$BASE" ]; then
  say "UNMEASURED  S9[frozen gate files] - no merge base with origin/main (set FROZEN_BASE)"; FAILS=$((FAILS + 1))
else
  # shellcheck disable=SC2086
  git diff --quiet "$BASE" -- $FROZEN; RC=$?
  verdict "S9[control: gate files byte-identical to merge base ${BASE:0:9}]" "$([ $RC = 0 ] && echo 1 || echo 0)" \
    "git diff --quiet $BASE -- $FROZEN exit=$RC (expected 0)"
  S="$SCRATCH/s9"; fresh_scratch "$S"
  mutate_file "$S" scripts/hl_publish_gate_policy.json '"unproven": "refuse"' '"unproven": "warn"' > "$PROOF_DIR/s9.apply.log" 2>&1
  if cmp -s "$S/scripts/hl_publish_gate_policy.json" <(git show "$BASE:scripts/hl_publish_gate_policy.json"); then
    verdict "S9[a loosened policy is detected as different from the base]" 0 "the mutated policy still equals the base (the check is blind)"
  else
    verdict "S9[a loosened policy is detected as different from the base]" 1 "cmp against the base copy reports a difference"
  fi
fi

say "== $FAILS proof(s) failed =="
[ "$FAILS" = 0 ]
