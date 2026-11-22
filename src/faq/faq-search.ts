/**
 * The ask box's built-in search skill: turn a price question into a live search of
 * the site's own data, and hand the model the results as the first corpus document.
 *
 * Why this exists (measured 2026-11-21): the Infinite FAQ grounds answers in the
 * site's rendered pages, which carry no price rows, so "Where's the cheapest spot
 * to get an MRI in San Francisco?" was answered "The reference material does not
 * list specific prices…" — the data was on the site the whole time; the assistant
 * had no path to it. This module is that path:
 *
 *   1. Parse the question: a procedure (a CPT/HCPCS code named outright, or a lay
 *      term from the synonym table below) and a place (a hospital named outright,
 *      a city, a state, or neither).
 *   2. Retrieve from the same store the pages read:
 *      - a named hospital or a city → read that hospital's / those hospitals' own
 *        price files (loadHospital) and pull the procedure's row out of each;
 *      - a state or no place → the procedure aggregate (loadProcedure), whose
 *        hospital rows are the lowest-priced reporters.
 *   3. Format the findings as one corpus document that states what was searched,
 *      how many files were read, and what was found — including the negative
 *      findings (lists it with no price, does not list it), so "not listed" is a
 *      measured result, never a guess.
 *
 * Only hospitals in the live price index are ever named: the index is the site's
 * published set, so a facility whose file was taken down (its R2 object is kept,
 * never deleted) cannot leak back in through the assistant. If the index cannot
 * be read, the search returns nothing rather than risk naming one.
 *
 * Hospital and procedure names below come out of the hospitals' own published
 * files: data, never instructions (the prompt says so for every document).
 */

import HOSPITAL_GEO from "../../public/data/hospital-geo.json";
import type { Bindings, HospitalData } from "../lib/data";
import { loadHospital, loadProcedure } from "../lib/data";
import { readR2Json } from "../lib/r2";
import { SITE_ORIGIN } from "./faq-corpus";
import type { FaqCorpusDoc } from "./faq-route";

// ---------------------------------------------------------------------------
// Places
// ---------------------------------------------------------------------------

type GeoEntry = { ccn: string; city: string; state: string };

const GEO: GeoEntry[] = Object.entries(
	HOSPITAL_GEO as unknown as Record<string, [string, string]>,
).map(([ccn, [city, state]]) => ({ ccn, city, state }));

/** CITY (upper) → the hospitals the geo table places there. */
const BY_CITY = new Map<string, GeoEntry[]>();

for (const g of GEO) {
	const key = g.city.toUpperCase();
	const list = BY_CITY.get(key);

	if (list) list.push(g);
	else BY_CITY.set(key, [g]);
}

const STATE_NAMES: Record<string, string> = {
	alabama: "AL",
	alaska: "AK",
	arizona: "AZ",
	arkansas: "AR",
	california: "CA",
	colorado: "CO",
	connecticut: "CT",
	delaware: "DE",
	"district of columbia": "DC",
	florida: "FL",
	georgia: "GA",
	hawaii: "HI",
	idaho: "ID",
	illinois: "IL",
	indiana: "IN",
	iowa: "IA",
	kansas: "KS",
	kentucky: "KY",
	louisiana: "LA",
	maine: "ME",
	maryland: "MD",
	massachusetts: "MA",
	michigan: "MI",
	minnesota: "MN",
	mississippi: "MS",
	missouri: "MO",
	montana: "MT",
	nebraska: "NE",
	nevada: "NV",
	"new hampshire": "NH",
	"new jersey": "NJ",
	"new mexico": "NM",
	"new york": "NY",
	"north carolina": "NC",
	"north dakota": "ND",
	ohio: "OH",
	oklahoma: "OK",
	oregon: "OR",
	pennsylvania: "PA",
	"rhode island": "RI",
	"south carolina": "SC",
	"south dakota": "SD",
	tennessee: "TN",
	texas: "TX",
	utah: "UT",
	vermont: "VT",
	virginia: "VA",
	washington: "WA",
	"west virginia": "WV",
	wisconsin: "WI",
	wyoming: "WY",
};

const STATE_ABBRS = new Set(Object.values(STATE_NAMES));

/** Metro shorthands visitors actually type. Checked before the city scan. */
const CITY_ALIASES: Record<string, string> = {
	sf: "SAN FRANCISCO",
	nyc: "NEW YORK",
};

