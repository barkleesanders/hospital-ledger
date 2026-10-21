---
name: autoresearch-hospital-ledger
user-invocable: true
description: Wave-based research skill for closing the Hospital Ledger coverage gap. Use for ANY systematic work on the 292-hospital gap (111 zero-row + 183 not-in-index): MRF URL discovery via deepsearch, parser-gap analysis, and CMS accountability mapping via /gov. Mandatory method for every coverage-research wave.
---

# Autoresearch Hospital Ledger

This is the **mandatory method** for all systematic coverage-gap research on the
Hospital Ledger (standing direction 2026-10-03). It closes the gap between
"live MRF" hospitals and "standardized prices" hospitals through wave-based
research: each wave picks a batch of missing hospitals, finds their MRF URLs
or documents why they're missing, and leaves the skill better than it found it.

## The gap (ground truth, verified 2026-10-03)

- **Compliant (live MRF known to CMS):** ~3,993
- **In price index with >0 rows:** ~3,699
- **In index with 0 rows:** 111 — almost all have NO tracked MRF URL (only
  16/111 are in the MRF queue). These are URL-discovery problems, not parse
  problems.
- **Not in index at all:** ~183 — live MRF per CMS, but no URL tracked and
  never ingested.
- **Total gap:** ~292

Key structural finding (2026-10-03): the MRF URL queue (1,241 entries) covers
only 729 of the 3,810 index hospitals. URL discovery is the bottleneck, not
parsing.

## Tool routing (task -> route)

| Task | Route |
|---|---|
| MRF URL discovery | `~/workspace/tools/deepsearch.sh "<hospital name> <state> price transparency machine readable file" -n 8 --json` (VM-local deepsearch engine; keys in `~/.config/websearch/keys.json`) |
| Verify a candidate MRF URL | `curl -sIL <url>` (headers only) then `curl -s -m 60 -o /tmp/probe -w '%{http_code} %{size_download}'` — never download multi-GB files fully |
| CMS regulatory research | deepsearch skill six-phase method; verify every citation via the /gov Exa Verification Rule |
| CMS enforcement / accountability | /gov skill (Six-Phase Accountability Method); the ledger's gap IS a CMS-enforcement story |
| Check current gap numbers | `https://hospitalledger.com/api/manifest` (summary: compliant, standardized_price_hospitals) + `https://hospitalledger.com/data/prices/index.json` (count n=0 entries) |
| Check if a CCN is in the MRF queue | `hl_wave_queue.json` in the goal's hidden_files |
| Check URL state / errors | `hl_wave_state.json` (urls, deferred, errors); `hl_waves.jsonl` (per-wave error classes) |

## Parser capabilities (what mrf_parse.py handles, 2026-10-03)

Handles: `csv-tall`, `csv-wide`, `json-v2` (CMS spec), `json-v1` (legacy),
`xlsx`/`xls`, `zip` (auto-extract + recurse). Keyword-matched column names,
delimiter sniffing, header-row detection.

Does NOT handle: **XML**, **PDF**, **HTML-embedded tables**. Hospitals posting
only these formats are parser gaps, not URL gaps — categorize them separately
so parser work can be prioritized by count.

## Research execution order (per wave)

1. **Pull the current gap list.** Fetch the live index, extract zero-row CCNs
   and compute not-in-index (compliant − in-index). Diff against the previous
   wave's list; new entries go first.
2. **Triage by category.** For each batch hospital, determine: (a) URL known?
   (b) URL dead or alive? (c) format parseable? Categorize into:
   `url-found`, `url-dead`, `format-gap` (XML/PDF/HTML), `no-transparency-page`
   (potential CMS violation), `blocked`.
3. **Deepsearch for missing URLs.** 2–3 query variations per hospital:
   `"<name>" <state> "price transparency"`, `"<name>" "machine readable file"`,
   `"<name>" "standard charges" filetype:csv`. Prefer the hospital's own
   domain; note aggregator hits (medlyze.com transparency ratings) as leads.
