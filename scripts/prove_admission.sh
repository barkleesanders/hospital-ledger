#!/usr/bin/env bash
# Sabotage proofs for INVENTORY M2 / step 2a ("admission lives in the repo").
#
# A check without a proof does not count: every new check must FAIL at the shape of the real
# past mistake and PASS on the current tree. Mutations are applied to a SCRATCH COPY, never to
# the tracked tree.
#
#   P1  batch-000 shape (`grade_pair`, 2026-11-02): replace admit()'s address anchoring with a
#       0.5 name-token-ratio test -> tests/test_admission.py FAILS naming the wrong-pair test
#       and CCNs; the lint FAILS on a difflib/SequenceMatcher/.ratio() shape (message names
#       batch-000) and on a raw ACCEPT verdict row written outside hl_admission.
#   P2  b3e2df9 shape: stop case-normalising candidate names -> the uppercase-registry tests
#       FAIL. Also replays the PRE-FIX processor (b3e2df9^) on the true-owner controls.
#   P3  W13 shape, measured: the PRE-CHANGE identity_gate (base commit) against the 14 frozen
#       wrong pairs + 7 true-owner controls, versus the new gate. Records what the old gate
#       wrongly VERIFIED. This is a measurement, not an assertion of failure.
#   P4  mutation: drop each rule (name necessity, state veto, city veto, address anchor,
#       evidence-required) one at a time -> at least one named test fails each time.
#   P5  current tree passes: tests + lint.
#
# Usage: bash scripts/prove_admission.sh [proof-dir] [base-commit]
# Exit 0 only if every proof holds. Needs git history for b3e2df9^ and the base commit.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
PROOF_DIR="${1:-$REPO/.ship/m2-proof}"
BASE="${2:-436de54}"
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

# Fresh scratch copy of the files the admission tests read (current working tree).
fresh_scratch() { # dest
  rm -rf "$1"; mkdir -p "$1"
  { git ls-files -z --cached --others --exclude-standard -- scripts tests; } \
    | tar --null -T - -cf - | tar -xf - -C "$1"
  ln -s "$REPO/db" "$1/db"
}

# mutate <dest> <file-in-dest> <python-expr-old> <python-expr-new>: exact single replacement, or die.
mutate_file() { # dest rel old new
  python3 -I - "$1/$2" "$3" "$4" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
if src.count(old) != 1:
    sys.exit(f"MUTATION DID NOT APPLY: {old!r} found {src.count(old)} times in {path}")
open(path, "w").write(src.replace(old, new))
PYEOF
}

run_tests() { # dest log
  (cd "$1" && python3 -m unittest tests.test_admission) > "$2" 2>&1
}

expect_fail() { # name log needle...  (the run must have FAILED and the log must name each needle)
  local name="$1" log="$2" rc="$3"; shift 3
  if [ "$rc" = 0 ]; then verdict "$name" 0 "sabotaged code PASSED (the check is blind to this mistake)"; return; fi
  local miss=""
  for needle in "$@"; do grep -qF -- "$needle" "$log" || miss="$miss [$needle]"; done
  if [ -n "$miss" ]; then verdict "$name" 0 "failed, but the log does not name:$miss"; else
    verdict "$name" 1 "fails as required, naming: $*"; fi
}

# ---------------------------------------------------------------- P5 (first: the control)
S="$SCRATCH/current"; fresh_scratch "$S"
run_tests "$S" "$PROOF_DIR/p5-current-tests.log"; RC=$?
verdict "P5[tests on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "tests/test_admission.py exit=$RC (expected 0)"
python3 scripts/check_identity_invariants.py > "$PROOF_DIR/p5-current-lint.log" 2>&1; RC=$?
verdict "P5[lint on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "check_identity_invariants.py exit=$RC (expected 0)"

# ---------------------------------------------------------------- P1 batch-000 shape
S="$SCRATCH/p1"; fresh_scratch "$S"
python3 -I - "$S/scripts/hl_admission.py" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
# Re-introduce the batch-000 shape: admission = token-overlap ratio >= 0.5, nothing else.
src += '''

def admit(ccn, registry, source, holds=None, quarantined=None):  # SABOTAGE: grade_pair shape
    a, b = set(tokens(source.get("hospital_name"))), set(tokens((registry or {}).get("name")))
    ratio = len(a & b) / max(len(a), len(b), 1)
    if ratio >= 0.5:
        return Verdict(ACCEPT, R_ANCHORED, evidence=(f"token ratio {ratio:.2f}",))
    return Verdict(REFUSE, R_NAME_MISMATCH)
'''
open(path, "w").write(src)
PYEOF
run_tests "$S" "$PROOF_DIR/p1-grade-pair-tests.log"; RC=$?
expect_fail "P1[grade_pair shape -> wrong-pair test]" "$PROOF_DIR/p1-grade-pair-tests.log" $RC \
  "test_every_wrong_pair_is_refused_for_its_recorded_reason" "known-wrong pairs admitted"