export type Place = { city?: string; state?: string };

function parsePlace(question: string): Place {
	const lower = ` ${question.toLowerCase().replace(/[^a-z0-9 ]+/g, " ")} `;
	let state: string | undefined;

	for (const [name, abbr] of Object.entries(STATE_NAMES)) {
		if (lower.includes(` ${name} `)) {
			state = abbr;
			break;
		}
	}

	// A bare abbreviation counts only in its unambiguous written forms: upper-case
	// in the original ("Austin, TX") — lower-case "in", "or", "me" are words.
	if (!state) {
		for (const m of question.matchAll(/\b([A-Z]{2})\b/g)) {
			if (STATE_ABBRS.has(m[1])) {
				state = m[1];
				break;
			}
		}
	}

	const upper = question.toUpperCase();

	for (const [alias, city] of Object.entries(CITY_ALIASES)) {
		if (new RegExp(`\\b${alias}\\b`, "i").test(question))
			return { city, state: state ?? BY_CITY.get(city)?.[0]?.state };
	}

	// The geo table is the city vocabulary: the longest city name that appears in
	// the question wins ("San Francisco" over "San", "Fort Worth" over "Fort").
	let city: string | undefined;

	for (const key of BY_CITY.keys()) {
		if (key.length < 3) continue;

		if (
			upper.includes(key) &&
			(!city || key.length > city.length) &&
			(!state || BY_CITY.get(key)?.some((g) => g.state === state) || !city)
		)
			city = key;
	}

	// When a state was named, a same-named city in another state is not the place.
	if (city && state && !BY_CITY.get(city)?.some((g) => g.state === state))
		city = undefined;

	return { city, state };
}

// ---------------------------------------------------------------------------
// Procedures
// ---------------------------------------------------------------------------

/**
 * Lay term → CPT codes, most specific first. A generic term maps to the codes a
 * visitor most plausibly means; the results document labels each code with the
 * description from the hospital files, so an ambiguity is shown, not hidden.
 * Grown by the AI Tutor probe loop: a question the table misses is a probe
 * failure with the fix being one row here.
 */
const SYNONYMS: Array<{ terms: string[]; codes: string[] }> = [
	{ terms: ["mri of the brain", "brain mri", "mri brain", "head mri"], codes: ["70551", "70553"] },
	{ terms: ["mri of the lumbar", "lumbar mri", "mri lumbar", "lower back mri", "mri of the lower back"], codes: ["72148"] },
	{ terms: ["mri of the cervical", "cervical mri", "neck mri"], codes: ["72141"] },
	{ terms: ["mri of the knee", "knee mri"], codes: ["73721"] },
	{ terms: ["mri of the shoulder", "shoulder mri"], codes: ["73221"] },
	{ terms: ["mri of the pelvis", "pelvis mri", "mri pelvis"], codes: ["72195"] },
	{ terms: ["mri", "magnetic resonance"], codes: ["70551", "72148", "73721"] },
	{ terms: ["ct of the abdomen", "ct abdomen", "ct scan of the abdomen", "abdominal ct"], codes: ["74177", "74176"] },
	{ terms: ["ct of the head", "head ct", "ct head", "brain ct"], codes: ["70450"] },
	{ terms: ["ct of the chest", "chest ct", "ct chest"], codes: ["71260"] },
	{ terms: ["ct scan", "cat scan"], codes: ["74177", "70450", "71260"] },
	{ terms: ["screening mammogram", "mammogram", "mammography"], codes: ["77067"] },
	{ terms: ["colonoscopy"], codes: ["45378", "45380"] },
	{ terms: ["knee replacement", "total knee"], codes: ["27447"] },
	{ terms: ["hip replacement", "total hip"], codes: ["27130"] },
	{ terms: ["heart transplant"], codes: ["33945"] },
	{ terms: ["kidney transplant"], codes: ["50360"] },
	{ terms: ["liver transplant"], codes: ["47135"] },
	{ terms: ["cataract surgery", "cataract"], codes: ["66984"] },
	{ terms: ["upper endoscopy", "endoscopy", "egd"], codes: ["43235"] },
	{ terms: ["gallbladder removal", "gallbladder surgery", "cholecystectomy"], codes: ["47562"] },
	{ terms: ["appendectomy", "appendix removal"], codes: ["44950"] },
	{ terms: ["hernia repair", "hernia surgery"], codes: ["49505"] },
	{ terms: ["tonsillectomy"], codes: ["42826"] },
	{ terms: ["echocardiogram", "echo test"], codes: ["93306"] },
	{ terms: ["abdominal ultrasound", "ultrasound of the abdomen"], codes: ["76700"] },
	{ terms: ["chest x-ray", "chest xray", "x-ray of the chest", "xray"], codes: ["71046"] },
	{ terms: ["emergency room visit", "er visit", "emergency department visit"], codes: ["99285"] },
	{ terms: ["psa test", "prostate screening"], codes: ["84153"] },
];

