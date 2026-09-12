/**
 * Shared <html><head><body> shell. Wraps every SSR page with the same
 * fonts, Tailwind CDN, security/perf meta, and footer.
 *
 * Tailwind is loaded via CDN (matches the legacy site/index.html). PostCSS
 * pipeline is intentionally out of scope this round.
 */

import { raw } from "hono/html";
import type { FC, PropsWithChildren } from "hono/jsx";
import summaryJson from "../../public/data/summary.json";

/**
 * The same generated summary the home page renders its headline stats from.
 * The Dataset node below derives every figure from this object rather than
 * repeating a literal, so a data refresh cannot leave the schema asserting a
 * number the page no longer shows.
 */
const DATA = summaryJson;

const n = (v: number) => v.toLocaleString("en-US");

/**
 * Dataset description. Google requires 50–5000 characters and requires the
 * markup to match the visible page, which is why this is assembled from DATA
 * rather than written out.
 */
const DATASET_DESCRIPTION =
	`Compliance status for ${n(DATA.total_facilities)} U.S. hospitals under the federal hospital ` +
	`price transparency rule (45 CFR Part 180), of which ${n(DATA.cms_required_total)} are required ` +
	`to publish a machine-readable file; ${n(DATA.compliant)} have a verified live file ` +
	`(${DATA.compliance_pct}%). Each record pairs CMS Hospital General Information with live ` +
	`verification of the hospital's machine-readable file URL, plus CMS enforcement history ` +
	`(${n(DATA.enforcement_actions_total)} actions). Includes ${n(DATA.standardized_price_rows)} ` +
	`standardized price rows across ${n(DATA.standardized_price_hospitals)} hospitals and ` +
	`${n(DATA.cpt_indexed_rows)} CPT-indexed rows. Public domain, no signup, no tracking.`;

export type LayoutProps = PropsWithChildren<{
	title: string;
	description: string;
	// Full canonical URL for this page (e.g. "https://hospitalledger.com/procedure/27130").
	// Optional for backwards compat — pages that omit it skip the canonical/og:url tags.
	url?: string;
	// Override the default OG image (defaults to https://hospitalledger.com/og.png).
	ogImage?: string;
	ogTitle?: string;
	bodyClass?: string;
	// Optional inline `<script>` body to ship below the rendered content.
	// Keep these tiny — heavy logic should live in /public/*.js.
	inlineScript?: string;
	// Optional <script src="..."> to defer below the body. Pass an array
	// when multiple ordered scripts are needed (e.g. cpt-names.js then
	// home-client.js, where the latter reads window.CPT_NAMES).
	scriptSrc?: string | string[];
}>;

const DEFAULT_OG_IMAGE = "https://hospitalledger.com/og.png";

