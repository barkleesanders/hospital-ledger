# hospital-ledger-refresh — Muse-native counts-tier pipeline

## What this is

The HospitalLedger.com weekly counts refresh, reverse-engineered from
`scripts/refresh_counts.sh` (bundle `195cd51f…`, spec `ops/muse-refresh-task.md`
`c735c242…`) and re-expressed as an agent-driven workflow. Previously the
scheduled job fired one opaque bash command; now the agent executes each phase
explicitly, checks the gate before advancing, and can retry or adapt a single
phase instead of re-running a black box.

**History it replaces:** a mac-mini launchd agent (`com.hospitalledger.refresh`,
Sundays 04:00) ran the FULL tier (`scripts/refresh.sh`: ingest → slim →
aggregates → R2 → `wrangler deploy`). It died in stages — wrangler had no
Cloudflare token under launchd, `mem_guard` SIGKILLed runs, the disk filled,
and the launchd job itself vanished by 2026-09-18. The site was stale from
2026-06-14. The counts tier exists because the parsed corpus only lives on R2
now: this tier re-probes MRF URLs, rebuilds counts, and publishes `meta/*` +
`prices/index.json` to R2, which the Worker serves directly
(`summary_source == "r2"`). No wrangler, no deploy, no git.

## Ownership

- **Muse owns the orchestration** (this skill + the cron body). Patch it freely
  when a phase needs a fix — that is the "update it automatically" part.
- **The operator owns the phase scripts** (`scripts/*.py`) via the pull bundle.
  Script fixes reach the job through the step-0 self-update; never edit
  `scripts/*` to make a check pass.
- **Data**: seed tables + `db/hospital_ledger.db` ship in the bundle.
  Caveat: a self-update re-extracts the bundle's DB copy, so probe history
  becomes whatever the bundle shipped.

## Pipeline contract (the whole thing)

Input: `~/hospital-ledger` (extracted bundle) + `~/hospital-ledger.env`
(R2 creds, mode 600). Output: refreshed `meta/*` + `prices/index.json` on R2
bucket `hl-mrf-parsed`, then the live site's `/api/manifest` reflecting it.

Invariants:
- Probe is `INSERT OR REPLACE` into `mrf_probe` — re-running the probe is safe.
- Never run `build_db.py` — it DROPs `mrf_probe` and erases all probe history.
- Publish order is load-bearing: `prices/index.json`, `meta/hospitals.json`,
  `meta/summary.json`, then `meta/manifest.json` LAST (it flips live
  `generated_at`; everything it describes must already be uploaded).
- `--limit` is a smoke test only; a partial probe must never be published
  (the old script refused this; the native workflow must too).
- Writes go only to `meta/` and `prices/index.json`. Never delete R2 objects.
- `validate_site_data.py --tier counts` must exit 0 before any publish.

## Native runbook — execute in order, gate each phase

Set up once per run (all commands run with `cd ~/hospital-ledger`):

```bash
ROOT=~/hospital-ledger; cd "$ROOT"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
LOG="$ROOT/data/refresh_logs/counts-$TS.log"; mkdir -p "$ROOT/data/refresh_logs"
PRODUCER="${HL_PRODUCER:-muse.ai nova}"
OUT="${OUT:-$ROOT/data/site-out}"
CONCURRENCY="${CONCURRENCY:-12}"
# log every phase:
exec > >(tee -a "$LOG") 2>&1
```

### Phase 0 — Self-update (first, every run)
Fetch the bundle pull endpoint (`/pull/pub/hospital-ledger-pipeline.sha256`)
(Bearer `BEARER_SHIM_KEY` from `~/.config/muse-creds/env`; never
paste the key anywhere). If it differs from
the counts-tier goal workspace file `hidden_files/pipeline-sha256.txt`:
download the tarball, `sha256sum`-verify it, `tar --no-same-owner -xzf` over
`~/hospital-ledger`, write the new sha to the watermark file.
(`~/hospital-ledger.env` lives outside the tree — verify its checksum is
unchanged after any re-extract.)

