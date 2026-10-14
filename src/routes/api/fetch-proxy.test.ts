import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type FetchEnv, fetchProxyHandler, parseTarget } from "./fetch-proxy";

const TOKEN = "t".repeat(48);
const MRF = "https://hospital.example.org/mrf/standardcharges.json";

function app(token: string | undefined = TOKEN) {
	const a = new Hono<FetchEnv>();
	a.get("/api/fetch", fetchProxyHandler);
	const env: FetchEnv["Bindings"] = { FETCH_PROXY_TOKEN: token };
	return (path: string, init: RequestInit = {}) => a.request(path, init, env);
}

const auth = { authorization: `Bearer ${TOKEN}` };
const q = (u: string) => `/api/fetch?url=${encodeURIComponent(u)}`;

function upstream(body: BodyInit | null, init: ResponseInit, url = MRF) {
	const r = new Response(body, init);
	Object.defineProperty(r, "url", { value: url });
	return r;
}

afterEach(() => vi.unstubAllGlobals());

describe("auth", () => {
	it("answers 503 when the secret is not configured (never open)", async () => {
		const fetchSpy = vi.fn();
		vi.stubGlobal("fetch", fetchSpy);
		const res = await app("")(q(MRF), { headers: auth });
		expect(res.status).toBe(503);
		expect(res.headers.get("x-hl-proxy-error")).toBe("not_configured");
		expect(fetchSpy).not.toHaveBeenCalled();
	});

	it.each([
		["no header", {}],
		["wrong token", { authorization: "Bearer nope" }],
		["wrong scheme", { authorization: `Basic ${TOKEN}` }],
	])("rejects %s with 401 before any upstream fetch", async (_, headers) => {
		const fetchSpy = vi.fn();
		vi.stubGlobal("fetch", fetchSpy);
		const res = await app()(q(MRF), { headers });
		expect(res.status).toBe(401);
		expect(fetchSpy).not.toHaveBeenCalled();
	});
});

describe("parseTarget", () => {
	it.each([
		[undefined],
		["not a url"],
		["ftp://h.example/x"],
		["file:///etc/passwd"],
		["https://user:pw@h.example/x"],
		["https://hospitalledger.com/api/fetch"],
		["https://www.hospitalledger.com/x"],
		[`https://h.example/${"a".repeat(5000)}`],
	])("refuses %s", (raw) => {
		expect(parseTarget(raw)).toBeNull();
	});

	it("accepts http and https hospital URLs", () => {
		expect(parseTarget(MRF)?.hostname).toBe("hospital.example.org");
		expect(parseTarget("http://h.example/a.csv")?.protocol).toBe("http:");
	});
});

describe("pass-through", () => {
	it("streams the body, keeps status, and reports the final URL", async () => {
		const fetchSpy = vi.fn(async () =>
			upstream(
				'{"a":1}',
				{
					status: 200,
					headers: {
						"content-type": "application/json",
						etag: '"abc"',
						"last-modified": "Tue, 13 Oct 2026 00:00:00 GMT",
						"content-length": "7",
						"set-cookie": "s=1",
					},
				},
				"https://cdn.example.org/final.json",
			),
		);
		vi.stubGlobal("fetch", fetchSpy);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(200);
		expect(await res.text()).toBe('{"a":1}');
		expect(res.headers.get("x-hl-final-url")).toBe(
			"https://cdn.example.org/final.json",
		);
		expect(res.headers.get("etag")).toBe('"abc"');
		expect(res.headers.get("x-hl-upstream-content-length")).toBe("7");
		expect(res.headers.get("set-cookie")).toBeNull();
		expect(res.headers.get("x-hl-proxy-error")).toBeNull();
	});

	it("forwards Range and conditional headers and preserves 206/304/404", async () => {
		for (const status of [206, 304, 404]) {
			const fetchSpy = vi.fn(async () =>
				upstream(status === 304 ? null : "x", {
					status,
					headers: status === 206 ? { "content-range": "bytes 0-0/9" } : {},
				}),
			);
			vi.stubGlobal("fetch", fetchSpy);
			const res = await app()(q(MRF), {
				headers: {
					...auth,
					range: "bytes=0-0",
					"if-none-match": '"abc"',
					"if-modified-since": "Tue, 13 Oct 2026 00:00:00 GMT",
				},
			});
			expect(res.status).toBe(status);
			expect(res.headers.get("x-hl-proxy-error")).toBeNull();
			const sent = new Headers(
				(fetchSpy.mock.calls[0] as unknown as [string, RequestInit])[1].headers,
			);
			expect(sent.get("range")).toBe("bytes=0-0");
			expect(sent.get("if-none-match")).toBe('"abc"');
			expect(sent.get("authorization")).toBeNull();
			if (status === 206)
				expect(res.headers.get("content-range")).toBe("bytes 0-0/9");
		}
	});

	it("sends HEAD upstream as HEAD", async () => {
		const fetchSpy = vi.fn(async () =>
			upstream(null, { status: 200, headers: { "content-length": "99" } }),
		);
		vi.stubGlobal("fetch", fetchSpy);
		const res = await app()(q(MRF), { method: "HEAD", headers: auth });
		expect(res.status).toBe(200);
		const init = (
			fetchSpy.mock.calls[0] as unknown as [string, RequestInit]
		)[1];
		expect(init.method).toBe("HEAD");
	});

	it("marks network failures as proxy errors, distinct from upstream 502s", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => {
				throw new TypeError("Network connection lost.");
			}),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(502);
		expect(res.headers.get("x-hl-proxy-error")).toBe("connect");

		vi.stubGlobal(
			"fetch",
			vi.fn(async () => upstream("bad gateway", { status: 502 })),
		);
		const real = await app()(q(MRF), { headers: auth });
		expect(real.status).toBe(502);
		expect(real.headers.get("x-hl-proxy-error")).toBeNull();
	});
});

