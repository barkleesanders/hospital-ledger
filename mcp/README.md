# HospitalLedger MCP server + CLI

Query [HospitalLedger](https://hospitalledger.com) hospital price-transparency data
programmatically — from any MCP client (Claude Code, Claude Desktop, …) or from
the terminal. Read-only: it never writes to R2, never touches site routes, and
never deploys anything.

## Data sources

| Tool data | Source |
|---|---|
| Hospital directory, states, payers, procedure index, price-index stats | Bundled repo datasets in `../public/data/` (refreshed by the Tier-3 pipeline) |
| Procedure descriptions | `../src/lib/cpt-names.ts` (curated map, imported — not duplicated) |
| Full per-hospital price payloads (`get-hospital` → `prices`) | Live `GET https://hospitalledger.com/api/prices/:ccn` (the same public read-only endpoint the homepage uses; R2-backed, not bundled in the repo) |

No credentials needed. If the network is unavailable, `get-hospital` still
returns the directory record + price-index entry and reports `prices.source:
"unavailable"` instead of failing.

## Setup

```bash
cd mcp
npm install     # installs @modelcontextprotocol/sdk + zod (new files under mcp/ only)
npm run build   # bundles dist/server.js and dist/cli.js with esbuild
```

Requires Node 18+.

### Environment overrides

| Variable | Default | Purpose |
|---|---|---|
| `HL_DATA_DIR` | `<repo>/public/data` | Point at a different dataset snapshot |
| `HL_API_BASE` | `https://hospitalledger.com` | Point `get-hospital` at a staging Worker |
| `HL_OFFLINE` | unset | Set to `1` to disable all network access |

## MCP server

Stdio transport. Add to your MCP client config:

```json
{
  "mcpServers": {
    "hospital-ledger": {
      "command": "node",
      "args": ["/Users/barkleesanders/projects/hospital-ledger/mcp/dist/server.js"]
    }
  }
}
```

With overrides:

```json
{
  "hospital-ledger": {
    "command": "node",
    "args": ["…/mcp/dist/server.js"],
    "env": { "HL_DATA_DIR": "/data/hl-snapshot/public/data", "HL_OFFLINE": "1" }
  }
}
```

## CLI

```bash
hl <tool> [--arg value ...]      # JSON on stdout
hl help                          # tool catalog
```

## Tool catalog

| Tool | Args | Returns |
|---|---|---|
| `search-hospitals` | `query*`, `state`, `limit` | Hospitals matching name / city / CCN, best match first |
| `get-hospital` | `ccn*`, `items_limit`, `items_offset` | Directory record + price-index entry (compliance grade) + paginated price payload |
| `search-procedures` | `query*`, `limit` | CPT/HCPCS codes by prefix or description keyword, with hospital counts + gross-charge ranges |
| `get-procedure` | `code*`, `limit`, `offset` | Per-hospital prices for one code, cheapest first (gross, cash, min/max, payer counts) |
| `get-price-index` | — | Dataset-wide stats: facilities, compliance, standardized rows |
| `list-states` | — | States/territories with hospital counts + priced counts |
| `search-payers` | `query*`, `limit` | Insurers by name: slug, category, hospital count, median rate |
| `get-payer` | `slug*` | Per-payer detail file (aliases, rates, hospitals) |
| `status` | — | Dataset presence check + data-source notes |

`*` required.

## Example queries

```bash
# Find Stanford's hospitals
hl search-hospitals --query Stanford --state CA --limit 5

# Full record + first 5 price items for Stanford Health Care (CCN 050441)
hl get-hospital --ccn 050441 --items_limit 5

# What does an office visit (99213) cost across hospitals? Cheapest first.
hl get-procedure --code 99213 --limit 10

# Which procedure codes match "mri"?
hl search-procedures --query mri --limit 10

# Dataset totals
hl get-price-index

# States with the most hospitals
hl list-states | head -40

# Which payers look like Aetna?
hl search-payers --query aetna
hl get-payer --slug aetna | head -60
```

Example `get-hospital` output (trimmed):

```json
{
  "found": true,
  "hospital": {
    "ccn": "050441",
    "name": "STANFORD HEALTH CARE",
    "city": "STANFORD",
    "state": "CA",
    "has_live_mrf": true
  },
  "price_index": {
    "n": 93388,
    "counts": { "CDM": 78075, "HCPCS": 14424, "MS-DRG": 889 },
    "compliance": { "score": 65, "grade": "D" }
  },
  "prices": {
    "source": "live-api",
    "n_slim": 93388,
    "items": [ { "code": "RX-210519", "type": "CDM", "gross": 500820.0, "cash": 200328.0, … } ],
    "pagination": { "offset": 0, "limit": 5, "total": 93388 }
  }
}
```

## Layout

```
mcp/
  README.md            this file
  package.json         self-contained npm package (bin: hl)
  tsconfig.json
  src/
    data.ts            dataset loaders + search + live price fetch
    tools.ts           tool registry shared by server and CLI
    server.ts          MCP server (stdio)
    cli.ts             hl CLI (thin wrapper over tools.ts)
  dist/                built bundles (npm run build)
```

`src/lib/cpt-names.ts` is imported from the main repo, not copied, so
procedure descriptions stay in sync with the site.

## Notes / limits

- Datasets are a snapshot of the last Tier-3 pipeline run; re-run the
  pipeline (or point `HL_DATA_DIR` at a fresh checkout) for newer numbers.
- `get-hospital` price items come from the live API and reflect current R2
  data; everything else reflects the bundled snapshot.
- This package is intentionally read-only. It does not deploy the Worker,
  publish anything, or modify site routes or R2.
