# Changelog

## 2026-09-18 — "Ask anything" row (infinite-faq)

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
