import { beforeEach, describe, expect, it } from "vitest";
import app from "../index";
import { bundledSummary, loadSummary, resetSummaryMemo } from "./site-data";

/**
 * R2-first site data (HOME-21cl3). The muse.ai wave publishes
 * meta/summary.json, meta/hospitals.json and prices/index.json to R2; the
 * site must serve those, and fall back to the build-time bundle only when R2
 * has nothing usable.
 */

const LIVE_SUMMARY = {
	...bundledSummary,
	generated_at: "2026-10-14T00:56:57Z",
	compliant: 3986,
	standardized_price_index_hospitals: 3831,
};

function bucket(objects: Record<string, string>): R2Bucket {
	const get = async (key: string) => {
		const text = objects[key];
		if (text === undefined) return null;
		return {
			text: async () => text,
			body: new Response(text).body,
			httpEtag: `"${key}"`,
			httpMetadata: { contentType: "application/json; charset=utf-8" },
		};
	};
	return { get } as unknown as R2Bucket;
}

function assets(files: Record<string, string>): Fetcher {
	const fetch = async (input: RequestInfo | URL) => {
		const path = new URL(String(input)).pathname;
		const text = files[path];
		return text === undefined
			? new Response("not found", { status: 404 })
			: new Response(text, {
					headers: { "content-type": "application/json; charset=utf-8" },
				});
	};
	return { fetch } as unknown as Fetcher;
}

const index = (n: number) =>
	JSON.stringify({
		hospitals: Array.from({ length: n }, (_, i) => ({ ccn: String(i) })),
	});

function env(r2: Record<string, string>, files: Record<string, string> = {}) {
	return { HL_MRF_PARSED: bucket(r2), ASSETS: assets(files) };
}

beforeEach(() => resetSummaryMemo());

describe("loadSummary", () => {
	it("serves a valid R2 summary", async () => {
		const r = await loadSummary(
			env({ "meta/summary.json": JSON.stringify(LIVE_SUMMARY) }),
		);
		expect(r.source).toBe("r2");
		expect(r.summary.generated_at).toBe("2026-10-14T00:56:57Z");
		expect(r.summary.standardized_price_index_hospitals).toBe(3831);
	});

	it.each([
		["missing", {}],
		["unparsable", { "meta/summary.json": "{trunc" }],
		[
			"partial",
			{
				"meta/summary.json": JSON.stringify({
					generated_at: "2026-10-14T00:00:00Z",
				}),
			},
		],
	])("falls back to the bundled copy when R2 is %s", async (_, objects) => {
		const r = await loadSummary(env(objects));
		expect(r.source).toBe("bundled");
		expect(r.summary.generated_at).toBe(bundledSummary.generated_at);
	});
});

describe("site routes render the R2 numbers", () => {
	it("home and /about-the-numbers show R2 counts and say so", async () => {
		const e = env({ "meta/summary.json": JSON.stringify(LIVE_SUMMARY) });
		for (const path of ["/", "/about-the-numbers"]) {
			resetSummaryMemo();
			const res = await app.request(path, {}, e);
			expect(res.status).toBe(200);
			expect(res.headers.get("x-hl-source")).toBe("r2");
			const html = await res.text();
			expect(html).toContain("3,986");
			expect(html).toContain("2026-10-14");
		}
	});
});

describe("/data/*.json", () => {
	it("serves summary and hospitals from R2", async () => {
		const e = env(
			{
				"meta/summary.json": '{"from":"r2"}',
				"meta/hospitals.json": '[{"r2":1}]',
			},
			{ "/data/summary.json": '{"from":"bundle"}' },
		);
		const s = await app.request("/data/summary.json", {}, e);
		expect(await s.text()).toBe('{"from":"r2"}');
		expect(s.headers.get("x-hl-source")).toBe("r2");
		const h = await app.request("/data/hospitals.json", {}, e);
		expect(await h.text()).toBe('[{"r2":1}]');
	});

	it("falls back to the bundled asset when R2 has no object", async () => {
		const e = env({}, { "/data/summary.json": '{"from":"bundle"}' });
		const s = await app.request("/data/summary.json", {}, e);
		expect(await s.text()).toBe('{"from":"bundle"}');
		expect(s.headers.get("x-hl-source")).toBe("bundled");
	});

	it("serves the R2 price index even when the stale bundle lists more hospitals", async () => {
		const e = env(
			{ "prices/index.json": index(3) },
			{ "/data/prices/index.json": index(5) },
		);
		for (const path of ["/data/prices/index.json", "/api/prices-index"]) {
			const res = await app.request(path, {}, e);
			expect(res.headers.get("x-hl-price-index-source")).toBe("r2");
			expect(res.headers.get("x-hl-price-index-hospitals")).toBe("3");
		}
	});

	it.each([
		["missing", {}],
		["empty", { "prices/index.json": index(0) }],
		["unparsable", { "prices/index.json": '{"hospitals":[' }],
	])(
		"falls back to the bundled price index when R2 is %s",
		async (_, objects) => {
			const e = env(objects, { "/data/prices/index.json": index(5) });
			const res = await app.request("/data/prices/index.json", {}, e);
			expect(res.headers.get("x-hl-price-index-source")).toBe("assets");
			expect(res.headers.get("x-hl-price-index-hospitals")).toBe("5");
		},
	);
});
