#!/usr/bin/env bash
# Source-only checks. No refresh, hospital fetch, model call or publication.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
VERIFY_PYTHON="${HOSPITAL_LEDGER_TEST_PYTHON:-python3}"
"$VERIFY_PYTHON" -m unittest discover -s tests -p 'test_*.py' -v
"$VERIFY_PYTHON" ops/wave/test_hl_wave_fixes.py
"$VERIFY_PYTHON" -m py_compile scripts/tune_ingest_workers.py scripts/ingest_capacity.py \
  scripts/coverage_closeout.py scripts/full_standardize.py scripts/batch_ingest.py scripts/retry_failed_ingest.py
shasum -a 256 ops/wave/vm_wave.py pipeline/vm_wave.py
cmp ops/wave/vm_wave.py pipeline/vm_wave.py
npm run typecheck
npm run test:site-data
npm run test:faq
npm run test:fetch-proxy
node --test tests/client-ux.test.js
npm run build
npm run build:faq-island
npm audit --audit-level=low