describe("bot-shield interstitials", () => {
	it("reports a SiteGround sgcaptcha 202 as a proxy error, never as the hospital's answer", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () =>
				upstream(
					'<html><head><meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=%2Fx.xlsx"></meta></head></html>',
					{ status: 202, headers: { "content-type": "text/html" } },
				),
			),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(502);
		expect(res.headers.get("x-hl-proxy-error")).toBe("upstream_challenge");
	});

	it("reports WP Engine's 404 'Site is not available' as a block, so the wave never marks the host dead", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () =>
				upstream(
					"<!DOCTYPE html><html><head>\n    <title>Site is not available</title></head></html>",
					{ status: 404, headers: { "content-type": "text/html" } },
				),
			),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(502);
		expect(res.headers.get("x-hl-proxy-error")).toBe("upstream_challenge");
	});

	it("reports Akamai's 'Access Denied' 404 as a block", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () =>
				upstream(
					"<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net/18.x</BODY></HTML>",
					{ status: 404, headers: { "content-type": "text/html" } },
				),
			),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.headers.get("x-hl-proxy-error")).toBe("upstream_challenge");
	});

	it("keeps a hospital's ordinary HTML 404 a 404", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () =>
				upstream("<html><title>Page not found</title></html>", {
					status: 404,
					headers: { "content-type": "text/html" },
				}),
			),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(404);
		expect(res.headers.get("x-hl-proxy-error")).toBeNull();
	});

	it("passes an ordinary HTML 403 through byte-for-byte, including bodies past the sniff window", async () => {
		const big = `<html>${"x".repeat(200_000)}</html>`;
		vi.stubGlobal(
			"fetch",
			vi.fn(async () =>
				upstream(big, {
					status: 403,
					headers: { "content-type": "text/html" },
				}),
			),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(403);
		expect(res.headers.get("x-hl-proxy-error")).toBeNull();
		expect(await res.text()).toBe(big);
	});

	it("does not sniff a 200 even when it looks like a challenge", async () => {
		const body = "<p>/.well-known/sgcaptcha/</p>";
		vi.stubGlobal(
			"fetch",
			vi.fn(async () =>
				upstream(body, {
					status: 200,
					headers: { "content-type": "text/html" },
				}),
			),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(200);
		expect(await res.text()).toBe(body);
	});
});

describe("Cloudflare-generated origin failures", () => {
	it.each([
		[522, "connect"],
		[530, "connect"],
		[524, "timeout"],
		[526, "upstream_tls"],
	])("maps %i to proxy error %s", async (status, kind) => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => upstream(`error code: ${status}`, { status })),
		);
		const res = await app()(q(MRF), { headers: auth });
		expect(res.status).toBe(502);
		expect(res.headers.get("x-hl-proxy-error")).toBe(kind);
	});
});

describe("HEAD against a bot shield", () => {
	it("re-asks with GET and reports the challenge instead of a fake 'alive' 202", async () => {
		const fetchSpy = vi.fn(async (_u: string, init: RequestInit) =>
			init.method === "HEAD"
				? upstream(null, {
						status: 202,
						headers: { "content-type": "text/html" },
					})
				: upstream(
						'<meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=x">',
						{
							status: 202,
							headers: { "content-type": "text/html" },
						},
					),
		);
		vi.stubGlobal("fetch", fetchSpy);
		const res = await app()(q(MRF), { method: "HEAD", headers: auth });
		expect(res.headers.get("x-hl-proxy-error")).toBe("upstream_challenge");
		expect(fetchSpy).toHaveBeenCalledTimes(2);
	});

	it("does not re-ask for an ordinary HEAD 200", async () => {
		const fetchSpy = vi.fn(async () =>
			upstream(null, { status: 200, headers: { "content-type": "text/csv" } }),
		);
		vi.stubGlobal("fetch", fetchSpy);
		const res = await app()(q(MRF), { method: "HEAD", headers: auth });
		expect(res.status).toBe(200);
		expect(fetchSpy).toHaveBeenCalledTimes(1);
	});
});