/** The codes a question asks about: a code named outright, else the synonym table. */
export function parseProcedureCodes(question: string): string[] {
	const direct = question.match(/\b(\d{5})\b/)?.[1];

	if (direct) return [direct];
	const lower = question.toLowerCase();

	for (const row of SYNONYMS)
		if (row.terms.some((t) => lower.includes(t))) return row.codes;

	return [];
}

// ---------------------------------------------------------------------------
// The live index — the only hospitals the search may name
// ---------------------------------------------------------------------------

type PriceIndex = {
	generated_at?: string;
	hospitals?: Array<{ ccn: string; n?: number; name?: string }>;
};

export const LIVE_INDEX_TTL_MS = 300_000;

let liveCache: {
	at: number;
	ccns: Set<string>;
	names: Map<string, string>;
} | null = null;

/**
 * The published set, per isolate: every CCN in prices/index.json (the store-
 * derived index the counts pages use). Null when the index cannot be read —
 * the caller then runs no search at all (fail closed, see the header).
 */
async function liveIndex(
	env: Bindings,
): Promise<{ ccns: Set<string>; names: Map<string, string> } | null> {
	const now = Date.now();

	if (liveCache && liveCache.at + LIVE_INDEX_TTL_MS > now)
		return { ccns: liveCache.ccns, names: liveCache.names };
	const idx = await readR2Json<PriceIndex>(
		env.HL_MRF_PARSED,
		"prices/index.json",
	);

	if (!idx?.hospitals?.length) return null;
	const ccns = new Set<string>();
	const names = new Map<string, string>();

	for (const h of idx.hospitals) {
		ccns.add(h.ccn);

		if (h.name) names.set(h.ccn, h.name);
	}

	liveCache = { at: now, ccns, names };

	return { ccns, names };
}

// ---------------------------------------------------------------------------
// A named hospital
// ---------------------------------------------------------------------------

const NAME_STOP = new Set([
	"HOSPITAL",
	"MEDICAL",
	"CENTER",
	"CENTRE",
	"HEALTH",
	"SYSTEM",
	"REGIONAL",
	"GENERAL",
	"MEMORIAL",
	"THE",
	"OF",
	"AND",
]);

/**
 * The hospital a question names outright, if any: the index name with generic
 * words stripped must survive as ≥4 chars (or an acronym) and appear verbatim
 * in the question — "UCSF MEDICAL CENTER" → "UCSF". Conservative on purpose:
 * a missed name degrades to the place search; a wrong one would not.
 */
export function parseNamedHospital(
	question: string,
	names: Map<string, string>,
): { ccn: string; name: string } | null {
	const upper = ` ${question.toUpperCase().replace(/[^A-Z0-9 ]+/g, " ")} `;

	for (const [ccn, name] of names) {
		const core = name
			.toUpperCase()
			.replace(/[^A-Z0-9 ]+/g, " ")
			.split(" ")
			.filter((w) => w && !NAME_STOP.has(w))
			.join(" ")
			.trim();

		if (core.length >= 4 && upper.includes(` ${core} `))
			return { ccn, name };
	}

	return null;
}

// ---------------------------------------------------------------------------
// Retrieval
// ---------------------------------------------------------------------------

/** Hospitals whose files are read one by one for a city question. */
// ceiling: a Worker ask is rate-limited (20/min/IP) and each file is one R2 read;
//   the fan-out runs 8 at a time. corpus: the largest city in the geo table has
//   ~60 hospitals on the site (New York); 48 covers every city but that one whole,
//   and the document states how many of how many were checked when it cannot.
export const MAX_CITY_FILES = 48;

const FANOUT_BATCH = 8;

