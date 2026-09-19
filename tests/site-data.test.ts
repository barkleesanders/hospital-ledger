/**
 * Unit tests for src/lib/site-data.ts — the R2-first summary/manifest loader.
 * Run with: npm run test:unit (esbuild-bundled, then node --test)
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
	isValidSummary,
	loadManifest,
	loadSummary,
} from "../src/lib/site-data.ts";

function fakeBucket(objects: Record<string, string>) {
	return {
		async get(key: string) {
			if (!(key in objects)) return null;
			const text = objects[key];
			return {
				text: async () => text,
				httpEtag: `"${key}"`,
				httpMetadata: { contentType: "application/json" },
			};
		},
	} as unknown as R2Bucket;
}

const GOOD = {
	generated_at: "2026-09-19T00:00:00Z",
	cms_required_total: 4625,
	compliant: 3990,
	compliance_pct: 86.3,
	enforcement_actions_total: 8700,
	standardized_price_hospitals: 3700,
};

test("isValidSummary accepts the contract's required fields and rejects a partial upload", () => {
	assert.equal(isValidSummary(GOOD), true);
	assert.equal(isValidSummary({ ...GOOD, generated_at: "yesterday" }), false);
	assert.equal(isValidSummary({ generated_at: GOOD.generated_at }), false);
	assert.equal(isValidSummary(null), false);
	assert.equal(isValidSummary("{}"), false);
});

test("loadSummary prefers a valid R2 meta/summary.json", async () => {
	const env = { HL_MRF_PARSED: fakeBucket({ "meta/summary.json": JSON.stringify(GOOD) }) };
	const { summary, source } = await loadSummary(env);
	assert.equal(source, "r2");
	assert.equal(summary.generated_at, GOOD.generated_at);
	assert.equal(summary.standardized_price_hospitals, 3700);
});

test("loadSummary memoises within an isolate (second read within TTL is served from memo)", async () => {
	let reads = 0;
	const bucket = fakeBucket({ "meta/summary.json": JSON.stringify(GOOD) });
	const orig = bucket.get.bind(bucket);
	(bucket as { get: unknown }).get = async (k: string) => {
		reads++;
		return orig(k);
	};
	await loadSummary({ HL_MRF_PARSED: bucket });
	await loadSummary({ HL_MRF_PARSED: bucket });
	// The memo is module-global and was primed by the previous test, so the
	// count here is 0 or 1 — never 2. Either way the second call did not re-read.
	assert.ok(reads <= 1, `expected <=1 R2 read, got ${reads}`);
});

test("loadManifest returns the R2 manifest when present, else describes the bundled copy", async () => {
	const m = await loadManifest({
		HL_MRF_PARSED: fakeBucket({
			"meta/manifest.json": JSON.stringify({
				contract_version: "1",
				generated_at: "2026-09-19T00:00:00Z",
				producer: "muse.ai nova",
			}),
		}),
	});
	assert.equal(m.producer, "muse.ai nova");
	const fallback = await loadManifest({ HL_MRF_PARSED: fakeBucket({}) });
	assert.equal(fallback.contract_version, "1");
	assert.match(fallback.producer ?? "", /bundled/);
	assert.match(fallback.generated_at, /^\d{4}-\d{2}-\d{2}T/);
});
