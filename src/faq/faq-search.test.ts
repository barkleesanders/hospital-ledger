import { describe, expect, it, vi } from "vitest";
import type { Bindings } from "../lib/data";

/**
 * The ask box's live price search (src/faq/faq-search.ts), against fixtures that
 * mirror the production ground truth measured 2026-11-21:
 *   - Kaiser San Francisco publishes the cheapest SF brain-MRI cash price ($2,089.85);
 *   - UCSF Medical Center (050454) lists no brain MRI and publishes cash prices for
 *     0% of its file;
 *   - CPMC Van Ness lists the code with no price at all;
 *   - 05130F (a federal facility whose file was taken down) has a price file in the
 *     store and a row in the procedure aggregate, but is NOT in the live index —
 *     the search must never name it.
 */

const INDEX = {
	generated_at: "2026-11-21T11:13:52Z",
	hospitals: [
		{ ccn: "050076", n: 100, name: "KAISER FOUNDATION HOSPITAL - SAN FRANCISCO" },
		{ ccn: "050457", n: 100, name: "UCSF HEALTH ST. MARY'S HOSPITAL" },
		{ ccn: "050454", n: 100, name: "UCSF MEDICAL CENTER" },
		{ ccn: "050047", n: 100, name: "CALIFORNIA PACIFIC MEDICAL CENTER- VAN NESS CAMPUS" },
		{ ccn: "361321", n: 100, name: "BARNESVILLE HOSPITAL ASSOCIATION, INC" },
		{ ccn: "450209", n: 100, name: "PAMPA REGIONAL MEDICAL CENTER" },
	],
};

const DETAIL_70551 = {
	code: "70551",
	desc: "MRI SCAN OF BRAIN WITHOUT CONTRAST",
	type: "CPT",
	stats: { hospital_count: 1668, cash_p50: 1675.04, cash_min: 83.0, cash_max: 15272.84, gross_p50: 3371.43 },
	hospitals: [
		{ ccn: "05130F", name: "TAKEN DOWN FEDERAL FACILITY", state: "CA", city: "SAN FRANCISCO", gross: 6480, cash: 1.0, min: null, max: null },
		{ ccn: "361321", name: "BARNESVILLE HOSPITAL ASSOCIATION, INC", state: "OH", city: "BARNESVILLE", gross: 166, cash: 83.0, min: null, max: null },
		{ ccn: "050076", name: "KAISER FOUNDATION HOSPITAL - SAN FRANCISCO", state: "CA", city: "SAN FRANCISCO", gross: 5971, cash: 2089.85, min: null, max: null },
	],
};

const DETAIL_27447 = {
	code: "27447",
	desc: "TOTAL KNEE ARTHROPLASTY",
	type: "CPT",
	stats: { hospital_count: 1304, cash_p50: 9000, cash_min: 1700, cash_max: 90000, gross_p50: 40000 },
	hospitals: [
		{ ccn: "450209", name: "PAMPA REGIONAL MEDICAL CENTER", state: "TX", city: "PAMPA", gross: 30000, cash: 1700, min: null, max: null },
		{ ccn: "361321", name: "BARNESVILLE HOSPITAL ASSOCIATION, INC", state: "OH", city: "BARNESVILLE", gross: 20000, cash: 1200, min: null, max: null },
	],
};

function hospitalFile(name: string, cashPct: number, items: unknown[]) {
	return {
		ccn: "000000",
		hospital_name: name,
		items,
		compliance: { score: 50, grade: "F", coverage: { cash_pct: cashPct } },
	};
}

const FILES: Record<string, unknown> = {
	"prices/index.json": INDEX,
	"aggregates/cpt-detail/70551.json": DETAIL_70551,
	"aggregates/cpt-detail/27447.json": DETAIL_27447,
	"prices/050076.json": hospitalFile("KAISER FOUNDATION HOSPITAL - SAN FRANCISCO", 100, [
		{ code: "70551", cash: 2089.85, gross: 5971 },
	]),
	"prices/050457.json": hospitalFile("UCSF HEALTH ST. MARY'S HOSPITAL", 42, [
		{ code: "70551", cash: 3388.7, gross: 4841 },
	]),
	"prices/050454.json": hospitalFile("UCSF MEDICAL CENTER", 0, []),
	"prices/050047.json": hospitalFile("CALIFORNIA PACIFIC MEDICAL CENTER- VAN NESS CAMPUS", 66.4, [
		{ code: "70551", cash: null, gross: null },
	]),
	"prices/05130F.json": hospitalFile("TAKEN DOWN FEDERAL FACILITY", 94.5, [
		{ code: "70551", cash: 3628.8, gross: 6480 },
	]),
};