/** Rows shown per procedure in the results document. */
export const TOP_ROWS = 8;

type Priced = {
	ccn: string;
	name: string;
	cash: number | null;
	gross: number | null;
	/** How many DISTINCT cash prices the file lists for the code (0 when none). */
	cashCount?: number;
};

function money(v: number): string {
	return `$${v.toLocaleString("en-US", {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2,
	})}`;
}

function rowLine(r: Priced, i: number): string {
	const price =
		r.cash !== null
			? `discounted cash price ${money(r.cash)}`
			: `gross charge ${money(r.gross ?? 0)} (no discounted cash price published)`;
	const lowest =
		r.cash !== null && (r.cashCount ?? 0) > 1
			? ` (lowest of ${r.cashCount} different cash prices its file lists for this code)`
			: "";

	return `${i + 1}. ${r.name} — ${price}${lowest}${r.cash !== null && r.gross !== null ? ` (gross charge ${money(r.gross)})` : ""} — ${SITE_ORIGIN}/hospital/${r.ccn}`;
}

/**
 * One hospital's price for one code, aggregated over every row its file carries
 * for that code. A file may list a code several times — different service lines
 * or descriptions — at different prices, or with a price on one row and none on
 * another (measured live 2026-11-21: UCSF Langley Porter's file carries 70551
 * at $432.30 and again with no price; Stroger's carries 45378 at $321.30 and
 * $1,663.20). Taking the first row would report whichever the file happened to
 * order first; the price the hospital publishes for the code is the lowest of
 * them, and the gross shown is the one on that same row.
 */
export function codePrices(
	data: HospitalData,
	code: string,
): { listed: boolean; cash: number | null; gross: number | null; cashCount: number } {
	const items = (data.items ?? []).filter((it) => String(it.code) === code);

	if (items.length === 0)
		return { listed: false, cash: null, gross: null, cashCount: 0 };
	const withCash = items
		.filter((it) => it.cash != null)
		.sort((a, b) => (a.cash ?? 0) - (b.cash ?? 0));

	if (withCash.length > 0)
		return {
			listed: true,
			cash: withCash[0].cash,
			gross: withCash[0].gross ?? null,
			cashCount: new Set(withCash.map((it) => it.cash)).size,
		};
	const withGross = items
		.filter((it) => it.gross != null)
		.sort((a, b) => (a.gross ?? 0) - (b.gross ?? 0));

	if (withGross.length > 0)
		return { listed: true, cash: null, gross: withGross[0].gross, cashCount: 0 };

	return { listed: true, cash: null, gross: null, cashCount: 0 };
}

type Section = { code: string; lines: string[]; url?: string };

/**
 * The standard basket for "which hospital is the best deal overall" questions —
 * common services most hospitals publish a cash price for. An overall ranking is
 * the median of a hospital's cash prices across the basket services it lists.
 */
export const BASKET: ReadonlyArray<readonly [string, string]> = [
	["70551", "brain MRI"],
	["72148", "lower-spine MRI"],
	["73721", "joint MRI"],
	["45378", "colonoscopy"],
	["27447", "knee replacement"],
	["27130", "hip replacement"],
	["66984", "cataract surgery"],
	["47562", "gallbladder removal"],
	["93306", "echocardiogram"],
	["74177", "CT scan of the abdomen and pelvis"],
];

/** A hospital needs cash prices for at least this many basket services to be ranked. */
export const BASKET_MIN_PRICES = 4;

function median(values: number[]): number {
	const sorted = [...values].sort((a, b) => a - b);
	const mid = Math.floor(sorted.length / 2);

	return sorted.length % 2 === 1
		? sorted[mid]
		: (sorted[mid - 1] + sorted[mid]) / 2;
}

/** Cash prices a hospital publishes across the basket, with its file's cash coverage. */
function basketPrices(
	data: HospitalData,
): { cashes: number[]; cashPct: number | undefined } {
	const cashes: number[] = [];

	for (const [code] of BASKET) {
		const agg = codePrices(data, code);

		if (agg.cash !== null) cashes.push(agg.cash);
	}
	const coverage = (
		data.compliance as { coverage?: { cash_pct?: number } } | undefined
	)?.coverage;

	return { cashes, cashPct: coverage?.cash_pct };
}

