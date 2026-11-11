import type { MiddlewareHandler } from "hono";

// ────────────── Wide-event logging (native observability, 2026-11-11) ──────────────
//
// One canonical JSON event per request, emitted to Workers Logs via
// console.log — the loggingsucks.com "wide event" pattern, implemented with
// Cloudflare-native tooling only (no external observability product):
// every field a future query needs (route, status, outcome, duration,
// colo, request id) rides on a single structured line instead of being
// scattered across free-text logs. Per-function detail comes from the
// platform: Workers Traces ([observability] in wrangler.jsonc) records
// spans for the runtime, fetch, and bindings automatically.
//
// This is the per-REQUEST event. The domain lifecycle logs elsewhere in
// this repo (the FAQ ask logger in src/faq/faq-route.ts, the submission
// logs in src/routes/api/submissions.ts) carry their own event names and
// are untouched — a request still gets exactly one "http_request" event.
//
// Privacy posture: pathname only — no query string (tokens can ride in
// it), no cookies, no body. The request id is the Cloudflare ray id,
// which joins this event to the platform trace and to Cloudflare Issues.

export const wideEvent: MiddlewareHandler = async (c, next) => {
	const start = Date.now();
	let errorName: string | undefined;
	try {
		await next();
	} catch (err) {
		errorName = err instanceof Error ? err.name : "UnknownError";
		throw err;
	} finally {
		let status = 500;
		if (!errorName) {
			try {
				status = c.res.status;
			} catch {
				status = 500;
			}
		}
		const cf = c.req.raw.cf;
		console.log(
			JSON.stringify({
				event: "http_request",
				ts: new Date().toISOString(),
				request_id: c.req.header("cf-ray"),
				method: c.req.method,
				path: new URL(c.req.url).pathname,
				status,
				outcome:
					errorName || status >= 500
						? "error"
						: status >= 400
							? "client_error"
							: "ok",
				duration_ms: Date.now() - start,
				colo: cf?.colo,
				country: cf?.country,
				user_agent: c.req.header("user-agent")?.slice(0, 120),
				...(errorName ? { error: errorName } : {}),
			}),
		);
	}
};
