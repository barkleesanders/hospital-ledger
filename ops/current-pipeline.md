# The refresh pipeline as it runs today

This is the current operating setup for keeping hospitalledger.com's price
data fresh, with operational updates through 2026-10-25. It supersedes the older runbooks where they
disagree (`ops/hl-refresh-runbook.md` describes the retired Mac launchd
full-tier refresh; `README-tier3-pipeline.md` is historical). The agent
skills that operate this pipeline live in `skills/` and are part of the
setup: they carry the lessons, the waves run them, and every wave writes
its new lessons back the same day.

## Shape of the system

Two tiers of code, one data store, one receipt:

- **Bundle-owned phase scripts** (`scripts/*.py`) ship as a versioned
  pipeline tarball. A run never edits them locally; fixes arrive in the
  next bundle and are picked up by the phase-0 self-update (below).
- **The VM-owned wave layer** (`ops/wave/hl_wave.py`) orchestrates:
  detect → reparse → sync → verify base → build → validate → publish.
  It evolves locally, and every fix is regression-tested
  (`ops/wave/test_hl_wave_fixes.py`, 25 tests).
- **R2 bucket `hl-mrf-parsed`** is the serving store. The site Worker
  reads it directly; there is no wrangler deploy and no site rebuild in
  the refresh path.
- **`meta/manifest.json` is the publish receipt.** It records the
  producer and, for every published artifact, its byte size and sha256.
  A publish is only "done" when the live manifest shows the new
  `generated_at` and the artifact records match what was uploaded.

## The wave

A cron job starts the systemd oneshot `hl-pricing-wave.service` daily at
03:11 PT. The unit runs:

```
hl_wave.py --wave-size 50 --max-reparse 8
```

Each wave probes a batch from the full seed set (~3,609 MRF URLs),
reparses what changed, and publishes. Waves fail closed: a failed gate
records the failure class and parks; nothing partial is ever published.

Publish invariants (non-negotiable):

- Never publish a partial probe. `--limit` runs are smoke tests only.
- Validation before publish: `validate_site_data.py` counts tier and the
  pricing-tier checks must pass on the exact tree being uploaded.
- Writes go only to `meta/` and `prices/index.json`. R2 objects are
  never deleted.
- Upload order is load-bearing: `prices/index.json`,
  `meta/hospitals.json`, `meta/summary.json`, then
  `meta/manifest.json` **last** — it flips the live `generated_at`, so
  everything it describes must already be in place.
- A hospital URL that errors in transport is an *error*, never "dead".
  Only a real 404/410 from the hospital marks a URL dead.

## Edge fetch: one approval instead of one per hospital

MRF fetches do not leave the VM directly. Every hospital request goes to
a Cloudflare Worker at `https://hospitalledger.com/api/fetch` (placed in
US-East), which fetches the hospital and streams the response back; the
VM only ever talks to hospitalledger.com. The route is token-gated
(`HL_FETCH_PROXY` / `HL_FETCH_TOKEN` in `~/hospital-ledger.env`, mode
600; the token value exists only there and in the Worker secret — never
in this repo, never in logs).

Consequences:

- A full wave raises **zero per-hospital approvals**; the queue covers
  all 3,609 seed URLs rather than a pre-approved host subset.
- Every wave runs the edge preflight first (`edge proxy ok:
  https://hospitalledger.com/api/fetch`); a refused preflight fails the
  wave without marking anything dead.
- Hosts the edge cannot reach (bot challenges, TLS failures, IP drops)
  land in the transport-error bucket — errors, never deaths.

Full design and measured reach data: `ops/edge-fetch.md`.

## Canonical index discipline

The R2 object `prices/index.json` is canonical. Nothing else is a
reference — in particular **not** the site-bundled copy at
`public/data/prices/index.json`, which is a stale build artifact (last
committed 2026-09-27, 3,810 hospitals). Read the live index at
`https://hospitalledger.com/api/prices-index`, and read its canonical
size/sha from the manifest's artifact record.

