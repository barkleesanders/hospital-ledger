/**
 * Grounding corpus for the "Ask anything" row.
 *
 * Two sources, deliberately kept apart (see the infinite-faq integration guide):
 *
 *   1. THIS SITE — the public explainer pages, rendered from their own components at
 *      first use so the corpus cannot drift from the served copy: the home page
 *      (hero, "how we count", the count gap, disclaimer, how it works, why this exists,
 *      methodology — src/routes/home.tsx) and /about-the-numbers (src/routes/
 *      about-the-numbers.tsx), plus the "why some prices look unrealistically low or
 *      high" explainer every procedure page carries (src/routes/procedure.tsx). Those
 *      are every SSR explainer route in src/routes (the rest are record pages and JSON
 *      APIs). Rendered once per isolate.
 *   2. THE RECORD ON SCREEN — a hospital, procedure or payer page adds ONE more document
 *      per request, through `recordContextDoc()`. The page sends only the record's id
 *      (CCN, code or slug); the route re-reads the record with the same loaders the page
 *      uses (src/lib/data.ts), so the fields the model sees are exactly what the page
 *      shows and nothing a request claims.
 *
 * The model is never shown the id itself: llama-3.3-70b copied a 36-char id with its
 * last character missing on 3 of 3 live runs (improvecortland, 2026-09-18). The
 * visitor is already on the record's page, so the document says so instead.
 */
import type {
	Bindings,
	HospitalData,
	PayerData,
	ProcedureData,
} from "../lib/data";
import { loadHospital, loadPayer, loadProcedure } from "../lib/data";
import { fmtMoney } from "../lib/format";
import { aboutPage } from "../routes/about-the-numbers";
import { homePage } from "../routes/home";
import { complianceExplainer, ELEMENT_LABELS } from "../routes/hospital";
import { cashMinNote, PriceCaveats, procedureName } from "../routes/procedure";
import type { FaqCorpusDoc, RecordContext } from "./faq-route";

export const SITE_ORIGIN = "https://hospitalledger.com";

export const SITE_NAME = "Hospital Ledger";

/**
 * Visible text of a server-rendered page: <head>, scripts, styles, the veteran-resource
 * <aside> and the <footer> (src/components/Layout.tsx chrome) are dropped; the home
 * page's hero lives in <header>, so header + main are kept. Block boundaries become
 * newlines, tags are stripped, the entities hono/jsx emits are decoded. Good enough for
 * a prompt; not an HTML parser.
 */
export function pageText(html: string): string {
	let s = html.replace(/<head[\s>][\s\S]*?<\/head>/, "");
	s = s.replace(
		/<(script|style|aside|footer|select|datalist)[\s>][\s\S]*?<\/\1>/g,
		"",
	);
	s = s.replace(
		/<\/(p|li|h[1-6]|div|section|summary|details|tr|blockquote|dd|dt|pre|header|main)>/g,
		"\n",
	);
	s = s.replace(/<br\s*\/?>/g, "\n");
	s = s.replace(/<[^>]+>/g, "");
	s = s
		.replace(/&amp;/g, "&")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">");

	return s
		.replace(/[ \t]+/g, " ")
		.replace(/\s*\n\s*/g, "\n")
		.trim();
}

type PublicPage = { title: string; path: string; render: () => string };

/** The public pages behind the corpus, in prompt order (most-asked first). */
const PUBLIC_PAGES: PublicPage[] = [
	{
		title:
			"Hospital Ledger — home page (what the site is, how we count, where the data comes from)",
		path: "/",
		render: () => String(homePage(`${SITE_ORIGIN}/`)),
	},
	{
		title: "About the numbers (methodology)",
		path: "/about-the-numbers",
		render: () => String(aboutPage(`${SITE_ORIGIN}/about-the-numbers`)),
	},
	{
		title:
			"Why some prices look unrealistically low or high (shown on every procedure page)",
		path: "/procedure/27130",
		render: () => String(PriceCaveats({})),
	},
];

/**
 * How a hospital page's compliance grade is scored — the card every /hospital/:ccn page
 * opens with (src/routes/hospital.tsx ELEMENT_LABELS + complianceExplainer + the page's
 * closing note). Built from the same exports the page renders from, so it cannot drift.
 * Measured live before this doc existed: "What does the compliance grade measure?" was
 * answered "not explicitly defined in the reference material" (2026-09-18).
 */
