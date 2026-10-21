import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { aboutPage } from "../routes/about-the-numbers";
import { homePage } from "../routes/home";
import { isSiteLink } from "./faq-links";
import { FAQ_ASK_ACTION, FAQ_ASK_HOST_ID, FaqAskRow } from "./faq-section";

const ROOT = join(__dirname, "..", "..");

describe("FaqAskRow", () => {
	it("renders the host with its data-* copy and a real no-JS form posting to the route", () => {
		const html = String(FaqAskRow({ variant: "hero" }));

		expect(html).toContain(`id="${FAQ_ASK_HOST_ID}"`);
		expect(html).toContain('class="faq-ask faq-ask--hero"');
		expect(html).toContain(`data-action="${FAQ_ASK_ACTION}"`);
		expect(html).toContain(
			`data-placeholder="Ask anything about a hospital&#39;s prices or this site"`,
		);
		expect(html).toContain(`method="post" action="${FAQ_ASK_ACTION}"`);
		expect(html).toContain('name="question"');
		expect(html).toContain('maxlength="600"');
		expect(html).not.toContain("data-context");
		expect(html).not.toContain('name="context"');
		// One placeholder, no helper text.
		expect(html).not.toMatch(/<p[\s>]/);
	});

	it("carries the record as a hidden context field AND a data attribute on a detail page", () => {
		const html = String(
			FaqAskRow({
				variant: "hospital",
				context: { kind: "hospital", id: "010001" },
			}),
		);
		const ctx =
			"{&quot;kind&quot;:&quot;hospital&quot;,&quot;id&quot;:&quot;010001&quot;}";

		expect(html).toContain('class="faq-ask faq-ask--hospital"');
		expect(html).toContain(`data-context="${ctx}"`);
		expect(html).toContain(
			`<input type="hidden" name="context" value="${ctx}"`,
		);
	});
});

describe("row placement", () => {
	it("home page: the row is inside the hero header, after the lead paragraph and before the CTA buttons", () => {
		const html = String(homePage("https://hospitalledger.com/"));
		const lead = html.indexOf(
			"CMS-required hospitals have a live machine-readable file.",
		);
		const row = html.indexOf(`id="${FAQ_ASK_HOST_ID}"`);
		const howWeCount = html.indexOf("How we count:");
		const cta = html.indexOf("Find a price →");
		const main = html.indexOf("<main");

		// Assert every index exists BEFORE comparing order (a -1 would "win" any order check).
		for (const i of [lead, row, howWeCount, cta, main])
			expect(i).toBeGreaterThan(-1);
		expect(row).toBeGreaterThan(lead);
		expect(row).toBeLessThan(howWeCount);
		expect(row).toBeLessThan(cta);
		expect(row).toBeLessThan(main);
		expect(html.match(new RegExp(`id="${FAQ_ASK_HOST_ID}"`, "g"))?.length).toBe(
			1,
		);
		expect(html).toContain('<link rel="stylesheet" href="/faq.css"');
		expect(html).toContain('<script type="module" src="/faq-island.js"');
	});

	it("the methodology page does not carry the row or the island", () => {
		const html = String(
			aboutPage("https://hospitalledger.com/about-the-numbers"),
		);
		expect(html).not.toContain(`id="${FAQ_ASK_HOST_ID}"`);
		expect(html).not.toContain("/faq-island.js");
	});
});

describe("committed island bundle", () => {
	it("public/faq-island.js is byte-identical to a fresh esbuild of src/faq/faq-island.tsx", () => {
		const committed = readFileSync(
			join(ROOT, "public", "faq-island.js"),
			"utf8",
		);
		const out = join(
			mkdtempSync(join(tmpdir(), "faq-island-")),
			"faq-island.js",
		);
		execFileSync(
			join(ROOT, "node_modules", ".bin", "esbuild"),
			[
				join(ROOT, "src", "faq", "faq-island.tsx"),
				"--bundle",
				"--format=esm",
				"--minify",
				"--target=es2022",
				"--jsx=automatic",
				"--jsx-import-source=hono/jsx/dom",
				`--outfile=${out}`,
				"--log-level=error",
			],
			{ cwd: ROOT },
		);
		const fresh = readFileSync(out, "utf8");

		expect(committed.length).toBeGreaterThan(10_000);
		expect(committed).toBe(fresh);
	});

	it("the island source never writes HTML strings or evals, and the bundle mounts on #faq-ask", () => {
		// hono/jsx/dom itself carries an innerHTML path for dangerouslySetInnerHTML,
		// so the string check is on OUR source; the bundle check is for eval/Function,
		// which script-src 'self' (no 'unsafe-eval') would refuse. Live proof is the
		// fcdp console check (0 CSP violations) in the PR.
		const src = readFileSync(
			join(ROOT, "src", "faq", "faq-island.tsx"),
			"utf8",
		);
		expect(src).not.toMatch(/\.innerHTML\s*=|dangerouslySetInnerHTML|\beval\(/);
		const js = readFileSync(join(ROOT, "public", "faq-island.js"), "utf8");
		expect(js).not.toMatch(/\beval\(/);
		expect(js).not.toMatch(/new Function\(/);
		expect(js).toContain('getElementById("faq-ask")');
	});
});

describe("isSiteLink", () => {
	it("allows only https URLs on hospitalledger.com and its subdomains", () => {
		expect(isSiteLink("https://hospitalledger.com/about-the-numbers")).toBe(
			true,
		);
		expect(isSiteLink("https://www.hospitalledger.com/procedure/27130")).toBe(
			true,
		);
		expect(isSiteLink("http://hospitalledger.com/")).toBe(false);
		expect(isSiteLink("https://hospitalledger.com.evil.example/")).toBe(false);
		expect(isSiteLink("https://evilhospitalledger.com/")).toBe(false);
		expect(isSiteLink("https://example-hospital.org/mrf.csv")).toBe(false);
		expect(isSiteLink("https://www.cms.gov/")).toBe(false);
		expect(isSiteLink("javascript:alert(1)")).toBe(false);
		expect(isSiteLink("not a url")).toBe(false);
	});
});
