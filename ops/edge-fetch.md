# Edge fetch: one approval instead of one per hospital

The muse.ai VM asks a human to approve each new destination domain. The MRF
seed spans 1,136 hospital hosts, so a wave raised one approval per hospital.

With the edge fetch on, the pipeline sends every hospital request to
`https://hospitalledger.com/api/fetch?url=<MRF URL>` and the Worker fetches the
hospital from Cloudflare. The VM only contacts hospitalledger.com, which the
wave already uses for `/api/manifest`.

| Piece | File |
|---|---|
| Route handler (owner-gated, streams the hospital's response unchanged) | `src/routes/api/fetch-proxy.ts` |
| Its own Worker, `hospital-ledger-fetch`, placed in US-East | `src/edge-fetch-worker.ts`, `wrangler.fetch.jsonc` |
| Python client (httpx transport + curl argv rewrite) | `scripts/hl_edge.py` |
| Callers | `scripts/probe_mrf_urls.py`, `scripts/mrf_parse.py`, `ops/wave/hl_wave.py`, `ops/wave/vm_wave.py` |
| Tests | `npm run test:fetch-proxy`, `python3 -m unittest scripts/test_hl_edge.py` |

Unset `HL_FETCH_PROXY` and nothing changes: every caller fetches directly, as before.

## Turn it on

1. Deploy the fetch Worker (via `/ship`) and set its secret:
   ```bash
   openssl rand -base64 36 | tr -d '\n' > ~/.config/hospital-ledger/fetch-proxy.token
   chmod 600 ~/.config/hospital-ledger/fetch-proxy.token
   ./node_modules/.bin/wrangler deploy -c ./wrangler.fetch.jsonc
   ./node_modules/.bin/wrangler secret put FETCH_PROXY_TOKEN -c ./wrangler.fetch.jsonc \
     < ~/.config/hospital-ledger/fetch-proxy.token
   ```
   Without the secret the route answers 503; with a wrong token, 401. The site
   Worker (`wrangler.jsonc`) is untouched; it needs no redeploy.

   It is a separate Worker because placement is per Worker. The zone serves
   this account from Paris (`/cdn-cgi/trace` colo=CDG), so a Worker there
   fetches hospitals from a French address, and small US hospitals drop it
   (522). Pinning the whole site to US-East would slow pages for visitors;
   pinning only `/api/fetch` does not. The route `hospitalledger.com/api/fetch*`
   runs before the site's Custom Domain, per Cloudflare's routing docs; confirm
   after deploy that `curl -sI https://hospitalledger.com/api/fetch?url=` returns
   `x-hl-proxy-error: unauthorized` (the fetch Worker), not the site's 404.
2. Add to `~/hospital-ledger.env` on the VM (mode 600):
   ```
   HL_FETCH_PROXY=https://hospitalledger.com/api/fetch
   HL_FETCH_TOKEN=<the token>
   ```
3. Check: `python3 scripts/hl_edge.py` prints `edge proxy ok`. Every wave runs
   the same preflight first and fails the wave (never marks hospitals dead) if
   the proxy refuses it.

With the edge on, `hl_wave.py` queues every seed host, not just the hosts in
`exact_hosts_granted_*.txt`, because the VM no longer contacts them.

## What the edge cannot reach

Measured 2026-10-13 on two random 300-host seed samples, hospitals alive
direct (from a US laptop) vs through a deployed probe Worker placed in IAD:
129 vs 131 and 137 vs 134. The same probe from the default Paris POP: 141 vs
131, mostly 522s, which is why the Worker is placed in the US. A few hosts
answered the edge but not the laptop, so the edge does not lose coverage
overall; the ones it does lose are below.

| Failure | Proxy error | Example |
|---|---|---|
| SiteGround `sgcaptcha` challenges Cloudflare's IPs | `upstream_challenge` | continuecare.org |
| Origin certificate invalid (Workers always verify) | `upstream_tls` | vhcharleston.com (526) |
| WP Engine answers the edge 404 "Site is not available" | `upstream_challenge` | acmhosp.wpengine.com |
| Akamai answers the edge 404 "Access Denied" | `upstream_challenge` | benefis.org |
| Origin drops Cloudflare's IPs (52x) | `connect` | kchd.org (522) |

For these the client falls back to a direct fetch, but only for hosts listed in
`HL_EDGE_DIRECT_HOSTS_FILE` (the waves set it to the latest
`exact_hosts_granted_*.txt`). A host that is not already approved stays an
error for that wave, so the fallback never raises a new approval.