### Phase 1 — Environment
```bash
[ -x .venv/bin/python3 ] || python3 -m venv .venv   # PEP-668-safe; needs python3-venv
export PATH="$ROOT/.venv/bin:$PATH"
.venv/bin/python -c "import httpx" 2>/dev/null || .venv/bin/python -m pip install --quiet "httpx==0.28.1"
# httpx 0.28.1 crashes on IPv6 literals in NO_PROXY (InvalidURL: Invalid port: ':1]'):
strip_ipv6() { printf '%s' "$1" | tr ',' '\n' | grep -v ':' | paste -sd, - ; }
[ -n "${NO_PROXY:-}" ] && export NO_PROXY="$(strip_ipv6 "$NO_PROXY")"
[ -n "${no_proxy:-}" ] && export no_proxy="$(strip_ipv6 "$no_proxy")"
```
Gate: `.venv/bin/python -c "import httpx; print(httpx.__version__)"` prints `0.28.1`.

### Phase 2 — Probe
```bash
.venv/bin/python scripts/probe_mrf_urls.py --all --concurrency "$CONCURRENCY"
```
~3,609 URLs, ~3–10 min. Gate: exit 0 and log tail shows `probe complete`.
Recovery: re-run the phase (INSERT OR REPLACE is idempotent).

### Phase 3 — Enforcement match
```bash
.venv/bin/python scripts/ingest_cms_enforcement.py   # idempotent, reads seed/cms_enforcement.csv
```
Gate: exit 0.

### Phase 4 — Registry + summary
```bash
.venv/bin/python scripts/build_site_data.py   # writes public/data/summary.json + hospitals.json
```
Gate: exit 0; both files newer than phase start.

### Phase 5 — Assemble contract tree
```bash
rm -rf "$OUT"; mkdir -p "$OUT/meta" "$OUT/prices"
cp public/data/summary.json "$OUT/meta/summary.json"
cp public/data/hospitals.json "$OUT/meta/hospitals.json"
cp public/data/prices/index.json "$OUT/prices/index.json"
.venv/bin/python scripts/make_manifest.py "$OUT" --producer "$PRODUCER" --tier counts \
  --note "native refresh $TS"
```
Gate: `$OUT/meta/manifest.json` exists.

### Phase 6 — Validate (the gate)
```bash
.venv/bin/python scripts/validate_site_data.py "$OUT" --tier counts --quiet
```
Gate: exit 0. Then print the two report lines:
```bash
.venv/bin/python -c "import json;print('generated_at:',json.load(open('$OUT/meta/summary.json'))['generated_at'])"
.venv/bin/python -c "import json;d=json.load(open('$OUT/meta/summary.json'));print('compliant:',d['compliant'],'/',d['cms_required_total'],'=',d['compliance_pct'],'%')"
```
On validation failure: report the FAIL lines, STOP — nothing is published.

### Phase 7 — Publish (only on a publish run)
Preconditions: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `R2_ENDPOINT`
set (from `~/hospital-ledger.env`); this is NOT a `--limit` run.
```bash
export R2_BUCKET="${PARSED_R2_BUCKET:-hl-mrf-parsed}"
.venv/bin/python scripts/r2_put.py --check   # HEAD bucket must be 200
for k in prices/index.json meta/hospitals.json meta/summary.json meta/manifest.json; do
  .venv/bin/python scripts/r2_put.py "$OUT/$k" "$k"   # manifest LAST
done
```
Gate: every upload exits 0. On failure: stop, report which key failed.

### Phase 8 — Live check
Poll `https://hospitalledger.com/api/manifest` (cache-busted) up to 20 × 10 s
until `generated_at` equals the run's and `summary_source == "r2"`.
Gate: match within 200 s. On failure: report last-seen values.

### Phase 9 — Report
- exit code + log path; `generated_at`; `compliant X / Y = Z%`
- delta vs the pre-run snapshot: newly-live and newly-dead MRF counts
  (diff `public/data/hospitals.json` before/after on `has_live_mrf`)
- on failure: last 20 log lines verbatim; retry the run at most ONCE;
  a second failure is reported and left for a human
- log the outcome as an activity entry on the tracked item

## Failure semantics (match the old script's exit codes)

0 ok · 1 build/validate failed · 3 publish failed · 4 post-publish live check
failed. Report the code, never invent a success.

## When something breaks

The agent may patch THIS SKILL and the cron body (orchestration is
Muse-owned). It may not patch `scripts/*.py` to force a check through — a
`validate_site_data.py` FAIL is reported as FAIL. If a phase script itself is
buggy, report it; the operator ships the fix in the next bundle and step 0 picks
it up.

## Lessons — 2026-10-13/14 publish-repair run (all live-verified)