function gradesDoc(): FaqCorpusDoc {
	return {
		title:
			"How a hospital's compliance grade is scored (shown on every hospital page)",
		url: `${SITE_ORIGIN}/`,
		text: [
			'Every hospital page opens with a "45 CFR § 180 compliance" card: a letter grade (A, B, C, D or F) and a score out of 100, with a dot for each of the six data elements the federal hospital price transparency rule (45 CFR Part 180) requires a hospital to publish:',
			...Object.values(ELEMENT_LABELS).map((label) => `- ${label}`),
			`Score 80 or above: ${complianceExplainer(80)} Score 60 to 79: ${complianceExplainer(60)} Score below 60: ${complianceExplainer(0)}`,
			"The compliance grade reflects how completely the hospital published the six required data elements, not the quality of care. Data comes straight from the hospital's federally-mandated machine-readable file.",
			"Each hospital page also shows how many procedures it lists, how many insurances it has negotiated rates with, how many CPT / HCPCS codes it covers, a link to the hospital's source file, and its most expensive procedures by gross charge with cash, minimum and maximum negotiated prices.",
		].join("\n"),
	};
}

/** Build the site corpus from scratch (exported for tests; the loader caches it). */
export function buildSiteCorpus(): FaqCorpusDoc[] {
	return [
		...PUBLIC_PAGES.map((p) => ({
			title: p.title,
			url: `${SITE_ORIGIN}${p.path}`,
			text: pageText(p.render()),
		})),
		gradesDoc(),
	];
}

let siteCached: FaqCorpusDoc[] | null = null;

/** Corpus loader for mountInfiniteFaq: the site's own pages, rendered once per isolate. */
export async function loadFaqCorpus(): Promise<FaqCorpusDoc[]> {
	if (!siteCached) siteCached = buildSiteCorpus();

	return siteCached;
}

/**
 * The record a visitor is looking at, as the first corpus document, re-read by id with
 * the page's own loader. Returns null for an unknown id, so a made-up id cannot put
 * anything into the prompt.
 */
/**
 * Rendered record documents, per isolate. A hospital file is a multi-MB JSON that
 * /hospital/:ccn parses once per 300 s thanks to its public cache; this route is
 * no-store, so without this every question re-read and re-parsed the file to keep
 * ten rows of it.
 * ceiling: a doc is ~3 KB, so 64 entries is ~200 KB against a 128 MB isolate.
 * corpus: the pages themselves are cached 300 s (src/routes/*.tsx), so a doc is
 *   never staler than the page beside it.
 */
export const RECORD_DOC_CACHE_MAX = 64;

export const RECORD_DOC_TTL_MS = 300_000;

const recordDocs = new Map<
	string,
	{ doc: FaqCorpusDoc | null; expires: number }
>();

export async function recordContextDoc(
	env: Bindings,
	ctx: RecordContext,
	request: Request,
): Promise<FaqCorpusDoc | null> {
	const key = `${ctx.kind}:${ctx.id}`;
	const now = Date.now();
	const hit = recordDocs.get(key);

	if (hit && hit.expires > now) return hit.doc;
	const doc = await loadRecordDoc(env, ctx, request);

	// Insertion order is the eviction order: the oldest entry goes first.
	if (recordDocs.size >= RECORD_DOC_CACHE_MAX) {
		const oldest = recordDocs.keys().next().value;

		if (oldest !== undefined) recordDocs.delete(oldest);
	}

	recordDocs.delete(key);
	recordDocs.set(key, { doc, expires: now + RECORD_DOC_TTL_MS });

	return doc;
}

async function loadRecordDoc(
	env: Bindings,
	ctx: RecordContext,
	request: Request,
): Promise<FaqCorpusDoc | null> {
	switch (ctx.kind) {
		case "hospital": {
			const data = await loadHospital(env, request, ctx.id);

			return data ? hospitalDoc(ctx.id, data) : null;
		}
		case "procedure": {
			const data = await loadProcedure(env, request, ctx.id);

			return data ? procedureDoc(data) : null;
		}
		case "payer": {
			const data = await loadPayer(env, request, ctx.id);

			return data ? payerDoc(ctx.id, data) : null;
		}
	}
}

/**
 * ceiling: a hospital page shows 50 rows; a procedure or payer page up to 500.
 * corpus: 10 rows carry the question people actually ask ("what is the most expensive
 *   procedure here", "which hospitals report this") at ~60-90 chars a row, so a record
 *   document stays under ~3,000 chars and the site pages fit beside it whole
 *   (MAX_CORPUS_CHARS in faq-route.ts; src/faq/faq-corpus.test.ts pins the sum).
 */
