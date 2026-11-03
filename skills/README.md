# Skills

Agent skills that describe how Hospital Ledger is operated and improved.
They are written to be executed — by the maintainer's agents today, and by
anyone who wants to run the same pipeline themselves.

## What's here

| Skill | What it covers |
|---|---|
| `autoresearch-hospital-ledger/` | The autoresearch loop that drives parser and coverage improvements. |
| `hospital-ledger-refresh/` | The recurring refresh of coverage data and counts. |
| `hl-fleet/` | The free-compute fleet (Modal, Daytona, E2B, the Mini, the VM, Oracle): how MRF fetch/parse work is routed, published, and verified. |
| `hl-parser-improvement/` | The parser improvement loop: grounded test generation, header-alias proposals, and bug-candidate triage for `scripts/mrf_parse.py`. |

## Public editions and the sync rule

`hl-fleet/` and `hl-parser-improvement/` are **public editions** of the
maintainer's operating skills. The canonical internal editions live in the
maintainer's skill registry; these copies are kept in sync on every
substantive update, and updates land here by pull request like any other
change.

The only differences between the internal and public editions are redactions
of infrastructure identifiers — cloud org IDs, database UUIDs, host
addresses, and credential file paths. All operational content (routing
rules, publish doctrine, grading rules, failure lessons) is identical, so
the public edition is a faithful description of how the dataset is built.

If you are adapting the fleet or parser-improvement workflow to your own
infrastructure, substitute your own lanes and credential stores wherever a
redaction appears.

## Contributing data

The parser (`scripts/mrf_parse.py`) and these skills are the exact tooling
used to process hospital price files. A contributor guide for submitting
processed hospitals as pull requests is in preparation; until it lands,
open an issue if you want to help process the backlog.
