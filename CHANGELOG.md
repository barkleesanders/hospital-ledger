# Changelog

## 2026-11-22 — One gate battery, no orphaned tests (G0)

- `scripts/verify.sh` is the only gate battery (there is no CI since `fa6142a`).
  `npm run predeploy` is now the network copy-truth audit followed by
  `scripts/verify.sh`, so the deploy path can no longer pass while verify is red.
- Ten regression tests that ran in no gate are wired in (`test_mrf_parse_guard`,
  `test_mrf_parse_decompile`, `test_r2_put_get`, `test_hl_edge`, `test_source_updated`,
  `test_hl_self_heal`, `test_lessons_gate`, `test_gate_hook_integration`,
  `test_hl_wave_concurrency`, `test_hl_wave_holds`), plus `csp_audit.py` and
  `lint:observability`, which were deploy-only.
- `scripts/check_gate_coverage.py` fails the battery if any test file is run by no gate,
  a gate is swallowed with `|| true`, or predeploy stops invoking verify. Its sabotage
  proofs against the past-mistake commits are `scripts/prove_gate_coverage.sh`.
- verify now fails if the battery modifies tracked files. Three suites were rewriting
  `data/header_aliases.json` and `ops/wave/hl_lessons_debt.json`; they now write to temp copies.
- `tools/test-harness.mjs` was red on main since PR #91 removed the home page's
  `#coverage-gap` block: the contrast scope silently widened to the whole `<main>`. The
  hero's accounting paragraph carries `id="coverage-gap"` again and the harness now fails
  by name when that anchor is missing. The 179 contrast findings the widened scope
  exposes on the home page (zinc-500 text, white on emerald-600) are not fixed here.

## 2026-10-14 — Refresh pipeline: current-setup sync

- Documented the pipeline as it runs today in `ops/current-pipeline.md`
  (daily wave, edge fetch, canonical index discipline, publish lock,
  failure modes) and added `ops/edge-fetch.md`.
- Synced the deployed code: `scripts/r2_put.py` (Content-Length-verified
  GET with ETag-pinned Range resume) + its 10-test suite, and
  `ops/wave/hl_wave.py` (verify_synced_base, hardened publish lock,
  per-key uploads) + its 25-test regression suite.
- Added `skills/` with the two living agent skills that operate the
  pipeline (counts-tier refresh runbook + coverage-gap research method).
- Latest publish at sync time: manifest `generated_at`
  2026-10-14T00:56:57Z, index 3,831 hospitals.


## 2026-09-18 — "Ask anything" row (infinite-faq)

### Security follow-up (a1845fe)

- `/api/faq/ask` refuses cross-site browser posts (`Sec-Fetch-Site: cross-site`, or an `Origin` whose host is not the site's) with 403 before the rate limiter or the model run.
- A second Rate Limit binding, `FAQ_RATE_LIMITER_GLOBAL` (600 questions/60 s across everyone), caps account-wide spend; both bindings are per-edge-machine and eventually consistent, so they bound a browser crowd, not a scripted one.
- Record context docs are cached per isolate (LRU 64, 5 min); the per-isolate fallback rate bucket sweeps expired IPs once it holds 10,000.

- Added `POST /api/faq/ask` (`src/faq/faq-route.ts`): zod-validated question
  (1..600 chars) plus an optional `{kind, id}` record context, SSE
  `text-delta` / `finish` / `[DONE]` wire, `text/plain` for a no-JS form POST,
  `private, no-store` on both the context and the Response, Cloudflare Rate
  Limiting 20/60s (`FAQ_RATE_LIMITER`, namespace `2026091804`) with a
  per-isolate fallback bucket, request body bounded at 8 KB (413),
  `enable_request_signal` so a Stop click cancels the model.
- Grounding (`src/faq/faq-corpus.ts`): the home page, `/about-the-numbers`, the
  procedure-page price caveats and the hospital-page grade key, rendered from
  their own components at first use (16,767 chars measured; 30,000-char budget
  pinned by test). A hospital / procedure / payer page adds one document
  re-read by the page's own loader; the model is never shown the CCN, code or
  slug.
- Row placement (`src/faq/faq-section.tsx`): home page under the hero lead
  paragraph in its column (above the fold at 412×915 and desktop); detail pages
  under the headline card. One placeholder, no helper text.
- Island (`src/faq/faq-island.tsx` → `public/faq-island.js`, committed and
  pinned to a fresh esbuild by test): shimmer before the first token, Stop keeps
  the partial answer, markdown subset rendered as DOM nodes, only
  `https://hospitalledger.com` links become `<a>` (`src/faq/faq-links.ts`).
- Root cause found and fixed on the way: Workers AI serialises a digit-only
  token's `response` field as a JSON **number** (`{"response":4,...}`), so the
  string-only chunk schema the drop-in shipped with silently dropped every such
  frame ("4,625" streamed as ",625"). `ChunkSchema` now accepts string|number
  and prefers `choices[0].delta.content`.
- Tooling: vitest suite (`vitest.faq.config.mjs`, 47 tests), three tsconfigs
  under `npm run typecheck`, `npm run predeploy` now also typechecks, runs the
  suite and rebuilds the island last; Pattern-43 constrained-browser probe
  (`npm run probe:constrained`).
