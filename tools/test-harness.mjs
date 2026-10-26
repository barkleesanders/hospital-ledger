import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";
import ts from "typescript";
import { createTestHarness } from "wrangler";

// Use the deployed config and built entry. Only local fixture bindings differ;
// remote Workers AI is exercised separately after an authorized release.
const root = resolve(import.meta.dirname, "..");
const parsed = ts.parseConfigFileTextToJson(
	"wrangler.jsonc",
	await readFile(resolve(root, "wrangler.jsonc"), "utf8"),
);
assert.equal(parsed.error, undefined);
const config = parsed.config;
delete config.build;
delete config.ai;
delete config.routes;
config.main = resolve(root, config.main);
config.assets.directory = resolve(root, config.assets.directory);
assert.ok(
	(await readFile(config.main)).length > 1000,
	"production build missing",
);
const summary = JSON.parse(
	await readFile(resolve(root, "public/data/summary.json")),
);
const hospital = (ccn, items, compliance = {}) => ({
	ccn,
	hospital_name: `Fixture Hospital ${ccn}`,
	source_url: "https://example.test/published.xlsx",
	format: "xlsx",
	n_slim: items.length,
	compliance,
	items,
});
const historical = hospital("400006", [
	{
		code: "LOCAL001",
		type: "CDM",
		desc: "Historical fixture charge",
		gross: 15,
		cash: null,
		min: null,
		max: null,
		gross_charge_source: {
			effective_date: "2020-01-01",
			header: "STD CHARGE 01/01/20",
			column_1based: 4,
		},
	},
]);
const ungraded = hospital("050025", [
	{
		code: "27130",
		type: "CPT",
		desc: "Fixture hip procedure",
		gross: 30,
		cash: 20,
		min: null,
		max: null,
	},
]);
const empty = hospital("070003", []);
const index = {
	hospitals: [historical, ungraded, empty].map((h) => ({
		ccn: h.ccn,
		name: h.hospital_name,
		n: h.n_slim,
		compliance: h.compliance,
	})),
};
const fixtureSummary = {
	...summary,
	total_facilities: 4,
	cms_required_total: 4,
	compliant: 4,
	standardized_price_hospitals: 2,
	standardized_price_index_hospitals: 3,
	standardized_price_rows: 2,
	zero_price_index_entries: 1,
	required_live_price_hospitals: 2,
	required_live_zero_price_hospitals: 1,
	required_live_unindexed_hospitals: 1,
	price_hospitals_outside_required_live: 0,
};
const fixtures = {
	"meta/summary.json": fixtureSummary,
	"meta/hospitals.json": ["400006", "050025", "070003", "390001"].map(
		(ccn) => ({ ccn, required: true, has_live_mrf: true }),
	),
	"prices/index.json": index,
	"prices/400006.json": historical,
	"prices/050025.json": ungraded,
	"prices/070003.json": empty,
	"aggregates/payer/aetna.json": {
		payer: { slug: "aetna", display: "Fixture payer", hospital_count: 1 },
		hospitals: [
			{
				ccn: "050025",
				name: "Fixture Hospital 050025",
				state: "CA",
				median_rate: 20,
			},
		],
	},
	"aggregates/cpt-detail/27130.json": {
		code: "27130",
		desc: "Fixture hip procedure",
		type: "CPT",
		stats: {
			hospital_count: 1,
			cash_p50: 20,
			cash_min: 20,
			cash_max: 20,
			gross_p50: 30,
		},
		hospitals: [
			{
				ccn: "050025",
				name: "Fixture Hospital 050025",
				state: "CA",
				gross: 30,
				cash: 20,
				min: null,
				max: null,
			},
		],
	},
};

