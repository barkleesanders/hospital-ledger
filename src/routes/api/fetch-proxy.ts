/**
 * GET|HEAD /api/fetch?url=<hospital MRF URL> — owner-gated edge fetch for the
 * data pipeline (ops/wave/*, scripts/probe_mrf_urls.py, scripts/mrf_parse.py).
 *
 * Why it exists: the muse.ai VM that runs the pipeline gates outbound traffic
 * per destination domain, one human approval each. The seeded MRF URLs span
 * 1,136 distinct hosts (`mrf_seed`, measured 2026-10-13), so every wave raised
 * a fresh approval per hospital. Routed through here, the VM contacts one
 * domain (hospitalledger.com, which it already uses for /api/manifest) and
 * Cloudflare fetches the hospitals. scripts/hl_edge.py is the client side.
 *
 * Semantics are a transparent pass-through so the Python callers keep their
 * logic: the upstream status (200/206/304/404/...) and body stream back
 * unchanged, redirects are followed here, and Range / conditional headers are
 * forwarded. The body is piped, never buffered, so multi-hundred-MB MRFs cost
 * no Worker CPU. Proxy-side failures are marked with `x-hl-proxy-error` so a
 * client never mistakes them for a hospital's own status.
 *
 * Owner-gated: Authorization: Bearer <FETCH_PROXY_TOKEN> (a Worker secret).
 * Without the secret configured the route answers 503 — never open.
 */

import type { Context } from "hono";

export type FetchEnv = {
	Bindings: {
		/** Secret bearer for /api/fetch. Unset = route disabled (503). */
		FETCH_PROXY_TOKEN?: string;
	};
};

const UA =
	"HospitalLedgerBot/0.1 (+https://github.com/hospital-ledger; open-source public-good price transparency crawler)";

// ceiling: the callers' own httpx timeouts are 30-120 s; this bounds only the
// wait for upstream response headers, never the body stream (MRFs take minutes).
const HEADERS_TIMEOUT_MS = 90_000;
// ceiling: real seed URLs top out far below this; it only bounds abuse.
const MAX_URL_CHARS = 4096;

const FORWARD_REQUEST_HEADERS = [
	"range",
	"if-none-match",
	"if-modified-since",
	"accept",
	"user-agent",
] as const;

const FORWARD_RESPONSE_HEADERS = [
	"content-type",
	"content-range",
	"accept-ranges",
	"etag",
	"last-modified",
] as const;

