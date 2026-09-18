import { Hono } from "hono";
import summaryJson from "../public/data/summary.json";
import {
	loadFaqCorpus,
	recordContextDoc,
	SITE_NAME,
	SITE_ORIGIN,
} from "./faq/faq-corpus";
import {
	type FaqAi,
	type FaqRateLimiter,
	mountInfiniteFaq,
} from "./faq/faq-route";
import { CPT_NAMES } from "./lib/cpt-names";
import { readR2Json } from "./lib/r2";
import { aboutNumbersPageHandler } from "./routes/about-the-numbers";
import { complianceRankingHandler } from "./routes/api/compliance-ranking";
import { cptIndexHandler } from "./routes/api/cpt-index";
import { payerHandler } from "./routes/api/payer";
import { payersIndexHandler } from "./routes/api/payers-index";
import { pricesHandler } from "./routes/api/prices";
import { pricesIndexHandler } from "./routes/api/prices-index";
import { procedureHandler } from "./routes/api/procedure";
import { homePageHandler } from "./routes/home";
import { hospitalPageHandler } from "./routes/hospital";
import { payerPageHandler } from "./routes/payer";
import { procedurePageHandler } from "./routes/procedure";

export type Env = {
	Bindings: {
		HL_MRF_PARSED: R2Bucket;
		HL_MRF_RAW: R2Bucket;
		ASSETS: Fetcher;
		SITE_NAME: string;
		/** Workers AI (wrangler.jsonc `ai`) — the "Ask anything" row's model. */
		AI: FaqAi;
		/** Cloudflare Rate Limiting (wrangler.jsonc `ratelimits`) for /api/faq/ask. */
		FAQ_RATE_LIMITER?: FaqRateLimiter;
	};
};

const app = new Hono<Env>();

// Security headers — ports site/_headers (defense-in-depth at the edge).
app.use("*", async (c, next) => {
	await next();
	const h = c.res.headers;
	if (!h.has("X-Frame-Options")) h.set("X-Frame-Options", "DENY");
	if (!h.has("X-Content-Type-Options"))
		h.set("X-Content-Type-Options", "nosniff");
	if (!h.has("Referrer-Policy"))
		h.set("Referrer-Policy", "strict-origin-when-cross-origin");
	if (!h.has("Permissions-Policy"))
		h.set("Permissions-Policy", "interest-cohort=()");
	// COOP/CORP — the two /ship Phase 4.05 baseline headers this site was missing.
	//
	// COOP severs the window.opener relationship, so a page this site opens (or
	// that opens this site) cannot reach back into its window. CORP is the more
	// consequential one here: this site's whole purpose is to serve public JSON
	// (/data/summary.json, /data/hospitals.json, and the per-hospital price
	// files), and `same-site` would break exactly that. `cross-origin` is the
	// correct value for a CC0 public dataset — it keeps the data embeddable by
	// anyone, which is the point, while still opting the responses out of being
	// silently pulled into another origin's process by Spectre-class attacks.
	//
	// Deliberately NOT setting COEP: it would require every cross-origin
	// subresource to opt in via CORP/CORS, and this page loads Tailwind's Play
	// CDN and Google Fonts, which do not. COEP's payoff is cross-origin isolation
	// (SharedArrayBuffer, precise timers) that a static data site has no use for.
	if (!h.has("Cross-Origin-Opener-Policy"))
		h.set("Cross-Origin-Opener-Policy", "same-origin");
	if (!h.has("Cross-Origin-Resource-Policy"))
		h.set("Cross-Origin-Resource-Policy", "cross-origin");
	if (!h.has("Strict-Transport-Security")) {
		h.set(
			"Strict-Transport-Security",
			"max-age=63072000; includeSubDomains; preload",
		);
	}
	// CSP (added 2026-07-06, /ship Phase 4.05b; FIXED 2026-08-02).
	//
	// The original directive allow-listed Google Fonts as "the only external loads
	// in rendered HTML" — that enumeration came from the <link> tags and MISSED the
	// <script src="https://cdn.tailwindcss.com"> in Layout.tsx (added 79e66b3,
	// 2026-05-13). Result: from 2026-07-06 the browser blocked Tailwind entirely
	// ("Loading the script ... violates the following Content Security Policy
	// directive"), so all ~484 utility classes in the markup rendered inert and the
	// site was visually unstyled in production for ~4 weeks. Only the ~1.9 KB inline
	// <style> below survived, which is why it looked broken but not blank.
	//
	// When changing this header, enumerate external hosts from EVERY tag that loads
	// a subresource (<script>, <link>, <img>, fetch/XHR) — not just <link> — and
	// then browser-verify: load the page and confirm zero "Refused to.../violates
	// the following Content Security Policy" console entries. A CSP that is merely
	// *present* is not a CSP that is *correct*.
	//
	// NOTE: cdn.tailwindcss.com is Tailwind's Play CDN, which Tailwind documents as
	// development-only, not for production (https://tailwindcss.com/docs/installation/play-cdn).
	// It is allow-listed here to restore production immediately; the correct
	// long-term fix is a Tailwind CLI build served from 'self' so this entry — and
	// the render-blocking third-party script — can be removed. See TODO below.
	if (!h.has("Content-Security-Policy")) {
		h.set(
			"Content-Security-Policy",
			"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://analytics-collect.hospitalledger.com https://static.cloudflareinsights.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' https://analytics-collect.hospitalledger.com https://cloudflareinsights.com; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
		);
	}
});

