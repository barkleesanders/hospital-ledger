# hospitalledger.com weekly refresh — runbook

> **RETIRED 2026-09-18.** The mac-mini launchd job described below no longer
> exists and must **not** be reinstalled. The weekly refresh is now a
> **muse.ai recurring task owned by Nova** — see `ops/muse-refresh-task.md`
> (counts tier, Sunday 04:00 America/Los_Angeles) and the data contract it
> produces against, `ops/data-contract.md`. Why the old job stopped working,
> with dates and log evidence: `ops/pipeline-ground-truth-2026-09-18.md`.
>
> Mini state at decommission (measured 2026-09-18 over ssh, with a positive
> control on `com.barklee.settings-watch`): no `com.hospitalledger.*` plist in
> `~/Library/LaunchAgents`, service not loaded (`launchctl print` → not found),
> no `refresh.sh` process, no crontab entry; the stale `=> enabled` override
> left in the launchd domain was cleared with
> `launchctl disable gui/501/com.hospitalledger.refresh`, so a `bootstrap` of
> the old plist will be refused until someone deliberately re-enables it.
>
> The site no longer needs a deploy to change its numbers: the Worker reads
> `meta/summary.json`, `meta/hospitals.json`, `meta/manifest.json` from R2
> (`src/lib/site-data.ts`), and `/api/manifest` reports which run is live.
> Verify Nova's run with
> `curl -s "https://hospitalledger.com/api/manifest?cb=$(date +%s)"`.
>
> **What is still manual:** the *full* tier (re-parse changed MRFs, rebuild
> `prices/`, `indexes/cpt-index.json`, `aggregates/`). `scripts/refresh.sh`
> steps 2–6 remain the reference for that, run by hand on a machine with the
> parsed corpus and ≥ 3 GB free disk, then published per `ops/data-contract.md`.
> Everything below is kept as the historical description of that script.

---

## (historical) Autonomous Sunday 04:00 refresh on the mac mini via launchd

Catches hospital MRF updates, re-attempts the gap, deploys the new numbers.

## What runs

`bash scripts/refresh.sh` — 9 steps:

| # | Step | What | Failure → |
|---|---|---|---|
| 0 | git sync | `git fetch && pull` if behind; refuse if local has unpushed work | exit 1 |
| 2 | `batch_ingest --resume --all` | Re-ingests live-MRF CCNs not already done | guards against >50% fail-rate (configurable via `--max-fail-pct`) → exit 2 |
| 3a | targeted slim | Slims survivors only — fast | — |
| 3b | full slim | Rebuilds cpt-index (~75 min); enforces ≤15 MB / 5,000 codes | abort if cpt-index out of bounds (catches the 650 MB P1 regression class) |
| 4 | promote_terminal_exceptions + build_aggregates + build_site_data | Refreshes summary.json / hospitals.json / per-CPT detail | — |
| 5 | predeploy_audit --fix + audit | Auto-corrects stale README/home.tsx numbers; blocks deploy on real drift | exit 1 |
| 6 | stage4_refresh UPLOAD_R2 | Uploads changed priced files to R2 via manifest (skips unchanged) | — |
| 7 | git commit + push | Allowlist only — NEVER `git add -A` (prevents the 3.3 GB `_cpt_detail_raw.jsonl` accident) | — |
| 8 | wrangler deploy | Production Cloudflare Worker deploy | exit 3 |
| 9 | `audit:copy:live` | Cache-busted curl against `hospitalledger.com` — checks summary, /api/prices-index, /api/cpt-index, **3 random priced CCNs** (catches "R2 missing file" — happened twice this session) | exit 3 |

## Install (first time, on the mac mini)

```bash
ssh mac-mini
cd ~/projects/hospital-ledger
git pull
cp ops/com.hospitalledger.refresh.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hospitalledger.refresh.plist
launchctl enable  gui/$(id -u)/com.hospitalledger.refresh
launchctl list | grep hospitalledger
```

## Test runs (mac mini)

```bash
cd ~/projects/hospital-ledger
npm run refresh:dry          # print-only; no mutations; ~1s
npm run refresh:quick        # --skip-ingest --no-deploy; ~5 min; exercises slim+audit
npm run refresh              # full pipeline; ~4-6 hr; deploys
# OR trigger the launchd job directly (uses the same env launchd will use weekly):
launchctl kickstart -k gui/$(id -u)/com.hospitalledger.refresh
tail -F data/refresh_logs/launchd.out.log
```

## Schedule