// Bot shields that answer Cloudflare's egress IPs with an interstitial instead
// of the file. Measured 2026-10-13: SiteGround returns 202 + a meta refresh to
// /.well-known/sgcaptcha/ for hospitals it serves directly with 200.
const CHALLENGE_MARKERS: ReadonlyArray<[RegExp, string]> = [
	[/\/\.well-known\/sgcaptcha\//, "sgcaptcha"],
	[/_cf_chl_opt|cf-chl-|<title>Just a moment/i, "cloudflare"],
	[/_Incapsula_Resource|incident_id/i, "incapsula"],
	[/aws-waf-token|awswaf/i, "aws-waf"],
	[/captcha-delivery\.com|geo\.captcha-delivery/i, "datadome"],
	[/sucuri_cloudproxy|Sucuri WebSite Firewall/i, "sucuri"],
	// WP Engine answers Cloudflare's egress with a 404 "Site is not available"
	// page (measured 2026-10-13, acmhosp.wpengine.com: direct 206, edge 404).
	// Passed through, the wave would mark a live hospital dead.
	[/<title>\s*Site is not available\s*<\/title>/i, "wpengine"],
	// Akamai's "Access Denied" carries an errors.edgesuite.net reference; it
	// reached the edge as a 404 for benefis.org (measured 2026-10-13).
	[/errors\.edgesuite\.net|<title>\s*Access Denied\s*<\/title>/i, "akamai"],
];
// ceiling: interstitials measured at 233-296 bytes; real HTML landing pages a
// caller needs are never sniffed because only these statuses are inspected.
const SNIFF_MAX_BYTES = 64 * 1024;
const SNIFF_STATUSES = new Set([202, 403, 404, 405, 429, 503]);

// Cloudflare-generated statuses for a fetch that never reached a usable origin.
// Measured 2026-10-13: an origin with an invalid certificate answers the edge
// 526 (a direct client with verification off gets the file), an origin that
// drops Cloudflare's egress answers 522. Reported as proxy errors so the
// pipeline can tell "the edge could not reach it" from "the hospital said no".
const EDGE_FAILURES: ReadonlyMap<number, string> = new Map([
	[521, "connect"],
	[522, "connect"],
	[523, "connect"],
	[530, "connect"],
	[524, "timeout"],
	[525, "upstream_tls"],
	[526, "upstream_tls"],
]);

const encoder = new TextEncoder();

async function tokenMatches(given: string, expected: string): Promise<boolean> {
	// Compare SHA-256 digests with a branch-free loop: equal-length inputs, so
	// the comparison time leaks neither the token nor its length. (Workers'
	// crypto.subtle.timingSafeEqual is non-standard and absent under node tests.)
	const [a, b] = await Promise.all([
		crypto.subtle.digest("SHA-256", encoder.encode(given)),
		crypto.subtle.digest("SHA-256", encoder.encode(expected)),
	]);
	const x = new Uint8Array(a);
	const y = new Uint8Array(b);
	let diff = 0;
	for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
	return diff === 0;
}

/**
 * Read up to `max` bytes of a body for sniffing, and hand back a stream that
 * replays them followed by the unread remainder, so nothing is lost or buffered.
 */
async function readHead(
	body: ReadableStream<Uint8Array>,
	max: number,
): Promise<{ head: Uint8Array; rest: ReadableStream<Uint8Array> }> {
	const reader = body.getReader();
	const chunks: Uint8Array[] = [];
	let size = 0;
	let done = false;
	while (size < max) {
		const r = await reader.read();
		if (r.done) {
			done = true;
			break;
		}
		chunks.push(r.value);
		size += r.value.byteLength;
	}
	const head = new Uint8Array(size);
	let off = 0;
	for (const c of chunks) {
		head.set(c, off);
		off += c.byteLength;
	}
	const rest = new ReadableStream<Uint8Array>({
		start(controller) {
			if (size) controller.enqueue(head);
			if (done) controller.close();
		},
		async pull(controller) {
			const r = await reader.read();
			if (r.done) controller.close();
			else controller.enqueue(r.value);
		},
		cancel(reason) {
			return reader.cancel(reason);
		},
	});
	return { head: head.subarray(0, Math.min(size, max)), rest };
}

function proxyError(status: number, kind: string, detail: string): Response {
	return new Response(JSON.stringify({ error: kind, detail }), {
		status,
		headers: {
			"content-type": "application/json; charset=utf-8",
			"cache-control": "no-store",
			"x-hl-proxy-error": kind,
		},
	});
}

/** Parse and vet the target. Returns null when it must not be fetched. */
export function parseTarget(raw: string | undefined): URL | null {
	if (!raw || raw.length > MAX_URL_CHARS) return null;
	let target: URL;
	try {
		target = new URL(raw);
	} catch {
		return null;
	}
	if (target.protocol !== "https:" && target.protocol !== "http:") return null;
	if (target.username || target.password) return null;
	const host = target.hostname.toLowerCase();
	// A Worker fetching its own custom domain is forwarded to the zone origin,
	// which this zone does not have; refuse instead of looping.
	if (host === "hospitalledger.com" || host.endsWith(".hospitalledger.com")) {
		return null;
	}
	return target;
}

export async function fetchProxyHandler(
	c: Context<FetchEnv>,
): Promise<Response> {
	const expected = c.env.FETCH_PROXY_TOKEN;
	if (!expected) {
		return proxyError(503, "not_configured", "FETCH_PROXY_TOKEN is not set");
	}
	const auth = c.req.header("authorization") ?? "";
	const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
	if (!given || !(await tokenMatches(given, expected))) {
		return proxyError(401, "unauthorized", "missing or wrong bearer token");
	}
	const target = parseTarget(c.req.query("url"));
	if (!target) {
		return proxyError(400, "bad_url", "url must be an absolute http(s) URL");
	}

	const method = c.req.method === "HEAD" ? "HEAD" : "GET";
	const headers = new Headers();
	for (const name of FORWARD_REQUEST_HEADERS) {
		const v = c.req.header(name);
		if (v) headers.set(name, v);
	}
	if (!headers.has("user-agent")) headers.set("user-agent", UA);

	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), HEADERS_TIMEOUT_MS);
	let upstream: Response;
	try {
		upstream = await fetch(target.toString(), {
			method,
			headers,
			redirect: "follow",
			signal: controller.signal,
		});
	} catch (e) {
		const name = e instanceof Error ? e.name : "Error";
		const message = e instanceof Error ? e.message : String(e);
		const kind = name === "AbortError" ? "timeout" : "connect";
		return proxyError(502, kind, `${name}: ${message.slice(0, 160)}`);
	} finally {
		clearTimeout(timer);
	}

	// A HEAD reply has no body to sniff, so a bot shield's 202 stub would read
	// as "alive". Re-ask with GET when HEAD lands on a sniffable status.
	if (
		method === "HEAD" &&
		SNIFF_STATUSES.has(upstream.status) &&
		(upstream.headers.get("content-type") ?? "").includes("text/html")
	) {
		const probe = await fetch(target.toString(), {
			method: "GET",
			headers,
			redirect: "follow",
		}).catch(() => null);
		if (probe?.body) {
			const { head, rest } = await readHead(probe.body, SNIFF_MAX_BYTES);
			await rest.cancel();
			const hit = CHALLENGE_MARKERS.find(([re]) =>
				re.test(new TextDecoder().decode(head)),
			);
			if (hit) {
				return proxyError(
					502,
					"upstream_challenge",
					`${hit[1]} challenged the edge (HTTP ${upstream.status})`,
				);
			}
		}
	}

	const edgeFailure = EDGE_FAILURES.get(upstream.status);
	if (edgeFailure) {
		await upstream.body?.cancel();
		return proxyError(
			502,
			edgeFailure,
			`edge reached no usable origin (HTTP ${upstream.status})`,
		);
	}

	if (
		method === "GET" &&
		upstream.body &&
		SNIFF_STATUSES.has(upstream.status) &&
		(upstream.headers.get("content-type") ?? "").includes("text/html")
	) {
		const { head, rest } = await readHead(upstream.body, SNIFF_MAX_BYTES);
		const text = new TextDecoder().decode(head);
		const hit = CHALLENGE_MARKERS.find(([re]) => re.test(text));
		if (hit) {
			await rest.cancel();
			return proxyError(
				502,
				"upstream_challenge",
				`${hit[1]} challenged the edge (HTTP ${upstream.status})`,
			);
		}
		upstream = new Response(rest, upstream);
	}

	const out = new Headers({
		"cache-control": "no-store",
		"x-hl-proxy": "1",
		"x-hl-final-url": (upstream.url || target.toString()).slice(0, 2000),
	});
	for (const name of FORWARD_RESPONSE_HEADERS) {
		const v = upstream.headers.get(name);
		if (v) out.set(name, v);
	}
	// The runtime hands us a decoded body, so the raw length/encoding are only
	// informational; they travel under their own names, never as framing.
	const cl = upstream.headers.get("content-length");
	if (cl) out.set("x-hl-upstream-content-length", cl);
	const ce = upstream.headers.get("content-encoding");
	if (ce) out.set("x-hl-upstream-content-encoding", ce);

	const noBody =
		method === "HEAD" || upstream.status === 204 || upstream.status === 304;
	if (noBody) await upstream.body?.cancel();
	return new Response(noBody ? null : upstream.body, {
		status: upstream.status,
		headers: out,
	});
}
