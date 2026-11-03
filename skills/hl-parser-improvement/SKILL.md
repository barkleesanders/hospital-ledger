---
name: hl-parser-improvement
description: "Hospital Ledger parser improvement loop on Barklee's free Kaggle GPU endpoint: turn each parser revision and each wave's new file layouts into verified improvements — grounded test generation, header-alias proposals, and triaged bug candidates — with frontier models (agentprofile claude-team) reserved for escalation and ship decisions. Endpoint mechanics live in local-kaggle-gpu-agent; this skill is the HL workflow that uses it. Triggers: 'hl improvement pass', 'test this parser change', 'wave triage', 'parser bug hunt', 'kaggle pass on the parser'."
---

> **Public edition.** This file is the public copy of the Hospital Ledger
> parser-improvement skill, kept in this repository at the maintainer's
> direction. The canonical internal edition lives in the maintainer's
> skill registry; the two are kept in sync on every substantive update,
> and repo updates land by pull request. Endpoint mechanics for the
> Kaggle lane live in a companion internal skill; this workflow is
> complete as published.


# HL Parser Improvement Loop (Kaggle endpoint)

Purpose: make the free 30B endpoint produce *verified* Hospital Ledger
improvements, not plausible text. Proven shape (pilot, 2026-11-02):
grounded direct-API generation hit 93% test pass and found a real
parser bug; an autonomous agent on the same endpoint hallucinated the
API and hit 42%. This skill encodes the proven shape and forbids the
failed one. It produces proposals + evidence only — parser edits,
commits, and publishes stay in the normal HL lanes and gates.

## Before any run — load the ground truth

1. Endpoint mechanics: follow `local-kaggle-gpu-agent` (session start,
   smoke test, shutdown discipline). Never improvise a session.
2. Parser under test: the CURRENT working copy is
   `~/hl-stamp-test/scripts/mrf_parse.py` on the Mac mini (2,793 lines
   at pilot time). `~/hospital-ledger/` does NOT exist on the Mini —
   if the canonical checkout moves, update this line the same day.
3. Existing suites (dedupe against ALL of them before generating):
   `test_mrf_parse_guard`, `test_mrf_parse_decompile`,
   `test_source_updated` (+ any suite added since — list the test dir).
4. State that the parser MUTATES: `header_aliases.json` (its learning
   cache). Snapshot it before every test run; restore byte-identical
   after. A run that leaves the cache changed is a failed run.

## The loop

1. **Scope the pass.** Targets come from evidence, never vibes:
   (a) the parser's git diff since the last pass → changed functions;
   (b) the latest wave's new layouts / parse failures → the column-
   resolution paths they exercise (`resolve_csv_columns`,
   `first_gross_col`, `first_col_excluding`, `kw_match`, the
   `*_COL_HINTS` families); (c) explicitly requested areas.
2. **Build the grounding packet mechanically.** Extract the target
   functions' real signatures, return shapes, and relevant constants
   from source with a script (grep/AST) — never hand-type them, never
   let the model guess them. This packet is the single deciding factor
   from the pilot; a pass without it does not run.
3. **Generate via direct API calls** to the endpoint (`/v1/chat/completions`
   or native `/api/generate`), one call per target area, prompt =
   grounding packet + coverage targets + "pytest, no imports beyond
   the parser + stdlib/pytest". **Do NOT use an autonomous coding
   agent (OpenCode/Aider) as the generator** — pilot round 1 is the
   standing counterexample. Agents may be used for mechanical work
   (file staging, running pytest), never for ungrounded generation.
4. **Verify on the Mini against the real parser.** Snapshot
   `header_aliases.json` → run pytest → restore. Triage EVERY failure
   as TEST BUG or PARSER BUG (candidate), each with a one-line probe
   (call the function directly, show actual vs expected). No untriaged
   failures, no exceptions.
5. **Escalate, don't decide.** Parser-bug candidates and ambiguous
   layouts go into an escalation packet (failing test + probe output +
   the source excerpt) for a frontier pass via the agentprofile front
   door (`claude-team`) or for Barklee. The 30B's word never ships a
   parser change; the frontier review + the HL ship lane do.
6. **Land and log.** Passing novel tests are staged as a proposed new
   suite file (never committed by this skill). Write RESULTS.md in the
   run's output dir: scope, packet source, counts, per-failure triage,
   novel cases, bug candidates, endpoint timing, GPU minutes used.
   One-line outcome into the Kaggle goal GOAL.md; bug candidates also
   into the HL wave record so the next wave inherits them.

## Pass / fail criteria (pre-registered, from the pilot)

- ≥25 runnable generated tests per pass; ≥80% pass against the real
  parser; ≥5 cases absent from the existing suites; 100% of failures
  triaged with probe evidence.
- Zero parser-bug candidates is a PASS outcome. Never manufacture a
  finding to make a run look productive.
- FAIL the pass (report, don't patch over): grounding packet missing,
  aliases cache left dirty, endpoint smoke test red, or the generator
  was an ungrounded autonomous agent.

## Budget and session discipline

- The endpoint pool is ~30 GPU-hours/week, shared with everything
  else on that account. Batch passes into session windows; a normal
  improvement pass costs ~5 GPU-minutes of generation (pilot measure)
  plus session warm time. Log hours in the Kaggle goal.
- Requests serialize on the endpoint: one generation call at a time.
  Never point two passes (or a pass and a wave job) at it at once.
- Stop the Kaggle session when the pass ends unless a wave is
  mid-flight and using it.

## Failure modes (all observed 2026-11-02)

| Symptom | Cause | Fix |
|---|---|---|
| Tests reference functions/fields that don't exist | Ungrounded generation (model invented the API) | Grounding packet (step 2) is mandatory; direct-API driver only |
| Pass looks productive, nothing verifies | Counting generated tests instead of passing ones | Criteria count pytest results, not files |
| `header_aliases.json` differs after a run | Parser's learning cache mutated during tests | Snapshot/restore in step 4; verify byte-identical |
| Slow/queued responses | Second client sharing the session | Single-tenant: one caller at a time |
| Working from a stale parser | Wrong checkout (`~/hospital-ledger/` doesn't exist) | Canonical path pinned above; confirm line count/mtime before the pass |

## Rules

- Proposals + evidence only: this skill never edits the parser,
  commits, publishes, or files anything.
- Frontier seats (agentprofile) are for escalation and decisions —
  never for bulk generation this endpoint can verifiably do.
- Every claim in RESULTS.md traces to a pytest run or a probe output
  from THIS pass. No carried-over numbers.