S="$SCRATCH/p1lint"; fresh_scratch "$S"
python3 -I - "$S/scripts/hl_admission.py" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
src = src.replace("import re\n", "import re\nimport difflib\n", 1)
src += '''

def _sabotage_grade_pair(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.5
'''
open(path, "w").write(src)
PYEOF
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/p1-lint-sequencematcher.log" 2>&1; RC=$?
expect_fail "P1[lint: SequenceMatcher/.ratio() shape]" "$PROOF_DIR/p1-lint-sequencematcher.log" $RC \
  "batch-000" "2026-11-02" "hl_admission.admit" "SequenceMatcher"

S="$SCRATCH/p1raw"; fresh_scratch "$S"
printf '%s\n' 'def write(ccn, url, d, f):' '    f.write(f"{ccn}\t{url}\tACCEPT\t{d}\n")' > "$S/scripts/sabotage_writer.py"
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/p1-lint-raw-accept.log" 2>&1; RC=$?
expect_fail "P1[lint: raw ACCEPT row outside hl_admission]" "$PROOF_DIR/p1-lint-raw-accept.log" $RC \
  "raw ACCEPT verdict literal" "sabotage_writer.py" "hl_admission.accept_row"

# ---------------------------------------------------------------- P2 b3e2df9 shape
S="$SCRATCH/p2"; fresh_scratch "$S"
mutate_file "$S" scripts/hl_admission.py \
  '        ct = set(tokens(cand))' \
  '        ct = set(re.findall(r"[a-z0-9]+", cand or ""))  # SABOTAGE: b3e2df9 (candidate not lowered)'
run_tests "$S" "$PROOF_DIR/p2-uppercase-tests.log"; RC=$?
expect_fail "P2[b3e2df9 shape: candidate not case-normalised]" "$PROOF_DIR/p2-uppercase-tests.log" $RC \
  "test_uppercase_registry_name_matches_mixed_case_file"

# P2b: the PRE-FIX processor (b3e2df9^) replayed on the true-owner controls (pure import, no side effects).
git show "b3e2df9^:scripts/process_submissions.py" > "$SCRATCH/ps_prefix_b3e2df9.py" 2>"$PROOF_DIR/p2b-git.log" || true
git show "$BASE:scripts/process_submissions.py" > "$SCRATCH/ps_base.py" 2>>"$PROOF_DIR/p2b-git.log" || true

cat > "$SCRATCH/replay.py" <<'PYEOF'
"""Replay the frozen fixture through an OLD process_submissions.identity_gate (path argv[1]) with
the b3e2df9-faithful publish_mini double (candidates are NOT lowercased by the helper), and through
the current repo gate. Prints one line per pair plus tallies."""
import importlib.util, json, os, re, sqlite3, sys

repo, old_path, label = sys.argv[1], sys.argv[2], sys.argv[3]
fx = json.load(open(os.path.join(repo, "tests/fixtures/identity/known_wrong_pairs.json")))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

class PM:  # publish_mini._name_consistent, copied faithfully (tests/test_submission_processor.py)
    @staticmethod
    def _name_consistent(header, candidates):
        ht = set(re.findall(r"[a-z0-9]+", (header or "").lower()))
        if len(ht) < 2: return False
        for cand in candidates:
            ct = set(re.findall(r"[a-z0-9]+", cand or ""))
            if len(ct) >= 2 and (ct <= ht or ht <= ct): return True
        return False

def make_db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE hospitals(ccn TEXT PRIMARY KEY, name TEXT, address TEXT, city TEXT, state TEXT, zip TEXT)")
    db.execute("CREATE TABLE mrf_seed(ccn TEXT, entity_name_legal TEXT, entity_name_common TEXT, mrf_url TEXT, mrf_page TEXT, state TEXT)")
    for p in fx["wrong_pairs"] + fx["true_owner_controls"]:
        r = p["registry"]
        db.execute("INSERT INTO hospitals VALUES (?,?,?,?,?,?)", (p["ccn"], r["name"], "", r["city"], r["state"], r["zip"]))
    return db

sys.path.insert(0, os.path.join(repo, "scripts"))
ps = load("ps_" + label, old_path)
db = make_db()
def src(p):
    s = dict(p["source"])
    if p.get("license"): s["license"] = p["license"]
    return s
def gate(p):
    return ps.identity_gate(src(p), "claimed", db, p["ccn"], PM)[0]

wrong_verified = []
for p in fx["wrong_pairs"]:
    v = gate(p)
    print(f"{label} WRONG   {p['ccn']} {p['registry']['name'][:34]:34} <- file '{p['source']['hospital_name'][:40]}': {v}")
    if v == "VERIFIED": wrong_verified.append(p["ccn"])
owners_verified = 0
for c in fx["true_owner_controls"]:
    v = gate(c)
    print(f"{label} OWNER   {c['ccn']} {c['registry']['name'][:34]:34} <- file '{c['source']['hospital_name'][:40]}': {v}")
    owners_verified += v == "VERIFIED"