function cityCandidates(place: Place, live: Set<string>): GeoEntry[] {
	return (BY_CITY.get(place.city ?? "") ?? []).filter(
		(g) => live.has(g.ccn) && (!place.state || g.state === place.state),
	);
}

async function hospitalSection(
	env: Bindings,
	request: Request,
	ccn: string,
	name: string,
	codes: string[],
	descs: Map<string, string>,
): Promise<Section | null> {
	const data = await loadHospital(env, request, ccn);

	if (!data) return null;
	const lines = [`Hospital asked about: ${name}`];
	const coverage = (
		data.compliance as
			| { coverage?: { cash_pct?: number } }
			| undefined
	)?.coverage;

	for (const code of codes) {
		const agg = codePrices(data, code);
		const desc = descs.get(code) ?? code;

		if (!agg.listed) {
			lines.push(
				`Its published file does not list ${desc} (CPT ${code}).`,
			);
		} else if (agg.cash !== null) {
			lines.push(
				`For ${desc} (CPT ${code}) its published discounted cash price is ${money(agg.cash)}${agg.cashCount > 1 ? ` (the lowest of ${agg.cashCount} different cash prices its file lists for this code)` : ""}${agg.gross != null ? ` and its gross charge is ${money(agg.gross)}` : ""}.`,
			);
		} else if (agg.gross !== null) {
			lines.push(
				`For ${desc} (CPT ${code}) it publishes a gross charge of ${money(agg.gross)} and no discounted cash price.`,
			);
		} else {
			lines.push(
				`Its file lists ${desc} (CPT ${code}) but publishes no price for it.`,
			);
		}
	}

	if (coverage?.cash_pct !== undefined)
		lines.push(
			`Across its whole file, it publishes a discounted cash price for ${coverage.cash_pct}% of its listed items.`,
		);

	return { code: codes[0], lines };
}

async function citySection(
	env: Bindings,
	request: Request,
	code: string,
	place: Place,
	live: Set<string>,
): Promise<Section | null> {
	const detail = await loadProcedure(env, request, code);

	if (!detail) return null;
	const inCity = cityCandidates(place, live);
	const checked = inCity.slice(0, MAX_CITY_FILES);
	const found: Priced[] = [];
	let listedNoPrice = 0;
	let loaded = 0;

	for (let i = 0; i < checked.length; i += FANOUT_BATCH) {
		const batch = await Promise.all(
			checked
				.slice(i, i + FANOUT_BATCH)
				.map((g) => loadHospital(env, request, g.ccn)),
		);

		for (let j = 0; j < batch.length; j++) {
			const data: HospitalData | null = batch[j];

			if (!data) continue;
			loaded += 1;
			const agg = codePrices(data, code);

			if (!agg.listed) continue;

			if (agg.cash == null && agg.gross == null) {
				listedNoPrice += 1;
				continue;
			}

			found.push({
				ccn: checked[i + j].ccn,
				name: data.hospital_name,
				cash: agg.cash,
				gross: agg.gross,
				cashCount: agg.cashCount,
			});
		}
	}

	const priced = found
		.filter((r) => r.cash !== null)
		.sort((a, b) => (a.cash ?? 0) - (b.cash ?? 0));
	const grossOnly = found
		.filter((r) => r.cash === null)
		.sort((a, b) => (a.gross ?? 0) - (b.gross ?? 0));
	const placeLabel = `${titleCase(place.city ?? "")}, ${place.state ?? ""}`;
	const lines = [
		`Procedure: ${detail.desc} (CPT ${code})`,
		`Place searched: ${placeLabel} — ${inCity.length} hospitals are on the site for this city; ${loaded} price files were read.`,
	];

	if (priced.length > 0) {
		lines.push(
			`Hospitals in ${placeLabel} that publish a discounted cash price for this procedure, lowest first:`,
			...priced.slice(0, TOP_ROWS).map(rowLine),
		);
	}

	if (grossOnly.length > 0) {
		lines.push(
			`Hospitals in ${placeLabel} that publish only a gross charge for this procedure (no discounted cash price):`,
			...grossOnly.slice(0, TOP_ROWS).map(rowLine),
		);
	}

	if (priced.length === 0 && grossOnly.length === 0)
		lines.push(
			`No hospital in ${placeLabel} publishes a price for this procedure.`,
		);
	const notListed = loaded - found.length - listedNoPrice;

	lines.push(
		`Also counted: ${listedNoPrice} list the procedure but publish no price for it; ${notListed} do not list it at all.`,
		`Full table for this procedure: ${SITE_ORIGIN}/procedure/${code}`,
	);

	return { code, lines };
}

