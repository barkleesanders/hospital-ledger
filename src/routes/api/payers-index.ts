/**
 * GET /api/payers-index
 *
 * Top patient-facing payers (Aetna, BCBS, UHC, Cigna, Medicare, Cash, etc.)
 * with hospital coverage counts. Powers the payer picker on the home page.
 *
 * Ports site/functions/api/payers-index.js.
 */

import type { Context } from "hono";
import type { Env } from "../../index";
import { json } from "../../lib/responses";
import { r2Passthrough } from "../../lib/r2";

export async function payersIndexHandler(c: Context<Env>): Promise<Response> {
  const env = c.env;
  const fromR2 = await r2Passthrough(env.HL_MRF_PARSED, "aggregates/payers-index.json", {
    source: "r2",
  });
  if (fromR2) return fromR2;

  if (env.ASSETS && typeof env.ASSETS.fetch === "function") {
    const url = new URL(c.req.url);
    url.pathname = "/data/payers-index.json";
    url.search = "";
    const resp = await env.ASSETS.fetch(url.toString());
    const ct = resp.headers.get("content-type") ?? "";
    if (resp.status !== 404 && !ct.includes("text/html")) {
      return new Response(resp.body, {
        headers: {
          "content-type": ct || "application/json; charset=utf-8",
          "cache-control": "public, max-age=300",
          "x-hl-source": "assets",
        },
      });
    }
  }
  return json({ error: "payers index not built yet" }, { status: 404 });
}
