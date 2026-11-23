#!/usr/bin/env bash
# Sabotage proofs for INVENTORY M2 / step 2b ("the publisher-facing gate lives in the repo").
#
# A check without a proof does not count: every new check must FAIL at the shape of the real past
# mistake and PASS on the current tree. Mutations are applied to a SCRATCH COPY, never the tracked tree.
#
#   Q1  W13 / batch-000 shape, MEASURED against the production publisher: the Mini's
#       publish_mini.identity_gate (read-only import) vs the repo gate on the 14 frozen wrong pairs
#       and the 7 true owners, with production-shaped canonical heads built from the fixture's
#       verbatim header_quote (street included). Production numbers are informational (it is not
#       this repo's code); the repo gate must refuse 14/14 and pass 7/7.
#   Q2  production regex shape: what publish_mini._address_cities_states returns on a street-less and
#       a spelled-out-state address (measured), and the repo parser mutated to production's
#       leading-comma shape -> the street-less test FAILS.
#   Q3  mutation, one rule at a time (hold, fail-closed holds, R0, EIN, REFUSE blocks, strict
#       policy, name-necessity) -> a named test fails each time.
#   Q4  lint sabotage: a standalone identity_gate with its own logic; difflib in the publisher
#       gate; the publisher gate removed -> the lint FAILS naming the fix.
#   Q5  current tree passes: tests + lint.
#
# Usage: bash scripts/prove_publish_gate.sh [proof-dir]
# Exit 0 only if every proof holds. Q1's production half needs ~/hl-fleet/publish_mini.py; without
# it that line is UNMEASURED (counted as a failure of the run, never as a pass).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
PROOF_DIR="${1:-$REPO/.ship/m2b-proof}"
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

run_tests() { (cd "$1" && python3 -m unittest tests.test_publish_gate) > "$2" 2>&1; }

expect_fail() { # name log rc needle...
  local name="$1" log="$2" rc="$3"; shift 3
  if [ "$rc" = 0 ]; then verdict "$name" 0 "sabotaged code PASSED (the check is blind to this mistake)"; return; fi
  local miss=""
  for needle in "$@"; do grep -qF -- "$needle" "$log" || miss="$miss [$needle]"; done
  if [ -n "$miss" ]; then verdict "$name" 0 "failed, but the log does not name:$miss"; else
    verdict "$name" 1 "fails as required, naming: $*"; fi
}

# ---------------------------------------------------------------- Q5 (first: the control)
S="$SCRATCH/current"; fresh_scratch "$S"
run_tests "$S" "$PROOF_DIR/q5-current-tests.log"; RC=$?
verdict "Q5[tests on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "tests/test_publish_gate.py exit=$RC (expected 0)"
python3 scripts/check_identity_invariants.py > "$PROOF_DIR/q5-current-lint.log" 2>&1; RC=$?
verdict "Q5[lint on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "check_identity_invariants.py exit=$RC (expected 0)"

# ---------------------------------------------------------------- Q1/Q2 measured against production
cat > "$SCRATCH/replay_prod.py" <<'PYEOF'
"""Read-only: the Mini's production publish_mini.identity_gate vs the repo gate, on production-shaped
canonical heads built from the fixture's verbatim header_quote. holds/verdicts are EMPTY on purpose so
only the identity logic is measured (the real b83/b84 REJECT rows and adjudicated_holds.txt would refuse
these pairs by URL/hold and say nothing about identity)."""
import gzip, hashlib, importlib.util, json, os, re, sys, tempfile

repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, "scripts"))
import hl_publish_gate as pg
fx = json.load(open(os.path.join(repo, "tests/fixtures/identity/known_wrong_pairs.json")))
prod_path = os.path.expanduser("~/hl-fleet/publish_mini.py")
print("production publish_mini.py sha256", hashlib.sha256(open(prod_path, "rb").read()).hexdigest())
spec = importlib.util.spec_from_file_location("publish_mini_prod", prod_path)
pm = importlib.util.module_from_spec(spec); spec.loader.exec_module(pm)

def quoted(q, key):
    m = re.search(key + r"""\s+(?:'([^']*)'|"([^"]*)")""", q)
    return (m.group(1) or m.group(2)) if m else None

tmp = tempfile.mkdtemp(prefix="q1-")
store, verdicts = os.path.join(tmp, "store"), os.path.join(tmp, "verdicts")
os.makedirs(store); os.makedirs(verdicts)
holds = os.path.join(tmp, "holds.txt"); open(holds, "w").close()

def head_of(p):
    q = p["header_quote"]
    si = {"hospital_name": quoted(q, "hospital_name"), "hospital_address": quoted(q, "hospital_address")}
    if quoted(q, "location_name"): si["location_name"] = quoted(q, "location_name")
    return {"source_url": f"https://example.invalid/{p['ccn']}_file.json", "source_identity": si}

