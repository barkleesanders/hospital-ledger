import { beforeEach, describe, expect, it } from "vitest";
import { loadFaqCorpus, pageText } from "../faq/faq-corpus";
import type { FaqAi } from "../faq/faq-route";
import app from "../index";
import { aboutPage } from "../routes/about-the-numbers";
import { homePage } from "../routes/home";
import { derivePriceCoverage, priceCoverage } from "./price-coverage";
import { bundledSummary, loadSummary, resetSummaryMemo } from "./site-data";

const registry = [
	{ ccn: "010001", required: true, has_live_mrf: true },
	{ ccn: "010002", required: true, has_live_mrf: true },
	{ ccn: "010003", required: true, has_live_mrf: true },
	{ ccn: "010004", required: false, has_live_mrf: true },
	{ ccn: "010005", required: true, has_live_mrf: false },
];

const index = {
	hospitals: [
		{ ccn: "010001", n: 12 },
		{ ccn: "010002", n: 0 },
		{ ccn: "010004", n: 20 },
		{ ccn: "010005", n: 30 },
		{ ccn: "25063E", n: 40 },
	],
};

const counts = {
	required_live_price_hospitals: 1,
	required_live_zero_price_hospitals: 1,
	required_live_unindexed_hospitals: 1,
	price_hospitals_outside_required_live: 3,
};

const legacySummary = {
	...bundledSummary,
	generated_at: "2026-10-26T01:00:00Z",
	total_facilities: 5,
	cms_required_total: 4,
	compliant: 3,
	standardized_price_index_hospitals: 5,
	standardized_price_hospitals: 4,
	zero_price_index_entries: 1,
};
// Remove the additive fields to exercise the actual old-producer contract.
for (const key of Object.keys(counts))
	Reflect.deleteProperty(legacySummary, key);

function environment(summary = legacySummary, prices = index) {
	const objects: Record<string, string> = {
		"meta/summary.json": JSON.stringify(summary),
		"meta/hospitals.json": JSON.stringify(registry),
		"prices/index.json": JSON.stringify(prices),
	};
	const get = async (key: string) => {
		const text = objects[key];
		return text === undefined
			? null
			: {
					text: async () => text,
					body: new Response(text).body,
					httpEtag: '"fixture"',
					httpMetadata: { contentType: "application/json" },
				};
	};
	// SAFETY: the subject only calls the R2 get/text/body metadata surface above.
	return { HL_MRF_PARSED: { get } as unknown as R2Bucket };
}

beforeEach(resetSummaryMemo);

describe("price coverage uses identifier intersections", () => {
	it("keeps legacy/nonrequired/nonlive prices without hiding the required-live gap", () => {
		expect(derivePriceCoverage(registry, index)).toEqual({
			...counts,
			registry_hospitals: 5,
			required_live_hospitals: 3,
			index_hospitals: 5,
			price_hospitals: 4,
		});
		expect(priceCoverage({ ...legacySummary, ...counts })).toEqual(counts);
	});

	it.each([
		["duplicate registry id", [...registry, registry[0]], index],
		[
			"duplicate price id",
			registry,
			{ hospitals: [...index.hospitals, index.hospitals[0]] },
		],
		["missing row count", registry, { hospitals: [{ ccn: "010001" }] }],
		["invalid CCN", registry, { hospitals: [{ ccn: "010/01", n: 1 }] }],
		["negative rows", registry, { hospitals: [{ ccn: "010001", n: -1 }] }],
		[
			"wrong registry flag",
			[{ ccn: "010001", required: "true", has_live_mrf: true }],
			index,
		],
	])("does not report coverage for %s", (_name, hospitals, prices) => {
		expect(derivePriceCoverage(hospitals, prices)).toBeNull();
	});

	it("does not turn missing or contradictory cohort fields into zero gaps", () => {
		expect(priceCoverage(legacySummary)).toBeNull();
		expect(
			priceCoverage({ ...legacySummary, ...counts, compliant: 99 }),
		).toBeNull();
		// The two sums balance, but the full index only has one zero-row entry.
		expect(
			priceCoverage({
				...legacySummary,
				...counts,
				required_live_price_hospitals: 0,
				required_live_zero_price_hospitals: 2,
				price_hospitals_outside_required_live: 4,
			}),
		).toBeNull();
	});
});

