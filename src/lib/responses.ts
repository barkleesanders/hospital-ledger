/**
 * Shared HTTP response helpers. Replaces the duplicated json/responseFromR2
 * helpers from every site/functions/api/*.js file.
 */

const DEFAULT_CACHE = "public, max-age=300";
const NO_STORE = "no-store";

export type JsonInit = {
	status?: number;
	cacheControl?: string;
	extraHeaders?: Record<string, string>;
};

export function json<T>(body: T, init: JsonInit = {}): Response {
	const status = init.status ?? 200;
	const cacheControl =
		init.cacheControl ?? (status === 200 ? DEFAULT_CACHE : NO_STORE);
	const headers: Record<string, string> = {
		"content-type": "application/json; charset=utf-8",
		"cache-control": cacheControl,
		...(init.extraHeaders ?? {}),
	};
	return new Response(JSON.stringify(body), { status, headers });
}

export function notFound(
	message: string,
	extra: Record<string, unknown> = {},
): Response {
	return json({ error: message, ...extra }, { status: 404 });
}

export function badRequest(
	message: string,
	extra: Record<string, unknown> = {},
): Response {
	return json({ error: message, ...extra }, { status: 400 });
}

export function htmlResponse(
	html: string,
	init: { status?: number; cacheControl?: string } = {},
): Response {
	const status = init.status ?? 200;
	const cacheControl = init.cacheControl ?? "public, max-age=60";
	return new Response(html, {
		status,
		headers: {
			"content-type": "text/html; charset=utf-8",
			"cache-control": cacheControl,
		},
	});
}
