# Mac mini OpenClaw Pipeline Runbook

Use the Mac mini for the heavy parser. The laptop should only sync, launch, and inspect status.

## Launch

```bash
cd /Users/barkleesanders/projects/hospital-ledger
RESOURCE_PERCENT=70 scripts/macmini_sync_and_launch.sh
```

The launcher syncs the repo to `/Users/barkleesanders/projects/hospital-ledger` on `mac-mini`, installs Python deps, and starts tmux session `hospital-ledger-openclaw`.
It sources `scripts/cloudflare_env.zsh` so non-interactive `wrangler` jobs use the Mac mini's existing `~/.cloudflared/cf-global-api-key.json`.

## Official CMS Validation Sidecar

The parser still extracts locally. CMS validation is a separate compliance check
using `@cmsgov/hpt-validator-cli` against the source MRFs referenced by
generated `site/data/prices/*.json`.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none mac-mini "cd /Users/barkleesanders/projects/hospital-ledger && tmux new-session -d -s hospital-ledger-cms-validate '.venv/bin/python scripts/cms_validation_monitor.py --poll-seconds 60 --notify-errors'"
```

Status:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none mac-mini 'cd /Users/barkleesanders/projects/hospital-ledger && cat data/cms_validation_monitor.status.json'
```

## Watchdog

The watchdog runs as tmux session `hospital-ledger-watchdog`. It checks tmux
session liveness, runner/CMS status, stale status timestamps, and free disk. On
an issue it starts an OpenClaw Telegram-delivered `/goal` turn with the current
status and repair instructions.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none mac-mini \
  "cd /Users/barkleesanders/projects/hospital-ledger && tmux new-session -d -s hospital-ledger-watchdog '.venv/bin/python scripts/openclaw_pipeline_watchdog.py --poll-seconds 60'"
```

Status:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none mac-mini 'cd /Users/barkleesanders/projects/hospital-ledger && cat data/openclaw_pipeline_watchdog.status.json'
```

## Resource Limits

The runner computes a plan from CPU count and RAM, then processes CCNs in chunks so the Mac mini does not need to hold the full `data/parsed` corpus:

- CPU budget: `floor(cpu_count * RESOURCE_PERCENT / 100)`
- RAM budget: `floor(total_ram_gb * RESOURCE_PERCENT / 100 / PARSER_GB_PER_WORKER)`
- Default `PARSER_GB_PER_WORKER=5`
- Default `MAX_TOTAL_WORKERS=8`
- Default `CHUNK_SIZE=50`
- Default `MIN_FREE_GB=10`

Override only when the Mac mini is stable:

```bash
RESOURCE_PERCENT=70 PARSER_GB_PER_WORKER=6 MAX_TOTAL_WORKERS=6 CHUNK_SIZE=25 scripts/macmini_sync_and_launch.sh
```

## Status

```bash
ssh -o BatchMode=yes mac-mini 'cd ~/projects/hospital-ledger && tmux capture-pane -pt hospital-ledger-openclaw -S -80'
ssh -o BatchMode=yes mac-mini 'cd ~/projects/hospital-ledger && cat data/macmini_openclaw_pipeline.status.json'
ssh -o BatchMode=yes mac-mini 'cd ~/projects/hospital-ledger && tail -80 data/macmini_openclaw_pipeline.log'
```

## OpenClaw Handoff

The runner writes `data/openclaw_ship_handoff.md`. When parsing and R2 upload finish, hand that prompt to OpenClaw with `/goal` enabled. If the runner errors, use `/carmack` against the status/log first. Use `/ship` for production deploy after upload is complete.

## R2 Upload Resume

`scripts/stage4_refresh.py` now writes `data/r2_upload_manifest.json`. Re-runs skip unchanged objects by path, size, and mtime, so interrupted uploads resume instead of starting from zero. Objects over Wrangler's single-put limit fall back to `rclone copyto` through `R2_RCLONE_REMOTE=r2-turbo`. Set `R2_FORCE=1` to re-upload everything.