4. **Probe candidates.** HEAD/partial GET to confirm the URL serves a file,
   record content-type and size. Do NOT ingest — hand verified URLs to the
   pipeline (append to the MRF queue via the wave script's intake).
5. **CMS-violation bucket.** Hospitals with no transparency page or no MRF
   link after thorough search go to the accountability track (/gov method):
   document the negative with coverage, map the CMS complaint path.
6. **Report with evidence.** Every wave writes its findings file with a table:
   CCN, name, category, URL (or negative-with-coverage), format.

## Wave brief template

Every wave coordinator brief MUST contain:
- Method: this skill (name it).
- Batch: which CCNs (from the current gap list, prioritized by category).
- Lanes: the execution order above.
- Completion Evidence Contract: exact deliverable path
  (`~/workspace/hl-gap-research/<wave-name>.md`) + minimum content criteria
  (every batched CCN categorized); final message must include `ls -la`.
- Skill lessons: final report must contain `## Skill lessons` with
  `SKILL-LESSON:` tagged lines (or "none") — wave not closed without it.
- Duplicate guard: `subagent.list` before dispatching.

## Standards (non-negotiable)

- **Ground-truth gate:** verify a candidate MRF URL with a real probe before
  handing it to the pipeline. A search-result link is a lead, not a URL.
- **Negative results are data:** "searched X, found nothing" with measured
  coverage (queries run, domains checked). A hospital with no findable MRF
  after 3+ query variations across 2+ providers is a CMS-violation candidate,
  not a research failure.
- **Never conflate:** `url-found` ≠ ingested. Only the pipeline's byte-verify
  confirms ingestion.
- **Cite or don't claim:** every CMS regulatory assertion carries a verified
  URL + retrieved date (gov Exa Verification Rule).

## Standing boundaries

- No contact with hospitals without explicit approval. Research is read-only:
  search, probe headers, document. Outreach (asking a hospital for its MRF)
  needs the operator's per-instance yes.
- CMS complaints are filings — draft only, never submit without his explicit
  approval on the exact content.
- Do not edit the production MRF queue or wave state directly; hand verified
  URLs to the pipeline intake and let the wave script consume them.

## Doctrines (learned across waves)

### URL discovery is the bottleneck, not parsing
The 111 zero-row hospitals are overwhelmingly "no URL known" (95/111 not in
the MRF queue at all), not "URL known but unparseable." Always check URL
existence before assuming a parse failure. The queue covers only ~19% of index
hospitals — discovery has 5x headroom before parsing becomes the binding
constraint.

### Format gaps cluster by vendor
Hospitals using the same revenue-cycle vendor post the same MRF format.
When one hospital's MRF is XML-only or PDF-only, check its sister hospitals
(same health system) — they likely share the format, and one parser fix
unlocks the whole system.

### Aggregators are leads, not sources
Sites like medlyze.com transparency ratings often link to the hospital's
actual MRF. Use them for discovery, then verify the primary URL directly.

### Measured negatives → CMS-violation bucket
A hospital with no findable MRF after thorough search isn't a dead end —
it's a potential 45 CFR Part 180 violation. Park it in the accountability
track with its coverage documented, per the /gov method.

### Check cms-hpt.txt first (2026-10-03, confirmed across 8+ hospitals)
Hospitals complying with the 2024 CMS rule publish a root `.txt` file naming
their MRF (e.g. `https://www.montagehealth.org/cms-hpt.txt` with `mrf-url:` /
`source-page-url:` fields). Always check `<hospital-root>/cms-hpt.txt` (and
common variants) before concluding no MRF exists — the txt is usually
fetchable even when the file itself is bot-blocked.

### EIN-embedded filenames (2026-10-03, confirmed across 8+ hospitals)
MRF filenames commonly embed the hospital EIN as prefix:
`<EIN>_<slug>_standardcharges.{csv,json,xlsx}`. Once you know the EIN, the URL
is often predictable within a known site path. Health-system patterns:
- KPC Health: `kpchealth.com/csv/v2/<EIN>_<slug>_standardcharges.csv`
- Banner Health: `bannerhealth.com/.../hospitals/<EIN>_<slug>_standardcharges.ashx` (Cloudflare-shielded; verify via Wayback)
- UHS: `uhsfilecdn.eskycity.net/ac/<EIN>_<slug>_standardcharges.csv`
Confirm via Wayback CDX rather than blind-guessing.

### Wayback CDX is the discovery fallback (2026-10-03)
When search APIs fail, the Wayback Machine CDX API recovers exact MRF URLs:
`http://web.archive.org/cdx/search/cdx?url=<host>*&output=json&filter=urlkey:.*(standardcharges|chargemaster|machine-readable|price-transparency).*&collapse=urlkey`.

### 403 is not "missing" (2026-10-03)
A 403/bot-block on the MRF file is NOT proof of absence. Categorize as
"site blocks automated access," never as "no MRF." These need a real-browser
byte-verification pass, not a re-search.

## Skill self-update protocol

Same two mechanisms as autoresearch-genealogy-vm:

### 1. Wave-level capture (mandatory)
Every wave's final report MUST contain `## Skill lessons` with `SKILL-LESSON:`
tagged lines (methods, URL patterns, site quirks — never hospital PII).
Wave not closed without it.

### 2. Promotion rule
When two or more waves confirm the same method, promote it into the doctrines
above with the date. One-off quirks stay in wave files.

## Campaign lessons log

Dated entries, newest at the bottom.
- 2026-10-03: Skill founded. Ground-truth gap verified live: 111 zero-row
  (95 without queued URLs), ~183 not-in-index, ~292 total. Parser handles
  CSV/JSON/XLSX/ZIP; XML/PDF/HTML are format gaps. MRF queue covers only
  729/3810 index hospitals — discovery is the bottleneck.
## Available connectors

Use `muse.connector_search` to find the current connector docs before following
recovered instructions.
