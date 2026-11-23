#!/usr/bin/env bash
# Sabotage proofs for INVENTORY M2 / step 2c ("unproven data does not publish; rebrands only through
# the named override list").
#
# A check without a proof does not count: every new check must FAIL at the shape of the real mistake
# and PASS on the current tree. Mutations are applied to a SCRATCH COPY, never the tracked tree.
#
#   S0  current tree passes: policy tests + the publisher-gate tests + lint (the control, run first)
#   S1  the lenient default comes back (DEFAULT_UNPROVEN -> warn)      -> strict-default tests fail
#   S2  an override skips the address anchor (admits on the name)      -> wrong-state/wrong-city test fails
#   S3  the policy fails OPEN (bad policy -> lenient, not refuse)      -> fail-closed test fails
#   S4  an override leaks to every CCN                                 -> other-CCN test fails
#   S5  the anchor overwrites a non-blank registry value               -> anchor-precedence test fails
#   S6  a dead override (one name word) is accepted at load            -> dead-override test fails
#   S7  an override is applied after a state/city contradiction        -> not-consulted-for-contradiction fails
#   S7b the file pin is ignored (override rides any file under the CCN) -> different-file test fails
#   S7c the attestation overwrites a name the file's header carries     -> not-rescued-by-attestation fails
#   S7d an override with no pinned file is accepted at load             -> must-be-pinned test fails
#   S8  the SHIPPED policy file is loosened to "warn"                  -> lint FAILS naming the policy, and
#                                                                         the shipped-policy test fails
#   S9  the strict default is edited out of the module                 -> lint FAILS naming DEFAULT_UNPROVEN
#
# Usage: bash scripts/prove_publish_policy.sh [proof-dir]
# Exit 0 only if every proof holds.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
PROOF_DIR="${1:-$REPO/.ship/m2c-proof}"
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

run_tests() { (cd "$1" && python3 -m unittest discover -s tests -p 'test_publish_gate*.py') > "$2" 2>&1; }

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
verdict "S0[tests on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "test_publish_gate*.py exit=$RC (expected 0)"
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

G=scripts/hl_publish_gate.py
mut lenient-default $G 'DEFAULT_UNPROVEN = UNPROVEN_REFUSE' 'DEFAULT_UNPROVEN = UNPROVEN_WARN' \
  "test_the_default_is_the_strict_policy" "test_name_without_address_refuses_by_default"
mut override-skips-anchor $G 'verdict = hl_admission.admit(ccn, _with_override(registry, ov), o_source)' \
  'verdict = hl_admission.Verdict(hl_admission.ACCEPT, "sabotage", evidence=("name only",))  # SABOTAGE' \
  "test_a_wrong_state_file_is_refused_even_with_the_override" "test_a_wrong_city_file_is_refused_even_with_the_override"
mut policy-fails-open $G '            return f"gate policy invalid, failing closed: {exc}", []' \
  '            policy = Policy(unproven=UNPROVEN_WARN)  # SABOTAGE: bad policy degrades to lenient' \
  "test_invalid_policy_file_refuses_even_a_true_owner"
mut override-leaks-to-all $G '    ov = overrides.get(ccn)' '    ov = next(iter(overrides.values()), None)  # SABOTAGE' \
  "test_the_override_does_not_extend_to_another_ccn"
mut anchor-overwrites $G '        if not str(row.get(key) or "").strip():' '        if True:  # SABOTAGE' \
  "test_the_anchor_never_replaces_a_non_blank_registry_value"
mut dead-override-accepted $G '            if len(set(hl_admission.tokens(n))) < 2:' '            if False:  # SABOTAGE' \
  "test_a_dead_override_is_rejected_at_load"
mut override-after-any-refusal $G '            and verdict.reason in _OVERRIDABLE' '            and True  # SABOTAGE: any refusal may be overridden' \
  "test_the_override_is_not_consulted_for_a_contradiction"
mut file-pin-ignored $G '            and canon_url and _norm_url(canon_url) == _norm_url(ov.source_url)):' \
  '            and True):  # SABOTAGE: the override rides any file under the CCN' \
  "test_a_different_file_under_the_same_ccn_gets_no_help"
mut attestation-overwrites-header $G '    if not str(src.get("hospital_name") or "").strip():' '    if True:  # SABOTAGE' \
  "test_a_name_that_matches_nothing_is_not_rescued_by_the_attestation"
mut unpinned-policy-accepted $G '        if not isinstance(url, str) or not re.match(r"^https?://\S+$", url.strip()):' \
  '        if False:  # SABOTAGE' \
  "test_override_must_be_pinned_to_a_file"

# ---------------------------------------------------------------- S8 / S9: the lint and the shipped file
S="$SCRATCH/s8"; fresh_scratch "$S"
mutate_file "$S" scripts/hl_publish_gate_policy.json '"unproven": "refuse"' '"unproven": "warn"' > "$PROOF_DIR/s8.apply.log" 2>&1
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/s8-lint-loosened-policy.log" 2>&1; RC=$?
expect_fail "S8[lint: shipped policy loosened to warn]" "$PROOF_DIR/s8-lint-loosened-policy.log" $RC \
  "hl_publish_gate_policy.json" "unproven" "refuse"
run_tests "$S" "$PROOF_DIR/s8-tests-loosened-policy.log"; RC=$?
expect_fail "S8[tests: shipped policy loosened to warn]" "$PROOF_DIR/s8-tests-loosened-policy.log" $RC \
  "test_it_is_strict"

S="$SCRATCH/s9"; fresh_scratch "$S"
mutate_file "$S" $G 'DEFAULT_UNPROVEN = UNPROVEN_REFUSE' 'DEFAULT_UNPROVEN = UNPROVEN_WARN' > "$PROOF_DIR/s9.apply.log" 2>&1
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/s9-lint-lenient-default.log" 2>&1; RC=$?
expect_fail "S9[lint: lenient default in the module]" "$PROOF_DIR/s9-lint-lenient-default.log" $RC \
  "DEFAULT_UNPROVEN" "UNPROVEN_REFUSE"

S="$SCRATCH/s9b"; fresh_scratch "$S"
rm "$S/scripts/hl_publish_gate_policy.json"
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/s9b-lint-policy-missing.log" 2>&1; RC=$?
expect_fail "S9b[lint: policy file removed]" "$PROOF_DIR/s9b-lint-policy-missing.log" $RC \
  "hl_publish_gate_policy.json is missing"

say "== $FAILS proof(s) failed =="
[ "$FAILS" = 0 ]