describe("old producer compatibility, served pages and FAQ", () => {
	it("derives coverage for SSR and API but preserves manifest-bound raw summary bytes", async () => {
		const env = environment();
		const loaded = await loadSummary(env);
		expect(priceCoverage(loaded.summary)).toEqual(counts);
		for (const path of ["/", "/about-the-numbers"]) {
			const response = await app.request(path, {}, env);
			expect(response.status).toBe(200);
			const text = pageText(await response.text());
			expect(text).toContain(
				"Of 3 CMS-required hospitals with a verified live file, 1 have searchable",
			);
			expect(text).toContain(
				path === "/"
					? "1 are not yet in the price index"
					: "1 hospitals aren't in the price index",
			);
			expect(text).not.toContain(
				"Every CMS-required hospital with a live file is now accounted for",
			);
		}
		const response = await app.request("/api/coverage", {}, env);
		expect(response.status).toBe(200);
		expect(await response.json()).toEqual({
			status: "available",
			source: "r2",
			generated_at: "2026-10-26T01:00:00Z",
			cms_required_live_hospitals: 3,
			all_price_hospitals: 4,
			...counts,
		});
		const raw = await app.request("/data/summary.json", {}, env);
		expect(await raw.text()).toBe(JSON.stringify(legacySummary));
		const docs = await loadFaqCorpus(env);
		expect(docs[0].text).toContain("2 still lack searchable rows");
		expect(docs[1].text).toContain("The 2-hospital gap");
	});

	it("refreshes FAQ grounding when data changes without a Worker deploy", async () => {
		const first = await loadFaqCorpus(environment());
		resetSummaryMemo();
		const changed = {
			...legacySummary,
			...counts,
			required_live_price_hospitals: 2,
			required_live_unindexed_hospitals: 0,
			price_hospitals_outside_required_live: 2,
		};
		const next = await loadFaqCorpus(environment(changed));
		expect(first[1].text).toContain("The 2-hospital gap");
		expect(next[1].text).toContain("The 1-hospital gap");
	});

	it("passes live cohort grounding through the actual FAQ request to the model boundary", async () => {
		const prompts: string[] = [];
		const AI: FaqAi = {
			run: async (_model, input) => {
				prompts.push(input.messages[0].content);
				return new ReadableStream<Uint8Array>({
					start(controller) {
						controller.enqueue(
							new TextEncoder().encode(
								'data: {"response":"Fixture grounded response."}\n\n',
							),
						);
						controller.close();
					},
				});
			},
		};
		const response = await app.request(
			"/api/faq/ask",
			{
				method: "POST",
				headers: {
					"content-type": "application/json",
					"cf-connecting-ip": "192.0.2.192",
				},
				body: JSON.stringify({
					question: "How many hospitals are missing searchable prices?",
				}),
			},
			{ ...environment(), AI },
		);
		expect(response.status).toBe(200);
		expect(response.headers.get("content-type")).toContain("text/event-stream");
		expect(response.headers.get("cache-control")).toContain("no-store");
		expect(await response.text()).toContain("Fixture grounded response.");
		expect(prompts).toHaveLength(1);
		expect(prompts[0]).toContain("The 2-hospital gap");
		expect(prompts[0]).toContain("1 are not yet in the price index");
	});

	it("reports mismatched snapshots unavailable instead of inventing a complete cohort", async () => {
		const env = environment({ ...legacySummary, compliant: 4 });
		const response = await app.request("/api/coverage", {}, env);
		expect(response.status).toBe(503);
		expect(response.headers.get("cache-control")).toBe("no-store");
		expect(await response.json()).toMatchObject({ status: "unavailable" });
		const { summary } = await loadSummary(env);
		for (const render of [homePage, aboutPage]) {
			const text = pageText(
				String(render("https://hospitalledger.com/", summary)),
			);
			expect(text).toContain("different populations");
			expect(text).not.toContain("0-hospital gap");
		}
	});

	it("keeps the page usable when optional cohort storage cannot be read", async () => {
		const get = async (key: string) => {
			if (key !== "meta/summary.json")
				throw new Error("fixture storage outage");
			return { text: async () => JSON.stringify(legacySummary) };
		};
		// SAFETY: the loader uses get/text only and deliberately receives a read failure.
		const env = { HL_MRF_PARSED: { get } as unknown as R2Bucket };
		const response = await app.request("/", {}, env);
		expect(response.status).toBe(200);
		expect(pageText(await response.text())).toContain(
			"breakdown is not currently available",
		);
		const coverage = await app.request("/api/coverage", {}, env);
		expect(coverage.status).toBe(503);
	});
});
