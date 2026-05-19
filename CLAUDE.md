# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Copy-Truth Gate (MANDATORY before every /ship)

Every deploy of hospital-ledger MUST pass `scripts/predeploy_audit.py` (exit 0).
The audit compares numeric claims in README.md and src/routes/ against ground
truth from `public/data/summary.json`, `db/hospital_ledger.db`, `data/_payer_raw.jsonl`,
and `data/parsed/` — catches stale counts before they reach users.

Workflow during /ship:
1. Pre-deploy (source vs local truth): `npm run audit:copy` (or `python3 scripts/predeploy_audit.py`).
   Also fires automatically as the `predeploy` npm hook before `npm run deploy`.
2. If FAIL: run `python3 scripts/predeploy_audit.py --fix` to auto-apply, then re-run.
3. Post-deploy (source vs live site): `npm run audit:copy:live`. Must pass before declaring done.

To add a new claim: append a `Claim(...)` entry to `CLAIMS` in `scripts/predeploy_audit.py`.
The regex must have ONE capture group with the displayed value; `truth` returns the
expected string. One-line addition for any future drift detection.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->


## Build & Test

_Add your build and test commands here_

```bash
# Example:
# npm install
# npm test
```

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

_Add your project-specific conventions here_