export const Layout: FC<LayoutProps> = ({
	title,
	description,
	url,
	ogImage,
	ogTitle,
	bodyClass,
	inlineScript,
	scriptSrc,
	children,
}) => {
	const resolvedOgImage = ogImage ?? DEFAULT_OG_IMAGE;
	const resolvedOgTitle = ogTitle ?? title;
	return (
		<html lang="en" class="bg-zinc-950 text-zinc-100">
			<head>
				<meta charset="utf-8" />
				<meta name="viewport" content="width=device-width,initial-scale=1" />
				<title>{title}</title>
				<meta name="description" content={description} />
				{url ? <link rel="canonical" href={url} /> : null}

				{/*
				  Entity anchor — HOME PAGE ONLY, and deliberately NOT an Organization.

				  `sameAs` ties this site to another page about the same thing, so a
				  crawler can reconcile them. Google lists it under Organization's
				  RECOMMENDED properties (docs re-read 2026-08-24); it is a hint for
				  entity understanding, not a ranking factor.

				  ⛔ WHY THERE IS NO Organization NODE HERE.
				  The three sibling sites (aivaclaims.com, improvebayarea.com,
				  esbe.tech) each declare an organization. This one does not, because
				  this site does not claim one: it says "wasn't cost-effective for one
				  person", it is AGPLv3, and it names no company anywhere.

				  Attaching ESBE Incorporated (the 501(c)(3) behind the other sites)
				  would be inventing an affiliation — a false public statement about a
				  real nonprofit's programs, made in machine-readable form that
				  aggregators would then repeat. An empty slot is honest; a plausible
				  claim is not. If this project ever IS put under an organization,
				  that is a decision for its owner to state, not for tooling to infer.

				  So the anchor is the one true external identity the site ALREADY
				  publishes in its own body text: the public source repository. The
				  node is a WebSite, which is what this is.

				  The /ship gate requires an Organization by default; this page is
				  cleared explicitly, so "no org" stays a stated decision rather than a
				  silent omission:
				    entity-sameas-check.sh https://hospitalledger.com \\
				      --allow-types WebSite \\
				      --forbid 'propublica|candid|every\\.org|aivaclaims'
				*/}
				{url === "https://hospitalledger.com/" ? (
					<script type="application/ld+json">
						{raw(
							// raw() instead of dangerouslySetInnerHTML so biome's
							// noDangerouslySetInnerHtml is satisfied by construction, not by a
							// suppression comment. The `<` escape is standard JSON-LD hardening:
							// nothing here is dynamic today, but a future field inherits the
							// safety instead of becoming an injection point.
							JSON.stringify({
								"@context": "https://schema.org",
								"@graph": [
									{
										"@type": "WebSite",
										"@id": "https://hospitalledger.com/#website",
										name: "Hospital Ledger",
										url: "https://hospitalledger.com/",
										description,
										// CC0, NOT AGPL. An earlier revision of this node carried
										// the AGPL URL — that was wrong and is corrected here: a
										// WebSite's `license` describes the CONTENT, and the page
										// says so in its own footer, "Data CC0 1.0 Universal ·
										// Code AGPLv3". The code license belongs to the repo, not
										// to the site's data.
										license:
											"http://creativecommons.org/publicdomain/zero/1.0/",
										mainEntity: {
											"@id": "https://hospitalledger.com/#dataset",
										},
										// NO sameAs — and that is the honest state, not an oversight.
										// The obvious anchor is the source repo the page advertises:
										//   "Built in the open at github.com/barkleesanders/hospital-ledger"
										// It was shipped here, and the /ship entity gate immediately
										// flagged it DEAD (404). `gh api` confirms why: the repository
										// is PRIVATE (verified 2026-08-24).
										//
										// A sameAs pointing at a 404 is worse than none — it tells a
										// crawler this site's identity lives at a URL that does not
										// exist. So it is removed rather than left to rot.
										//
										// Two ways to close this, both the owner's call, not tooling's:
										//  * make the repo public -> restore the sameAs AND make the
										//    page's "built in the open" claim true at the same time
										//  * keep it private -> the body copy should stop saying the
										//    project is built in the open at a link nobody can open
										// Until one of those happens the gate reports BAD for this
										// site, which is correct: there is no verifiable public
										// identity to anchor to.
									},
									{
										// Dataset — the node that earns this site a discovery
										// surface it is currently absent from.
										//
										// This site IS a dataset: per-hospital compliance records
										// derived from CMS sources. Google indexes Dataset markup
										// into Google Dataset Search, a SEPARATE index from web
										// search with far less competition than "hospital prices".
										// Unlike most schema, this is not a rich-result garnish on
										// a page that would rank anyway — it is the entry ticket
										// to a surface that otherwise cannot see us.
										//
										// EVERY NUMBER BELOW IS DERIVED, NEVER TYPED. They read
										// from the same public/data/summary.json the home page
										// renders, so a stat cannot drift from the visible text —
										// which is both Google's requirement ("structured data
										// matches the visible text") and this repo's own
										// Copy-Truth Gate (scripts/predeploy_audit.py). Hardcoding
										// 5,426 here would pass tsc, pass lint, render fine, and
										// silently become a lie on the next data refresh.
										"@type": "Dataset",
										"@id": "https://hospitalledger.com/#dataset",
										name: "U.S. Hospital Price Transparency Compliance Ledger",
										description: DATASET_DESCRIPTION,
										url: "https://hospitalledger.com/",
										license:
											"http://creativecommons.org/publicdomain/zero/1.0/",
										isAccessibleForFree: true,
										dateModified: DATA.generated_at,
										spatialCoverage: {
											"@type": "Place",
											name: "United States",
										},
										keywords: [
											"hospital price transparency",
											"machine-readable file",
											"CMS compliance",
											"healthcare prices",
											"chargemaster",
											"45 CFR Part 180",
										],
										// The upstream sources the page already cites and links.
										// Naming them lets an aggregator place this derived work
										// relative to its origins.
										isBasedOn: [
											"https://data.cms.gov/provider-data/dataset/xubh-q36u",
											"https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/hospital-price-transparency-enforcement-activities-and-outcomes",
										],
										// Real, live, public JSON — both verified 200
										// application/json before being written here. A
										// distribution URL that 404s is the same failure as a dead
										// sameAs: it points an aggregator at data that isn't there.
										distribution: [
											{
												"@type": "DataDownload",
												name: "Nationwide compliance summary",
												encodingFormat: "application/json",
												contentUrl:
													"https://hospitalledger.com/data/summary.json",
											},
											{
												"@type": "DataDownload",
												name: "Per-hospital records",
												encodingFormat: "application/json",
												contentUrl:
													"https://hospitalledger.com/data/hospitals.json",
											},
										],
										// NO `creator`. Google lists it as recommended and it is
										// tempting to fill — but this site names no person or
										// organization as author anywhere in its own text, and
										// inventing one to satisfy a schema field is the exact
										// fabrication the sibling sites' entity work avoided.
										// sourceOrganization is CMS because that is true and the
										// page says it; authorship stays unstated because the page
										// leaves it unstated.
										sourceOrganization: {
											"@type": "GovernmentOrganization",
											name: "Centers for Medicare & Medicaid Services",
											url: "https://www.cms.gov/",
										},
									},
								],
							}).replace(/</g, "\\u003c"),
						)}
					</script>
				) : null}

				{/* Open Graph */}
				<meta property="og:title" content={resolvedOgTitle} />
				<meta property="og:description" content={description} />
				<meta property="og:type" content="website" />
				<meta property="og:site_name" content="Hospital Ledger" />
				{url ? <meta property="og:url" content={url} /> : null}
				<meta property="og:image" content={resolvedOgImage} />
				<meta property="og:image:width" content="1200" />
				<meta property="og:image:height" content="630" />
				<meta
					property="og:image:alt"
					content="Hospital Ledger — what every U.S. hospital actually charges"
				/>

				{/* Twitter / X */}
				<meta name="twitter:card" content="summary_large_image" />
				<meta name="twitter:title" content={resolvedOgTitle} />
				<meta name="twitter:description" content={description} />
				<meta name="twitter:image" content={resolvedOgImage} />

				<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
				<link rel="alternate icon" href="/favicon.ico" />
				<link rel="apple-touch-icon" href="/apple-touch-icon.png" />
				<link rel="preconnect" href="https://fonts.googleapis.com" />
				<link
					rel="preconnect"
					href="https://fonts.gstatic.com"
					crossorigin="anonymous"
				/>
				<link
					href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap"
					rel="stylesheet"
				/>
				<script src="https://cdn.tailwindcss.com" />
				<style
					dangerouslySetInnerHTML={{
						__html: `
  body { font-family: 'Inter', ui-sans-serif, -apple-system, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  .serif { font-family: 'Instrument Serif', Georgia, ui-serif, serif; font-weight: 400; letter-spacing: -0.01em; }
  .label-eyebrow { font-family: 'Inter', sans-serif; font-size: 10px; letter-spacing: 0.15em; text-transform: uppercase; color: #a1a1aa; }
  .mono, .tab-num { font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; }
  .num-step { font-family: 'JetBrains Mono', ui-monospace, monospace; font-weight: 500; color: #34d399; }
  .editorial-rule { border-top: 1px solid #27272a; }
  .fade-in { animation: fade .4s ease-out; }
  @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
  details[open] summary svg { transform: rotate(180deg); }
  summary { list-style: none; }
  summary::-webkit-details-marker { display: none; }
  .lift { transition: transform .15s ease, border-color .15s ease, background .15s ease; }
  .lift:hover { transform: translateY(-2px); border-color: rgba(52,211,153,0.35); background: rgba(24,24,27,0.85); }
  input:focus, select:focus, button:focus-visible { outline: 2px solid #34d399; outline-offset: 1px; }
  .section-rule { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.5rem; }
  .section-rule .num { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: #71717a; letter-spacing: 0.1em; }
  .section-rule .label { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.15em; color: #a1a1aa; }
  .section-rule .line { flex: 1; height: 1px; background: #27272a; }
  .carousel-track::-webkit-scrollbar { display: none; }
  .carousel-track { scrollbar-width: none; -ms-overflow-style: none; }
  @media print { body { background: white; color: black; } .no-print { display: none; } }
        `,
					}}
				/>

				{/* Traks Analytics (self-hosted, cookieless) */}
				<script
					defer
					src="https://analytics-collect.hospitalledger.com/t.js"
					data-site="pb_live_yx6qihmu9eceff3n9wcubpmr"
				/>
			</head>
			<body class={bodyClass ?? "min-h-screen"}>
				{children}
				<aside
					aria-label="Veteran resource"
					class="mx-auto max-w-6xl px-4 sm:px-6 mt-10"
				>
					<div class="rounded-xl border border-zinc-700 bg-zinc-900 p-5 sm:p-6">
						<p class="text-base font-semibold text-zinc-100">
							Preparing a VA disability claim?
						</p>
						<p class="mt-2 max-w-3xl text-sm leading-relaxed text-zinc-300">
							AIVA Claims helps veterans organize medical records and prepare
							claim documents. You review the documents and submit your own
							claim to the VA.
						</p>
						<a
							href="https://aivaclaims.com/?utm_source=hospitalledger&utm_medium=referral&utm_campaign=veteran_resources"
							class="mt-3 inline-flex min-h-11 items-center text-sm font-semibold text-emerald-300 underline underline-offset-4 hover:text-emerald-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4"
						>
							Explore AIVA Claims
						</a>
						<p class="mt-2 text-xs leading-relaxed text-zinc-400">
							AIVA is a document-preparation tool, not a medical provider or a
							VA-accredited claims agent.
						</p>
					</div>
				</aside>
				<footer class="border-t border-zinc-800 mt-10 sm:mt-12 py-6 text-center text-xs text-zinc-500">
					<a href="/" class="hover:text-zinc-300">
						Hospital Ledger
					</a>{" "}
					· © 2026 · CC0 data · No tracking
				</footer>
				{Array.isArray(scriptSrc) ? (
					scriptSrc.map((src) => <script src={src} defer />)
				) : scriptSrc ? (
					<script src={scriptSrc} defer />
				) : null}
				{inlineScript ? (
					<script dangerouslySetInnerHTML={{ __html: inlineScript }} />
				) : null}
			</body>
		</html>
	);
};

export const PageHeader: FC<{ eyebrow?: string }> = ({ eyebrow }) => (
	<header class="border-b border-zinc-800">
		<div class="mx-auto max-w-6xl px-4 sm:px-6 pt-6 sm:pt-8 pb-4">
			<div class="flex items-center justify-between">
				<a href="/" class="text-sm text-zinc-400 hover:text-zinc-200">
					← Hospital Ledger
				</a>
				{eyebrow ? (
					<div class="label-eyebrow text-emerald-300">{eyebrow}</div>
				) : null}
			</div>
		</div>
	</header>
);
