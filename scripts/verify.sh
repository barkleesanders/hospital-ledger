#!/usr/bin/env bash
# Source-only checks. No refresh, hospital fetch, model call or publication.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
VERIFY_PYTHON="${HOSPITAL_LEDGER_TEST_PYTHON:-python3}"
"$VERIFY_PYTHON" -m unittest discover -s tests -p 'test_*.py' -v
"$VERIFY_PYTHON" ops/wave/test_hl_wave_fixes.py
"$VERIFY_PYTHON" ops/wave/test_hl_wave_chunk.py
"$VERIFY_PYTHON" ops/wave/test_hl_wave_reparse.py
"$VERIFY_PYTHON" scripts/test_mrf_parse_charge_history.py
"$VERIFY_PYTHON" -m py_compile scripts/tune_ingest_workers.py scripts/ingest_capacity.py \
  scripts/coverage_closeout.py scripts/full_standardize.py scripts/batch_ingest.py scripts/retry_failed_ingest.py
shasum -a 256 ops/wave/vm_wave.py pipeline/vm_wave.py
cmp ops/wave/vm_wave.py pipeline/vm_wave.py
npm run typecheck
npm run test:site-data
npm run test:faq
npm run test:fetch-proxy
npm run test:submissions
node --test tests/client-ux.test.js
npm run build
npm run build:faq-island
# Dependency audit gate (replaces the bare `npm audit --audit-level=low`).
#
# Tolerated advisory — exactly one, named, with reason:
#   GHSA-fc5q-39xc-vjpf (picomatch, high, ReDoS via repeated extglobs).
#   GitHub's advisory record: affects picomatch <= 4.0.7 — every released
#   version (4.0.7 is the latest) — with NO patched version in existence.
#   picomatch is a build-time-only transitive dependency (vite/vitest
#   file globbing); it is never shipped to or executed by the site, and
#   the flaw needs attacker-supplied glob patterns, which this build
#   never accepts. Tolerance approved by Barklee 2026-11-17 to unblock
#   CI. REMOVE it as soon as a patched picomatch ships and the lockfile
#   is bumped past it.
# Every other advisory, at any severity, fails the gate — the same
# strictness --audit-level=low enforced. The gate fails closed when the
# audit report cannot be produced or parsed.
AUDIT_JSON="$(mktemp)"
trap 'rm -f "$AUDIT_JSON"' EXIT
npm audit --json > "$AUDIT_JSON" || true
"$VERIFY_PYTHON" - "$AUDIT_JSON" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        report = json.load(fh)
except Exception as exc:
    print(f"audit gate: cannot parse npm audit report: {exc}", file=sys.stderr)
    sys.exit(1)
vulns = report.get("vulnerabilities")
if not isinstance(vulns, dict):
    print("audit gate: npm audit report has no vulnerabilities map; failing closed",
          file=sys.stderr)
    sys.exit(1)
TOLERATED = {"GHSA-fc5q-39xc-vjpf"}
found, tolerated = {}, {}
for pkg, entry in vulns.items():
    for via in entry.get("via", []):
        if isinstance(via, dict):
            url = via.get("url", "")
            ghsa = url.rstrip("/").rsplit("/", 1)[-1] if url else "?"
            bucket = tolerated if ghsa in TOLERATED else found
            bucket[ghsa] = (pkg, via.get("severity", "?"), via.get("title", ""))
for ghsa, (pkg, sev, title) in sorted(tolerated.items()):
    print(f"audit gate: TOLERATED {ghsa} in {pkg} ({sev}): {title}"
          " — see the note above this gate in verify.sh")
if found:
    for ghsa, (pkg, sev, title) in sorted(found.items()):
        print(f"audit gate: FAIL {ghsa} in {pkg} ({sev}): {title}", file=sys.stderr)
    sys.exit(1)
print("audit gate: no non-tolerated advisories")
PYEOF
