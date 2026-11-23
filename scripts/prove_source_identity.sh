#!/usr/bin/env bash
# Sabotage proofs for the repo-owned source-identity reader (remediation wave 4, lane D).
#
# A check without a proof does not count: every new check must FAIL at the shape of the real past
# mistake and PASS on the current tree. Mutations are applied to a SCRATCH COPY, never the tracked tree.
#
#   D1  current tree: tests/test_source_identity.py passes.
#   D2  mutation, one read at a time (byte-order mark kept, `hospital_location` unknown, blank line
#       under the header kept as a row, header row read as a meta row, a one-key first line taken
#       for a header row) -> a named test fails each time.
#   D3  MEASURED against the fleet's installed reader (~/hl-fleet/remote_build.py, read-only, lifted
#       by AST because that file runs a build at import) on every retained staged source file:
#       the repo reader must lose nothing the fleet reader reads. Without the fleet on this machine
#       the line is UNMEASURED (counted as a failure of the run, never as a pass).
#
# Usage: bash scripts/prove_source_identity.sh [proof-dir]
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
PROOF_DIR="${1:-$REPO/.ship/lane-d-proof}"
mkdir -p "$PROOF_DIR"
PROOF_DIR="$(cd "$PROOF_DIR" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
FAILS=0
SUMMARY="$PROOF_DIR/SUMMARY.txt"
: > "$SUMMARY"
say() { echo "$*" | tee -a "$SUMMARY"; }
verdict() { if [ "$2" = 1 ]; then say "PASS  $1 - $3"; else say "FAIL  $1 - $3"; FAILS=$((FAILS + 1)); fi; }

fresh_scratch() { rm -rf "$1"; mkdir -p "$1/scripts" "$1/tests"; cp scripts/hl_source_identity.py "$1/scripts/"; cp tests/test_source_identity.py "$1/tests/"; }
run_tests() { (cd "$1" && python3 -m unittest tests.test_source_identity) > "$2" 2>&1; }
mutate_file() { # path old new  (exact single replacement, or die)
  python3 -I - "$1" "$2" "$3" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
if src.count(old) != 1:
    sys.exit(f"MUTATION DID NOT APPLY: {old!r} found {src.count(old)} times in {path}")
open(path, "w").write(src.replace(old, new))
PYEOF
}
mut() { # name old new needle...
  local name="$1" old="$2" new="$3"; shift 3
  local S="$SCRATCH/d-$name"; fresh_scratch "$S"
  if ! mutate_file "$S/scripts/hl_source_identity.py" "$old" "$new" > "$PROOF_DIR/d-$name.apply.log" 2>&1; then
    say "UNMEASURED  D2[$name] - mutation did not apply: $(cat "$PROOF_DIR/d-$name.apply.log")"; FAILS=$((FAILS + 1)); return
  fi
  run_tests "$S" "$PROOF_DIR/d-$name.log"; local rc=$?
  if [ "$rc" = 0 ]; then verdict "D2[$name]" 0 "sabotaged code PASSED (the check is blind to this mistake)"; return; fi
  local miss=""
  for needle in "$@"; do grep -qF -- "$needle" "$PROOF_DIR/d-$name.log" || miss="$miss [$needle]"; done
  if [ -n "$miss" ]; then verdict "D2[$name]" 0 "failed, but the log does not name:$miss"; else
    verdict "D2[$name]" 1 "fails as required, naming: $*"; fi
}

S="$SCRATCH/current"; fresh_scratch "$S"
run_tests "$S" "$PROOF_DIR/d1-current-tests.log"; RC=$?
verdict "D1[tests on current tree]" "$([ $RC = 0 ] && echo 1 || echo 0)" "tests/test_source_identity.py exit=$RC (expected 0)"

mut keep-byte-order-mark 'return raw_head.decode("utf-8", "replace").lstrip(_BOM)' 'return raw_head.decode("utf-8", "replace")' \
  "test_csv_header_row_behind_a_byte_order_mark_is_read" "test_json_behind_a_byte_order_mark_is_read_as_json" \
  "test_zipped_csv_member_behind_a_byte_order_mark_is_read"
mut keep-byte-order-mark-gzip 'head = f.read(_HEAD).lstrip(_BOM)' 'head = f.read(_HEAD)' \
  "test_gzipped_file_behind_a_byte_order_mark_is_read"
mut hospital-location-unknown '_READ_KEYS = _ID_KEYS + ("hospital_location",)' '_READ_KEYS = _ID_KEYS' \
  "test_hospital_location_is_stored_as_location_name"
mut blank-line-is-a-row '    rows = [r for r in rows if any(c.strip() for c in r)]' '    pass' \
  "test_a_blank_line_under_the_header_does_not_hide_the_value_row"