function envWith(files: Record<string, unknown>): Bindings {
	return {
		HL_MRF_PARSED: {
			get: async (key: string) => {
				const data = files[key];

				return data === undefined
					? null
					: {
							text: async () => JSON.stringify(data),
							json: async () => structuredClone(data),
							httpEtag: null,
							httpMetadata: {},
						};
			},
		} as unknown as R2Bucket,
		ASSETS: undefined as unknown as Fetcher,
	};
}

const REQ = new Request("https://hospitalledger.com/");

/** Fresh module per test: the live-index cache is module state. */
async function search() {
	vi.resetModules();

	return await import("./faq-search");
}

describe("askSearchDoc", () => {
	it("answers a city price question from the hospitals' own files, cheapest first", async () => {
		const { askSearchDoc } = await search();
		const doc = await askSearchDoc(
			envWith(FILES),
			"Where is the cheapest place to get an MRI of the brain without contrast in San Francisco?",
			REQ,
		);

		expect(doc).not.toBeNull();
		const text = doc?.text ?? "";

		expect(text).toContain("$2,089.85");
		expect(text.indexOf("KAISER FOUNDATION")).toBeLessThan(
			text.indexOf("ST. MARY'S"),
		);
		expect(text).toContain("4 hospitals are on the site for this city");
		expect(text).toContain("1 list the procedure but publish no price");
		expect(text).toContain("1 do not list it at all");
		expect(text).toContain("https://hospitalledger.com/procedure/70551");
	});

	it("never names a hospital that is not in the live index", async () => {
		const { askSearchDoc } = await search();
		const city = await askSearchDoc(
			envWith(FILES),
			"Where is the cheapest place to get an MRI of the brain without contrast in San Francisco?",
			REQ,
		);
		const nation = await askSearchDoc(
			envWith(FILES),
			"What is the cheapest hospital in the country for a brain MRI without contrast?",
			REQ,
		);

		for (const doc of [city, nation]) {
			expect(doc?.text).not.toContain("05130F");
			expect(doc?.text).not.toContain("TAKEN DOWN");
			expect(doc?.text).not.toContain("$3,628.80");
			expect(doc?.text).not.toContain("$1.00");
		}
		expect(nation?.text).toContain("BARNESVILLE");
		expect(nation?.text).toContain("$83.00");
	});

	it("answers a named-hospital question with what its file actually says", async () => {
		const { askSearchDoc } = await search();
		const doc = await askSearchDoc(
			envWith(FILES),
			"What does UCSF charge for an MRI brain scan?",
			REQ,
		);
		const text = doc?.text ?? "";

		expect(text).toContain("Hospital asked about: UCSF MEDICAL CENTER");
		expect(text).toContain("does not list MRI SCAN OF BRAIN WITHOUT CONTRAST");
		expect(text).toContain("0% of its listed items");
	});

	it("answers a state question from the procedure aggregate, filtered to the state", async () => {
		const { askSearchDoc } = await search();
		const doc = await askSearchDoc(
			envWith(FILES),
			"What is the cheapest knee replacement in Texas?",
			REQ,
		);
		const text = doc?.text ?? "";

		expect(text).toContain("PAMPA REGIONAL MEDICAL CENTER");
		expect(text).toContain("$1,700.00");
		// The cheaper Ohio row must not leak into a Texas answer.
		expect(text).not.toContain("BARNESVILLE");
	});

	it("returns null for a question with no recognisable procedure", async () => {
		const { askSearchDoc } = await search();
		const doc = await askSearchDoc(
			envWith(FILES),
			"How does this site count hospitals?",
			REQ,
		);

		expect(doc).toBeNull();
	});

	it("fails closed (null) when the live index cannot be read", async () => {
		const { askSearchDoc } = await search();
		const files = { ...FILES };

		delete files["prices/index.json"];
		const doc = await askSearchDoc(
			envWith(files),
			"Where is the cheapest place to get an MRI in San Francisco?",
			REQ,
		);

		expect(doc).toBeNull();
	});
});

describe("parseProcedureCodes", () => {
	it("maps lay terms to codes, specific before generic", async () => {
		const { parseProcedureCodes } = await search();

		expect(
			parseProcedureCodes("cheapest brain MRI in San Francisco"),
		).toEqual(["70551", "70553"]);
		expect(parseProcedureCodes("cheapest MRI in San Francisco")).toEqual([
			"70551",
			"72148",
			"73721",
		]);
		expect(parseProcedureCodes("how much is a colonoscopy")).toEqual([
			"45378",
			"45380",
		]);
	});

	it("takes a code named outright", async () => {
		const { parseProcedureCodes } = await search();

		expect(parseProcedureCodes("what does CPT 27447 cost")).toEqual(["27447"]);
	});
});