// URL normalization: 301-redirect trailing-slash and uppercase entity paths
// to a single canonical form. Skips /api/* (case-/slash-strict by contract).
app.use("*", async (c, next) => {
	const url = new URL(c.req.url);
	if (url.pathname.startsWith("/api/")) return next();
	// Trailing slash → no slash (except root)
	if (url.pathname.length > 1 && url.pathname.endsWith("/")) {
		return c.redirect(url.pathname.slice(0, -1) + url.search, 301);
	}
	// Lowercase only the entity prefix paths (the params themselves are already canonical)
	const lowered = url.pathname.toLowerCase();
	if (
		lowered !== url.pathname &&
		/^\/(procedure|payer|hospital)\//i.test(url.pathname)
	) {
		return c.redirect(lowered + url.search, 301);
	}
	return next();
});

// "Ask anything" — POST /api/faq/ask streams a Workers AI answer grounded in the
// site's own pages (src/faq/faq-corpus.ts) and, on a detail page, in the record on
// screen. Registered with the API routes, above every SSR handler. The rules below
// are the site's anti-fabrication contract: this is a price database, so a number the
// model "completes" is worse than no answer.
mountInfiniteFaq(app, {
	siteName: SITE_NAME,
	fallbackUrl: `${SITE_ORIGIN}/about-the-numbers`,
	corpus: loadFaqCorpus,
	contextDoc: recordContextDoc,
	rateLimiter: (env) => env.FAQ_RATE_LIMITER,
	extraRules: [
		"Prices, counts, grades, dates and dollar amounts must be copied whole and exactly as the reference material states them, or reported as not listed — never rounded, completed or estimated.",
		"Prices are what each hospital published in its own machine-readable file: they are not a quote, and what a patient pays depends on their insurance, deductible and the services provided; say so when a visitor asks what something will cost them.",
		"The site has no phone numbers, email addresses, street addresses, charity-care or financial-assistance data: when asked for one, say the site does not list it and point the visitor to the hospital directly.",
		"Never speak for CMS, HHS or a hospital, and never say whether a hospital is breaking the law; describe only what it published and the compliance grade the site gave it.",
	],
});

// API routes (preserve byte-similar shapes with the legacy Pages Functions).
app.get("/api/prices-index", pricesIndexHandler);
app.get("/api/cpt-index", cptIndexHandler);
app.get("/api/payers-index", payersIndexHandler);
app.get("/api/compliance-ranking", complianceRankingHandler);
app.get("/api/prices/:ccn", pricesHandler);
app.get("/api/payer/:slug", payerHandler);
app.get("/api/procedure/:code", procedureHandler);

// Legacy /api/{hospitals,summary}.json shorthands → /data fallback.
app.get("/api/hospitals.json", (c) => c.redirect("/data/hospitals.json", 301));
app.get("/api/summary.json", (c) => c.redirect("/data/summary.json", 301));

