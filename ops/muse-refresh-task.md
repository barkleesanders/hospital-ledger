# hospitalledger.com refresh — muse.ai recurring task (owner: Nova)

Handed over 2026-09-18. This replaces the mac-mini launchd job
(`ops/pipeline-ground-truth-2026-09-18.md` explains why it stopped working).

## What Nova pulls (once, and again whenever the tarball's sha256 changes)

From the existing authenticated pull endpoint (same Bearer key as `priv/env`):

| URL | What | Auth |
|---|---|---|
| `https://authshim.barkleesanders.com/pull/pub/hospital-ledger-pipeline.tar.gz` | the repo's tracked files: `scripts/`, `seed/`, `db/hospital_ledger.db`, `public/data/`, `ops/`, `requirements.txt` (~28 MB) | Bearer |
| `https://authshim.barkleesanders.com/pull/pub/hospital-ledger-pipeline.sha256` | its checksum — verify before extracting | Bearer |
| `https://authshim.barkleesanders.com/pull/priv/hospital-ledger.env` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `R2_ENDPOINT`, `R2_BUCKET` — R2 credential scoped to **read/write on bucket `hl-mrf-parsed` only** (it cannot touch `hl-mrf-raw` or anything else on the account) | Bearer |

## The recurring task (counts tier) — every Sunday 04:00 America/Los_Angeles

```
# step 0 — self-update: if /pull/pub/hospital-ledger-pipeline.sha256 differs from the
# sha last extracted, pull the tarball, verify it, re-extract over ~/hospital-ledger,
# record the new sha (this is how script fixes reach the task without a nudge)
cd ~/hospital-ledger            # extracted tarball
set -a; . ~/hospital-ledger.env; set +a
export HL_PRODUCER="muse.ai nova"
bash scripts/refresh_counts.sh --publish
```

That single command: re-probes every seeded MRF URL (3,609 HTTP requests,
concurrency 12, ~10 min), rebuilds the hospital registry + compliance counts,
assembles the contract tree under `data/site-out/`, writes + verifies the
manifest, runs `scripts/validate_site_data.py` (must exit 0), uploads
`prices/index.json`, `meta/hospitals.json`, `meta/summary.json`,
`meta/manifest.json` to R2 via `scripts/r2_put.py` (stdlib SigV4 — no `aws`
or `rclone` needed), then polls `https://hospitalledger.com/api/manifest`
until `generated_at` equals the new run and `summary_source == "r2"` (exit 0),
or gives up after 200 s (exit 4).

Needs: Python 3.9+ with the `venv` module (the script creates `./.venv` and
installs `httpx` into it — this works on PEP-668 "externally managed" Pythons
where `pip install --user` is refused), ~100 MB disk, outbound HTTPS. No
`aws`/`rclone`, no Cloudflare account access, no git push, no deploy.

### Report, natively (Activity + Main chat), after every run

- exit code and the log path (`data/refresh_logs/counts-<ts>.log`)
- `generated_at`, `compliant / cms_required_total = compliance_pct` (the script prints both)
- the delta vs the previous run: newly live MRFs, newly dead MRFs (compare
  `public/data/hospitals.json` before/after on `has_live_mrf`)
- on exit ≠ 0: the last 20 log lines verbatim, and **do not retry more than
  once**; a second failure is reported and left for a human.

### Hard rules

1. Never publish a `--limit` (partial) probe. The script refuses; do not work around it.
2. Never delete objects / write outside `meta/` and `prices/index.json`
   in the counts tier. `prices/{ccn}.json`, `parsed/`, `indexes/`, `aggregates/` are
   the price corpus; the counts tier carries them forward untouched.
3. Never edit `scripts/*` to make a check pass. If `validate_site_data.py` fails,
   report the FAIL lines; the contract is the spec.
4. The R2 credential lives only in `~/hospital-ledger.env` (mode 600) inside the
   VM; never paste it into chat, logs, or the report.
5. If the tarball sha256 on the pull endpoint changes, re-pull before the next run
   (that is how script fixes reach you).

## Full tier (monthly, or when a counts run shows ≥ 50 newly-live MRFs) — NOT yet handed over

`scripts/refresh.sh` steps 2–6 re-parse changed MRFs and rebuild `prices/`,
`indexes/cpt-index.json`, `aggregates/`. It needs the parsed corpus locally
(R2 `parsed/`, ~5.8 GB — needs an S3 sync tool, which is why it is not yet handed over) plus ~8 GB RAM
and 4–6 h. Run it only after reporting `df -h`, `free -g`, `nproc` from the VM
and getting an explicit go-ahead in chat. The output tree is validated with
`validate_site_data.py --tier full` and uploaded per
`ops/data-contract.md` — `meta/` last.

## How a human verifies Nova's run (no trust in the Activity card)

```
curl -s "https://hospitalledger.com/api/manifest?cb=$(date +%s)"   # generated_at advanced? producer == "muse.ai nova"? summary_source == r2?
```