print(f"{label} TALLY wrong_pairs_wrongly_VERIFIED={len(wrong_verified)}/{len(fx['wrong_pairs'])} {wrong_verified} "
      f"true_owners_VERIFIED={owners_verified}/{len(fx['true_owner_controls'])}")
PYEOF
python3 -I "$SCRATCH/replay.py" "$REPO" "$SCRATCH/ps_prefix_b3e2df9.py" "b3e2df9-prefix" > "$PROOF_DIR/p2b-prefix-replay.log" 2>&1; RC=$?
if [ $RC = 0 ]; then
  grep -q "true_owners_VERIFIED=0/7" "$PROOF_DIR/p2b-prefix-replay.log" \
    && verdict "P2b[b3e2df9^ processor]" 1 "pre-fix processor VERIFIES 0/7 true owners (uppercase registry names never matched): $(grep TALLY "$PROOF_DIR/p2b-prefix-replay.log")" \
    || verdict "P2b[b3e2df9^ processor]" 0 "pre-fix replay did not show the uppercase failure: $(grep TALLY "$PROOF_DIR/p2b-prefix-replay.log")"
else
  say "UNMEASURED  P2b[b3e2df9^ processor] - replay could not run (exit=$RC); see p2b-prefix-replay.log. Not counted as a pass."
  FAILS=$((FAILS + 1))
fi

# ---------------------------------------------------------------- P3 W13 shape, measured
python3 -I "$SCRATCH/replay.py" "$REPO" "$SCRATCH/ps_base.py" "base-$BASE" > "$PROOF_DIR/p3-base-gate-replay.log" 2>&1; RC=$?
python3 -I "$SCRATCH/replay.py" "$REPO" "$REPO/scripts/process_submissions.py" "new" > "$PROOF_DIR/p3-new-gate-replay.log" 2>&1; RC2=$?
if [ $RC = 0 ] && [ $RC2 = 0 ]; then
  say "MEASURED  P3[W13 pairs, base gate]: $(grep TALLY "$PROOF_DIR/p3-base-gate-replay.log")"
  say "MEASURED  P3[W13 pairs, new gate ]: $(grep TALLY "$PROOF_DIR/p3-new-gate-replay.log")"
  grep -q "wrong_pairs_wrongly_VERIFIED=0/14" "$PROOF_DIR/p3-new-gate-replay.log" \
    && grep -q "true_owners_VERIFIED=7/7" "$PROOF_DIR/p3-new-gate-replay.log" \
    && verdict "P3[new gate]" 1 "0/14 wrong pairs VERIFIED, 7/7 true owners VERIFIED" \
    || verdict "P3[new gate]" 0 "new gate does not refuse all wrong pairs / verify all owners"
  BASE_WRONG="$(grep TALLY "$PROOF_DIR/p3-base-gate-replay.log" | sed -E 's/.*wrongly_VERIFIED=([0-9]+)\/.*/\1/')"
  verdict "P3[base gate fails at the past mistake]" "$([ "${BASE_WRONG:-0}" -ge 1 ] && echo 1 || echo 0)" \
    "base gate wrongly VERIFIED ${BASE_WRONG:-?} of the 14 known-wrong pairs (>=1 required for this to be a proof)"
else
  say "UNMEASURED  P3 - replay could not run (base rc=$RC, new rc=$RC2); see logs. Not counted as a pass."
  FAILS=$((FAILS + 1))
fi

# ---------------------------------------------------------------- P4 mutation, one rule at a time
mut() { # name old new needle...
  local name="$1" old="$2" new="$3"; shift 3
  S="$SCRATCH/p4-$name"; fresh_scratch "$S"
  if ! mutate_file "$S" scripts/hl_admission.py "$old" "$new" > "$PROOF_DIR/p4-$name.apply.log" 2>&1; then
    say "UNMEASURED  P4[$name] - mutation did not apply: $(cat "$PROOF_DIR/p4-$name.apply.log")"; FAILS=$((FAILS + 1)); return
  fi
  run_tests "$S" "$PROOF_DIR/p4-$name.log"; local rc=$?
  expect_fail "P4[drop $name]" "$PROOF_DIR/p4-$name.log" $rc "$@"
}
mut name-necessity    '    if not name_consistent(file_name, names):' '    if False:  # SABOTAGE' \
  "test_state_and_city_equal_still_needs_the_name"
mut state-veto        '    if f_state and reg_state and f_state != reg_state:' '    if False:  # SABOTAGE' \
  "test_every_wrong_pair_is_refused_for_its_recorded_reason"
mut city-veto         '    if f_city and reg_city and not city_eq and not zip_eq:' '    if False:  # SABOTAGE' \
  "test_name_is_necessary_but_never_sufficient"
mut address-anchor    '    if state_ok and (city_eq or zip_eq):' '    if state_ok:  # SABOTAGE' \
  "test_state_only_is_unproven"
mut evidence-required '        if self.outcome == ACCEPT and not self.evidence:' '        if False:  # SABOTAGE' \
  "test_accept_without_evidence_cannot_be_constructed"

say "== $FAILS proof(s) failed =="
[ "$FAILS" = 0 ]
