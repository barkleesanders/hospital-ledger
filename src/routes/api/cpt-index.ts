/**
 * GET /api/cpt-index
 *
 * Cross-hospital CPT/HCPCS index. R2 canonical, ASSETS fallback.
 * Ports site/functions/api/cpt-index.js.
 */

import type { Context } from "hono";
import type { Env } from "../../index";
import { json } from "../../lib/responses";
import { r2Passthrough } from "../../lib/r2";

export async function cptIndexHandler(c: Context<Env>): Promise<Response> {
  const env = c.env;
  const fromR2 = await r2Passthrough(env.HL_MRF_PARSED, "indexes/cpt-index.json");
  if (fromR2) return fromR2;

  if (env.ASSETS && typeof env.ASSETS.fetch === "function") {
    const url = new URL(c.req.url);
    url.pathname = "/data/cpt-index.json";
    url.search = "";
    const resp = await env.ASSETS.fetch(url.toString());
    const ct = resp.headers.get("content-type") ?? "";
    if (resp.status !== 404 && !ct.includes("text/html")) return resp;
  }
  return json({ error: "cpt index not found" }, { status: 404 });
}
