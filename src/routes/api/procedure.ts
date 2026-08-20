/**
 * GET /api/procedure/:code
 *
 * Cross-hospital comparison for a single CPT/HCPCS code. Includes gross,
 * cash, min/max negotiated, and top-5 payer rates per hospital.
 *
 * Ports site/functions/api/procedure/[code].js.
 */

import type { Context } from "hono";
import type { Env } from "../../index";
import { r2Passthrough } from "../../lib/r2";
import { badRequest, json } from "../../lib/responses";

// CPT codes are 5 digits. HCPCS codes are 1 letter + 4 digits.
// Allow both, plus DRG variants. Reject anything weirder.
const VALID_CODE = /^[A-Z0-9][A-Z0-9-]{1,9}$/i;

export async function procedureHandler(c: Context<Env>): Promise<Response> {
	const code = String(c.req.param("code") ?? "").toUpperCase();
	if (!VALID_CODE.test(code)) {
		return badRequest("invalid procedure code");
	}
	const env = c.env;
	const fromR2 = await r2Passthrough(
		env.HL_MRF_PARSED,
		`aggregates/cpt-detail/${code}.json`,
		{
			source: "r2",
		},
	);
	if (fromR2) return fromR2;

	if (env.ASSETS && typeof env.ASSETS.fetch === "function") {
		const url = new URL(c.req.url);
		url.pathname = `/data/cpt-detail/${code}.json`;
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
	return json({ error: "procedure not indexed", code }, { status: 404 });
}
