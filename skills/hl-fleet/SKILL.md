---
name: hl-fleet
description: Hospital Ledger free-compute fleet — route MRF fetch/parse jobs across Modal, Daytona, the Mini, the VM, and Oracle instead of one lane at a time. Use for HL ingest waves, discovery-pool (D1/D2/D3) ingestion, big-ZIP canonical builds, and any "parallelize the hospital work" request.
---

> **Public edition.** This file is the public copy of the Hospital Ledger
> fleet operating skill, kept in this repository at the maintainer's
> direction. The canonical internal edition lives in the maintainer's
> skill registry; the two are kept in sync on every substantive update,
> and repo updates land by pull request. Infrastructure identifiers
> (cloud org IDs, database UUIDs, host addresses, credential file
> paths) are redacted in this edition — everything about how the work
> is done is intact.


# HL Fleet — free-compute lanes for Hospital Ledger

Built 2026-10-27 at Barklee's direction ("set up whatever makes the most sense
for these VMs for hospital ledger work and parallelize it as much as you can").
One CLI: `hlfleet.py` in `~/workspace/hl-fleet/` (VM copy) and `~/hl-fleet/` (Mini
copy — the Mini is where lane controllers + credentials live).

## The lanes (all proven live 2026-10-27 unless noted)

| Lane | Shape | Money | Use for |
|---|---|---|---|
| **Modal** | fetch + parse in containers, `.starmap` parallel | $30/mo Starter credits, recurring forever; workspace `barkleesanders` | Default lane. Open egress (fetched Sitecore CDN fine). Proof: 013029+013030, 22k items each, ~48s incl. image build. |
| **Daytona** | **parse-only** — sources fetched on the Mini, staged into the sandbox | **$1,100 prepaid credit, never expires** (Tier 2, region us, auto top-up OFF, no card on file) | High-RAM parsing (huge ZIPs/CSVs) once staged. Parses took ~2s for 4–7 MB CSVs; sandbox cold start ~50s. |
| **Mini** | staging fetcher + controller host + **the publisher** (since 2026-10-28) | — | Healthy egress when the VM proxy degrades; owns all lane credential files; runs `publish_mini.py` from the Share publish tree (see Publish doctrine). |
| **VM (Muse)** | small local chunks via `hl_wave.py` (the daily wave only) | — | **OUT of the fleet publish path since 2026-10-28** (was "sole publisher" Oct 26/27 — SUPERSEDED, see Publish doctrine). Never let a lane touch R2. |
| **Oracle micro** | VM.Standard.E2.1.Micro, 1 GB RAM, RUNNING | Always Free | Fetch-relay / scheduler class only — too small to parse. Original SSH keypair is lost; access needs console recovery if ever wanted. |
| **Oracle A1** | VM.Standard.A1.Flex 2 OCPU / 12 GB (of the 4/24 Always-Free allowance) | Always Free | Not yet landed: `~/oci-a1-retry/oci-a1-retry.py` retries every 30 min via launchd `com.barklee.oci-a1-retry` (re-armed 2026-10-27; San Jose capacity is the blocker since Sep 2026). On SUCCESS.json, provision per `provision-hermes-a1.sh` and add as a persistent lane. |

E2B Hobby stood up 2026-10-27: account via Google sign-in (WorkOS had an
existing identity for the email; the GitHub button routes elsewhere), workspace
"Personal Workspace", API key vaulted on the Mini (0600). Hobby terms as displayed: 20 concurrent sandboxes, 1 h max session,
8 vCPU / 8 GiB max, 10 GB disk; no numeric credit balance is shown anywhere.
Proof sandbox ran clean. Use for short jobs only (1 h cap).
Codespaces (120 core-h/mo) is the one lane NOT stood up: the Mini's `gh` token
needs the `codespace` scope, and GitHub's device-authorize page defeats the
automation routes (the Authorize button enables only once scrolled into view;
the segmented code input drops JS-set values — it wants real key events; two
device codes were burned 2026-10-27). Finish it with a human at the Mini
Chrome: `gh auth refresh -h github.com -s codespace`, type the code, scroll
to the button, click. Full survey: `~/workspace/hl-gap-research/free-vm-survey.md`.

## Hard-won lane facts (do not re-derive)

- **Daytona sandbox egress is allowlisted**: pypi.org and raw.githubusercontent.com
  answer 200; example.com, hospital CDNs (Sitecore edge, IU Health) get
  connection-reset. Never design a Daytona job that fetches hospital files
  directly — stage sources from the Mini (`daytona_controller.py` does this).
- **Daytona per-sandbox disk cap is 10 GB** (Tier 2): requesting 12 GB fails
  sandbox creation outright.