// Runs in Chromium. Every changed consumer below must contain painted text;
// translucent layers are composited in paint order, with a conservative bound
// for the site's dark linear gradients. Unresolved paint fails explicitly.
function measure() {
	const rgb = (text) => {
		const match = text.match(/^rgba?\(([^)]+)\)$/);
		if (!match) throw Error(`Unmeasured color: ${text}`);
		const p = match[1]
			.split(/[, /]+/)
			.filter(Boolean)
			.map(Number);
		return [p[0], p[1], p[2], p[3] ?? 1];
	};
	const blend = (a, b) =>
		[0, 1, 2].map((i) => a[i] * a[3] + b[i] * (1 - a[3])).concat(1);
	const luminance = (c) =>
		c
			.slice(0, 3)
			.map((v) => v / 255)
			.map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
			.reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
	const inspect = (e) => {
		let bg = [255, 255, 255, 1];
		const chain = [];
		for (let x = e; x; x = x.parentElement) chain.unshift(x);
		for (const x of chain) {
			const s = getComputedStyle(x);
			if (s.filter !== "none" || Number(s.opacity) !== 1)
				throw Error("Unmeasured filter/opacity");
			bg = blend(rgb(s.backgroundColor), bg);
			if (s.backgroundImage !== "none") {
				if (!s.backgroundImage.startsWith("linear-gradient"))
					throw Error("Unmeasured image");
				const colors = [...s.backgroundImage.matchAll(/rgba?\([^)]+\)/g)].map(
					(m) => blend(rgb(m[0]), bg),
				);
				if (!colors.length) throw Error("Unmeasured gradient");
				bg = [0, 1, 2]
					.map((i) => Math.max(...colors.map((c) => c[i])))
					.concat(1);
			}
		}
		const s = e.matches("input[placeholder]")
			? getComputedStyle(e, "::placeholder")
			: getComputedStyle(e);
		const fg = blend(rgb(s.color), bg);
		const a = luminance(fg),
			b = luminance(bg);
		return {
			text: e.textContent.trim().slice(0, 90),
			tag: e.tagName,
			color: s.color,
			background: bg,
			font: s.fontSize,
			weight: s.fontWeight,
			ratio: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05),
			required:
				Number.parseFloat(s.fontSize) >= 24 ||
				(Number.parseFloat(s.fontSize) >= 18.667 && Number(s.fontWeight) >= 700)
					? 3
					: 4.5,
		};
	};
	const scope =
		document.querySelector("#coverage-gap") ?? document.querySelector("main");
	if (!scope) throw Error("Missing affected consumer");
	const elements = [...scope.querySelectorAll("*")].filter(
		(e) =>
			(e.matches("select,input[placeholder]") ||
				[...e.childNodes].some(
					(n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim(),
				)) &&
			e.getBoundingClientRect().width > 0 &&
			getComputedStyle(e).visibility !== "hidden" &&
			getComputedStyle(e).clip === "auto" &&
			!e.closest("script,style,option"),
	);
	const texts = elements.map(inspect);
	if (!texts.length) throw Error("Zero painted texts");
	const controls = [];
	for (const css of [
		"color:white;background:white",
		"color:#18181b;background:#09090b",
		"color:rgba(255,255,255,.1);background:rgba(255,255,255,.05)",
	]) {
		const bad = document.createElement("p");
		bad.textContent = "contrast negative control";
		bad.style.cssText = css;
		scope.append(bad);
		controls.push(inspect(bad).ratio < 4.5);
		bad.remove();
	}
	const badSelect = document.createElement("select");
	badSelect.innerHTML = "<option>Native control</option>";
	badSelect.style.cssText = "color:white;background:white";
	scope.append(badSelect);
	controls.push(inspect(badSelect).ratio < 4.5);
	badSelect.remove();
	return {
		texts,
		controls,
		h1: document.querySelectorAll("h1").length,
		overflow: document.documentElement.scrollWidth > innerWidth + 1,
		overflowing: [...document.querySelectorAll("body *")]
			.filter((e) => {
				const r = document.createRange();
				r.selectNodeContents(e);
				return (
					r.getBoundingClientRect().right > innerWidth + 1 &&
					!e.closest(".overflow-x-auto")
				);
			})
			.slice(-12)
			.map((e) => ({
				tag: e.tagName,
				class: e.className,
				text: e.textContent.slice(0, 70),
				right: e.getBoundingClientRect().right,
			})),
		favicon: !!document.querySelector('link[rel="icon"]'),
		failures: texts.filter((t) => t.ratio + 0.01 < t.required),
		badText: /undefined|NaN|Invalid Date|\[object Object\]/.test(
			texts.map((t) => t.text).join(" "),
		),
	};
}

