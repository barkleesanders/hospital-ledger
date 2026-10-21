/**
 * GET /api/manifest
 *
 * Returns the publish manifest from R2 (meta/manifest.json).
 * The manifest carries generated_at, git_commit/git_repo, summary_source,
 * and expected hashes — the wave verifier uses this for exact public-byte
 * verification after publication.
 */

import type { Context } from "hono";
import type { Env } from "../../index";
import { json } from "../../lib/responses";

export async function manifestHandler(c: Context<Env>): Promise<Response> {
	const env = c.env;
	if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== "function")
		return json({ error: "manifest not available" }, { status: 503 });
	const obj = await env.HL_MRF_PARSED.get("meta/manifest.json");
	if (!obj) return json({ error: "manifest not found" }, { status: 404 });
	const text = await obj.text();
	return new Response(text, {
		headers: {
			"content-type": "application/json; charset=utf-8",
			"cache-control": "public, max-age=60",
			...(obj.httpEtag ? { etag: obj.httpEtag } : {}),
		},
	});
}
