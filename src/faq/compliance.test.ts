import { describe, expect, it } from "vitest";
import { GradeBadge } from "../components/ComplianceBar";
import app from "../index";
import { complianceElementStatus, complianceRating } from "../lib/compliance";
import type { HospitalData } from "../lib/data";
import { grossChargeDate } from "../lib/price-provenance";
import { buildSiteCorpus, hospitalDoc, pageText, payerDoc } from "./faq-corpus";

// Minimal excerpt of served prices/050025.json captured during recovery on
// 2026-10-25: 15,414 usable items, but no compliance metadata.
const ungraded: HospitalData = {
	ccn: "050025",
	hospital_name: "UC SAN DIEGO HEALTH HILLCREST - HILLCREST MED CTR",
	n_slim: 15414,
	items: [
		{
			code: "RX-149131",
			type: "CDM",
			gross: 14534100,
			cash: 7993755,
			min: 1453410,
			max: 14534100,
		},
	],
};

async function hospitalHtml(data: HospitalData) {
	const body = JSON.stringify(data);
	const get = async () => ({ text: async () => body });
	const response = await app.request(
		`/hospital/${data.ccn}`,
		{},
		{
			// SAFETY: the hospital loader only consumes R2 get/text in this fixture.
			HL_MRF_PARSED: { get } as unknown as R2Bucket,
		},
	);
	expect(response.status).toBe(200);
	return response.text();
}

describe("missing compliance metadata is unmeasured", () => {
	it("preserves historical price dates in the page and FAQ without treating fetch time as effective time", async () => {
		const historical: HospitalData = {
			...ungraded,
			items: [
				{
					...ungraded.items[0],
					gross_charge_source: {
						effective_date: "2013-01-01",
						header: "STD CHARGE 01/01/13",
						column_1based: 32,
					},
				},
			],
		};
		const text = pageText(await hospitalHtml(historical));
		expect(text).toContain("Historical charges");
		expect(text).toContain("not verified current prices");
		expect(text.match(/Source date: 2013-01-01/g)).toHaveLength(2);
		const doc = hospitalDoc(historical.ccn, historical).text;
		expect(doc).toContain("source date 2013-01-01");
		expect(doc).toContain("not verified current prices");
		expect(pageText(await hospitalHtml(ungraded))).not.toContain(
			"Historical charges",
		);
	});

	it.each([
		undefined,
		{},
		{ effective_date: "2025-02-30", header: "STD CHARGE", column_1based: 1 },
		{ effective_date: "2020-01-01", header: "", column_1based: 1 },
		{ effective_date: "2020-01-01", header: "STD CHARGE", column_1based: 0 },
	])("does not invent a date from invalid source metadata: %j", (value) => {
		expect(grossChargeDate(value)).toBeNull();
	});

	it("keeps recovered prices visible without inventing a failing grade or failed elements", async () => {
		const html = await hospitalHtml(ungraded);
		const text = pageText(html);
		expect(text).toContain("Grade unavailable");
		expect(text).toContain(
			"Missing assessment metadata does not establish noncompliance",
		);
		expect(text).toContain("Gross / standard charges: unknown");
		expect(text).toContain("RX-149131");
		expect(text).toContain("$14,534,100");
		expect(html).toContain("compliance grade unavailable");
		expect(html).not.toContain("compliance grade F");
		expect(text).not.toContain("F · 0");
		expect(text).not.toContain("published little");
		expect(text).not.toContain("NOT published");
		expect(html).not.toContain('href="/procedure/RX-149131"');

		const doc = hospitalDoc(ungraded.ccn, ungraded).text;
		expect(doc).toContain("Compliance grade and score: unavailable");
		expect(doc).toContain("Gross / standard charges: unknown");
		expect(doc).toContain("Procedures listed: 1");
		expect(doc).not.toContain("Compliance grade: F");
		expect(doc).not.toContain("NOT published");
	});

	it("keeps clinical-code comparison links while local CDM rows stay on the hospital page", async () => {
		const html = await hospitalHtml({
			...ungraded,
			items: [{ ...ungraded.items[0], code: "99213", type: "CPT" }],
		});
		expect(html).toContain('href="/procedure/99213"');
		expect(hospitalDoc(ungraded.ccn, ungraded).text).toContain(
			"Hospital-local CDM identifiers are not interchangeable",
		);
	});

	it("preserves an explicit F/0 and distinguishes true, false and absent element flags", async () => {
		const measured: HospitalData = {
			...ungraded,
			compliance: {
				grade: "F",
				score: 0,
				elements: { mrf: true, gross: false },
			},
		};
		const text = pageText(await hospitalHtml(measured));
		expect(text).toContain("F · 0");
		expect(text).toContain("published little");
		expect(text).toContain(
			"Machine-readable file published: assessment threshold met",
		);
		expect(text).toContain(
			"Gross / standard charges: below assessment threshold",
		);
		expect(text).not.toContain("NOT published");
		expect(text).toContain("Discounted cash price: unknown");
		const doc = hospitalDoc(measured.ccn, measured).text;
		expect(doc).toContain("Compliance grade: F, score 0 out of 100");
		expect(doc).toContain(
			"Gross / standard charges: below assessment threshold",
		);
		expect(doc).toContain("Discounted cash price: unknown");
		expect(String(GradeBadge({ grade: "F", score: 0 }))).toContain("F · 0");
	});

	it("grounds the global FAQ in the difference between missing and failing assessments", () => {
		expect(buildSiteCorpus()[3].text).toContain(
			"the page says Grade unavailable",
		);
		expect(buildSiteCorpus()[3].text).toContain(
			"An element without an explicit assessment is unknown",
		);
	});

	it("uses the same complete rating contract in payer badges and FAQ grounding", () => {
		const data = {
			payer: { slug: "aetna", display: "Aetna" },
			hospitals: [
				{
					ccn: "050025",
					name: "Ungraded fixture",
					state: "CA",
					compliance_grade: "F",
					compliance_score: null,
				},
			],
		};
		expect(payerDoc("aetna", data).text).toContain(
			"compliance grade not graded",
		);
		expect(
			payerDoc("aetna", {
				payer: data.payer,
				hospitals: [{ ...data.hospitals[0], compliance_score: 0 }],
			}).text,
		).toContain("compliance grade F");
	});
});