const server = createTestHarness({ root, workers: [{ config }] });
let browser;
const results = [];
const output = resolve(root, ".ship");
await mkdir(output, { recursive: true });
try {
	const { url } = await server.listen();
	const env = await server.getWorker().getEnv();
	for (const [key, value] of Object.entries(fixtures))
		await env.HL_MRF_PARSED.put(key, JSON.stringify(value));
	browser = await chromium.launch();
	const page = await browser.newPage();
	// Disable measurement on external analytics; rendered CSS/fonts stay real.
	await page.route("https://analytics-collect.hospitalledger.com/**", (route) =>
		route.abort(),
	);
	for (const path of [
		"/",
		"/about-the-numbers",
		"/hospital/400006",
		"/hospital/050025",
		"/hospital/070003",
		"/hospital/999999",
		"/payer/aetna",
	]) {
		const response = await page.goto(new URL(path, url).href);
		assert.equal(response.status(), path.endsWith("999999") ? 404 : 200, path);
		await page.waitForFunction(
			() =>
				typeof window.tailwind !== "undefined" &&
				getComputedStyle(document.documentElement).backgroundColor ===
					"rgb(9, 9, 11)",
		);
		await page.evaluate(() => document.fonts.ready);
		if (path === "/hospital/400006") {
			assert.match(
				await page.locator("#historical-price-notice").textContent(),
				/not verified current/,
			);
			assert.equal(
				await page.locator('a[href="/procedure/LOCAL001"]').count(),
				0,
			);
		}
		if (path === "/hospital/050025")
			assert.match(
				await page.locator("body").textContent(),
				/Grade unavailable/,
			);
		for (const width of [320, 375, 414, 768, 1280])
			for (const theme of ["light", "dark"])
				for (const scale of [1, 1.5]) {
					await page.setViewportSize({ width, height: 900 });
					await page.emulateMedia({
						colorScheme: theme,
						reducedMotion: "reduce",
					});
					await page.evaluate((s) => {
						document.documentElement.style.fontSize = `${s * 100}%`;
					}, scale);
					const result = await page.evaluate(measure);
					if (path === "/hospital/050025")
						assert.ok(
							result.texts.some((t) => t.text === "Grade unavailable"),
							"grade omitted from measured scope",
						);
					if (path === "/payer/aetna")
						assert.equal(
							result.texts.filter((t) => t.tag === "SELECT").length,
							2,
							"native selects omitted",
						);
					results.push({ path, width, theme, scale, ...result });
					assert.equal(result.h1, 1, path);
					assert.ok(result.favicon, path);
					assert.ok(
						result.controls.every(Boolean),
						"contrast instrument failed negative controls",
					);
					assert.equal(
						result.overflow,
						false,
						`overflow ${path} ${width} ${scale}: ${JSON.stringify(result.overflowing)}`,
					);
					assert.equal(result.badText, false, path);
					assert.deepEqual(result.failures, [], `${path} ${width} ${scale}`);
				}
		if (path === "/hospital/400006")
			await page.screenshot({
				path: resolve(output, "historical-hospital.png"),
				fullPage: true,
			});
	}
	// Existing native controls must enter the ordinary measurement path.
	assert.equal(await page.locator("select").count(), 2);
	await page.locator("select").evaluateAll((elements) =>
		elements.forEach((e) => {
			e.style.setProperty("color", "white", "important");
			e.style.setProperty("background", "white", "important");
		}),
	);
	assert.equal(
		(await page.evaluate(measure)).failures.filter((t) => t.tag === "SELECT")
			.length,
		2,
	);
	// Current route is payer; inject into the measured owner rather than a selector
	// that may match nothing. This also proves the normal gate's failure path.
	await page.locator("main").evaluate((e) => {
		e.style.color = "white";
		e.style.backgroundColor = "white";
		e.querySelectorAll("*").forEach((x) => {
			x.style.setProperty("color", "white", "important");
			x.style.setProperty("background", "white", "important");
		});
	});
	assert.ok(
		(await page.evaluate(measure)).failures.length > 0,
		"injected regression was not detected",
	);
	console.log(
		JSON.stringify({
			event: "harness.pass",
			cells: results.length,
			negativeControls: results.length * 4,
			runtime: "built Worker / local R2 / Chromium",
		}),
	);
} finally {
	await writeFile(
		resolve(output, "readability.json"),
		JSON.stringify(results, null, 2),
	);
	await browser?.close();
	await server.close();
}