/**
 * "Best deal overall" in a city: rank the city's hospitals by the median of
 * their cash prices across the standard basket. The method is stated in the
 * document itself, so the answer explains the ranking instead of crowning a
 * winner the data cannot support.
 */
async function basketSection(
	env: Bindings,
	request: Request,
	place: Place,
	live: Set<string>,
): Promise<Section | null> {
	const inCity = cityCandidates(place, live);
	const checked = inCity.slice(0, MAX_CITY_FILES);
	const ranked: Array<{
		ccn: string;
		name: string;
		median: number;
		count: number;
		cashPct: number | undefined;
	}> = [];
	let loaded = 0;

	for (let i = 0; i < checked.length; i += FANOUT_BATCH) {
		const batch = await Promise.all(
			checked
				.slice(i, i + FANOUT_BATCH)
				.map((g) => loadHospital(env, request, g.ccn)),
		);

		for (let j = 0; j < batch.length; j++) {
			const data: HospitalData | null = batch[j];

			if (!data) continue;
			loaded += 1;
			const { cashes, cashPct } = basketPrices(data);

			if (cashes.length >= BASKET_MIN_PRICES)
				ranked.push({
					ccn: checked[i + j].ccn,
					name: data.hospital_name,
					median: median(cashes),
					count: cashes.length,
					cashPct,
				});
		}
	}

	if (ranked.length === 0) return null;
	ranked.sort((a, b) => a.median - b.median);
	const placeLabel = `${titleCase(place.city ?? "")}, ${place.state ?? ""}`;
	const basketList = BASKET.map(([, label]) => label).join(", ");
	const lines = [
		`Question type: which hospital is the best overall deal in ${placeLabel} — no single procedure was named.`,
		`Method: each hospital is ranked by the MEDIAN of its published discounted cash prices across a standard basket of ${BASKET.length} common services (${basketList}), counting only the services that hospital publishes a cash price for. Hospitals with cash prices for fewer than ${BASKET_MIN_PRICES} of the ${BASKET.length} are not ranked.`,
		`Place searched: ${placeLabel} — ${inCity.length} hospitals are on the site for this city; ${loaded} price files were read; ${ranked.length} could be ranked.`,
		`Hospitals in ${placeLabel} ranked by that median, lowest first:`,
		...ranked
			.slice(0, TOP_ROWS)
			.map(
				(r, i) =>
					`${i + 1}. ${r.name} — median cash price ${money(r.median)} across ${r.count} of the ${BASKET.length} basket services${r.cashPct !== undefined ? `; publishes a cash price for ${r.cashPct}% of its file` : ""} — ${SITE_ORIGIN}/hospital/${r.ccn}`,
			),
		`There is no single best hospital for everything: this ranking compares the basket only, and the right choice depends on the care needed. For one specific service, ask about that service and the answer will use its exact prices.`,
	];

	return { code: "", lines, url: SITE_ORIGIN };
}

/** A named hospital with no procedure named: its file's shape, not a punt. */
async function hospitalProfileSection(
	env: Bindings,
	request: Request,
	ccn: string,
	name: string,
): Promise<Section | null> {
	const data = await loadHospital(env, request, ccn);

	if (!data) return null;
	const { cashes, cashPct } = basketPrices(data);
	const compliance = data.compliance as
		| { score?: number; grade?: string }
		| undefined;
	const lines = [`Hospital asked about: ${name}`];

	lines.push(
		`Its published file lists ${(data.items ?? []).length.toLocaleString("en-US")} priced items.`,
	);

	if (cashPct !== undefined)
		lines.push(
			`It publishes a discounted cash price for ${cashPct}% of its listed items.`,
		);

	if (compliance?.grade !== undefined && compliance?.score !== undefined)
		lines.push(
			`Its price-file compliance grade on this site is ${compliance.grade} (score ${compliance.score} out of 100).`,
		);

	if (cashes.length > 0)
		lines.push(
			`Across the standard basket of ${BASKET.length} common services, it publishes a cash price for ${cashes.length}, with a median of ${money(median(cashes))}.`,
		);
	lines.push(
		`For the exact price of one service, ask about that service and the answer will use its published price.`,
	);

	return { code: "", lines, url: `${SITE_ORIGIN}/hospital/${ccn}` };
}