def prod(p):
    h = head_of(p)
    body = json.dumps({"ccn": p["ccn"], "hospital_name": h["source_identity"]["hospital_name"],
                       **h})[:-1] + ', "items": []}'
    with gzip.open(os.path.join(store, f"{p['ccn']}.json.gz"), "wt") as f: f.write(body)
    r = p["registry"]
    pm._REGISTRY_CACHE[p["ccn"]] = {"name": r["name"], "address": "", "city": r["city"], "state": r["state"], "zip": r["zip"]}
    return pm.identity_gate(p["ccn"], store_dir=store, verdicts_dir=verdicts, holds_path=holds)[0]

def repo_gate(p):
    return pg.identity_gate(p["ccn"], head_of(p), dict(p["registry"]), holds=set())[0]

pw, rw, po, ro = [], [], [], []
for p in fx["wrong_pairs"]:
    a, b = prod(p), repo_gate(p)
    print(f"WRONG {p['ccn']} {p['registry']['name'][:30]:30} <- {head_of(p)['source_identity']['hospital_name'][:34]:34} | production: {'REFUSED' if a else 'PASSED '} | repo: {'REFUSED' if b else 'PASSED '}")
    if not a: pw.append(p["ccn"])
    if not b: rw.append(p["ccn"])
for c in fx["true_owner_controls"]:
    a, b = prod(c), repo_gate(c)
    print(f"OWNER {c['ccn']} {c['registry']['name'][:30]:30} <- {head_of(c)['source_identity']['hospital_name'][:34]:34} | production: {'REFUSED' if a else 'PASSED '} | repo: {'REFUSED' if b else 'PASSED '}")
    if a: po.append(c["ccn"])
    if b: ro.append(c["ccn"])
print(f"TALLY production wrong_pairs_PASSED={len(pw)}/14 {pw} owners_REFUSED={len(po)}/7 {po}")
print(f"TALLY repo       wrong_pairs_PASSED={len(rw)}/14 {rw} owners_REFUSED={len(ro)}/7 {ro}")
for s in ("Alton, IL 62002", "One Memorial Drive, Alton, IL 62202", "1325 N. Highland, Aurora, Illinois 60506",
          "150 New Providence Rd, Mountainside, NJ 07092 | 200 Somerset St., New Brunswick, NJ 08901"):
    print(f"ADDR {s!r}: production _address_cities_states -> {pm._address_cities_states(s)} | repo parse_addresses -> "
          f"{[(a['city'], a['state']) for a in pg.parse_addresses(s)]}")
PYEOF
if [ -f "$HOME/hl-fleet/publish_mini.py" ]; then
  python3 -I "$SCRATCH/replay_prod.py" "$REPO" > "$PROOF_DIR/q1-production-vs-repo.log" 2>&1; RC=$?
  if [ $RC = 0 ]; then
    say "MEASURED  Q1[production publisher, 14 wrong pairs + 7 owners]: $(grep 'TALLY production' "$PROOF_DIR/q1-production-vs-repo.log")"
    say "MEASURED  Q1[repo gate, same heads                         ]: $(grep 'TALLY repo' "$PROOF_DIR/q1-production-vs-repo.log")"
    grep -q "TALLY repo       wrong_pairs_PASSED=0/14 \[\] owners_REFUSED=0/7 \[\]" "$PROOF_DIR/q1-production-vs-repo.log" \
      && verdict "Q1[repo gate]" 1 "0/14 wrong pairs would publish, 0/7 true owners refused" \
      || verdict "Q1[repo gate]" 0 "repo gate does not refuse all wrong pairs / pass all owners"
    PW="$(grep 'TALLY production' "$PROOF_DIR/q1-production-vs-repo.log" | sed -E 's/.*wrong_pairs_PASSED=([0-9]+)\/.*/\1/')"
    verdict "Q1[production fails at the past mistake]" "$([ "${PW:-0}" -ge 1 ] && echo 1 || echo 0)" \
      "production passes ${PW:-?} of the 14 known-wrong pairs (>=1 required for this to be a proof)"
    say "MEASURED  Q2[address shapes]:"; grep '^ADDR ' "$PROOF_DIR/q1-production-vs-repo.log" | sed 's/^/             /' | tee -a "$SUMMARY"
  else
    say "UNMEASURED  Q1/Q2 - production replay could not run (exit=$RC); see q1-production-vs-repo.log. Not counted as a pass."
    FAILS=$((FAILS + 1))
  fi
else
  say "UNMEASURED  Q1/Q2 - ~/hl-fleet/publish_mini.py not present on this machine. Not counted as a pass."
  FAILS=$((FAILS + 1))
