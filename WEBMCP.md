# WebMCP — Hospital Ledger

Tier: **1** (public task site) — /ship Phase 1.4w, rules in the ship skill's `shared/webmcp.md`.
Minimum for tier 1: ≥1 Answer + ≥1 Action on the primary task route. This site declares 3 Answer + 1 Action.

Primary task: searching hospitals / looking up prices. Every tool calls the same
data path the site's own UI uses — no new backend was added:

- Hospital search UI (`public/home-client.js`) fetches `/data/hospitals.json` and filters by name/city/CCN.
- Procedure form (`public/home-client.js`) checks and links via `/api/procedure/:code`.
- Hospital price preview (`public/home-client.js`) fetches `/api/prices/:ccn`.

## Tool inventory (all main routes, via Layout)

Scripts load with `defer` at the end of `<body>` in `src/components/Layout.tsx`
(appended after each page's own `scriptSrc`), so home (`/`), hospitals
(`/hospitals`, `/hospitals/:state`), hospital detail (`/hospital/:ccn`), and
procedure (`/procedure/:code`) pages all register the same tools. Browsers
without `document.modelContext` are untouched.

| Tool | Class | Inputs | Backed by | Sample input |
|---|---|---|---|---|
| `search_hospitals` | Answer | `query` (required), `state` (optional 2-letter) | `GET /data/hospitals.json`, filtered in-page (same logic as home-client.js) | `{"query":"Alaska Native"}` |
| `get_procedure_prices` | Answer | `code` (required, CPT/HCPCS) | `GET /api/procedure/<code>` | `{"code":"99213"}` |
| `get_hospital_prices` | Answer | `ccn` (required, 6 chars) | `GET /api/prices/<ccn>` | `{"ccn":"020026"}` |
| `navigate_to_hospital` | Action | `ccn` (required, 6 chars, validated `^[0-9A-Z]{6}$`) | `location.assign("/hospital/" + ccn)` — internal paths only | `{"ccn":"020026"}` |

No Sensitive tools. Definitions: `public/webmcp-tools.js`; registrar:
`public/webmcp.js` (canonical drop-in from the ship skill, byte-identical).

## Usage beacon

The registrar is opted in via `window.WEBMCP_BEACON_URL =
"/api/webmcp-beacon"` in `public/webmcp-tools.js`. It fires one `register`
event per page load that registers tools and one `execute` event per tool
call (tool name + argument *key names* only — never values). The Worker
endpoint `POST /api/webmcp-beacon` (`src/index.tsx`) validates +
whitelists the payload, always returns 204, and stores each event as a
JSON object in the already-bound `HL_MRF_PARSED` bucket under
`webmcp-beacon/<YYYY-MM-DD>/<ts>-<event>-<tool>-<rand>.json`, with server
timestamp + User-Agent. No new store was added.

Read events back (from the Mac mini, via the cf wrapper):

```sh
# list today's events
~/tools/cf-wrapper/cf-auth.sh r2 objects list --bucket-name hl-mrf-parsed \
  --prefix "webmcp-beacon/$(date +%F)/" --per-page 100
# read one event
~/tools/cf-wrapper/cf-auth.sh r2 objects get "<key>" --bucket-name hl-mrf-parsed
# or with wrangler:
npx wrangler r2 object get hl-mrf-parsed/<key>
```

Counts = number of objects per day prefix, split by the `-register-` /
`-execute-` infix in the key.

## Verification record

- Pre-deploy: `node <ship>/tools/webmcp-check.mjs --source . --require 1`
  must print `verdict=ok`.
- Execution proof: `node tools/webmcp-harness.mjs` loads the served/local
  tool files in a Node VM with a fake `document.modelContext`, registers the
  tools, and executes every Answer tool once with the sample inputs above
  against the live site (`--base https://hospitalledger.com`, the production
  domain from `wrangler.jsonc` routes) — the data indexes it serves already exist.
- Post-deploy (after this PR is merged and shipped, not part of the PR):
  `webmcp-check.mjs --live https://hospitalledger.com --require 1`, then
  re-run the harness against the served files.

## Origin-trial enrollment status

Per the Phase 1.4w "enabled" standard (2026-11-03), this origin needs a
Chrome WebMCP origin-trial token embedded in the served head.
**Status: NOT YET ENROLLED** — enrollment happens in Google's Origin
Trials portal (interactive, account sign-in) and has not been done for
hospitalledger.com / www.hospitalledger.com yet. Tools are reachable
today in WebMCP-native browsers (ChatGPT desktop). When enrolled, record
here: origins, enrollment date, token expiry (tied to the trial end).
