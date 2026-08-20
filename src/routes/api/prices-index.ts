/**
 * GET /api/prices-index
 *
 * Returns the slim per-hospital price-availability index from R2 with an
 * ASSETS fallback. Picks whichever source has more `hospitals[]` entries
 * (some legacy assets are stale; R2 is canonical when present).
 *
 * Ports site/functions/api/prices-index.js. Keeps the
 * x-hl-price-index-source / x-hl-price-index-hospitals headers the home
 * page references when diagnosing a stale index.
 */

import type { Context } from "hono";
import type { Env } from "../../index";
import { json } from "../../lib/responses";

type IndexCandidate = {
	source: "r2" | "assets";
	text: string;
	count: number;
	etag: string | null;
	contentType: string;
};

function hospitalCount(text: string): number {
	try {
		const parsed = JSON.parse(text) as { hospitals?: unknown[] };
		return Array.isArray(parsed?.hospitals) ? parsed.hospitals.length : 0;
	} catch {
		return 0;
	}
}

async function fromR2(env: Env["Bindings"]): Promise<IndexCandidate | null> {
	if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== "function")
		return null;
	const obj = await env.HL_MRF_PARSED.get("prices/index.json");
	if (!obj) return null;
	const text = await obj.text();
	return {
		source: "r2",
		text,
		count: hospitalCount(text),
		etag: obj.httpEtag ?? null,
		contentType:
			obj.httpMetadata?.contentType ?? "application/json; charset=utf-8",
	};
}

async function fromAssets(
	env: Env["Bindings"],
	request: Request,
): Promise<IndexCandidate | null> {
	if (!env.ASSETS || typeof env.ASSETS.fetch !== "function") return null;
	const url = new URL(request.url);
	url.pathname = "/data/prices/index.json";
	url.search = "";
	const resp = await env.ASSETS.fetch(url.toString());
	const ct = resp.headers.get("content-type") ?? "";
	if (resp.status === 404 || ct.includes("text/html")) return null;
	const text = await resp.text();
	return {
		source: "assets",
		text,
		count: hospitalCount(text),
		etag: resp.headers.get("etag"),
		contentType: ct || "application/json; charset=utf-8",
	};
}

function indexResponse(c: IndexCandidate): Response {
	const headers: Record<string, string> = {
		"content-type": c.contentType,
		"cache-control": "public, max-age=300",
		"x-hl-price-index-source": c.source,
		"x-hl-price-index-hospitals": String(c.count),
	};
	if (c.etag) headers.etag = c.etag;
	return new Response(c.text, { headers });
}

export async function pricesIndexHandler(c: Context<Env>): Promise<Response> {
	const env = c.env;
	const request = c.req.raw;
	const [r2, assets] = await Promise.all([
		fromR2(env),
		fromAssets(env, request),
	]);
	const candidates = [r2, assets].filter(
		(x): x is IndexCandidate => x !== null,
	);
	if (!candidates.length)
		return json({ error: "price index not found" }, { status: 404 });
	candidates.sort((a, b) => b.count - a.count);
	const best = candidates[0];
	if (!best) return json({ error: "price index not found" }, { status: 404 });
	return indexResponse(best);
}
