/**
 * hospital-ledger-fetch: the pipeline's edge fetch, as its own Worker.
 *
 * Routed at hospitalledger.com/api/fetch* (wrangler.fetch.jsonc), so the
 * muse.ai VM still talks to one domain, and placed in US-East so hospitals
 * see a US address. Measured 2026-10-13 on 300-host seed samples, alive
 * hospitals direct vs through the edge: from the zone's default POP (Paris
 * for this account) 141 vs 131; placed in IAD 129 vs 131 and 137 vs 134.
 * Placement is per Worker, so the site keeps serving from the nearest POP.
 * The route itself is src/routes/api/fetch-proxy.ts.
 */

import { Hono } from "hono";
import { type FetchEnv, fetchProxyHandler } from "./routes/api/fetch-proxy";

const app = new Hono<FetchEnv>();
// Hono serves HEAD from GET routes; the handler keeps HEAD as HEAD upstream.
app.get("/api/fetch", fetchProxyHandler);

export default app;
