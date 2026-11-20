import { describe, expect, it } from "vitest";
import {
	breadcrumbListNode,
	fitTitle,
	fitTitleWithSuffix,
	graphJsonLd,
	itemListNode,
	TITLE_MAX,
	webPageNode,
} from "./seo";

/**
 * SEO template helpers (audit items #12/#15/#17/#30, 2026-11-20): titles
 * must land inside the 50–60 char window and the JSON-LD builders must
 * emit the shapes the templates claim in their markup.
 */

describe("title fitting", () => {
	it("leaves a short title untouched", () => {
		expect(fitTitle("Aetna prices")).toBe("Aetna prices");
	});

	it("truncates an over-length title at a word boundary within the max", () => {
		const out = fitTitle(
			"Colonoscopy, Diagnostic, Including Collection Of Specimen(s) By Brushing Or Washing (45378)",
		);
		expect(out.length).toBeLessThanOrEqual(TITLE_MAX);
		expect(out.endsWith("…")).toBe(true);
	});

	it("keeps the suffix intact while truncating the lead", () => {
		const out = fitTitleWithSuffix(
			"Fine Needle Aspiration Biopsy, Including Ultrasound Guidance; First Lesion",
			" (10005)",
		);
		expect(out.length).toBeLessThanOrEqual(TITLE_MAX);
		expect(out.endsWith(" (10005)")).toBe(true);
	});
});

describe("JSON-LD builders", () => {
	it("builds a positioned BreadcrumbList with absolute URLs", () => {
		const node = breadcrumbListNode([
			{ name: "Home", path: "/" },
			{ name: "Hospitals", path: "/hospitals" },
			{ name: "California", path: "/hospitals/CA" },
		]);
		expect(node["@type"]).toBe("BreadcrumbList");
		expect(node.itemListElement.map((e) => e.position)).toEqual([1, 2, 3]);
		expect(node.itemListElement[2].item).toBe(
			"https://hospitalledger.com/hospitals/CA",
		);
	});

	it("carries dateModified on WebPage only when supplied", () => {
		const withDate = webPageNode({
			name: "About the numbers",
			url: "https://hospitalledger.com/about-the-numbers",
			dateModified: "2026-11-20T11:22:40Z",
		});
		expect(withDate.dateModified).toBe("2026-11-20T11:22:40Z");
		const bare = webPageNode({
			name: "Submit",
			url: "https://hospitalledger.com/submit",
		});
		expect("dateModified" in bare).toBe(false);
	});

	it("reports the full count when the ItemList is a capped sample", () => {
		const node = itemListNode({
			name: "Hospitals in California",
			numberOfItems: 360,
			items: [{ name: "A", url: "https://hospitalledger.com/hospital/1" }],
		});
		expect(node.numberOfItems).toBe(360);
		expect(node.itemListElement).toHaveLength(1);
	});

	it("wraps nodes in a single @graph with one @context", () => {
		const g = graphJsonLd(
			{ "@type": "WebPage", name: "x" },
			breadcrumbListNode([{ name: "Home", path: "/" }]),
		);
		expect(g["@context"]).toBe("https://schema.org");
		expect(g["@graph"]).toHaveLength(2);
	});
});
