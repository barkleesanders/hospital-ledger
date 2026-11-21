/**
 * GET /about — what this site is, how to use it, why it matters, and how
 * it got here. The plain-English story page; the count-by-count methodology
 * lives on /about-the-numbers and this page links to it rather than
 * repeating it.
 */

import type { Context } from "hono";
import { Breadcrumbs } from "../components/Breadcrumbs";
import { Layout } from "../components/Layout";
import type { Env } from "../index";
import {
	breadcrumbListNode,
	type Crumb,
	graphJsonLd,
	webPageNode,
} from "../lib/seo";
import { bundledSummary, loadSummary, type Summary } from "../lib/site-data";

const fmt = (n: number) => n.toLocaleString("en-US");

export function storyPage(url: string, S: Summary = bundledSummary) {
	const hospitals = S.standardized_price_hospitals ?? 5580;
	const rows = S.standardized_price_rows ?? 87649306;
	const verified = S.compliant ?? 3873;
	const required = S.cms_required_total ?? 4625;
	const updated = S.generated_at.slice(0, 10);
	const crumbs: Crumb[] = [
		{ name: "Home", path: "/" },
		{ name: "About", path: "/about" },
	];
	const jsonLd = graphJsonLd(
		webPageNode({
			name: "About Hospital Ledger",
			url,
			dateModified: S.generated_at,
		}),
		breadcrumbListNode(crumbs),
	);
	return (
		<Layout
			title="About — Hospital Ledger"
			jsonLd={jsonLd}
			description={`What Hospital Ledger is, how to use it, and how it was built: ${fmt(hospitals)} hospitals and ${fmt(rows)} prices, taken straight from the files hospitals must publish by law — free, no account, CC0 data.`}
			ogTitle="About — Hospital Ledger"
			url={url}
			summary={S}
		>
			<main class="mx-auto max-w-3xl px-4 sm:px-6 py-10 sm:py-14">
				<Breadcrumbs items={crumbs} />
				<p class="text-xs uppercase tracking-widest text-emerald-400">
					About
				</p>
				<h1 class="mt-3 serif text-3xl sm:text-4xl md:text-5xl tracking-tight leading-tight">
					What this site is, and how it got here
				</h1>
				<p class="mt-4 text-zinc-400 text-sm">
					Updated <span class="tab-num">{updated}</span> · figures from{" "}
					<a href="/data/summary.json" class="underline hover:text-zinc-200">
						/data/summary.json
					</a>
					.
				</p>

				<section class="mt-10">
					<h2 class="text-xl font-semibold tracking-tight">
						What this site does
					</h2>
					<p class="mt-4 text-zinc-300 leading-relaxed">
						Federal law (45 CFR § 180) requires every hospital in the
						United States to publish its prices in a machine-readable
						file. Most hospitals do post a file — a spreadsheet with
						millions of rows, in hundreds of different formats, that
						almost nobody can open, let alone compare.
					</p>
					<p class="mt-4 text-zinc-300 leading-relaxed">
						Hospital Ledger collects those files, standardizes them
						into one format, checks that each file really belongs to
						the hospital it is published under, and makes the prices
						searchable. Today that is{" "}
						<span class="tab-num">{fmt(hospitals)}</span> hospitals
						and <span class="tab-num">{fmt(rows)}</span> prices. It
						is free, needs no account, sets no cookies, and the data
						is CC0 — anyone may reuse it for anything.
					</p>
				</section>

				<section class="mt-10">
					<h2 class="text-xl font-semibold tracking-tight">
						How to use it
					</h2>
					<ol class="mt-4 list-decimal list-outside ml-5 space-y-3 text-zinc-300 leading-relaxed">
						<li>
							<strong class="text-zinc-100">Find a hospital.</strong>{" "}
							Search by name, city, or state from the{" "}
							<a href="/" class="underline hover:text-zinc-100">
								home page
							</a>
							. A hospital's page lists the prices from its own
							file and shows whether that file passes the
							publication check the law requires.
						</li>
						<li>
							<strong class="text-zinc-100">
								Compare a procedure.
							</strong>{" "}
							Search for a service — an MRI, a delivery, a knee
							replacement — to see what different hospitals charge
							for the same thing.
						</li>
						<li>
							<strong class="text-zinc-100">
								Read the price type.
							</strong>{" "}
							One service carries several prices: the gross
							charge, the discounted cash price, and the rates
							negotiated with insurers. Every price here is
							labeled with its type, so compare like with like.
						</li>
						<li>
							<strong class="text-zinc-100">Use the data.</strong>{" "}
							Everything is CC0. Download it, quote it, build on
							it — no permission needed.
						</li>
					</ol>
					<p class="mt-4 text-zinc-300 leading-relaxed">
						Run a hospital?{" "}
						<a href="/submit" class="underline hover:text-zinc-100">
							Submit your price file
						</a>{" "}
						and we will validate and standardize it the same way.
					</p>
				</section>

				<section class="mt-10">
					<h2 class="text-xl font-semibold tracking-tight">
						Why it matters
					</h2>
					<p class="mt-4 text-zinc-300 leading-relaxed">
						The same procedure can cost thousands of dollars more at
						one hospital than at another across town. Patients
						almost never see a price before care, and the files the
						law produced were too raw for anyone — patients,
						journalists, researchers, even hospitals checking
						themselves — to actually use. A price nobody can read
						might as well not be posted. This site exists to make
						the posted prices readable: of the{" "}
						<span class="tab-num">{fmt(required)}</span> hospitals
						the rule covers, we have verified a live file for{" "}
						<span class="tab-num">{fmt(verified)}</span> so far,
						and the count is checked and republished every week.
					</p>
				</section>

				<section class="mt-10">
					<h2 class="text-xl font-semibold tracking-tight">
						The road here
					</h2>
					<p class="mt-4 text-zinc-300 leading-relaxed">
						This site was built in the open over six months, in
						more than 400 recorded changes — including the mistakes,
						which taught the lessons below.
					</p>
					<ul class="mt-4 space-y-3 text-zinc-300 leading-relaxed">
						<li>
							<strong class="text-zinc-100">May 2026.</strong> The
							first version covered about 1,900 hospitals. The
							prices were real but the files were fragile, and the
							counts mixed different things together.
						</li>
						<li>
							<strong class="text-zinc-100">Summer 2026.</strong>{" "}
							The catalog grew past CMS's own published list,
							adding specialty, children's, rehabilitation, and
							territorial hospitals that list covers poorly —
							each new facility checked against the identifiers
							inside its own file before publication.
						</li>
						<li>
							<strong class="text-zinc-100">
								November 2026.
							</strong>{" "}
							A full recheck of the live catalog against the
							files' own headers found 119 prices files published
							under the wrong hospital — inherited bulk approvals,
							files shared across a system, files that belonged
							to a sister facility. All 119 came down. 57 are back
							with their own verified files; 4 stay down, because
							no file of their own could be found. The
							count-by-count account is on{" "}
							<a
								href="/about-the-numbers"
								class="underline hover:text-zinc-100"
							>
								About the numbers
							</a>
							.
						</li>
						<li>
							<strong class="text-zinc-100">Today.</strong> The
							catalog refreshes daily and is fully recounted
							every week, so the numbers on this page are the
							numbers the pipeline verified most recently — not
							a launch-day snapshot.
						</li>
					</ul>
				</section>

				<section class="mt-10">
					<h2 class="text-xl font-semibold tracking-tight">
						What we learned
					</h2>
					<ul class="mt-4 space-y-3 text-zinc-300 leading-relaxed">
						<li>
							<strong class="text-zinc-100">
								A posted file is not proof it belongs to that
								hospital.
							</strong>{" "}
							Files get shared across hospital systems and posted
							under the wrong name. Nothing is published here
							until the name and identifiers inside the file
							match the hospital it will be listed under.
						</li>
						<li>
							<strong class="text-zinc-100">
								A count has to say what it counts.
							</strong>{" "}
							"Hospitals" can mean CMS's list, the hospitals the
							rule covers, the ones with a verified file, or the
							ones with usable prices — four different numbers.
							This site keeps them separate and defines each one.
						</li>
						<li>
							<strong class="text-zinc-100">
								Corrections go on the record.
							</strong>{" "}
							When a file here is wrong, it comes down and the
							change is written up where anyone can read it —
							not quietly edited after the fact.
						</li>
					</ul>
					<p class="mt-6 text-zinc-400 leading-relaxed">
						For the full methodology — how files are found, how
						each count is built, and where the remaining gaps are —
						see{" "}
						<a
							href="/about-the-numbers"
							class="underline hover:text-zinc-200"
						>
							About the numbers
						</a>
						.
					</p>
				</section>
			</main>
		</Layout>
	);
}

export async function aboutPageHandler(c: Context<Env>): Promise<Response> {
	const { summary, source } = await loadSummary(c.env);
	return c.html(storyPage("https://hospitalledger.com/about", summary), 200, {
		"cache-control": "public, max-age=300",
		"x-hl-template": "about-ssr",
		"x-hl-source": source,
	});
}