Sunday 04:00 local. Why:
- Off-peak (low US west-coast traffic 04:00-07:00)
- 4-6 hr budget fits before Monday morning crawlers wake up
- Mac mini idle hours
- Re-tries up to 9 hours later if mac is asleep (launchd default)

To change: edit `StartCalendarInterval` in the plist + re-bootstrap.

## Failure response

Each exit code maps to a fix:
- **exit 1** (pre-flight): git is dirty or audit drift the `--fix` couldn't resolve. SSH in; `git status`; resolve; re-run.
- **exit 2** (>50% ingest failures): something upstream broke (mass hospital outage, parser regression, CDN block). Read `data/parse_errors/{ccn}.log` for the new cluster of errors. Don't deploy.
- **exit 3** (deploy or post-deploy audit): wrangler failed OR live site is serving wrong data. Last-known-good: previous `wrangler deployments list` version. Roll back: `wrangler rollback <prev-version-id>`.

Logs:
- `data/refresh_logs/run-<ISO>.log` — per-run full transcript
- `data/refresh_logs/launchd.out.log` / `.err.log` — launchd-level
- `data/full_standardize_status.json` — last ingest run snapshot
- `data/parse_errors/{ccn}.log` — per-CCN failure detail

## Daily health check (separate, not yet implemented)

Recommended companion: a daily cron that runs ONLY `npm run audit:copy:live` (no
deploy) and alerts on any failure. Catches mid-week regressions between weekly
refreshes. Plist + alert wiring TODO.

## Why allowlist commits

This session almost committed `data/_cpt_detail_raw.jsonl` at 3.3 GB into git
history (would have wrecked the repo). The full slim regenerates it as a build
artifact (consumed by `build_aggregates.py`, then discarded). `refresh.sh`
commits a strict allowlist — never `-A`. The side-cars stay in the working
tree, gitignored.

## Status & remote monitoring (where this actually runs)

**Hospital Ledger does not run on the dev laptop.** The autonomous pipeline runs
on the **mac mini** and the live site runs on **Cloudflare** — the laptop is only
a dev checkout. This section is the source of truth for "where is it, and how do
I check it from anywhere."

| Where | What runs | How |
|---|---|---|
| **mac mini** (`mac-mini` / Tailscale `100.93.165.20`; `pmset sleep 0` → never sleeps, so the Sunday slot never gets skipped) | The full 11-stage refresh, weekly **Sun 04:00 local** | launchd job `com.hospitalledger.refresh` (this plist) |
| **Cloudflare Workers + R2** | Live `hospitalledger.com` site & API | `wrangler deploy` (refresh.sh step 8); buckets `hl-mrf-raw`, `hl-mrf-parsed` |
| dev laptop / any other machine | nothing scheduled | just for editing code + manual `npm run refresh:dry` |

Proof it fires weekly: `data/refresh_logs/run-*.log` has entries stamped exactly
`Sun 04:00` (e.g. `run-2026-05-31T11-00-05Z`, `run-2026-06-07T11-00-01Z` = 04:00
PT). Note: `launchctl print … | grep runs` can read `runs = 0` right after the
job is re-bootstrapped (the counter resets on reload) — trust the run logs and
the auto-commit history (`chore(refresh): weekly data refresh <date>`), not the
counter.

### Check it from anywhere (Tailscale SSH — works from laptop, another Mac, phone)

```bash
# Is the weekly cron loaded + when did it last run?
ssh mac-mini 'launchctl list | grep hospitalledger; \
  ls -lt ~/projects/hospital-ledger/data/refresh_logs/run-*.log | head -5'

# Watch the most recent / in-progress run
ssh mac-mini 'tail -F ~/projects/hospital-ledger/data/refresh_logs/launchd.out.log'

# Last autonomous refresh commit (proves the weekly job shipped data)
ssh mac-mini 'cd ~/projects/hospital-ledger && git log --oneline -3 --grep=refresh'

# Trigger an out-of-band refresh by hand (runs ON the mini, not your laptop)
ssh mac-mini 'launchctl kickstart -k gui/$(id -u)/com.hospitalledger.refresh'

# Prove the LIVE site is current (cache-busted; runs from anywhere, no SSH)
curl -sS "https://hospitalledger.com/api/summary?cb=$(date +%s)" | python3 -m json.tool | head
```

### Known gap
There is **no failure alerting** yet — a broken weekly refresh is silent until
someone looks (see "Daily health check" below). The weekly job itself is healthy.
