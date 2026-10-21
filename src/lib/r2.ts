/**
 * Thin R2 helpers. Replaces the duplicated fetchR2/responseFromR2 helpers
 * from every site/functions/api/*.js file.
 */

export type R2Result = {
	text: string;
	etag: string | null;
	contentType: string;
};

async function getWithRetry(bucket: R2Bucket, key: string) {
	for (let attempt = 0; ; attempt++) {
		try {
			return await bucket.get(key);
		} catch (error) {
			// Observed on a real procedure request: R2 explicitly asks us to retry.
			// Only the idempotent read is retried; missing objects and other errors
			// keep their existing semantics. Do not turn storage outages into 404s.
			if (
				attempt >= 2 ||
				!(error instanceof Error) ||
				error.message !==
					"get: We encountered an internal error. Please try again. (10001)"
			) {
				throw error;
			}
			console.warn({
				event: "r2-read-retry",
				code: 10001,
				attempt: attempt + 1,
			});
			await new Promise((resolve) =>
				setTimeout(
					resolve,
					100 * 2 ** attempt + Math.floor(Math.random() * 25),
				),
			);
		}
	}
}

export async function readR2Text(
	bucket: R2Bucket | undefined,
	key: string,
): Promise<R2Result | null> {
	if (!bucket || typeof bucket.get !== "function") return null;
	const obj = await getWithRetry(bucket, key);
	if (!obj) return null;
	return {
		text: await obj.text(),
		etag: obj.httpEtag ?? null,
		contentType:
			obj.httpMetadata?.contentType ?? "application/json; charset=utf-8",
	};
}

export async function readR2Json<T>(
	bucket: R2Bucket | undefined,
	key: string,
): Promise<T | null> {
	const r = await readR2Text(bucket, key);
	if (!r) return null;
	try {
		return JSON.parse(r.text) as T;
	} catch {
		return null;
	}
}

export type R2PassthroughOptions = {
	cacheControl?: string;
	source?: string;
};

/**
 * Stream an R2 object directly back to the client. Mirrors the existing
 * Pages-Functions pattern of returning `new Response(object.body, {...})`.
 * Returns null when the bucket binding is unavailable or the key is missing.
 */
export async function r2Passthrough(
	bucket: R2Bucket | undefined,
	key: string,
	opts: R2PassthroughOptions = {},
): Promise<Response | null> {
	if (!bucket || typeof bucket.get !== "function") return null;
	const obj = await getWithRetry(bucket, key);
	if (!obj) return null;
	const headers: Record<string, string> = {
		"content-type":
			obj.httpMetadata?.contentType ?? "application/json; charset=utf-8",
		"cache-control": opts.cacheControl ?? "public, max-age=300",
	};
	if (obj.httpEtag) headers.etag = obj.httpEtag;
	if (opts.source) headers["x-hl-source"] = opts.source;
	return new Response(obj.body, { headers });
}