Canonical state before the 2026-10-14 publish: 1,386,703 bytes, 3,823
hospitals, sha256
`d8f6d61d7253685cfb2633d4252e0edf15abb4940592f6c0a9367580cbb9b2d7`.

Two mechanisms enforce this:

- **`verify_synced_base()`** (`ops/wave/hl_wave.py`): before building,
  the synced build base is checked against the live manifest's recorded
  size+sha256 for that key. Mismatch — or a manifest that cannot be
  verified — parks the wave (`base-verify-fail`). A stale base is
  impossible to build on, not merely caught downstream.
- **Base-count gate**: validation refuses a build whose hospital count
  regresses below the pre-wave count (this is what stopped the
  stale-base build at 3,818 < 3,823 during the October repair run).

**Update 2026-10-13 (site Worker `ad5c96b9`, PR #8):** the site now
serves the R2 objects directly on its public routes — home,
`/about-the-numbers`, `/data/summary.json`, `/data/hospitals.json` and
`/data/prices/index.json` all read R2 (response headers
`x-hl-source: r2`, `x-hl-price-index-source: r2`), so a wave publish
reaches the site within ~5 minutes with no Worker deploy. In
particular `/data/prices/index.json` now byte-matches the R2
`prices/index.json` (verified 2026-10-13 against the manifest record:
1,389,589 bytes / sha256 `007e52651400…` / 3,831 hospitals at
`generated_at` 2026-10-14T00:56:57Z); the bundled copy described above
is no longer what that route serves. If an R2 object is missing or
unparsable the Worker falls back to the bundled copy and the header
reads `bundled` / `assets` — treat that as a publish problem. The
discipline above is unchanged: R2 + the manifest remain the canonical
reference, and `verify_synced_base()` still gates every build.

## Moving bytes: `scripts/r2_put.py`

All R2 traffic goes through `scripts/r2_put.py`. Its GET path is built
for networks that cut transfers mid-body (see failure modes):

- Every body is checked against its declared Content-Length.
- A short body **resumes from the first missing byte** with a signed
  Range request, pinned with `If-Match` to the first response's ETag,
  so two different versions of an object can never be joined.
- A single-part ETag is verified as the body MD5 at the end.
- Attempts that add bytes do not consume the retry budget; the GET
  fails closed only after 5 attempts with no progress.
- `get_file` writes to `.part` and renames only when complete.

Measured on the production path: the 1,386,703-byte index arrived in 7
requests (1 + 6 resumes) through a proxy that cuts bodies at
278–377 KB. Tests: `scripts/test_r2_put_get.py` (10 tests; reverting to
restart-only fetching fails the resume test).

## Publish lock

Publishing holds an R2-object lock so waves and retries never interleave
uploads. Its semantics were hardened against the same cutting network:

- Lock **release retries 3×** — one cut DELETE otherwise left a zombie
  lock standing for the full 900 s TTL, colliding with later attempts.
- Lock **contention is not a route-health failure** (misclassifying it
  once earned a bogus 30-minute cooldown on a healthy route).
- Uploads run **one key per `r2_put` invocation** with per-key retries —
  one cut connection costs one key, not the publish. PUTs are
  idempotent; order is preserved; manifest still goes last.
- A retry may adopt a lock only when the holder parsed from the lock
  error (`lock held by <holder>`) is its own owner. Ownership errors
  name *both* parties, and a substring match once false-adopted a
  foreign lock live.

## Phase 0: self-update

The counts-refresh runner starts by pulling the pipeline bundle: its sha256 is
verified before unpacking, and the tree is re-extracted over the working
copy (`~/hospital-ledger.env` lives outside the tree and is untouched).
Bundle-owned scripts are never patched locally to make a check pass — a
validation FAIL is reported as FAIL, and the fix ships in the next
bundle. The wave layer and the skills are the locally-owned surface;
their lessons write back the same day they are learned (see
`skills/hospital-ledger-refresh/SKILL.md` → "Lessons").

The daily wave and pending retry call their installed validator directly;
they do **not** run phase 0. Publishing a bundle is not proof that those
jobs have received it. Before expanding the price index above the old
validator's compliance-population bound, verify the installed validator on
the VM (or a completed counts-run installation). The VM's SSH routes were
refusing connections on 2026-10-25, so that installation remains unverified.

## Chunk continuation (2026-10-25)

`hl_wave.py --chunk-ccns CCN[,CCN...]` processes at most ten requested
identifiers; the continuation driver uses at most five. Chunk results
neither increment nor reset the standing loop's failure streak. Parse and
slim timeouts park individual rows. A nonempty parsed corpus can produce
an empty price preview; that row must restore both its pre-slim index and
price file (or remove a new rejected file) and park before valid peers
enter publication. Backups stay on disk and decoded previews are scoped
to one slim call, not retained across the next parser. Requested failures
cannot be re-adopted through pending carry in the same chunk.

Publication stays within the requested chunk size, including pending
carry: only unused slots may adopt earlier work. Unselected unpublished
and pending entries survive success, transport parking, and stage-only
builds. The daily wave and publish-retry paths retain their existing
behavior; this bound applies to explicit chunk mode.

Pricing and compliance counts have different source populations.
`build_site_data.py` defines `compliant` from CMS-required registry rows
with a verified MRF, while pricing metrics count all price-index entries.
The 2026-10-25 continuation exposed 53 other registry entries and 128
legacy seed identifiers in an index of 3,987 records. The valid required
MRF numerator was 3,986. The validator checks pricing totals against the
price index and compliance totals against the registry independently.

Coordinate this validator update with the original producer runtime before
publishing an index above its old `index <= compliant` bound. The isolated
Mini continuation restored its last compatible generation and retained
additional parsed records for publication after that update. Its loop
snapshot staying byte-identical is evidence for the isolated continuation;
it is not a fresh read of the unavailable VM's loop or schedules.

Regression commands are wired into `scripts/verify.sh`, including chunk
streak isolation, per-row timeout/empty-preview handling, and separate
count populations. Bundle-owned fixes must be packaged from this source
tree for release; an isolated continuation is not proof of VM installation.

## Historical publish receipt (2026-10-14)

- Producer: `muse.ai nova (wave-loop)`
- `generated_at`: `2026-10-14T00:56:57Z`
- `prices/index.json`: 1,389,589 bytes, sha256
  `007e52651400fc843b3017484e0a67ff2c4f30fbd7dc64c678f676a9be692629`,
  **3,831 hospitals** (canonical 3,823 + 8 staged reparses)
- Verify anytime: `curl -s https://hospitalledger.com/api/manifest`

## Failure modes learned (each cost a real incident)

| Failure | What actually happened | Structural fix |
|---|---|---|
| Silent truncation | The old GET streamed to EOF with no length check; a 1,343,452-byte prefix of the index reached the JSON parser (`Unterminated string … char 1343418`) and the diagnosis compared it against the stale site bundle, not R2 | Content-Length verification + Range resume in `r2_put.py`; canonical-reference discipline (R2 + manifest, never the site bundle) |
| Restart-only retry | 15 of 15 whole-object retries ended short (278–377 KB each): through a cutting proxy, restarting from byte 0 can *never* finish | Resume from the first missing byte; progress doesn't consume the retry budget |
| Stale build base | An S3 sync failure fell back to the CDN's site-bundled index (3,810 hospitals) as the build base | `verify_synced_base()` + the base-count gate; the stale base cannot be built on |
| Zombie publish lock | A cut lock-release DELETE left the lock standing for its 900 s TTL; retries collided with it | Release retries 3×; holder-parsed adoption; contention ≠ route failure |
| Per-hospital approvals | The VM's egress approval flow raised one approval per new hospital host (1,136 hosts) | Edge fetch: the VM only contacts hospitalledger.com |

## The skills

- `skills/hospital-ledger-refresh/SKILL.md` — the counts-tier runbook
  the agent executes phase by phase, with the dated lessons section the
  waves append to.
- `skills/autoresearch-hospital-ledger/SKILL.md` — the coverage-gap
  research method (URL discovery waves that feed the pipeline's queue).

Both are living documents: a wave that learns something and doesn't
write it back the same day hasn't finished.