async function regionSection(
	env: Bindings,
	request: Request,
	code: string,
	place: Place,
	live: Set<string>,
): Promise<Section | null> {
	const detail = await loadProcedure(env, request, code);

	if (!detail) return null;
	const rows = detail.hospitals
		.filter((h) => live.has(h.ccn))
		.filter((h) => !place.state || h.state === place.state);
	const priced = rows
		.filter((h) => h.cash !== null)
		.sort((a, b) => (a.cash ?? 0) - (b.cash ?? 0));
	const scope = place.state
		? `the state named in the question (${place.state})`
		: "the whole country";
	const lines = [
		`Procedure: ${detail.desc} (CPT ${code})`,
		`${detail.stats.hospital_count.toLocaleString("en-US")} hospitals on the site report this procedure. Lowest published discounted cash prices in ${scope}:`,
	];

	if (priced.length > 0)
		lines.push(
			...priced
				.slice(0, TOP_ROWS)
				.map((h, i) =>
					rowLine(
						{
							ccn: h.ccn,
							name: `${h.name} (${titleCase(h.city ?? "")}, ${h.state})`,
							cash: h.cash,
							gross: h.gross,
						},
						i,
					),
				),
		);
	else
		lines.push(
			`No hospital in ${scope} publishes a discounted cash price for this procedure.`,
		);
	lines.push(`Full table for this procedure: ${SITE_ORIGIN}/procedure/${code}`);

	return { code, lines };
}

function titleCase(s: string): string {
	return s
		.toLowerCase()
		.replace(/\b\w/g, (ch) => ch.toUpperCase());
}

// ---------------------------------------------------------------------------
// The search document
// ---------------------------------------------------------------------------

/**
 * Runs the search for one question. Returns null when the question carries no
 * recognisable procedure (a site question, answered from the pages as before),
 * or when the live index cannot be read.
 */
export async function askSearchDoc(
	env: Bindings,
	question: string,
	request: Request,
): Promise<FaqCorpusDoc | null> {
	const codes = parseProcedureCodes(question);
	const live = await liveIndex(env);

	if (!live) return null;
	const place = parsePlace(question);

	if (codes.length === 0) {
		// No procedure named: a named hospital gets its file's profile, and a
		// city gets the overall-value basket ranking. Anything else is a site
		// question, answered from the pages as before.
		const named = parseNamedHospital(question, live.names);
		const section =
			named && live.ccns.has(named.ccn)
				? await hospitalProfileSection(env, request, named.ccn, named.name)
				: place.city
					? await basketSection(env, request, place, live.ccns)
					: null;

		if (!section) return null;

		return {
			title: "Live price search, run for this question",
			url: section.url ?? SITE_ORIGIN,
			text: [
				"These results were computed for this question from the hospitals' own published price files — the same files the hospital and procedure pages show. Answer from these rows and copy the prices whole.",
				...section.lines,
			].join("\n"),
		};
	}

	const descs = new Map<string, string>();

	for (const code of codes) {
		const detail = await loadProcedure(env, request, code);

		if (detail) descs.set(code, detail.desc);
	}

	const named =
		!place.city && !place.state
			? parseNamedHospital(question, live.names)
			: null;
	const sections: Section[] = [];

	if (named && live.ccns.has(named.ccn)) {
		const section = await hospitalSection(
			env,
			request,
			named.ccn,
			named.name,
			codes.filter((c) => descs.has(c)),
			descs,
		);

		if (section) sections.push(section);
	} else if (place.city) {
		for (const code of codes) {
			const section = await citySection(env, request, code, place, live.ccns);

			if (section) sections.push(section);
		}
	} else {
		for (const code of codes) {
			const section = await regionSection(env, request, code, place, live.ccns);

			if (section) sections.push(section);
		}
	}

	if (sections.length === 0) return null;

	return {
		title: "Live price search, run for this question",
		url: sections[0].url ?? `${SITE_ORIGIN}/procedure/${sections[0].code}`,
		text: [
			"These results were computed for this question from the hospitals' own published price files — the same files the hospital and procedure pages show. Answer from these rows and copy the prices whole.",
			...sections.flatMap((s) => s.lines),
		].join("\n"),
	};
}
