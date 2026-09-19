# How hospitalledger.com was kept updated — ground truth, 2026-09-18

Written before handing the update loop to muse.ai. Every claim below was
measured on 2026-09-18 (commands in the right-hand column); nothing is from
memory or from the older runbooks, which this file corrects.

## The mechanism that existed

| Layer | What | Where | Evidence |
|---|---|---|---|
| Scheduler | macOS launchd agent `com.hospitalledger.refresh`, Sun 04:00 local | mac mini (`mac-mini`, Tailscale 100.93.165.20) | `ops/com.hospitalledger.refresh.plist` |
| Job | `scripts/refresh.sh` — 10 steps: git sync → `batch_ingest --resume --all` → `slim_parsed` (targeted + full, rebuilds cpt-index) → `promote_terminal_exceptions` + `build_aggregates` + `build_site_data` → `predeploy_audit --fix` → `stage4_refresh UPLOAD_R2` → allow-listed `git commit && git push` → `npm run deploy` (vite build + wrangler deploy) → `audit:copy:live` → cleanup | repo | `scripts/refresh.sh` |
| Guards | `mem_guard.sh` (SIGKILL on RSS > 8 GB or free disk < 3 GB), background QoS, >50 % ingest-failure abort, cpt-index must be exactly 5,000 codes / ≤ 15 MB, Telegram alert on non-zero exit via `~/.hermes/.env` | mini | `scripts/refresh.sh` lines 40–110 |
| Heavy path (one-off) | `openclaw_macmini_pipeline.py` under tmux `hospital-ledger-openclaw`, CMS validator sidecar, watchdog → OpenClaw `/goal` | mini | `ops/macmini-openclaw-runbook.md` |
| Data store | Cloudflare R2 `hl-mrf-parsed` (`prices/`, `parsed/`, `indexes/`, `aggregates/`) + `hl-mrf-raw`; site counts **baked into the Worker bundle** from `public/data/summary.json` at build time | Cloudflare | `wrangler.jsonc`, `src/index.tsx` (pre-2026-09-18) |
| Delivery | Cloudflare Worker `hospital-ledger` on hospitalledger.com / www | Cloudflare | `wrangler deploy` |

Hermes: **no** `hospital-ledger` cron ever existed on the mini (`hermes` binary is
not even on the mini's PATH; user crontab has only `pr-auto-merge` and
`pub78-watch`). GitHub Actions: none (`.github/` absent). So the *only*
scheduler was that one launchd plist.

## What actually happened (the site has been stale since 2026-06-14)

`curl -s https://hospitalledger.com/data/summary.json | jq .generated_at` → **`2026-06-14T11:41:52Z`** (measured 2026-09-18).

| Date (mini, local) | Run | Outcome | Evidence |
|---|---|---|---|
| 2026-05-31, 06-07 | Sun 04:00 launchd | died before deploy, silently (159-byte logs) — this is why the Telegram alert was added | `data/refresh_logs/run-2026-05-31T11-00-05Z.log`, `…06-07…` |
| 2026-06-08 | manual | full run, committed `priced=3692` | commit `fc3e3d4` |
| 2026-06-14 | Sun 04:00 launchd | **data refreshed and committed (`1672205`), R2 uploaded, but `wrangler deploy` FAILED**: `Failed to fetch auth token: 400 … In a non-interactive environment, it's necessary to set a CLOUDFLARE_API_TOKEN` — launchd has no wrangler OAuth session and `scripts/cloudflare_env.zsh` is not sourced by `refresh.sh`. The live site kept the 06-14 numbers only because that run's `build_site_data` output was also what got deployed manually later. | `data/refresh_logs/launchd.out.log` tail (still shows the 06-14 wrangler error) |
| 2026-06-28 | Sun 04:00 launchd | ran ingest + slim (8 KB log ends mid-slim), **mem_guard SIGKILL** (`launchd.err.log`: `Killed: 9`), pushed to a side branch `feat/explain-gap-and-close-it`, never deployed | `launchd.err.log` |
| 2026-07 → 2026-09-06 | — | **no run logs at all** for ten Sundays | `ls data/refresh_logs/` |
| 2026-09-13 | Sun 04:00 | **`mem_guard: BREACH free_disk=1164396KB < floor=3145728KB` — killed at step 0** (the disk floor, before `git pull` even finished) | `run-2026-09-13T11-00-51Z.log` (189 bytes) |
| 2026-09-18 (today) | — | launchd agent is **gone**: `launchctl print gui/501/com.hospitalledger.refresh` → `Could not find service`; no `com.hospitalledger.*` in `~/Library/LaunchAgents` (92 plists). Something ran on 09-13 at 04:00, so it was unloaded/removed between 09-13 and 09-18 (the mini's disk cleanup on 09-15 is the likely event). | `launchctl print`, `ls ~/Library/LaunchAgents` |

Mini state today: disk **2.8 GB free of 228 GB** (below the job's own 3 GB
floor, so it cannot run there as-is); repo checkout 11 commits behind origin
with a 0-byte stray `_cpt_detail_raw.jsonl`; `data/parsed/` does **not exist** on
the mini (the local parsed corpus is gone — the only copy is R2 `parsed/`);
`.venv` python is 3.14.

## Root causes, ranked

1. **The deploy was in the data loop.** Numbers were `import`ed into the Worker
   bundle, so updating the site required `wrangler deploy` with Cloudflare
   auth — the one step a headless launchd job could not do. Every "successful"
   data refresh after 06-14 would still have left the site stale.
2. **Host-bound, resource-fragile.** 4–6 h, 8 GB RSS, a local 5.8 GB corpus,
   a 3 GB disk floor, on a shared 16 GB / 228 GB mini that other jobs fill up.
   Two kernel-panic-class incidents (06-07 slim 11.8 GB RSS) drove the guards;
   the guards then killed the job, correctly, every time the host was tight.
3. **Silent scheduler loss.** launchd agents vanish with a plist; nothing
   noticed for ten weeks. The Telegram alert only fires on a run that
   *starts* — a job that never starts is silent.

## What changed on 2026-09-18 (this handoff)

- The Worker is now **R2-first** for `summary.json` / `hospitals.json`
  (`meta/*` keys) with `/api/manifest` reporting `generated_at` +
  `summary_source`. A producer updates the live site by writing R2 objects —
  no deploy. `src/lib/site-data.ts`, `wrangler.jsonc#assets.run_worker_first`.
- The artifact set is a written **contract** with a validator
  (`ops/data-contract.md`, `scripts/validate_site_data.py`,
  `scripts/make_manifest.py`) and a one-command **counts-tier producer**
  (`scripts/refresh_counts.sh`) that any host with Python 3 (venv) + outbound HTTPS
  can run (R2 uploads via `scripts/r2_put.py`, stdlib SigV4 — no `aws`/`rclone`) in ~15 min on ~50 MB of disk.
- A **bucket-scoped R2 credential** (Item Read+Write on `hl-mrf-parsed` only;
  denied on `hl-mrf-raw`, verified) exists for the producer.
- The muse.ai recurring task owns the loop: see `ops/muse-refresh-task.md`.
- The mini launchd job is not reinstalled. `refresh.sh` remains for a manual
  full-tier run on a machine with disk.