- **Transport is bundle-owned; never hand-roll an alternative.** The VM
  egress proxy cuts large S3 transfers at random points (urllib GET of the
  1.39 MB index truncated 15/15 in early diagnosis). The operator's fix (bundle
  `4fe3aed1…`, watermark it in phase 0) makes `r2_put.py` GET **resume from
  the first missing byte** with a signed Range request pinned by If-Match
  to the first response's ETag, verifies single-part ETag == body MD5, and
  doesn't consume retry budget for progress-making attempts. Live proof on
  this VM: canonical index fetched in 7 requests (1 + 6 resumes) —
  1,386,703 B, sha256 d8f6d61d…, and `scripts/test_r2_put_get.py` is 10
  tests. If a future GET stalls, count the `body ended at N/M bytes,
  resuming` lines on stderr before concluding anything.
- **Verify the synced build base before building (structural, in
  `hl_wave.py`).** The live manifest records every artifact's size+sha256.
  Wave 35's S3 sync failed and the CDN fallback served the **stale
  site-bundled index** (Sept 27, 3,810 hospitals) as the build base; only
  the count gate saved the publish. `verify_synced_base()` now checks the
  synced `prices/index.json` against `/api/manifest`'s artifact record at
  sync time and parks (`base-verify-fail`, transport class) on any
  mismatch or unverifiable manifest. A stale base is impossible to build
  on, not merely caught downstream. Site routing facts behind it:
  `/data/prices/index.json` serves the site bundle (NOT the R2 object);
  `/data/prices/<ccn>.json` 404s; the live index is served at
  `/api/prices-index` (use it for live spot-checks).
- **Canonical-reference discipline.** Before diagnosing any "corrupt
  index", establish which copy is canonical from R2 itself (direct
  `--get-file` + the manifest record), never from the site bundle or a
  local file. Canonical at this run: 1,386,703 B / 3,823 hospitals /
  sha256 d8f6d61d7253685c….
- **Publish-lock semantics under a cutting proxy (hl_wave.py, VM-owned
  layer; regression-tested in `test_hl_wave_fixes.py`).** A cut response is
  ambiguous — the op may have landed. Therefore: lock RELEASE retries 3×
  (one cut DELETE left a zombie lock for the full 900 s TTL); lock ACQUIRE
  retries transport failures and may adopt a lock held by its OWN owner —
  parse the holder from `lock held by <holder>` and compare, because
  r2_put ownership errors name BOTH parties (`… not <requester>`) and a
  substring match false-adopts a foreign lock (happened live once);
  lock CONTENTION is never recorded as r2_put route-health failure (it
  earned a bogus 30 m cooldown that delayed a healthy retry by an hour);
  uploads run ONE KEY per `r2_put` invocation with per-key retries —
  the old single 12-key invocation lost all progress to one cut
  connection. PUTs are idempotent; order preserved, manifest LAST.
- **The std-gate streak is a manual-review gate, not a wall.** After
  `no_progress_streak >= 3` waves stop with exit 3. The sanctioned reset:
  diagnose + fix the root causes, verify the fixes, back up
  `~/workspace/tools/state/loops/hospital-ledger-wave.json`, zero the
  streak, and write the evidence into `resume_reason` (precedent
  2026-09-29 and 2026-10-13). Same pattern for a route-health cooldown
  earned by misclassified failures: back up `hl_r2_route_health.json`,
  reset, document why the failures were misclassified.
- **Flaky-proxy failure shape to expect mid-publish:** `RemoteDisconnected`
  in `http.client._read_status` on small ops too (lock acquire/release,
  upload status reads), in bursts of ~1–3 minutes. The correct response is
  the retry machinery above plus patience — never a transport rewrite and
  never a publish without byte-verify + live manifest read-back.
- Run evidence for this section: wave of 2026-10-14T00:14Z + two
  retry-publishes; final live state — manifest `generated_at`
  2026-10-14T00:56:57Z, producer `muse.ai nova (wave-loop)`; index
  1,389,589 B / sha256 007e52651400… / **3,831 hospitals** (canonical
  3,823 + the 8 staged reparses), verified identically via R2 direct
  fetch, `/api/manifest`, and `/api/prices-index`. `hl_wave.py` fix suite:
  `test_hl_wave_fixes.py`, 25 tests. Ops record:
  `hidden_files/publish-fix-run-2026-10-13.md` in the goal tree.

## Available connectors

Use `muse.connector_search` to find the current connector docs before following
recovered instructions.
