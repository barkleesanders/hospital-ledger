/**
 * GET /api/payer/:slug
 *
 * Per-payer page: hospitals that have a negotiated rate with this payer,
 * median rates, links to the per-hospital detail. Powers /payer/:slug.
 *
 * Ports site/functions/api/payer/[slug].js.
 */

import type { Context } from "hono";
import type { Env } from "../../index";
import { r2Passthrough } from "../../lib/r2";
import { badRequest, json } from "../../lib/responses";

const VALID_SLUG = /^[a-z0-9][a-z0-9-]{0,80}$/;

export async function payerHandler(c: Context<Env>): Promise<Response> {
	const slug = String(c.req.param("slug") ?? "").toLowerCase();
	if (!VALID_SLUG.test(slug)) {
		return badRequest("invalid payer slug");
	}
	const env = c.env;
	const fromR2 = await r2Passthrough(
		env.HL_MRF_PARSED,
		`aggregates/payer/${slug}.json`,
		{
			source: "r2",
		},
	);
	if (fromR2) return fromR2;

	if (env.ASSETS && typeof env.ASSETS.fetch === "function") {
		const url = new URL(c.req.url);
		url.pathname = `/data/payer/${slug}.json`;
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
	return json({ error: "payer not found", slug }, { status: 404 });
}