// XML sitemap — top procedures, top payers, top hospitals, plus the home page.
app.get("/sitemap.xml", async (c) => {
	const SITE = "https://hospitalledger.com";
	// lastmod is the DATA's generation date, not `new Date()`.
	//
	// It used to be `new Date().toISOString().slice(0,10)`, which told crawlers
	// that every URL in the sitemap had changed today — every day, forever. That
	// is not a stale-date bug, it is the opposite one, and it costs more: Google
	// documents that it "uses the <lastmod> value if it's consistently and
	// verifiably (for example by comparing to the last modification of the page)
	// accurate" (developers.google.com/search/docs/crawling-indexing/sitemaps/
	// build-sitemap, checked 2026-08-24). An always-today value fails that
	// comparison on every page, so the signal is discarded wholesale — including
	// for the pages that genuinely did change.
	//
	// This site's pages are renderings of one dataset, so the honest answer to
	// "when did this page last significantly change?" is "when the data was
	// regenerated" — the same timestamp the home page's Dataset node already
	// publishes as `dateModified`. It is stable between refreshes (so Google can
	// verify it), it advances on its own the next time `npm run refresh` runs,
	// and it cannot drift from the page, because it IS the page's source.
	const lastmod = summaryJson.generated_at.slice(0, 10);

	// Hospitals: try R2 prices/index.json first; fall back to static
	// /data/hospitals.json filtered to has_live_mrf=true.
	let hospitalCcns: string[] = [];
	const pricesIndex = await readR2Json<{ hospitals?: { ccn?: string }[] }>(
		c.env.HL_MRF_PARSED,
		"prices/index.json",
	);
	if (pricesIndex?.hospitals?.length) {
		hospitalCcns = pricesIndex.hospitals
			.map((h) => String(h.ccn ?? ""))
			.filter(Boolean)
			.slice(0, 200);
	} else if (c.env.ASSETS && typeof c.env.ASSETS.fetch === "function") {
		const url = new URL(c.req.url);
		url.pathname = "/data/hospitals.json";
		url.search = "";
		const resp = await c.env.ASSETS.fetch(url.toString());
		if (resp.ok) {
			const arr = (await resp.json()) as Array<{
				ccn?: string;
				has_live_mrf?: boolean;
			}>;
			hospitalCcns = arr
				.filter((h) => h.has_live_mrf && h.ccn)
				.map((h) => String(h.ccn))
				.slice(0, 200);
		}
	}

	// Payers: R2 only (no static fallback); skip silently if missing.
	let payerSlugs: string[] = [];
	const payersIndex = await readR2Json<{
		featured?: { slug?: string }[];
		payers?: { slug?: string }[];
	}>(c.env.HL_MRF_PARSED, "aggregates/payers-index.json");
	const payerList = payersIndex?.featured ?? payersIndex?.payers ?? [];
	if (payerList.length) {
		payerSlugs = payerList
			.map((p) => String(p.slug ?? ""))
			.filter(Boolean)
			.slice(0, 100);
	}

	// CPT codes: full curated map (103 entries).
	const cptCodes = Object.keys(CPT_NAMES);

	const urls: string[] = [
		`<url><loc>${SITE}/</loc><lastmod>${lastmod}</lastmod><priority>1.0</priority></url>`,
	];
	for (const code of cptCodes) {
		urls.push(
			`<url><loc>${SITE}/procedure/${code}</loc><lastmod>${lastmod}</lastmod><priority>0.9</priority></url>`,
		);
	}
	for (const slug of payerSlugs) {
		urls.push(
			`<url><loc>${SITE}/payer/${slug}</loc><lastmod>${lastmod}</lastmod><priority>0.8</priority></url>`,
		);
	}
	for (const ccn of hospitalCcns) {
		urls.push(
			`<url><loc>${SITE}/hospital/${ccn}</loc><lastmod>${lastmod}</lastmod><priority>0.7</priority></url>`,
		);
	}

	const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.join("\n")}
</urlset>`;

	return new Response(xml, {
		headers: {
			"content-type": "application/xml; charset=utf-8",
			"cache-control": "public, max-age=3600",
		},
	});
});

// SSR pages — preserve original URLs (/procedure/:code, /payer/:slug, /hospital/:ccn).
app.get("/procedure/:code", procedurePageHandler);
app.get("/payer/:slug", payerPageHandler);
app.get("/hospital/:ccn", hospitalPageHandler);

// SSR home page.
app.get("/", homePageHandler);

// SSR methodology page explaining the gap between CMS-required, live MRF,
// and standardized-price counts. Linked from the home-page hero + Section 03.
app.get("/about-the-numbers", aboutNumbersPageHandler);
app.get("/security.txt", (c) => c.redirect("/.well-known/security.txt", 301));

export default app;