export const RECORD_ROWS = 10;

/**
 * Third-party strings (a hospital name, a plan alias, a procedure description) come out
 * of the hospital's own published file. One line, quotes flattened, so a crafted value
 * stays one value in the prompt — the prompt already says these are data, not
 * instructions.
 */
function quoted(s: string | null | undefined, max = 120): string {
	const flat = String(s ?? "")
		.replace(/\s+/g, " ")
		.replace(/"/g, "'")
		.trim();

	return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

const n = (v: number) => v.toLocaleString("en-US");

/** The document for one hospital page — the same fields /hospital/:ccn renders, never the CCN. */
export function hospitalDoc(_ccn: string, data: HospitalData): FaqCorpusDoc {
	const name = quoted(data.hospital_name) || "This hospital";
	const compliance = data.compliance ?? {};
	const grade = (compliance.grade ?? "F").toUpperCase();
	const score = Number(compliance.score ?? 0);
	const elements = compliance.elements ?? {};
	const items = data.items ?? [];
	const cptCount = items.filter(
		(it) => it.type === "CPT" || it.type === "HCPCS",
	).length;
	const payerSet = new Set<string>();
	for (const it of items) {
		for (const p of it.payers ?? []) {
			if (p.p) payerSet.add(p.p);
		}
	}

	const lines = [
		`The hospital page the visitor is reading on ${SITE_NAME} (this page).`,
		`Hospital: "${name}"`,
		"The law: 45 CFR Part 180 (the federal hospital price transparency rule) requires every hospital to publish a machine-readable file with six data elements: a machine-readable file; gross / standard charges; a discounted cash price; payer-specific negotiated rates; minimum and maximum negotiated charges; and free public access with no login.",
		`Compliance grade: ${grade}, score ${score} out of 100. ${complianceExplainer(score)} The grade reflects how completely the hospital published the six required data elements, not the quality of care.`,
		"What this hospital published, element by element:",
		...Object.entries(ELEMENT_LABELS).map(
			([k, label]) =>
				`- ${label}: ${elements[k] ? "published" : "NOT published"}`,
		),
		`Procedures listed: ${n(items.length)}`,
		`Insurances with rates: ${n(payerSet.size)}`,
		`CPT / HCPCS codes: ${n(cptCount)}`,
		`Source machine-readable file: ${data.source_url ? `linked on the page (format: ${quoted(data.format, 20) || "file"})` : "not linked"}`,
	];

	const top = items.slice(0, RECORD_ROWS);

	if (top.length) {
		lines.push(
			`Most expensive procedures by gross charge (the page shows the top ${Math.min(50, items.length)} of ${n(items.length)}; the first ${top.length} are):`,
			...top.map(
				(it) =>
					`- ${quoted(it.code, 12)} ${quoted(it.desc, 80)}: gross ${fmtMoney(it.gross)}, cash ${fmtMoney(it.cash)}, min payer ${fmtMoney(it.min)}, max payer ${fmtMoney(it.max)}, ${it.pc ?? 0} insurers`,
			),
		);
	}

	lines.push(
		"Prices are reproduced as published in the hospital's machine-readable file (45 CFR § 180); they are not a quote and what a patient pays depends on insurance and the services provided.",
		"The visitor is on this hospital's own page, so do not repeat or link its web address; each procedure listed has its own page, reached by clicking it.",
	);

	return {
		title: "This hospital",
		url: `${SITE_ORIGIN}/`,
		text: lines.join("\n"),
	};
}

/** The document for one procedure page — the same fields /procedure/:code renders, never the code. */
export function procedureDoc(data: ProcedureData): FaqCorpusDoc {
	const name = quoted(procedureName(data)) || "This procedure";
	const s = data.stats;
	const hospitals = data.hospitals ?? [];
	const med = s.cash_p50 ?? 0;
	const states = new Set<string>();
	const insurers = new Set<string>();
	for (const h of hospitals) {
		if (h.state) states.add(h.state);
		for (const p of h.payers ?? []) if (p.slug) insurers.add(p.slug);
	}

	// The page's default order: unflagged rows nearest the median first.
	const representative = [...hospitals]
		.sort((a, b) => {
			const qa = a.quality === "normal" ? 0 : 1;
			const qb = b.quality === "normal" ? 0 : 1;
			if (qa !== qb) return qa - qb;
			const da = a.cash !== null ? Math.abs(a.cash - med) : Infinity;
			const db = b.cash !== null ? Math.abs(b.cash - med) : Infinity;
			return da - db;
		})
		.slice(0, RECORD_ROWS);

	const lines = [
		`The procedure page the visitor is reading on ${SITE_NAME} (this page).`,
		`Procedure: "${name}" (a ${quoted(data.type, 10) || "CPT"} code; the code itself is shown at the top of the page)`,
		`Hospitals reporting a price for it: ${n(s.hospital_count)}, in ${n(states.size)} states, with negotiated rates from ${n(insurers.size)} insurers.`,
		`Cheapest published cash price: ${fmtMoney(s.cash_min)}. ${cashMinNote(s.flagged_low ?? 0)}`,
		`Median cash price across ${n(s.hospital_count)} hospitals: ${fmtMoney(s.cash_p50)} — the realistic middle of the cash-price distribution; use it as the benchmark.`,
		`Most expensive published cash price: ${fmtMoney(s.cash_max)} — often a gross-charge list price rarely actually paid.`,
		`Median gross charge: ${fmtMoney(s.gross_p50)}.`,
	];

	if (representative.length) {
		lines.push(
			`Representative hospitals (nearest the median; the page lists ${n(Math.min(500, hospitals.length))} in all):`,
			...representative.map(
				(h) =>
					`- "${quoted(h.name, 80)}" (${quoted(h.state, 2) || "state not given"}): cash ${fmtMoney(h.cash)}, gross ${fmtMoney(h.gross)}, negotiated ${fmtMoney(h.min)} to ${fmtMoney(h.max)}, ${(h.payers ?? []).length} insurers${h.quality === "low_outlier" ? " (flagged: likely a partial-cost line item)" : h.quality === "high_outlier" ? " (flagged: unusually high)" : ""}`,
			),
		);
	}

	lines.push(
		'"Cash price" is the discounted self-pay rate hospitals must publish for uninsured patients. Prices reflect what each hospital published in its machine-readable file (45 CFR § 180); what a patient actually pays depends on the plan, deductible and other factors.',
		"The visitor is on this procedure's own page, so do not repeat or link its web address; each hospital listed has its own page, reached by clicking it.",
	);

	return {
		title: "This procedure",
		url: `${SITE_ORIGIN}/`,
		text: lines.join("\n"),
	};
}

/** The document for one payer page — the same fields /payer/:slug renders, never the slug. */
export function payerDoc(slug: string, data: PayerData): FaqCorpusDoc {
	const p = data.payer ?? { slug, display: slug };
	const display = quoted(p.display) || "This insurance";
	const hospitals = data.hospitals ?? [];
	const hospitalCount = data.hospital_count ?? hospitals.length;
	const states = new Set(hospitals.map((h) => h.state).filter(Boolean));
	const aliases = (p.raw_aliases ?? [])
		.slice(0, 5)
		.map((a) => `"${quoted(a, 60)}"`);

	const rows = [...hospitals]
		.sort((a, b) => (b.n_items_with_payer ?? 0) - (a.n_items_with_payer ?? 0))
		.slice(0, RECORD_ROWS);

	const lines = [
		`The insurance page the visitor is reading on ${SITE_NAME} (this page).`,
		`Insurance: "${display}" (${quoted(p.category, 30).replace("-", " ") || "insurance"})`,
		`Hospitals that have negotiated rates with this insurance: ${n(hospitalCount)}, in ${n(states.size)} states.`,
		`Median negotiated rate: ${fmtMoney(p.median_rate)}.`,
		`Plan-name aliases hospitals use for it on their files: ${aliases.length ? aliases.join(", ") : "none listed"}.`,
	];

	if (rows.length) {
		lines.push(
			`Hospitals with the most procedures negotiated (the page lists ${n(Math.min(500, hospitals.length))} in all):`,
			...rows.map(
				(h) =>
					`- "${quoted(h.name, 80)}" (${quoted(h.state, 2) || "state not given"}): ${n(h.n_items_with_payer ?? 0)} procedures with a rate, median rate ${fmtMoney(h.median_rate)}, compliance grade ${quoted(h.compliance_grade, 2) || "not graded"}`,
			),
		);
	}

	lines.push(
		"\"Negotiated rate\" is what each hospital agreed to accept from this insurance for a specific procedure; the patient's cost depends on the plan's deductible, copay, coinsurance and out-of-network rules.",
		"The visitor is on this insurance's own page, so do not repeat or link its web address; each hospital listed has its own page, reached by clicking it.",
	);

	return {
		title: "This insurance",
		url: `${SITE_ORIGIN}/`,
		text: lines.join("\n"),
	};
}