mut header-row-read-as-meta-row 'for r in rows[1 if header_row else 0:14]:' 'for r in rows[:14]:' \
  "test_a_blank_hospital_name_value_is_not_replaced_by_a_column_name"
mut one-key-line-is-a-header 'sum(1 for c in r0 if c.split("|")[0].strip() in _HEADER_TOKENS) >= 2' 'True' \
  "test_meta_row_form"

cat > "$SCRATCH/parity.py" <<'PYEOF'
"""Read-only: the repo reader vs the fleet's installed reader on every retained staged file."""
import ast, collections, csv, gzip, hashlib, io, json, os, re, sys, zipfile
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, "scripts"))
import hl_source_identity as new
builder = os.path.expanduser("~/hl-fleet/remote_build.py")
staging = os.path.realpath(os.path.expanduser("~/hl-fleet/staging"))
print("fleet remote_build.py sha256", hashlib.sha256(open(builder, "rb").read()).hexdigest())
want = {"_ID_KEYS", "_si_from_text", "_si_from_csv_head", "extract_source_identity"}
picked = []
for node in ast.parse(open(builder).read()).body:
    name = node.name if isinstance(node, ast.FunctionDef) else (
        node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) else None)
    if name in want:
        picked.append(node); want.discard(name)
if want:
    sys.exit(f"the fleet builder lacks {sorted(want)}")
ns = {"json": json, "csv": csv, "io": io, "re": re, "zipfile": zipfile, "gzip": gzip}
exec(compile(ast.Module(body=picked, type_ignores=[]), builder, "exec"), ns)
old = ns["extract_source_identity"]
C = collections.Counter(); lost = []
files = sorted(f for f in os.listdir(staging) if f.endswith(".src"))
for f in files:
    p = os.path.join(staging, f)
    with open(p, "rb") as fh:
        magic = fh.read(2); fh.seek(0)
        raw = fh.read() if (magic == b"PK" and os.path.getsize(p) <= 2_000_000_000) else fh.read(1 << 22)
    a, b = old(raw), new.extract_source_identity(raw)
    if a == b: C["same"] += 1
    elif not a and b: C["new-read"] += 1
    elif any(k not in b for k in a): C["LOST"] += 1; lost.append(f)
    elif all(b[k] == a[k] for k in a): C["added-keys"] += 1
    else: C["value-changed"] += 1; print("VALUE-CHANGED", f, json.dumps(a), "->", json.dumps(b)[:300])
print(f"TALLY files={len(files)} same={C['same']} new_read={C['new-read']} added_keys={C['added-keys']} "
      f"value_changed={C['value-changed']} LOST={C['LOST']} {lost}")
PYEOF
if [ -f "$HOME/hl-fleet/remote_build.py" ] && [ -d "$HOME/hl-fleet/staging" ]; then
  python3 -I "$SCRATCH/parity.py" "$REPO" > "$PROOF_DIR/d3-fleet-parity.log" 2>&1; RC=$?
  if [ $RC = 0 ] && grep -q '^TALLY ' "$PROOF_DIR/d3-fleet-parity.log"; then
    say "MEASURED  D3[fleet reader vs repo reader, retained staged files]: $(grep '^TALLY ' "$PROOF_DIR/d3-fleet-parity.log")"
    grep '^VALUE-CHANGED' "$PROOF_DIR/d3-fleet-parity.log" | sed 's/^/             /' | tee -a "$SUMMARY"
    grep -q '^TALLY .* LOST=0 \[\]' "$PROOF_DIR/d3-fleet-parity.log" \
      && verdict "D3[nothing the fleet reader reads is lost]" 1 "LOST=0" \
      || verdict "D3[nothing the fleet reader reads is lost]" 0 "the repo reader drops an identity the fleet reader reads"
    NR="$(grep '^TALLY ' "$PROOF_DIR/d3-fleet-parity.log" | sed -E 's/.*new_read=([0-9]+).*/\1/')"
    verdict "D3[the fleet reader fails at the past mistake]" "$([ "${NR:-0}" -ge 1 ] && echo 1 || echo 0)" \
      "the fleet reader reads nothing from ${NR:-?} retained files the repo reader reads (>=1 required for this to be a proof)"
  else
    say "UNMEASURED  D3 - the fleet parity run could not complete (exit=$RC); see d3-fleet-parity.log. Not counted as a pass."
    FAILS=$((FAILS + 1))
  fi
else
  say "UNMEASURED  D3 - ~/hl-fleet/remote_build.py or its staging directory is not on this machine. Not counted as a pass."
  FAILS=$((FAILS + 1))
fi

say "== $FAILS proof(s) failed =="
[ "$FAILS" = 0 ]
