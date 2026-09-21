# Cloud refresh operations

Hospital Ledger's production refresh is designed for an ephemeral Linux worker. The Mac mini is not part of the production architecture. Cloudflare R2 holds every durable checkpoint needed by the next run.

## Schedule contract

Run `bash scripts/cloud_refresh.sh` every Sunday at 4:17 a.m. in `America/Los_Angeles`. The production schedule belongs to the Codex Work Mode automation. The GitHub workflow validates the runner and tests on ordinary hosted runners, but it does not attempt the multi-hour production crawl.

The production worker needs at least 8 GB RAM, 60 GB free disk, Python 3.12, Node 22, and outbound HTTPS access.

## Required authentication

The preferred unattended path uses the Cloudflare Global API Key that is already
kept in the owner's private operations backup. Load these values into the process
without printing or committing them:

```text
R2_ACCOUNT_ID
CLOUDFLARE_EMAIL
CLOUDFLARE_API_KEY
```

At startup, `cloud_refresh.sh` exchanges that credential for an account-owned R2
token scoped only to `hl-mrf-parsed`. The token expires after 72 hours and is
revoked by the exit handler after the lock is released. The plaintext token value
is converted in memory to the R2 S3 secret-key format and is never logged. The
temporary environment file is mode `0600` and is deleted on exit. Expiration is
the fallback if the worker is terminated too abruptly to run its exit handler.

An externally managed `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` pair remains
supported as an override. With that override, Wrangler must also have either
`CLOUDFLARE_API_TOKEN` or the Global API Key credentials above. The runner never
revokes externally managed R2 credentials.

## Run lifecycle

1. Verify the target R2 bucket and write a `running` heartbeat.
2. Acquire an R2-backed distributed lock, clear all derived hydration targets, then hydrate the validator state, SQLite database, public indexes, and gzipped parsed corpus from R2.
3. Probe every preferred hospital MRF URL concurrently.
4. Compare URL, ETag, Last-Modified, content length, and a bounded content sample. Schedule a deterministic forced refresh when a server exposes weak validators.
5. Store the immutable plan, post-probe SQLite database, and an upload-complete marker in an isolated `_pipeline/staging/<run-id>/` prefix.
6. Parse only the explicit changed-hospital worklist in bounded shards. The planner records the exact selected URL for every CCN, and each ingest shard consumes that map without independently re-ranking candidates. Each successful shard is slimmed, gzipped, and checkpointed to staging immediately, so the first rebuild never accumulates the roughly 107 GB uncompressed corpus and an interrupted run resumes from its completed shards. A failed parse restores the prior raw record so global indexes retain last-known-good data.
7. Rebuild and audit all public aggregates.
8. Snapshot every R2 key that will change and record the active Worker version.
9. Upload changed hospital objects and the CPT index, deploy the Worker, and run the live audit.
10. If publication or the live audit fails, roll back both the Worker version and R2 snapshot.
11. Upload parsed checkpoints and public indexes. Upload the validator state last as the atomic completion marker, then remove staging.

Run heartbeats are stored at:

```text
r2://hl-mrf-parsed/_pipeline/runs/latest.json
r2://hl-mrf-parsed/_pipeline/runs/<run-id>.json
```

Rollback manifests and server-side object snapshots are stored below `_pipeline/rollback/<run-id>/`.
Resumable shard checkpoints are stored below `_pipeline/staging/<run-id>/` until a generation commits.
The refresh lock is stored at `_pipeline/locks/cloud-refresh.json`. It blocks overlapping manual and scheduled runs and expires after 48 hours if a worker is terminated before cleanup.
Before the first live object changes, the runner writes a durable `publishing` heartbeat containing the rollback manifest key and prior Worker version. A later worker automatically restores both layers if the earlier worker was terminated before writing the `committed` heartbeat.

## First cloud run

The old Mac parsed cache was never backed up. When `_pipeline/parsed/` is empty, the first cloud run performs a full raw-corpus rebuild. It requires at least 85 percent of reachable candidates to parse successfully before publication. Later incremental runs require at least 60 percent by default and preserve the last-known-good record for every failed changed hospital.

Tune only when evidence justifies it:

```bash
PROBE_CONCURRENCY=32 INGEST_WORKERS=4 R2_WORKERS=16 bash scripts/cloud_refresh.sh
```

Useful safe checks:

```bash
bash scripts/cloud_refresh.sh --plan-only
bash scripts/cloud_refresh.sh --no-deploy
python -m unittest discover -s tests -v
```

`--plan-only` does not require Cloudflare credentials and does not mutate production. `--no-deploy` hydrates production checkpoints and performs the full local build and audits, but it does not publish or advance validator state.

## Recovery

An interrupted run before publication leaves production unchanged. An interrupted run during publication triggers the exit handler, which restores the captured R2 keys and previous Worker version. If the process is killed too abruptly for the handler to run, use the persisted rollback manifest with:

```bash
python scripts/r2_store.py restore-snapshot data/refresh_logs/rollback-<run-id>.json
wrangler rollback <previous-version-id> --name hospital-ledger --yes
```

Do not advance `cloud_refresh_state.json` manually. A missing or older validator state causes safe reprocessing on the next run.