fi

# ---------------------------------------------------------------- Q2 mutation: production's regex shape
mut() { # name old new needle...
  local name="$1" old="$2" new="$3"; shift 3
  S="$SCRATCH/q-$name"; fresh_scratch "$S"
  if ! mutate_file "$S" scripts/hl_publish_gate.py "$old" "$new" > "$PROOF_DIR/q-$name.apply.log" 2>&1; then
    say "UNMEASURED  Q[$name] - mutation did not apply: $(cat "$PROOF_DIR/q-$name.apply.log")"; FAILS=$((FAILS + 1)); return
  fi
  run_tests "$S" "$PROOF_DIR/q-$name.log"; local rc=$?
  expect_fail "Q[$name]" "$PROOF_DIR/q-$name.log" $rc "$@"
}
mut leading-comma-regex 'r"(?:^|,)\s*([^,|]+?)\s*,\s*([A-Za-z]{2}|"' 'r",\s*([^,|]+?)\s*,\s*([A-Za-z]{2}|"' \
  "test_street_less_address_still_yields_a_location"
mut no-spelled-out-state '    return _STATES.get(t.lower(), t.upper())' '    return t.upper()' \
  "test_spelled_out_state"

# ---------------------------------------------------------------- Q3 mutation, one rule at a time
mut drop-hold          '    if ccn in set(holds):' '    if False:' \
  "test_a_hold_beats_clean_evidence"
mut fail-open-holds    '    if holds is None:
        return "adjudicated' '    holds = set() if holds is None else holds
    if False:
        return "adjudicated' \
  "test_unreadable_holds_refuses_everything"
mut drop-r0            '            if verdict == "REJECT" and url and _norm_url(url) == cu:' '            if False:' \
  "test_exact_rejected_file_is_refused"
mut drop-ein           '    if file_ein and win_ein and file_ein != win_ein:' '    if False:' \
  "test_ein_contradiction_is_refused"
mut refuse-not-blocking '    if verdict.outcome == hl_admission.REFUSE:' '    if False:' \
  "test_every_wrong_pair_is_refused" "known-wrong pairs would PUBLISH"
mut strict-is-noop     '            return f"identity unproven: {verdict.detail}", warnings' '            pass' \
  "test_name_without_address_refuses_when_strict"
mut production-name-rule 'verdict = hl_admission.admit(ccn, registry, source_from_identity(si))' \
  'verdict = hl_admission.admit(ccn, dict(registry or {}, name=source_from_identity(si)["hospital_name"] or (registry or {}).get("name")), source_from_identity(si))  # SABOTAGE: name never separates a same-state-and-city pair' \
  "test_state_and_city_equal_pairs_are_refused_on_the_name"

# ---------------------------------------------------------------- Q4 lint sabotage
S="$SCRATCH/q4a"; fresh_scratch "$S"
printf '%s\n' 'def identity_gate(ccn, head, registry):' '    return None if head else "no head"' > "$S/scripts/sabotage_gate.py"
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/q4-lint-second-gate.log" 2>&1; RC=$?
expect_fail "Q4[lint: a second, independent identity_gate]" "$PROOF_DIR/q4-lint-second-gate.log" $RC \
  "defines \`identity_gate\` without importing hl_admission" "sabotage_gate.py" "hl_publish_gate.py"

S="$SCRATCH/q4b"; fresh_scratch "$S"
mutate_file "$S" scripts/hl_publish_gate.py 'import os
import re' 'import difflib
import os
import re' > "$PROOF_DIR/q4-difflib.apply.log" 2>&1
printf '%s\n' '' 'def _sabotage(a, b):' '    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.5' >> "$S/scripts/hl_publish_gate.py"
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/q4-lint-publisher-difflib.log" 2>&1; RC=$?
expect_fail "Q4[lint: similarity matching in the publisher gate]" "$PROOF_DIR/q4-lint-publisher-difflib.log" $RC \
  "batch-000" "hl_publish_gate.py" "SequenceMatcher"

S="$SCRATCH/q4c"; fresh_scratch "$S"
mutate_file "$S" scripts/hl_publish_gate.py 'def identity_gate(' 'def identity_gate_renamed(' > "$PROOF_DIR/q4-rename.apply.log" 2>&1
python3 scripts/check_identity_invariants.py --root "$S" > "$PROOF_DIR/q4-lint-gate-removed.log" 2>&1; RC=$?
expect_fail "Q4[lint: publisher gate removed/renamed]" "$PROOF_DIR/q4-lint-gate-removed.log" $RC \
  "no longer defines \`identity_gate\`"

say "== $FAILS proof(s) failed =="
[ "$FAILS" = 0 ]