describe("compliance rating validation", () => {
	it.each([
		undefined,
		null,
		{},
		{ grade: "F" },
		{ score: 0 },
		{ grade: "F", score: null },
		{ grade: "E", score: 50 },
		{ grade: "A", score: -1 },
		{ grade: "A", score: 101 },
		{ grade: "A", score: NaN },
		{ grade: "A", score: Infinity },
		{ grade: "A", score: "100" },
	])("rejects an incomplete or invalid assessment: %j", (value) => {
		expect(complianceRating(value)).toBeNull();
	});

	it("normalizes valid lowercase letters and preserves both score boundaries", () => {
		expect(complianceRating({ grade: "b", score: 83 })).toEqual({
			grade: "B",
			score: 83,
		});
		expect(complianceRating({ grade: "F", score: 0 })).toEqual({
			grade: "F",
			score: 0,
		});
		expect(complianceRating({ grade: "A", score: 100 })).toEqual({
			grade: "A",
			score: 100,
		});
	});

	it.each([{}, { grade: "F" }, { score: 0 }, { grade: "E", score: 0 }])(
		"uses an unavailable badge for missing or invalid metadata: %j",
		(value) => {
			const badge = String(GradeBadge(value));
			expect(badge).toContain("Not graded");
			expect(badge).not.toContain("F · 0");
		},
	);

	it("does not treat malformed element flags as measured true or false", () => {
		expect(
			[undefined, null, 0, 1, "false", "true"].map(complianceElementStatus),
		).toEqual([
			"unknown",
			"unknown",
			"unknown",
			"unknown",
			"unknown",
			"unknown",
		]);
	});
});