- **Tailscale inside a Daytona sandbox is impossible** (probed 2026-10-27 at
  Barklee's request): login/controlplane/pkgs/DERP endpoints are all
  connection-reset by the same egress allowlist, and no TUN device exists.
  Don't re-attempt; the Mini-staging pattern already provides the data path
  Tailscale would have added.
- **The parser has a sibling module**: `scripts/mrf_parse.py` imports `hl_edge`
  (line 47). Any lane payload MUST ship `hl_edge.py` beside it and put the
  scripts dir on `sys.path` (remote_build.py does). Forgetting it cost one
  failed run on each new lane.
- Modal `.map()` does NOT unpack tuples — use `.starmap()` for (ccn, name, url).
- Modal image: `add_local_file(..., copy=True)` for payload files; pip deps:
  httpx duckdb polars openpyxl ijson xlrd pysimdjson.
- Canonical record contract: lanes produce `data/parsed`-shaped
  `<ccn>.json.gz` via `remote_build.py` (same logic as build_canonical_mini.py,
  incl. the ZIP inner-CSV declared-date probe). Item counts + `source_updated`
  in the RESULT line are the acceptance check.
- Credentials live ONLY as 0600 env files on the Mini (per-lane files;
  paths in fleet records). Never print their values in chat, memory, or logs;
  never copy them to the VM (the VM's PyPI route was degraded anyway — the SDK
  venv is Mini-side at `~/hl-fleet/.venv`).

## CLI

```bash
hlfleet.py submit jobs.tsv [--lane auto|modal|daytona]   # TSV: ccn \t name \t seed_url
hlfleet.py publish 013029 013030 ...                     # LEGACY VM path — retired 2026-10-28, do not use
hlfleet.py status
publish_mini.py [--dry-run] CCN [CCN ...]                # Mini only: seed -> slim -> publish (THE publish route)
daytona_slim.py CCN [--cpu 4] [--mem 8]                  # Mini: heavy slim (>1 GB raw) inside a Daytona sandbox
```

## Publish doctrine — the Mini is the publisher (Barklee directive 2026-10-28)

**Supersession:** the Oct 26/27 doctrine "VM is the sole publisher" is
SUPERSEDED by Barklee's 2026-10-28 11:27 PDT directive ("use Daytona and the
$1,000 credit more, leverage the Mac mini more — publishes can't keep dying
mid-publish because the VM keeps restarting"). Direct cause: method-work
publish-3 (39 CCNs) died mid-slim when the Muse VM restarted ~11:04 PDT
2026-10-28 (059810 slim rc=-9 first); only the campaign driver's later VM
relaunch landed them. The publish path now lives where restarts can't kill
it:

- **Publisher = the Mini.** `~/hl-fleet/publish_mini.py` (source of truth:
  VM `~/workspace/hl-fleet/mini-publish/`), run detached:
  `ssh mini 'cd ~/hl-fleet && nohup python3 publish_mini.py CCN... > publish-mini-<label>.log 2>&1 < /dev/null &'`
  Logs: `~/hl-fleet/publish-mini-<label>.log`; state: `~/hl-fleet/publish-state.json`;
  lock: `~/hl-fleet/publish.lock` (never two publishes at once — check for a
  running publish, VM or Mini, before launching).
- **Publish tree** = `/Volumes/Share/hl-fleet/publish-tree` (symlink
  `~/hl-publish-tree`): the VM pipeline tree's `scripts/` + `db/` +
  `data/header_aliases.json` + aggregates, synced 2026-10-28. Canonicals are
  seeded from the local store (`~/hl-fleet/results`) — no pulls, no scp leg.
  Discipline kept from the old path: provenance-sidecar slim-completeness
  gate (a price file counts only if its sidecar sha256 matches the CURRENT
  store canonical), aggregate-shrink guard, R2 base sync before patching,
  no-regression validation vs that base, r2_put upload (meta/index keys
  last), per-key byte-verify, never delete R2 objects, 150042 refused.
  **CCN key guard (2026-11-03):** every store canonical key and registry CCN
  must match `^\d{6}$` before any publish treats it as a hospital. A wave-4
  litter file `45188E.json.gz` sat in the store for days; on 2026-11-03 the
  campaign driver published it as a live index entry — a phantom duplicate
  of Ennis Regional (true CCN 450833, identical n=19,042) that inflated the
  live count 4,995 → 4,996. Remedy: rename in store to
  `45188E.json.gz.QUARANTINE-malformed-key` (never delete), then remove the
  phantom from the published index (mechanics below). Any publisher/driver
  path that derives publish sets from the store or registry must filter
  non-`^\d{6}$` keys out and log them as litter, never publish them.
  Guards shipped 2026-11-03: the publisher FATAL-refuses any non-6-digit
  publish argument (alongside the 150042 refusal), and the registry queue
  drops non-CCN rows with a stderr note. Phantom removal mechanics: the
  index is a PATCH accumulation over the published base (a no-regression
  validation forbids count drops), so removal means editing the published
  index + summary objects directly (byte-verify after; the summary's
  standardized_* and cpt_* fields are exact sums over the index — recompute
  and assert), deleting the CCN's price file from the local publish tree so
  no patch resurrects it, and leaving its data object orphaned (never
  delete). Index JSON shape: `{"hospitals": [ {ccn, n, ...}, ... ]}` — a
  LIST under `hospitals`, not a CCN-keyed dict; parse it that way in every
  ad-hoc check (the dict-parse silently yields an empty live set).
  A fresh tree must sync `meta/hospitals.json` from R2 before patch_index —
  site_data_metrics reads it (dry-run FATAL 2026-10-28 taught this).
- **Heavy slim (>1 GB uncompressed canonical) → Daytona first.**
  `~/hl-fleet/daytona_slim.py <ccn>`: stages the store canonical + the tree's
  aggregates into a sandbox (default 4 vCPU / 8 GB / 10 GB disk), runs
  slim_parsed there, downloads the price file + updated aggregates back,
  writes the provenance sidecar. Proven 2026-10-28 on the FULL untrimmed
  059810 (1.28 GB raw, ~8.97M payer entries, 49.8 MB gz): 17,243 items —
  byte-for-byte the same count the one successful quiet-VM slim produced —
  sandbox lived 34 s, usage logged to `~/hl-fleet/daytona-slim-usage.jsonl`.
  Fallback if Daytona is down or Mini-local slimming binds memory: stream-trim
  the canonical with the top-60-per-item payer cap (keep the 59 highest
  rate_dollar entries + the lowest per item with >60 payer_rates; streaming
  brace-depth splitter, O(1) RAM — method-work `trim_059810_stream.py`), then
  slim the trimmed copy. The store canonical itself stays untrimmed.
- **The VM is out of the publish path.** `hlfleet.py publish` and
  `fast_publish.py` are legacy; the daily pricing wave (`hl_wave.py`) keeps
  its own VM mechanics and its own state — the Mini publisher never touches
  `hl_wave_state.json`.
- First Mini publish: **451312 (Rice Medical Center), 2026-10-28 11:53 PDT** —
  SUCCESS, byte-verify OK, live 4,397 → 4,398 (coverage gen 18:53:19Z). The
  050390 dry-run before it reproduced live counts exactly (4,397 → 4,397).

## Routing rules

1. Default bulk ingest → Modal (fetch+parse in one hop, parallel by `.starmap`).
2. Files too big for the VM parser (the ~870 MB IU-class CSVs) or RAM-bound →
   Daytona staged parse (12 GB-class sandboxes; raise memory on the create call).
3. Hosts that block datacenter egress (403-class) → Mini fetch first (its
   residential-class egress + real Chrome are the trusted path), then stage.
4. Publishing is ALWAYS the Mini home lane under the publish lock with
   byte-verification (`publish_mini.py` — see Publish doctrine above; the
   VM `hlfleet publish` path is retired). A lane result is never "live" until
   the live coverage read says so.
6. Publish-upload mechanics (root-caused 2026-10-27): fast_publish ships the
   out dir to the Mini and runs ONE ssh call to `~/tools/hl-r2/r2_put.py` with
   an 1800 s cap. Sequential PUTs of ~138 keys died three times (sync read,
   upload abort, then the 1800 s timeout). r2_put.py is now parallel
   (ThreadPoolExecutor, `R2_PUT_WORKERS` default 8; bulk keys first, meta/
   index keys LAST) — the same publish then succeeded in minutes. Original
   kept at `r2_put.py.bak-20261027` on the Mini.
   Slim-completeness gate (added 2026-10-27 after a wrapper died mid-slim
   leaving 82/171 price files and the publish FATALed downstream):
   `hlfleet publish` now checks every CCN's price file in
   `~/hospital-ledger/public/data/prices` before registering state, re-slims
   ONLY the missing ones (2 passes, timeout-tolerant), and exits 3 without
   touching state if the set is still incomplete. Recovery from any
   mid-publish death = re-run `hlfleet publish <ccns>`; it is resumable by
   construction. Pre-patch copy: `hlfleet.py.bak-20261027` (VM workspace).
7. **Mini disk is the fleet's real capacity limit**: the Mini sits at ~99%
   full (3.2 GB free of 228 GB on 2026-10-27) and batch 2b died with ENOSPC
   writing results. Check `df` before launching batches; results dirs and
   staging are the fleet's own footprint (~1 MB per canonical). The big
   reclaim lever is `~/Library/Developer` (~12 GB) — Barklee's call, never
   automatic.
8. **Fleet storage lives on the external drive** (moved 2026-10-27 at
   Barklee's direction): the canonical store is `/Volumes/Share/hl-fleet/`
   on the Mini's 8 TB external (APFS "Share" volume, ~4.2 TB free; the
   MacMini-TM / MacBook-TM volumes on the same disk are Time Machine —
   never touch). `~/hl-fleet/results` and `~/hl-fleet/staging` are
   symlinks into it, so controllers, hlfleet, and the publish leg work
   unchanged. A mirror of results also lands at VM
   `~/workspace/hl-fleet/results/` via rsync; R2 remains the published
   truth. Rule: fleet data NEVER accumulates on the Mini's internal disk
   again — new data dirs go on Share (symlink if a tool expects a
   home-dir path).
5. Daytona credit burn guardrails: `auto_delete_interval` set on every create;
   sandboxes deleted in a `finally`; stopped sandboxes still bill disk — never
   park a fleet of them.

## Campaign state when this skill was written

D1 discovery pool: 520 ACCEPTed sources promoted; ingestion via fleet lanes is
the active campaign (`~/workspace/hl-gap-research/DISCOVERY-CAMPAIGN.md`).
Chunk-1/2 parks (670125, 050390 slim-class; 392033 parse-class; 344004
unparsed) sit in wave-state `deferred` — re-drive them through a lane with
remote_build's explicit RESULT errors instead of the wave's silent parks.

## Campaign driver — the loop that keeps batches moving (added 2026-10-27)

**Why this section exists:** batches 1+2 (305 hospitals) were launched BY HAND
inside one session. When the session ended, nothing owned the next batch — the
10-minute progress job only *monitors* (and republishes a stuck queue), the
daily pricing wave is a 50-URL freshness loop over already-seeded hosts (and
failed its 2026-10-27 run at the edge-proxy preflight), the publish-retry only
drains `hl_pending_publish.json`, and the weekly harvest only feeds the wave's
seed queue on Sundays. Net effect: zero backlog progress between sessions
while every dashboard read "healthy." **A monitor is not a driver.** The fix
is cron `hl-campaign-driver` (hourly 2026-10-28 → **every 10 minutes with
parallel lanes since 2026-10-30, both at Barklee's direction**, goal-owned):
derive the queue → top up free controller slots → when a controller finishes
OR DIES, publish its yield serially and let re-derivation recycle the
remainder → keep the lessons gate discharged → update
`hidden_files/hl_campaign_state.json` → report only on change. The 10-minute
monitor stays as the fast detector; the driver is the mover. Checking cadence
does not speed a batch — batches run continuously once launched — it caps
the idle gap between a batch ending/dying and the next one starting; that
gap is why hourly starved a 65-deep queue and 10 minutes replaced it.

**Parallel-parse contract (Barklee, 2026-10-30 — this SUPERSEDES the old
"never run two fleet batches at once" limit; the never-two-publishes rule
stands unchanged):** up to 3 controllers run concurrently, ONLY on disjoint
CCN partitions recorded in the state file's `active_partitions` (batch,
lane, log, full CCN list, launched_at) BEFORE launch; in-flight CCNs are
excluded from queue derivation; a finished/dead partition's unlanded CCNs
recycle through re-derivation, never a named relaunch. The Daytona
controller is a worker pool (parallel sandboxes inside one controller,
`--parallel N`, default 32, hard cap 100). Daytona's ceiling is a tier
compute pool, not a sandbox count (daytona.io/docs/limits, verified
2026-10-30): Tier 2 = 100 vCPU/200 GiB (card + $25 top-up), Tier 3 =
250 vCPU/500 GiB ($500 top-up) — with 4 GiB sandboxes RAM binds first
(~50 at Tier 2, ~125 at Tier 3), so "100 sandboxes" is a Tier-3-and-lean
number; sandbox creation rate limit (300–600/min) never binds. The binding
local constraints are Mini-side: staging lives on `/Volumes/Share/hl-fleet/
staging` (moved off the ~11 GiB-free internal disk in the pool build), and
fetches are capped per-host (2 concurrent per hostname — hospital CDNs 403
readily) with a ~24 global fetch cap. Interim rule while staging was still
internal: only one ZIP/XLSX chunk at a time. Big sources (>100 MB staged)
get a dedicated 8 GiB sandbox (the exit-137 OOM class). E2B fills a slot via
`e2b_controller.py` under the same jobs-TSV/SUMMARY contract when present.

**Modal workspace can die mid-batch (2026-10-27 23:41 PDT):** batch 3
stopped after 3 results with `ConflictError: workspace ... is disabled`
(app ap-jD4Bl3WVFmVXu4TLxSZytI, created 23:39, stopped 23:41). The
10-minute monitor missed it for ~25 minutes because it measured artifacts
(fleet file count went UP by 3), not controller process + log health.
Cause unverified — Starter-credit exhaustion is the leading hypothesis
(fresh $30 lands Nov 1). Driver protocol: a batch log with a terminal
error and no live controller process = DEAD batch; publish what landed,
set `modal_disabled` in the campaign state, and relaunch the remainder on
Daytona the same run. Never relaunch blindly into a disabled lane.

**Daytona sandbox disk hygiene (fixed 2026-10-28, batch 5 post-mortem):**
the controller uploaded each job's staged source into ONE long-lived sandbox
but never deleted it, so batch 5 died by accumulation: the first two jobs
shared a 3.7 GB source (University Hospitals), the 10 GB sandbox disk filled
by job 3, and 40 of 45 jobs failed upload with "no space left on device" —
each error looking per-job while the cause was batch-global. Fix: after every
job (success AND error paths) the controller runs `rm -rf src-<ccn> out`
inside the sandbox before the next upload. Batch 6 (same queue) ran its
first 27 jobs with zero disk errors. Rule for any future controller: a
sandbox's disk is a per-job budget, not a batch budget — clean up inside the
job loop, always.

**Daytona controller is failure-isolated per job (fixed 2026-10-28):**
the first Daytona fallback chunk landed 2 jobs, recorded a third, then a
source-fetch 403 (`resp.raise_for_status()`) crashed the whole controller
and stranded 41 jobs. The per-job body is now try/except-wrapped: a
fetch/parse failure records `{ccn, error}` in the summary and the batch
continues. Any future controller (any lane) ships with per-job isolation
from the start — one bad hospital must never be able to kill a batch.

**Queue derivation — D1 REGISTRY IS PRIMARY since 2026-10-30** (migration
report: ~/workspace/hl-fleet/method-work/d1-registry-migration-20261030.md):
the campaign registry lives in Cloudflare D1 database
`hospital-ledger-registry` (uuid in fleet records),
table `hospital_registry`, **CCN is the PRIMARY KEY** — the duplicate-row
class is now structurally impossible. Driver flow: refresh with
`~/workspace/hl-gap-research/registry/import_registry.py --refresh`
(idempotent rebuild from the verdict TSVs + live index + Mini store), then
`registry_queue.py` prints the submit-ready jobs TSV (stage='submit_ready';
file-class first, then ZIP, then XLSX). Equivalence gate PASSED zero-diff
2026-10-30 after the 7 adjudicated not-found stale ACCEPT rows (070020,
100287, 219801, 21C0001564, 281353, 370243, 39013F) were deleted from
b1–b3 (backups .bak-20261030-notfound7); the importer's hardcoded
NOT_FOUND_7 keeps their stage='not_found' across refreshes. The registry
universe is the UNION set (hospitals table ∪ verdict CCNs ∪ live-index
CCNs) = **5,968 rows** — the staging hospitals table alone (5,426) misses
446 live-index CCNs and 324 ACCEPTs; the 5,426-based headline arithmetic
is the staging-DB subset, kept for continuity. TSV fallback recipe
(unchanged, used only when a registry refresh fails): submit-ready =
D1-pass ACCEPTs (`~/workspace/hl-gap-research/d1_verdicts_b*.tsv`) − CCNs
live with n>0 (live index via the Mini leg) − CCNs already in the fleet
results store —
the results-store set is parsed-but-not-live (dialect/staged problems, NOT
fetch jobs; refetching them wastes a batch). Exclude quarantined 150042
always. **Registry-refresh silent deaths — root-caused + fixed 2026-11-02:**
detached `import_registry.py` processes on the VM die silently at random
ages (no traceback; the launching agent session's lifecycle reaps them —
proven by elimination: a supervised run the same afternoon completed with
rc=0 at 36 MB peak RSS, so not OOM and not the swap phase). On 2026-10-31
a run even died between the staging swap and the READ-BACK print. What
changed: egress retry storms (60 s timeout × 4 attempts per D1 query)
stretched a refresh from ~2 min to a measured 778 s, so it stopped
fitting inside its launcher's life — five consecutive launches died on
2026-11-02 (frozen at RECONCILIATION / batches 28, 44, 54, 54, and after
60/60). Fixes in the importer (backup
`import_registry.py.bak-20261102-resume-swap`): (1) resume-through-swap —
a fingerprint-matching `hospital_registry_new` at ANY landed count now
resumes, and at landed == row count the batch loop no-ops and the run
goes straight to verify + swap (previously the complete staging table
was DROPped and all 60 batches rebuilt); (2) mid-swap recovery — if the
live table name is missing with the previous generation parked in
`hospital_registry_old`, it is restored before any DROP; (3) `d1_query`
tuned to 30 s timeout / 5 attempts / 3-6-9-12 s backoff; (4) SWAP phase
prints + flush=True throughout, so a death is never silent. Structural
note: a refresh that must outlive its launcher belongs in a systemd
oneshot or a persistent session, not a cron worker's nohup — proposed,
not yet adopted.
**One ACCEPT row per CCN, ever:** when re-discovery corrects a CCN's
URL (misattributed/shared-file class), the new row goes in the next bN file
AND the stale row is deleted from its original file the same pass (backups
kept) — duplicate rows leave the winning URL to implementation chance
(learned 2026-10-30, misattributed-20 re-discovery: 13 corrected rows in b6,
13 stale rows removed from b1–b3). File-class first (cap ~150/batch), then ZIP, then XLSX via
`extract_zip_csv.py`. Names from the staging DB hospitals table
(`~/workspace/hl-install-20261026/v3/tree/hospital-ledger/db/hospital_ledger.db`).
Verified 2026-10-27 23:45 PDT: 205 ACCEPT-not-live = 25 already-parsed +
180 submit-ready (136 file / 27 ZIP / 17 XLSX); batch 3 = the 136 file-class.

**Launch mechanics (Mini):** disk-preflight first; scp the jobs TSV to
Mini `~/hl-fleet/`; launch `nohup ~/hl-fleet/.venv/bin/python3 hlfleet.py
submit batchN-jobs.tsv --lane modal > d1-batch-modal-N.log 2>&1 < /dev/null &`
— macOS has NO `setsid` (a setsid launch attempt died instantly 2026-10-27);
plain nohup + disown survives the SSH session ending.

**Lessons-gate hygiene:** any `hl_wave.py` chunk run accrues a lessons debt in
`hl_lessons_debt.json`; an owed debt silently blocks the gated chunk path.
Chunk-2's debt sat owed from 08:30 UTC while fleet work bypassed the gate —
discharged 2026-10-27 with 3 lessons (publish-parked = transport verdict;
parks are lane-specific; discharge is part of the run). Check
`hl_lessons_gate.py status` in every campaign pass; 0 owed is the healthy
state.

**First-seen-domain approval cards — ALL direct VM egress cards, scripts
included (root-caused 2026-11-01, Barklee-directed /debug; this entry
CORRECTS an earlier same-day draft that blamed the cron worker's browsing):**
the VM's egress gate raises a human approval card for ANY first-seen
hospital domain, from ANY process — agent web tools, shell curl, or a
Python script's httpx/urllib. (Diagnosed 2026-09-21: "shell approval cards
arise only from first-seen domain attempts; re-probing already-seen hosts
produces no cards." The wave pipeline was immunized 2026-10-13 by routing
through `hl_edge` → `hospitalledger.com/api/fetch`.) The CMS HPT harvest
scripts were never migrated: on 2026-11-01 `cms_hpt_expand.py` probed
~16k domains directly, and card bursts tracked ITS progress (~25 at 14:07,
17 at 16:37 in a 0.2s machine burst, 10 more at 16:53–16:58) — decisively,
cards kept arriving AFTER the supervising cron job was deleted, while the
detached script still ran; killing the script (13,900/16,361) stopped the
stream, and the still-unpatched `promote_discoveries.py` (urllib) added a
few more from its own verify hosts. Two wording-level fixes failed first
(job-body HARD RULE rewrite; deleting the job) because the mechanism was
never the worker's wording at all. **CODE FIX (shipped 2026-11-01):**
`cms_hpt_expand.py`, `cms_hpt_harvester.py`, and `promote_discoveries.py`
now load HL_FETCH_PROXY/HL_FETCH_TOKEN from the fleet env file on the Mini (the
file `hl_wave.py` reads; `.bak-20261101-edge` backups alongside) and build
every client with `hl_edge.httpx_kwargs(...)` — the VM only ever connects
to hospitalledger.com. Proof: `test_hl_edge.py` 10/10 in a sanitized env;
smoke test fetched scripps cms-hpt.txt via the edge (5 entries) with ZERO
new cards; the patched full rerun's hit rate jumped (261 HPT hits in the
first 2,200 domains vs 243 in 13,900 direct — Cloudflare egress isn't
bot-walled like the proxy legs). Standing rules: (1) before running ANY
HL script that touches hospital URLs from the VM, grep it for `hl_edge` —
no edge routing, no run; new fetchers use `hl_edge` from the first commit.
(2) Agent web tools remain forbidden for bulk work (they card too), and
cron-job probes are shell curl to example.com / hospitalledger.com ONLY —
never a hospital domain (the 2 standing schedule-scoped cards for
scripps/mayoclinic came from health-check curls). (3) Cards appearing
during a run are a CANARY for broken edge config, not a wording problem:
stop the fetcher, check the `hl_edge` preflight line, fix config/code —
never just rewrite the job body and re-fire. (4) A `cron.stop` does not
cancel a queued occurrence; it auto-starts and needs its own stop.
Pending cards from stopped runs are inert leftovers (the permission
surface is list-only; Nova cannot clear them) — Barklee can dismiss them.
**Second source, same day (17:22–17:33 PST): the campaign-driver cron
worker.** After the scripts were proven clean at the socket level (the
harvest process held 60 connections, ALL to 127.0.0.1; the proxy log
carried only hospitalledger.com), a fresh burst still landed — card
purposes worded as browsing ("wants to visit poplarhospital.net", "wants
to open the Riverside Regional Medical Center website"). The 17:20 and
17:30 driver runs had improvised source-URL "verification" with their own
web tools; the driver body never asked for any fetch. Forensics note:
each agent fetch raises a PAIR of cards (a TLS-connection card and a
share-info card) that surface ~4–6 minutes apart, so bursts smear past
the moment the fetcher stops — judge a fix by fetches, not by the last
card's timestamp. Remediation: the driver was stopped mid-run and
disabled, then re-enabled only after its body gained the explicit
fetching rule; the SAME one-paragraph rule ("never use agent web tools
on any URL in this job; all contact via the scripts / the Mini / the
body's named curl targets; record the need and move on") was added to
hl-progress-10m, hl-publish-retry, hospital-ledger-pricing-wave, and
hospital-ledger-freshness-watch the same evening. **Standing rule: EVERY
HL cron body carries that paragraph — a body that never mentions
fetching still produces fetches when a worker improvises verification,
so the ban must be explicit, not implied by the steps.**

## Deepsearch discovery lane (added 2026-11-03 — the wave-5/6 engine)

When the parse lanes starve (no submit-ready sources), discovery runs on a
multi-provider deepsearch engine, NOT on a single search API. Built
2026-11-02/03 after the wave scripts' hardwired Exa lane died
(`NO_MORE_CREDITS`, 2026-11-02 20:33) and the campaign briefly treated one
provider's exhaustion as a search-lane block. **Rule: a dead provider means
swap engines, never stall** — no single provider (Exa included) is ever on
the campaign's critical path again.

**Engine facts.** Invocation is positional:
`deepsearch -n 8 [-p <provider>] "<query>"`, JSON on stdout. The remote
route rejects long flags (`-q`, `--max-results`, `--json` → rc=2 with empty
stdout); keep the call shape exactly as the proven runners do. Providers
(verified 2026-11-03): default order ≈ searxng, tavily, context
(context.dev), tinyfish live; brave EXHAUSTED (402), jina EMPTY BALANCE,
exa NO CREDITS. A runner must NEVER swallow call errors into empty
results — log rc/stderr per call (a swallowed-error sweep once reported a
clean zero-yield that was really two invocation bugs).

**Failover hides the strong lanes — fan out instead.** The engine is
failover-by-design (first success wins), so single-lane runs are answered
almost entirely by the fastest/weakest provider. The discovery default is
therefore a FAN-OUT: the same query to tavily + context + tinyfish in
parallel, results merged by URL with per-lane attribution. Benchmark
(2026-11-03, identical file-oriented query, 237 never-searched pool CCNs,
hits = file+page URLs surfaced):

| lane | targets with hits | file URLs (unique) | page URLs (unique) |
|---|---|---|---|
| tavily | 91 | 1 (1) | 94 (86) |
| context | 69 | 5 (4) | 52 (31) |
| tinyfish | 47 | 2 (1) | 32 (14) |
| default (searxng) | 7 | 3 (3) | 7 (6) |

Tavily is the page hunter, context.dev the file hunter, tinyfish a useful
third; the union (10 files, 159 pages) held dozens of finds every single
lane missed. The fan-out also absorbs a lane outage mid-run (tavily went
"worker unreachable" during the state sweep; context+tinyfish carried it).

**Query shapes (rotate, don't repeat).** Pass 1 (csv/json):
`"<NAME>" <city> <state> standard charges filetype json OR csv OR xlsx
machine readable`. Pass 2 (snippet harvest): filetype-oriented query,
harvest file URLs from result SNIPPETS too (tavily snippets carry direct
file links the result list misses — pass 2 out-yielded pass 1 ~10x on
pages). Variant pass (wave 6): `... chargemaster price transparency
filetype:xlsx OR filetype:zip` — thinner (2 publishes from 224 targets) but
nonzero on a pool two earlier shapes had covered.

**State central postings crack state psych hospitals.** State-operated
psychiatric facilities often post through the state DMH/DHHS central site,
not their own domain (pattern proven: TN DMH ×4, Maine DHHS, MS DMH —
Whitfield). Sweep shape: one fan-out query per state SYSTEM naming the
agency + flagship hospitals + "standard charges", extract the agency
pages, grade files under every in-state pool CCN from that system.
Enforcement complaints are not the only psych path.

**Pipeline.** Search runners are resumable via a done-file (error rows
never count as done) and emit `candidates-*.jsonl` + `pages-*.jsonl`. Then:
page→file extraction → content-identity fetch → STRICT grader
(state/street vetoes, umbrella-anchor rule) → umbrella reassignment
(in-file hospital name matched to the hospital DB at token score ≥0.6,
re-graded under the true CCN) → canonical build → store → SERIAL
publish, byte-verified, live-index read-back. Aggregator-pointer trap: an
MRF-index site's data.json can pass the identity grade while containing a
pointer, not prices (items=1 priced=0 at build) — the build's priced count
is the last gate; a priced=0 build is a REJECT, never a publish. Aggregator
records are leads, never evidence: cross-matching an MRF index against the
pool produced 4 grader RIGHTs of which 2 were same-name cross-state
impostors (a CA-licensed file for Springfield IL's Memorial Medical
Center; a Virginia file for Burke SD's Community Memorial) — both passed
on name-only identity because the fetched head carried no contradicting
state evidence, and both were caught only by manually reading the file
header (`license_number|CA`) and the DB city/state before publishing.
**Rule: for generic hospital names (Memorial / Community / Union /
St Mary …), eyeball the raw file header and the DB city/state yourself
before any build is published — the grader's silence is not identity
proof.**

**CMS V3.0.0 dual-header CSVs parse as 0 items (diagnosed 2026-11-03).**
The V3 template CSV layout is: row 1 metadata header
(`hospital_name,last_updated_on,version,…`, some names carrying embedded
tab characters), row 2 the identity values, row 3 the DATA header
(`Description, code|1, standard_charge|gross, …`), then data rows. The
fleet CSV path reads row 1 as the header and produces 0 items (both
lanes, confirmed). Lossless workaround: rewrite to a V2 single-header
file (strip tabs from the row-3 header names, drop rows 1–2) and build
that — the canonical records the hospital's true source URL. The V3 XLSX
path already handles its layout. Open parser task: teach the CSV dialect
detector this layout, with a V3 dual-header file as the regression
fixture.

**Ad-hoc runner duties (session waves).** (1) Re-assert the 150042
quarantine when building ANY target pool — pool files do not encode it
(2026-11-03 near-miss: the fan-out pool included it; grading/build gates
caught it, its real file now sits in verdicts as a held REJECT).
(2) Parse the live set from the index's `hospitals` LIST (see the CCN-key
guard note in Publish doctrine) — a dict-parse silently yields an empty
live set and duplicate work. (3) Copy the working invocation from a proven
runner instead of re-deriving flags. (4) Detached launches only
(setsid/nohup + log); outcomes must be recoverable from logs and done-files
alone — session-owned controllers die at compaction.

## Method-work lessons (2026-10-28, remaining-hospitals mission)

- **Dialect queue was mostly stale parses, not bad files.** Fresh Mini fetch +
  current parser flipped 21 of 27 priced=0 dialect CCNs (incl. Seattle
  Children's 503300: fleet canonical had literal quote contamination and 0
  priced; fresh parse 19,047/19,047). First move for any priced=0 class:
  re-fetch + reparse before touching aliases.
- **Poisoned learned alias:** fingerprint 428961199a411c20 mapped gross=null
  for Hemet's CDM layout (CDMCHARGE col) and adaptive recovery missed it
  (token similarity 0.80 < 0.82 cutoff). Fix: `cdmcharge` added to
  GROSS_COL_HINTS + first_gross_col in payload AND repo mrf_parse.py;
  alias deleted from payload header_aliases.json. When a priced=0 file's
  raw source visibly contains prices, suspect the learned alias first.
- **Spanish headers:** minimo/maximo added to MIN/MAX_COL_HINTS (PR files).
  Note the slim gate still requires gross/cash/payer — a min/max-only file
  (Auxilio Mutuo's shoppable list) cannot go live regardless.
- **SiteGround sgcaptcha class (403s):** Mini curl gets HTTP 202 + a
  240-byte captcha page even for direct file URLs. Working route: fcdp in
  the Mini's real Chrome — opening the file URL passes the captcha and
  downloads to ~/Downloads; for stale upload paths, fetch the site's
  /price-transparency/ page from inside the tab and extract the current
  file link (monthly WP upload rotation changes both folder and filename,
  e.g. Caribbean's "standarcharges1.csv" typo is real). Wayback works too
  when the PT page is archived (050543 chcm.us).
- **Verifier misattribution is a class, not noise:** same-name/wrong-hospital
  ACCEPTs (Rochester Regional's shared RGH tall file seeded to 5 sibling
  CCNs; UMMS x4; Florida AHCA MiamiDade xlsx x4; San Jose page for FL/NY
  Good Samaritans). Before parsing a shared/system file for a CCN, read
  its metadata rows (hospital_name/location) — never publish system rows
  under a sibling CCN.
- **OOM class:** Mini-local duckdb parses with DUCKDB_MEMORY_LIMIT=6GB +
  TMPDIR on the external volume cleared 3-5.3 GB tall ZIPs. For giant
  JSON, install ijson on the Mini and use stream_parse_large_json over a
  localhost http.server. **10 GB single-CSV class (170049 UKHS Olathe,
  landed 2026-10-31):** remote_build.py's `open().read()` of the whole
  source is itself the OOM (exit 137 in every sandbox, incl. the
  dedicated 8 GiB one, whose upload also timed out). Working route —
  Mini-local, no sandbox: (1) duckdb CLI extracts the rows with the
  EXACT call `duckdb_delimited_rows` makes (read_csv_auto, all_varchar,
  header=false, ignore_errors, null_padding, sample_size=20480, LIMIT
  max_rows+64 = 200,064) into a rows.csv on Share; (2) a driver mirrors
  the sandbox layout (scripts/ + data/header_aliases.json copied from
  payload, so mrf_parse ROOT/alias behavior is identical), monkeypatches
  `mp.read_delimited_rows` to return the extracted rows, and calls the
  UNMODIFIED `mp.parse_csv_tall` — record construction copied verbatim
  from remote_build. Note the parser's own 200k data-row cap binds on
  every engine path: for payer-dense tall files (~253 payer rows/code
  at UKHS) the canonical covers the first 200k data rows (170049: 790
  items, all priced) — that is the pipeline standard, not a truncation
  bug; raising the cap is a parser-wide decision, not a per-CCN fix.
  Work dir kept at `/Volumes/Share/hl-fleet/work-170049/`.
  **Non-UTF-8 giant CSV class (210055 UM Laurel, landed 2026-11-02):**
  a source carrying raw Windows-1252 bytes (0x93/0x94 smart quotes in
  notes fields) makes the duckdb-CLI extract SILENTLY truncate — COPY
  writes only the header rows while count(*) still succeeds, masking
  the failure. Byte-check the source for non-UTF-8 BEFORE choosing the
  duckdb route; when found, skip duckdb and feed the parser its stdlib
  row path instead (decode errors='replace' + mrf_parse's own
  delimited_rows, first 200,064 rows), replicating parse_inner's
  dispatch — the parser itself stays unmodified. 210055 took the wide
  branch (CMS v3 All-Payers / HSCRC single-rate): 200,000 items, all
  priced. Work dir kept at `/Volumes/Share/hl-fleet/work-210055/`.
- **Rendered-page discovery patterns (2026-11-02, nine CCNs landed via
  fcdp on the Mini's real Chrome):** (1) Emory's transparency page
  renders per-hospital current-year CSV links directly in the DOM —
  when static seed files 404, render the page before concluding
  source-dead. (2) Valley Hospital (NJ) gates its MRF behind a Drupal
  AJAX webform with NO user fields — clicking "Download Machine
  Readable File" in the real browser downloads the file. (3) ClaraPrice
  portals (e.g. Graham Regional via grahamrmc.com) redirect
  /machine-readable to the EIN-named JSON. (4) Valley Health
  (valleyhealthlink.com) rotated from dated ZIP paths (now 404/Akamai)
  to per-hospital CSVs under `/app/files/public/<uuid>/` linked from
  its transparency page; curl still 403s on the file host — download
  through the browser tab. (5) NAME-COLLISION TRAP: chc.la is College
  Hospital Cerritos (CA), NOT Graham Regional (TX) — the 450085 seed
  pointing there was a misattribution; when a seed domain's files all
  name a different hospital, re-discover from the hospital's own site
  and correct the registry ACCEPT rows (corrected row to the newest
  b-file, stale row removed, backup kept). UCSD 059810: 47,768 items but ~8.97M payer
  entries (49.8 MB gz canonical) — VM slim OOMs on payer volume even when
  item count is modest; it passed once on a quiet VM. Payer-cap fallback:
  top-60/item (method-work/trim_059810_stream.py, pure-python streamer).
- **Home-grown fee headers + stale-staging trap (100298 Florida State
  Hospital, landed 2026-11-02):** the FL DCF chargemaster XLSX uses
  `STANDARD FEE` / `DISCOUNTED CASH FEE`; strict GROSS_COL_HINTS has no
  'fee' token, and adaptive recovery's last-resort (exactly one unclaimed
  numeric column) was blocked because REF # is also numeric — so gross
  learned as null and 202 items priced 0. Fix per the alias mechanism's
  own scope: aliases are keyed by header-layout FINGERPRINT (sha256 of
  the ordered normalized header list), so correcting fp
  37e1a69cd58509c7's entry to gross=3 touches only files with this exact
  11-column layout — no code change, no global hint. Corrected entry
  written to the payload + work-dir + VM wave-tree header_aliases.json
  copies (backups kept); re-parse priced 202/202 with full-file
  (code, fee) equality vs the workbook; regression = 100113 re-parse
  pre/post fix byte-identical to its published canonical. Two traps in
  the same CCN: (1) the DISCOUNTED CASH FEE column is populated on 0/202
  rows — cash null is the hospital's data, not a parse miss; check
  column population before assuming a mapping failure. (2) The Oct-28
  `staging/100298.src` is an AHCA facility-DIRECTORY workbook (single
  'Facility Data' sheet), not the MRF — the real source was
  `staging/six-20261102/100298.src` (26 KB, sheet 'FSH'). A staged .src
  is not necessarily the file its CCN's canonical was built from;
  verify sheet names/headers against the canonical's items before
  reusing a staged source.
- **hlfleet publish hardening (all 2026-10-28):** (1) pull skip now
  requires local size == Mini store size — a gzip-valid STALE canonical
  was silently slimmed to n=0 (050390); (2) a fresh pull invalidates the
  derived slim price file; (3) slim runs in chunks of 3 with rcs printed —
  one 15-CCN slim process died silently (VM 7.9 GB, no swap); (4) publish
  refuses to proceed if _compliance/_payer_raw line counts shrink (a
  SIGKILLed slim truncates the incremental aggregates; rebuild them from
  the price files with method-work/rebuild_aggregates.py — full-mode slim
  is NOT the rebuild path on this VM: its index_from_prices CPT branch
  OOMs and merge defaults off).
- **fast_publish does not consume the local aggregates** (build_site_data
  ignores them); the R2-synced index is canonical and patch-merged.
  Aggregate truncation is an analytics-offline injury, not a publish
  blocker — but the shrink guard stays: it caught a real corruption.
- **D2 EIN-prefix crawl:** rejects are mostly rotated WP uploads; the EIN
  token in the filename is stable across rotations. One PT-page crawl per
  origin re-mapped 6 (all verified ACCEPT and landed). Yield is low where
  PT pages are JS-driven — next lever is rediscover_mrf_urls.py over the
  repo DB + find_missing_mrfs.py (Exa) for the D3 tail.
- **WMCHealth consolidation (330158 Good Samaritan of Suffern, landed
  2026-11-02):** goodsamhosp.org is retired (its billing page AND the
  2022 seed XLSX return genuine 404s, no redirect). All WMCHealth
  facility files now hang off the single system PT page
  wmchealth.org/patients-and-visitors/insurance-and-billing/price-transparency
  as per-facility EIN-named CSVs under wp-content/uploads (standard-charges/
  for the Bon Secours-community facilities). Fetch asymmetry: Mini curl
  gets the Cloudflare WAF block page on the PT page path but downloads
  the uploads files fine; hl_edge fetches the page fine (403-class via
  the seeded goodsamhosp page, so the script alone found nothing — the
  PT page URL came from the wmchealth page-sitemap). Suffern's file:
  131740104_GoodSamaritanHospital_standardcharges.csv (EIN matches the
  2022 seed), CMS v3.0 WIDE, 141,657 items / 140,711 priced, live
  n_slim 134,062. The dead b6 ACCEPT it replaced was a dated 2026-01
  wmchealth.org/sites/default/files JSON under a different EIN token
  (590623173) — WMCHealth URL rotations change both host path AND
  filename; the EIN in the CURRENT PT-page link + the file's own
  metadata (name + 255 Lafayette Ave., Suffern, NY 10901) is the
  adjudication, never the stale row.
- **Site-numbered legal names (670211/670212 Americas ER, landed
  2026-11-02):** one ACCEPT row had attached the SAME file (E001) to two
  CCNs and both sat held as an "unattributed shared file." The
  transparency page actually links a per-site file family (E001/E002,
  P001/P002, U001/U002, IM002) anchored "America's ER Site 001" /
  "Site 002" — and the CMS legal names ARE "America's Er Site 001, Llc"
  (670211, Magnolia TX) and "America's Er Site 002, Llc" (670212,
  Cypress TX). The site number is the facility's legal name, so the
  page anchor is attribution by name; each file is that site's own
  (prices differ per site). Before holding a "shared" file, enumerate
  the page's full token family and check whether the token is the
  CCN's legal name in the hospitals/seed tables. Registry corrected
  same pass (670212→E002 in b15, stale b3 row removed, backup kept);
  both built unmodified-parser from hash-verified staged sources and
  published (1,189 and 1,176 priced; coverage 4,601→4,603).
- **Puerto Rico portal crawl (2026-11-02, PR cluster closed: 3 landed,
  2 adjudicated no-source):** (1) The PR Dept of Health Blazor Server
  portal `portaldelpaciente.salud.pr.gov/PriceTransparency/<ROUTE>`
  hosts per-facility routes (HURRA = Ruiz Arnau 400105; HUA = Univ. de
  Adulto 400061). The base path renders Blazor "Not found" — routes are
  per-hospital acronyms. HUA renders "No data available to display at
  this moment" = the facility publishes NOTHING through the portal
  (a verdict, not a dead end to retry). (2) Blazor Server download
  mechanics: "Download (.json)" streams a server-generated file via
  `downloadFileFromStream` (DotNetStreamReference) into ~/Downloads.
  fcdp traps, all hit live: DOM `.click()` and text/coordinate clicks
  on the dropdown item while the menu lacks real layout (bounding rect
  0x0) silently do nothing — force the menu open (add `show` class +
  display:block to the `ul.dropdown-menu`), read the anchor's real
  rect, then `fcdp click` those coordinates. NEVER do a modal-close in
  the same JS call as the menu-open (the Blazor re-render discards the
  forced DOM and the click lands on stale geometry). NEVER wrap
  `downloadFileFromStream` to inspect the stream — `arrayBuffer()`
  single-consumes the reference and kills the real download; hook only
  as a diagnostic, then reload the page for the clean capture. With
  the menu genuinely open, the HURRA stream (16.7 MB) completed in
  <10 s; the "slow server generation" theory was wrong — it was click
  geometry all along. (3) Metro Pavia facilities publish via the
  Inmediata HPT platform: each hospital's own site footer carries
  "Price Transparency Download File" → `hpt.inmediata.com/?handler=DF
  &d=<facility GUID>` serving `<EIN>_<Name>_standardcharges.json`
  (CMS v3 layout but charges under the v2 key
  `standard_charge_information` — the stock parser handles it).
  SIBLING TRAP paid rent again: hospitalpaviapr.com is the SANTURCE
  facility's site — its GUID serves Santurce's file; Hato Rey's file
  hangs off hospitalpaviahr.com's own GUID. The GUID on the CCN's OWN
  site + in-file name/address/EIN is the adjudication. (4) Wilma N.
  Vázquez (400115) CHOICES Medical portal (precios.wilmamed.com)
  exposes ONLY a shoppable.json (2,388 entries, dated 2022-09-10, bare
  array with NO in-file hospital identity, payer fields as methodology
  strings) — shoppable-only, REJECT; no full MRF exists on that portal.
- **Dialect queue CLEARED (2026-11-02, all 6 resolved):** the five
  Rochester CCNs (330030 Newark-Wayne, 330073 United Memorial, 330197
  Canton-Potsdam, 330265 Clifton Springs, 330411 Unity) were NOT a
  parse-dialect problem and needed NO re-discovery: the registry (b6)
  already held the correct per-facility MRF2026 URLs the whole time —
  the store canonicals were stale builds from a superseded seed, all
  five parsed from Rochester General Hospital's 2024 file
  (`160743134_rochester-general-hospital_standardcharges.csv`, in-file
  identity "Rochester General Hospital" — RGH is none of the five).
  **First move for any "shared file seeded to siblings" cluster: diff
  each store canonical's `source_url` against the CURRENT registry
  ACCEPT URL before re-discovering or touching aliases** — here the
  fix was a plain Mini-curl fetch of the already-verdicted URLs
  (msc.rochesterregional.org MRF2026 per-facility EIN-named CSVs, CMS
  v3.0 WIDE, in-file hospital_name exact per CCN, last_updated
  2026-04-01), unmodified-parser rebuild (~99% priced), one serial
  publish_mini: live 4,606 → 4,611. The b6 ACCEPT rows needed no edit
  (one row per CCN, already correct — appending duplicates to the
  newest b-file would recreate the duplicate-row class). Residual:
  RGH's OWN tall file genuinely parses priced=0 (adaptive-recovery
  desc='hospital_name') — that dialect issue belongs to RGH's CCN,
  not the siblings. 400016 (Auxilio Mutuo) closed the same pass as a
  formal REJECT: store canonical verified 310 items / 0 priced, the
  source is the CMS shoppable-services list ("SIN CARGOS PRIVADOS")
  with no standard charges in the file at all — REJECT row to b15,
  stale ACCEPT removed from b3 (backups `.bak-20261102-400016`),
  mirroring the 400115 shoppable-only adjudication.
  dialect-queue.txt is now EMPTY (backup `.bak-20261102-cleared`);
  work artifacts kept at `/Volumes/Share/hl-fleet/staging/dialect-20261102/`
  (sources, out/, build.log, old-shared/ = the poisoned RGH canonicals).
- **RGH residual RESOLVED (2026-11-02, 330125 Rochester General LIVE
  with its own data):** the full picture was worse than a dialect bug —
  330125 itself had been LIVE since 2026-10-28 with UNITY HOSPITAL's
  data (the b1 ACCEPT pointed at Unity's 237221763 file, which parses
  fine; a misattributed file that prices well is invisible to every
  priced=0 queue — only an identity read of the live record catches
  it). Root cause of the siblings' priced=0: the Oct-27 lane builds
  resolved columns on the file's 7-column CMS METADATA header instead
  of its 25-column charge header (the canonicals' own column_notes:
  `adaptive-recovery:desc='hospital_name'`). On the meta header, strict
  hints find no desc and no gross; adaptive recovery salvaged
  desc→hospital_name, gross stayed None, so all 47,547 items built
  unpriced — while real descriptions still flowed because both headers
  put their name field at index 0, masking the failure as a file
  dialect. find_header_row is a scored contest (meta header = 3 signal
  groups, charge header = 8); the Oct-27 build path lost that contest.
  The CURRENT parser (hl-stamp-test and payload copies alike) picks
  row 2 and parses BOTH the 2024 v2.0.0 file (16,727/16,727 priced on
  a 24 MB range-prefix, values exact) and the current MRF2026 v3.0.0
  file correctly, and correct strict aliases are already learned for
  both charge layouts (fp 313345c35223e0ee, fp 00639534b2e71288) — NO
  alias or code change was needed, and no poisoned alias for the
  meta-header fp (aa55e975ac6aeb1c) exists in any alias copy. The fix
  was data + registry: corrected ACCEPT appended to b17 (own MRF2026
  URL; in-file identity verbatim — Rochester General Hospital, 1425
  Portland Avenue, Rochester, NY 14621, last_updated 2026-04-01),
  stale b1 row removed (backup `.bak-20261102-330125`), rebuilt with
  the unmodified parser: 20,695 items / 20,486 priced (spot checks
  exact vs source: gross 2564.47 / cash 1666.91 on code 11000001;
  Fidelis negotiated 75428.81 on code 0011); regression = 100113
  re-parse identical to its published canonical on every field excl.
  fetched_at; publish_mini SUCCESS, byte-verify OK — live 330125 is
  now Rochester General, n_slim 20,486 (headline coverage unchanged
  at 4,641: the CCN was already counted, carrying the wrong hospital's
  prices). Standing moves: (1) **a live CCN's identity is part of
  verification** — read hospital_name/source_url on the live record,
  not just n>0. (2) When a priced=0 canonical's notes show adaptive
  recovery picking a METADATA column (hospital_name/hospital_address),
  suspect header-row detection, not the file — reproduce with the
  current parser before touching aliases or hints.
- **A NEW d1_verdicts b-file MUST start with the header row
  `ccn\turl\tverdict\tdetail` (found 2026-11-02):** b15 was created
  headerless by the dialect pass; the next registry refresh relaunch
  died instantly with KeyError 'verdict' (the importer parses with a
  header). The campaign driver repaired it same-run (header prepended,
  backup kept, relaunch verified alive). Rule: when creating b(N+1),
  write the header first, then rows; when appending, verify line 1 of
  the target file is the header before writing.
- **hl_edge preflight rides the ambient proxy (found 2026-11-02):**
  `hl_edge.preflight()` is a SYNC httpx.get that honors the shell's
  ambient HTTPS_PROXY — with the ambient 3128 leg dead, every
  edge-routed script (rediscover included) dies in preflight with a
  ReadTimeout BEFORE any fetch, looking like an edge failure while the
  edge is fine. Run HL scripts with `env -u NO_PROXY -u no_proxy
  HTTPS_PROXY=http://127.0.0.1:8899 https_proxy=http://127.0.0.1:8899`
  (resilient-egress leg): preflight passes in ~2 s and the async client
  constructs cleanly (the unset also clears the bracketed-IPv6 NO_PROXY
  httpx crash). Diagnose leg health with the preflight standalone
  before blaming the script or the target host.
- **Landing-page extraction patterns that paid:** HCA facilities' files
  sit on a shared Azure blob (stctrprodsnsvc00455826e6.../pt-final-posting-files/
  <EIN>_<NAME>_standardcharges.json + a shared SAS query); Intermountain
  per-facility files are /locations/<slug>/about/hospital-pricing pages
  linking /-/media/files/.../hospital-prices/<EIN>_<name>_standardcharges.ashx
  (sitemap.xml lists every facility's pricing page).
- **D3 Exa discovery + attribution verification (2026-10-30, batch 1:**
  130 targeted → 7 verified, 5.4% yield; full record in
  `~/workspace/hl-fleet/method-work/d3-batch1/REPORT.md`): the engine
  (`find_missing_mrfs.py`, imported unmodified) finds candidates for
  only ~10% of the no-candidate tail; the dominant class is "no live
  candidate" (117/130). Verification needs an attribution layer
  d1_verify lacks — the file must name THIS facility: CMS v3 CSVs carry
  `hospital_name` as the first header COLUMN with the value in the next
  row's first cell (parse identity with stdlib csv — mrf_parse's
  `delimited_rows` returns section-grouped repr cells for v3 files and
  is unusable for this; also strip a BOM that sits outside the opening
  quote). Sibling trap: a shared system token ('tampa') must not carry
  a match when the evidence has its own unmatched place tokens
  ('crystal river') and the target city is absent — that guard caught
  Tampa General Crystal River and UCHealth Broomfield seeded against
  sibling CCNs. ZIPs still rest on the EIN filename slug alone.
- **D3 false-negative adjudication pass (2026-11-02, 8 of the 9 flagged
  recovered, live 4,611 → 4,619; verdicts in b16):** every flagged
  borderline was decided by the codified test — in-file `location_name`
  + `hospital_address` matching the CMS address of record + EIN slug —
  applied to the file's FULL metadata row, which the D3 verifier never
  reads (it scores `hospital_name` only). Recovered shapes: DBA-verbatim
  (261336 Iron County), legal-name hospital_name + verbatim
  location_name (261307 Pershing, 451361 Sabine County), operator
  hospital_name + verbatim location_name/address (290032 Northern
  Nevada — the 490067 distinction), LLC-suffix/apostrophe token diffs
  (360066 St Rita's, 150167 Parkview Ortho short brand), operator
  rename with own-domain + own-EIN + verbatim address (330019 NY
  Community → Maimonides Midwood), and the ZIP case below. Standing
  moves: (1) for ANY attribution-mismatch reject, read the metadata
  row's location_name + hospital_address before concluding — an
  operator/legal/DBA `hospital_name` is the norm, not a mismatch;
  (2) **ZIP "no in-file identity" means "not looked"** — the verifier
  never opens ZIPs; `unzip -p | head` on 360218's inner CSV carried
  name + location + address + EIN-license verbatim. (3) The test's
  edge, held not seeded: 451350's ClaraPrice JSON carries ONLY
  `hospital_name` (the single-hospital district) — no location/address
  fields exist in the JSON metadata to check — so operator-only JSONs
  fail the test and stay REJECT until the rule itself is changed;
  circumstance (one-hospital district, facility's own portal) is not
  a substitute for an in-file tie.
- **Single-hospital-district exception (established 2026-11-02,
  precedent 451350 Plains Memorial, published under it the same day):**
  an operator-only-identity file may seed to the operator's hospital
  ONLY when ALL THREE hold: (a) the operator verifiably operates
  exactly one hospital (checked against the CMS hospitals table —
  Castro County TX contains exactly one CCN), (b) the file comes from
  that facility's/district's own portal or domain (451350: the
  facility's own ClaraPrice path), and (c) the case is individually
  approved by Barklee (451350 approved in chat 2026-11-02). Outside
  this exception, circumstance is NEVER a substitute for an in-file
  tie — the codified metadata-row test still governs every other
  operator-name file. Companion trap found in the same case: CMS v3
  JSON puts the identity keys (location_name, hospital_address)
  AFTER the standard_charge_information array — a head-sample read
  sees only hospital_name and can falsely conclude "no in-file
  identity" (that is how 451350 was originally held: the full file
  in fact carries location_name 'Castro County Hospital District
  dba Plains Memorial Hospital' + the CMS address verbatim). For
  JSON MRFs, read the TAIL metadata before adjudicating identity.
- **Concatenated multi-document JSON class (140210 Harrisburg Medical
  Center, FIXED + LIVE 2026-11-02, PR #32 merged 1906685):** SIH's
  v3.0.0 export is not one JSON document — it is 134,920 complete
  documents concatenated back to back, one
  `standard_charge_information` entry each, metadata repeated per
  document. Every single-document loader rejects it (`json.loads`:
  'Extra data' at the end of document #1; simdjson the same), so the
  byte-buffered path (`parse()` -> `parse_json_v2` ->
  `load_json_bytes`) yielded 0 items while the file was a valid,
  identity-exact MRF; the CCN stayed live on UPMC Pinnacle's CSV (a
  Harrisburg PA/IL name collision). It surfaced in the canonical-build
  path (build_canonical_mini / remote_build) — the wave's ingest_one
  stream route (>50 MB + Content-Length + ijson, multiple_values=True)
  is the only path that could ever have tolerated the shape, which is
  how sibling 140011 Herrin (same SIH generator) landed as
  'json-stream' in May. Fix: `load_json_bytes_multidoc()` merges the
  stream (metadata from doc 1, charges concatenated) and
  `parse_json_v2` falls back to it ONLY when the single-doc load
  fails; acceptance is strict (whole stream decodes, >= 2 object
  documents, >= 2 carrying charges) so doc+garbage, truncated streams,
  metadata-only docs, and NDJSON flat-row files still yield 0.
  Regression: tests/test_mrf_parse_multidoc.py (9 tests). Real file:
  134,920 items / 134,920 priced, live n_slim 13,279, grade A;
  ACCEPT in b24. Staged-corpus census (ijson root-close probe over
  all 71 JSON-object sources in fleet staging): 140210 was the ONLY
  multi-doc instance; the class is rare but the unlock is general —
  any sub-50 MB or no-Content-Length multi-doc file previously
  failed on every path. Diagnostic signature for the class: a JSON
  source whose first raw_decode ends far before EOF with another
  '{' following. Copies synced post-merge: VM pipeline tree,
  method-work, hl-install tree, Mini hl-stamp-test + publish-tree +
  fleet payload (backups .bak-20261102-multidoc); the producer
  pipeline bundle regenerates from repo main on its own cycle.
- **Oversized CSV field class (231305 Ascension Standish, FIXED +
  LIVE 2026-11-02, PR #33 merged 922d95f):** the stdlib csv module's
  default 128 KiB field limit killed the whole build
  (`_csv.Error: field larger than field limit (131072)` in
  delimited_rows) on a normal CMS tall export. Diagnosis of the one
  oversized field (row 84351, col code_3_msdrg, 1,014,794 chars): NOT
  a real field and NOT garbage — a single unbalanced double-quote in
  Ascension's source flipped csv quote parity and swallowed ~5,068
  physical lines of ordinary chargemaster rows into one field (100%
  printable, no NULs; remnant `11-EPXID"` at the span tail). Rule:
  before widening any limit, read the oversized field — real data in
  a broken frame gets the remedy; garbage bytes would not. Fix:
  `csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)` = bounded 64 MiB, set
  once at mrf_parse import (process-global — covers delimited_rows
  tall/wide/dialect, the sniffers, and stream_parse_large_csv);
  bounded, never sys.maxsize. Regression:
  tests/test_mrf_parse_field_limit.py (4 tests incl. the
  unbalanced-quote span shape). Real file: 0 items -> 15,231 items /
  12,302 priced (tall-merged, in line with sibling Ascension yields),
  live n_slim 10,259; ACCEPT in b26. Diagnostic signature for the
  class: `_csv.Error: field larger than field limit` on an otherwise
  ordinary CSV — find the >128 KiB field with a one-off raised-limit
  scan and classify its content before touching the parser. Copies
  synced post-merge: same six as PR #32 (VM pipeline tree,
  method-work, hl-install v3 tree, Mini hl-stamp-test + publish-tree
  + fleet payload; backups .bak-20261102-fieldlimit; new parser
  sha256 d2986ebf…). Note: 231305 had NO prior store canonical (the
  wrong live record was wave-era) — install the built canonical into
  /Volumes/Share/hl-fleet/results/ and publish_mini seeds from there.
- **JSON tail-metadata re-check pass (2026-11-02, derived set = 1,
  recovered 0; coverage unchanged 4,641):** the audit recipe for the
  451350 class — scan the verdict corpora (b-file REJECT details by
  identity keywords; the D3 batches' `work/verdicts*.tsv` by fmt=json
  + detail=attribution-mismatch/unproven; d2_rejects .json rows
  cross-checked back into the b-files) — found the class is RARE:
  of 261 b-file REJECTs and 181 sweep verdict rows, the only
  JSON + identity-reason cases ever recorded were 140043 (recovered
  in-sweep via its twin), 451350 (recovered under the exception),
  320057 (excluded: all 7 candidates CSV — a true acronym-collision
  sibling reject), and 400115. **The trap's dual: some JSONs have no
  metadata ANYWHERE.** 400115's shoppable.json is a bare top-level
  ARRAY (3.9 MB, 2,388 entries, first byte `[`, last `}]`) — a full
  parse shows the 37 entry keys are service/payer columns only, with
  zero identity keys in the entire file; there is no tail block to
  miss. Rule: before concluding either way on a JSON identity
  verdict, establish the top-level type — a v3 OBJECT gets a tail
  read; a bare ARRAY gets a full-parse key union, and its "no
  in-file identity" verdict stands on content class too (a
  shoppable-services extract is not the standard-charges MRF, the
  400016 closure class; PR shoppable exports also mix numeric payer
  rates with methodology strings like 'PER DIEM'). 400115 was
  considered against the single-hospital-district exception and NOT
  flagged: own portal (b) plausibly holds, but there is no
  individual approval (c) and the content class disqualifies it
  regardless — the exception rescues operator-named MRFs, not
  shoppable lists.
- **D3 had-rows-unverified re-verification (2026-11-02, 21 of the 52
  recovered, live 4,619 → 4,640; verdicts in b17):** the sweep's exact
  52 = CCNs with ≥1 engine candidate row minus the 30 verified (20 of
  them had already resolved via b13/b14/b16 + the PR crawl before this
  pass). The unverified remnant was NOT mostly attribution rejects —
  the dominant class was **candidates the engine probe could not read**
  (403/202/404-at-probe-time) that were never actually verified.
  Recovery levers, in order of yield: (1) **fcdp real-Chrome download
  of the recorded candidate URL** — every `app/files/public/<uuid>/`
  host (St Bernards, Adventist HealthCare, Billings, UNC, Firelands,
  Tidelands) 403s scripts but downloads cleanly in the real browser,
  and the SiteGround 202 (Ocean Beach) resolves the same way; an
  engine-probe 403 is a bot wall, not a dead file. (2) **Rotated
  filename/month, same system page** — Allina dropped the underscores
  in St. Francis's filename, WVU uploads rotated 2026/03 → 2026/08,
  Virtua rotated its charges folder monthly; the system's own
  rendered PT page always carries the current link (extract anchors
  with fcdp js when curl gets a shell). (3) **System PT pages list
  per-facility files the hub probe under-extracted** — HSHS (Breese's
  own ZIP sat beside the three rejected siblings), Ballad (Johnson
  County's own ZIP beside Johnson City's), Baystate (files are Canto
  document links), HPH (Panacea `MRFDownload/hph/<slug>` endpoints).
  (4) **PARA FinalLinks apps carry their dbName in the page JS** —
  read it and reuse the b13 Reports.aspx pattern (Sierra Vista:
  dbSVRMSANLUISOBISPOCA). (5) ZIPs again: New London + Breese +
  Johnson County all verified only once the inner CSV's metadata row
  was read. Residual 11 adjudicated REJECT in b17 with evidence:
  aggregator-only rows (hcris/care­ranks/atlas), two IHS facilities
  whose candidates were aggregator pages or a namesake system
  (Presbyterian "PHS" acronym collision), Pemiscot's chargemaster
  dump (record closure), and two non-publishers (Norwood demolished;
  East Ohio closed, domain dead).

## Self-improvement loop — every fleet failure pays rent (standing protocol)

Barklee's standing rule (reaffirmed 2026-10-28): this skill keeps learning
from failures, in the same run that surfaces them — never a later sweep.
Every fleet failure (dead batch, dead publish, wrong count, bad lane call)
binds the agent that hits it to four steps before the run closes:

1. **Root-cause it** — name the mechanism, not the symptom (a count that
   "looks fine" while a controller is dead is a measurement failure first).
2. **Fix the code in the same pass** — patch the thing that allowed the
   failure and strengthen the guard that should have caught it; recovering
   the run alone is half the job.
3. **Write the lesson into this skill immediately** — a new gate, check, or
   method change in the relevant section above, dated. Logged in a run note
   is not learned; in the skill is learned.
4. **Discharge any lessons-gate debt** — `hl_lessons_gate.py status`; an
   owed debt silently stalls the gated chunk path.

Seeded lessons (each already cost us once; each maps to a fix above):

- **Modal workspace death (batch 3, 2026-10-27 23:41 PDT):** a lane can die
  mid-batch with `ConflictError: workspace is disabled` while artifact
  counts still rise. Measure controller process + log health, not just
  outputs; a terminal log error + no live process = DEAD batch — publish
  what landed, set `modal_disabled`, relaunch the remainder on Daytona the
  same run. (Campaign driver section.)
- **Daytona sandbox disk-fill → per-job cleanup (batch 5, 2026-10-28):**
  staged sources were never deleted inside the long-lived sandbox; two
  3.7 GB sources filled the 10 GB disk by job 3 and 40/45 jobs failed
  "no space left" — a batch-global cause wearing per-job disguises. Fix:
  `rm -rf` the job's src + out inside the sandbox on success AND error
  paths. A sandbox's disk is a per-job budget, not a batch budget.
- **Controller per-job failure isolation (2026-10-28):** one job's 403
  (`resp.raise_for_status()`) crashed the whole Daytona controller and
  stranded 41 jobs. Every controller on every lane wraps the per-job body
  in try/except and records `{ccn, error}` — one bad hospital must never
  kill a batch.
- **Publish slim-completeness gate (2026-10-27):** a wrapper died mid-slim
  leaving 82/171 price files and no trace; the publish FATALed downstream.
  Fix: `publish` (now `publish_mini.py`) verifies every CCN's price file —
  and, since 2026-10-28, its provenance sidecar against the CURRENT store
  canonical — re-slims only the missing, and exits without touching state
  if the set is incomplete. A killed slim is printed (`rc=` per chunk),
  never silent.
- **Publish-3 VM-restart death (2026-10-28 ~11:04 PDT) → publisher moved to
  the Mini, heavy slim to Daytona:** a VM-hosted publish died mid-slim when
  the Muse VM restarted (059810 slim rc=-9 first — payer volume, not item
  count, is what OOMs). Root cause: the publish path lived on the one host
  that restarts. Fix: publishing runs on the always-on Mini
  (`publish_mini.py`, Publish doctrine above) and >1 GB canonicals slim in
  Daytona sandboxes (heavy-slim rule above). Never host a long publish on
  the VM again.
- **Harvest scripts fetched direct → first-seen-domain card bursts
  (2026-11-01):** the VM egress gate cards ANY first-seen domain from ANY
  process; `cms_hpt_expand.py` / `cms_hpt_harvester.py` /
  `promote_discoveries.py` had never been migrated to `hl_edge`, so the
  weekly harvest's direct probing carded Barklee in bursts — including
  after its cron job was deleted, which proved the script (not the worker)
  was the source. Two wording fixes failed before the root cause landed.
  Fix: all three scripts load the edge config from the fleet env file on the Mini
  and build clients with `hl_edge.httpx_kwargs(...)`; verified by smoke
  test (zero new cards) + `test_hl_edge.py` 10/10. Full protocol in the
  Campaign driver section ("First-seen-domain approval cards").
- **Wave index guard: set membership, not counts (2026-10-28):** once the
  campaign publishes independently (Mini publisher), the daily wave's
  `verify_index_complete` count-equality guard false-failed wave 48 — a
  campaign publish landing mid-wave grew the index 4358 → 4383 and the
  wave's correct update read as "index-regression" (streak 2/3). Any guard
  that shares an index with another publisher must compare SETS: the
  wave's own updates present + priced, zero pre-existing priced CCNs lost
  or zeroed, concurrent extra growth accepted and logged. Fixed in
  `hl_wave.py` (both the wave and chunk call sites) with
  `test_hl_wave_concurrency.py` (7/7) as the regression proof; the full
  lesson lives in the autoresearch-hospital-ledger skill's Campaign
  lessons log (2026-10-28 entry).

## Live-identity integrity sweep (2026-11-02, re-runnable recipe)

Why: priced-based queues cannot see misattribution — a CCN can be live,
compliant-grade, and serving ANOTHER hospital's file (330125 Rochester
General served Unity Hospital's prices for 5 days, invisibly). The sweep
audits the identity of every live record, not its parse health.

Recipe (all machinery in
`/Volumes/Share/hl-fleet/staging/integrity-sweep-20261102/scripts/`):
1. Harvest live state for all priced CCNs (/api/prices head reads:
   source_url + n_slim) and the canonical store's source_urls.
2. Flag pairs by local evidence: filename-slug vs DB name mismatch
   (hard/soft), shared source URLs across CCNs, registry-vs-live EIN
   mismatch, canonical first-row facility ≠ CCN, meta-header poison.
3. Fetch in-file identity for every distinct flagged URL
   (meta_fetch.py: CSV metadata row, JSON head AND tail, ZIP inner
   file, XLSX sharedStrings). XLS/XLSX often carry NO in-file identity —
   the seed filename slug is the only evidence (filename-tier posture).
4. Verdict pass (verdict_pass.py): codified strict-attribution test —
   in-file location_name/hospital_name + hospital_address vs the CMS
   record + EIN slug → legitimate / ambiguous / no-identity /
   confirmed-wrong (+ correct facility when identifiable).
5. Fix candidates from mrf_seed + mrf_rediscovered + slug-matched pool
   URLs; verify identity for each; grade EVERY pair with the tightened
   grader (grade.py) — publish ONLY grade-RIGHT pairs.
6. Fix flow per batch (~12): preserve store canonical →
   `<batch>/old-wrong/`, download src (Content-Length verified, 3
   attempts), build with the unmodified parser, publish SERIALLY via
   ~/hl-fleet/publish_mini.py, verify-live = live source_url == job URL
   AND n>0 AND the live record's identity re-checked. Registry:
   corrected ACCEPT to the next d1_verdicts b-file (line 1 MUST be the
   header `ccn\turl\tverdict\tdetail`), stale ACCEPT rows removed from
   earlier b-files only under a dated .bak.
7. PT-page discovery (pt_discover.py) for no-candidate CCNs: fetch the
   hospital's own seed/rediscovered transparency pages, extract MRF
   links, slug-match, identity-verify, grade. System HUB pages are the
   master key for system clusters (pattern_probe.py: the Ascension hub
   page alone unlocked the Via Christi / Seton / Alexian Behavioral /
   St Thomas residuals; ECU Health's hub unlocked the Vidant rebrands).

### The tightened grader (grade.py) — every rule is a proven trap

The bare 0.5 token-ratio test is NOT sufficient evidence to publish a
fix. It passed sibling files on one common token (MERCY, CENTRAL,
PROVIDENCE) and caused the batch-000 incident (below). grade_pair()
returns RIGHT only via, in order:
- state veto: file-address state ≠ CCN state → WRONG (parse ALL state
  formats: "City, ST 12345", "City ST 12345", "City, ST, 12345");
- exact normalized full-name match;
- multi-location ("|" in the address string) → umbrella rule: only
  the anchor (FIRST address is this CCN + name tie) is RIGHT, everyone
  else UNPROVEN — the LifeBridge Sinai/Carroll combined file matched
  Carroll's address verbatim in its second segment;
- street/zip-level address match (addr_strong): zip5 equal + city, or
  DB street number+name in the file address — with a street-number
  VETO: if both addresses carry street numbers and they differ, the
  address is a different building however well zip+city line up
  (Lake Health Beachwood 25501 Chagrin vs Cleveland Clinic Rehab
  Beachwood 3025 Science Park, same ZIP 44122);
- specific-other conflict: a non-legal file name carrying distinctive
  tokens beyond the CCN's name tokens (minus city tokens, minus known
  OPERATOR_BRANDS: Ascension/Intermountain/Providence/Trinity/…)
  names a different facility → WRONG. Legal-entity strings (DBA/INC/
  LLC/CORP) are skipped — they are not facility identifiers, and
  operator legal names are the norm (St. Joseph Hospital & Health
  Center, Inc. IS Ascension St Vincent Kokomo). A small-DB-token name
  defeats the naive name_match ratio (Broward Health Medical Center's
  distinctive set is just {BROWARD} — Imperial Point "matched" it);
- distinguisher guards, BOTH directions: DB distinguisher token
  (BEHAVIORAL/CHILDREN/SURGICAL/REHABILITATION/…) missing from every
  file name → the file is the general sibling (Alexian Brothers
  Medical Center for the Behavioral Health CCN); file distinguisher
  absent from the DB name → specialty sibling (Encompass Rehab for
  Children's Hospital of San Antonio). Runs BEFORE weak addr_pass;
- ≥2 shared distinctive tokens with min DB frequency ≤ 25;
- single shared token ONLY if DB frequency ≤ 2 AND geographic
  corroboration (file-address state == CCN state, or zip3 match) —
  the KELLER trap (Helen Keller Hospital AL vs Everest Rehab of
  Keller, TX) and EATON (Eaton Rapids vs Sparrow Eaton) both passed
  the naive rare-token rule;
- weak addr_pass (city+state) only with no conflict, no distinguisher
  issue, no street conflict.
Also: GENERIC token set includes HOME and NURSING ("Hospital & Nursing
Home" names are everywhere and identify nothing).

### Batch-000 weak-verification incident (2026-11-02, disclose always)

The sweep's first fix batch (20 CCNs) was processed under the loose
0.5-ratio verification before the tightened grader existed: 8 RIGHT
(published, live-verified), 6 WRONG (one wrong file swapped for
another — coverage unchanged, no data loss, but NOT fixed), 2 UNPROVEN
(operator files), 4 rejected on re-grade. Root cause: the ratio test
passes siblings on a single common token with tiny token sets. Lesson
codified in the grader above: an identity fix is only as good as its
grader; grade with address-anchored evidence or don't publish.

### Sweep results (2026-11-02)

1,600 (ccn,url) pairs adjudicated: legitimate 622, ambiguous 460,
no-identity 279, confirmed-wrong 220 open (+19 pairs already fixed).
Fixed + live-verified: 103 total — 77 of the 220 (8 batch-000 RIGHT,
64 in six queue batches, 5 hub-page hard cases), 23 phase-1 (the
21-CCN Sutter cluster + 171384 + 261318), plus 330125 and 451350
(fixed outside the pair pass). One nuance: 144031's flagged URL was
re-published unchanged — the audit flag rested on the combined
Ascension file's metadata head naming the sibling; the file is the
facility's own posting (counted once, as fixed-verified). Still open:
143 (136 never re-published — own file not found to grader standard;
7 batch-000 bad re-publishes awaiting real files) + named build/download
blockers: 140210 (own file is CMS v3.0 JSON, unmodified parser yields
0 items), 230066 (own ZIP's inner CSV is 5.7 GB — OOM twice on the
Mini; giant-file lane), 450056 (257 MB wide Ascension CSV — OOM twice),
390265 (366 MB AHN file truncated by egress on 9 attempts).
Ambiguous + no-identity classes (739 pairs) stay a stated residual:
shared system files and operator files whose facility-level identity
the staged evidence cannot settle — never force verdicts, never
unpublish. Coverage unchanged by design: 4,641 before and after (every
fix is an identity swap on an already-counted CCN). Registry: 101
ACCEPTs in d1_verdicts_b22.tsv (sweep staging registry dir), stale
ACCEPTs removed from b1 (4) + b3 (2) under .bak-20261102-sweep.
A parallel sibling sweep ran Alaska (020xxx) batches + registry
b18–b21 from staging/integrity/ (retired after D1 ingest); overlap
with this sweep's 220 was zero.
