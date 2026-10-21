# Hospital Ledger Tier-3 VM pipeline

VM-native rebuild pipeline for the Hospital Ledger Tier 3 dataset
(3,699 hospitals, weekly refresh). Runs on the Muse VM under systemd;
no longer depends on the Mac mini for execution.

## Layout

- `pipeline/vm_coordinator.py` — wave orchestrator (systemd service
  `hl-coordinator.service`). Owns the per-wave 5-attempt auto-fix ladder:
  resume-stage (verify-first, idempotent) -> full re-run -> halt.
- `pipeline/vm_wave.py` — single-wave runner: download -> parse ->
  standardize (5% quarantine gate with `known-quarantined.json`
  documented exclusions) -> slim -> stage to R2 (byte-verified PUTs,
  manifest last) -> finalize. Supports `--resume-stage` for
  staging-only failures.
- `pipeline/vm_merge.py` — merges the 11 wave outputs; keeps all 3,699
  CCNs including documented zero-row hospitals.
- `pipeline/vm_r2client.py` — R2 client with truncation guards
  (rejects short verify reads, one re-PUT on genuine mismatch).
- `pipeline/known-quarantined.json` — 19 CCNs whose published MRFs
  contain zero price signals (hospital-published placeholders /
  wrong-format files). Documented, still staged as zero-row price
  files, excluded from the unexpected-quarantine failure rate.
- `pipeline/wave-plan-11.json` — 11-wave CCN sharding plan.
- `pipeline/drive_archive.py`, `pipeline/hl_counts_refresh.sh`,
  `pipeline/hl_weekly_guard.py` — archive + counts-refresh tooling.
- `systemd/hl-coordinator.service` — systemd unit (durable copy; the
  live unit is restored from this after VM reboots).

## Data flow

Data (per-CCN price files, indexes) goes to Cloudflare R2; pipeline
code goes here. Never delete R2 objects; never publish partial
generations; validation gates must pass before publish.
