/**
 * Tool registry shared by the MCP server (server.ts) and the CLI (cli.ts).
 * Every tool handler takes plain args and returns JSON-serializable data —
 * real data from the repo datasets or the site's public API, never synthetic.
 */

import {
	dataDirStatus,
	fetchHospitalPrices,
	getProcedurePrices,
	hospitalByCcn,
	listStates,
	loadPayerFile,
	loadPayersIndex,
	loadSummary,
	priceIndexEntry,
	searchHospitals,
	searchPayers,
	searchProcedures,
	slimHospital,
} from "./data.js";

export type ArgType = "string" | "number" | "boolean";
export interface ArgDef {
	type: ArgType;
	required?: boolean;
	default?: string | number | boolean;
	description: string;
}
export interface ToolDef {
	description: string;
	args: Record<string, ArgDef>;
	run: (args: Record<string, unknown>) => Promise<unknown> | unknown;
}

function str(v: unknown, d = ""): string {
	return typeof v === "string" ? v : d;
}
function num(v: unknown, d: number): number {
	if (typeof v === "number" && Number.isFinite(v)) return v;
	if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
	return d;
}
function reqStr(v: unknown, name: string): string {
	const s = str(v).trim();
	if (!s) throw new Error(`missing required argument: ${name}`);
	return s;
}

async function tSearchHospitals(a: Record<string, unknown>) {
	return {
		query: str(a.query),
		results: searchHospitals(reqStr(a.query, "query"), str(a.state) || undefined, num(a.limit, 20)),
	};
}

async function tGetHospital(a: Record<string, unknown>) {
	const ccn = reqStr(a.ccn, "ccn").toUpperCase();
	const h = hospitalByCcn(ccn);
	if (!h) return { found: false, ccn };
	const idx = priceIndexEntry(ccn);
	return {
		found: true,
		hospital: {
			...slimHospital(h),
			address: String(h.address ?? ""),
			zip: String(h.zip ?? ""),
			ownership: String(h.ownership ?? ""),
			emergency: Boolean(h.emergency),
			required: Boolean(h.required),
		},
		price_index: idx ?? null,
		prices: await fetchHospitalPrices(ccn, num(a.items_limit, 25), num(a.items_offset, 0)),
	};
}

async function tSearchProcedures(a: Record<string, unknown>) {
	return {
		query: str(a.query),
		results: searchProcedures(reqStr(a.query, "query"), num(a.limit, 20)),
	};
}

async function tGetProcedure(a: Record<string, unknown>) {
	const code = reqStr(a.code, "code");
	const r = getProcedurePrices(code, num(a.limit, 25), num(a.offset, 0));
	return { ...r, pagination: { limit: num(a.limit, 25), offset: num(a.offset, 0) } };
}

async function tGetPriceIndex() {
	const s = loadSummary();
	return {
		generated_at: s.generated_at,
		total_facilities: s.total_facilities,
		cms_required_total: s.cms_required_total,
		live_mrf_total: s.live_mrf_total,
		compliant: s.compliant,
		compliance_pct: s.compliance_pct,
		missing: s.missing,
		standardized_price_hospitals: s.standardized_price_hospitals,
		standardized_price_rows: s.standardized_price_rows,
		cpt_indexed_hospitals: s.cpt_indexed_hospitals,
		cpt_indexed_rows: s.cpt_indexed_rows,
		count_definitions: s.count_definitions,
	};
}

async function tListStates() {
	return { states: listStates() };
}

async function tSearchPayers(a: Record<string, unknown>) {
	return {
		query: str(a.query),
		results: searchPayers(reqStr(a.query, "query"), num(a.limit, 20)),
	};
}

async function tGetPayer(a: Record<string, unknown>) {
	const slug = reqStr(a.slug, "slug").toLowerCase();
	const data = loadPayerFile(slug);
	if (data === null) {
		const { featured } = loadPayersIndex();
		const close = featured
			.filter((p) => p.slug.includes(slug) || slug.includes(p.slug))
			.slice(0, 5)
			.map((p) => p.slug);
		return { found: false, slug, hint: "no per-payer file", similar_slugs: close };
	}
	return { found: true, slug, data };
}

async function tStatus() {
	return {
		data: dataDirStatus(),
		note: "per-hospital price payloads come from the site's public read-only API (live), everything else from bundled repo datasets",
	};
}

export const TOOLS: Record<string, ToolDef> = {
	"search-hospitals": {
		description:
			"Search hospitals by name, city, or 6-digit CMS certification number (CCN). Optional state filter (2-letter code).",
		args: {
			query: { type: "string", required: true, description: "Hospital name, city, or CCN (partial matches ok)" },
			state: { type: "string", description: "2-letter state code, e.g. CA" },
			limit: { type: "number", default: 20, description: "Max results (1-100)" },
		},
		run: tSearchHospitals,
	},
	"get-hospital": {
		description:
			"Full record for one hospital by CCN: directory info, price-index entry with compliance grade, and price payload (items paginated). Price payload is fetched live from the site's public API.",
		args: {
			ccn: { type: "string", required: true, description: "6-digit CMS certification number, e.g. 050441" },
			items_limit: { type: "number", default: 25, description: "Price items per page (1-200)" },
			items_offset: { type: "number", default: 0, description: "Price items page offset" },
		},
		run: tGetHospital,
	},
	"search-procedures": {
		description:
			"Search procedure codes by code prefix (e.g. 992) or description keyword (e.g. mri). Returns hospital counts and gross-charge ranges per code.",
		args: {
			query: { type: "string", required: true, description: "Code prefix or description keyword" },
			limit: { type: "number", default: 20, description: "Max results (1-100)" },
		},
		run: tSearchProcedures,
	},
	"get-procedure": {
		description:
			"Cross-hospital price comparison for one CPT/HCPCS code: per-hospital gross, cash, min/max and payer counts, cheapest first.",
		args: {
			code: { type: "string", required: true, description: "CPT/HCPCS code, e.g. 99213 or J2354" },
			limit: { type: "number", default: 25, description: "Max rows (1-200)" },
			offset: { type: "number", default: 0, description: "Row offset" },
		},
		run: tGetProcedure,
	},
	"get-price-index": {
		description:
			"Dataset-wide price-transparency stats: facility counts, compliance, standardized price rows, CPT-indexed coverage.",
		args: {},
		run: tGetPriceIndex,
	},
	"list-states": {
		description: "All states/territories with hospital counts and how many have published price data.",
		args: {},
		run: tListStates,
	},
	"search-payers": {
		description: "Search health insurers/payers by name. Returns slug, category, hospital count, median rate.",
		args: {
			query: { type: "string", required: true, description: "Payer name, e.g. aetna" },
			limit: { type: "number", default: 20, description: "Max results (1-100)" },
		},
		run: tSearchPayers,
	},
	"get-payer": {
		description: "Detail file for one payer by slug (from search-payers): aliases, rates, hospital list.",
		args: {
			slug: { type: "string", required: true, description: "Payer slug, e.g. aetna" },
		},
		run: tGetPayer,
	},
	status: {
		description: "Show which datasets the server is reading and whether live API access is enabled.",
		args: {},
		run: tStatus,
	},
};

export const TOOL_NAMES = Object.keys(TOOLS);

export async function runTool(name: string, args: Record<string, unknown>): Promise<unknown> {
	const def = TOOLS[name];
	if (!def) {
		const names = TOOL_NAMES.join(", ");
		throw new Error(`unknown tool "${name}". Available: ${names}`);
	}
	// apply defaults
	const withDefaults: Record<string, unknown> = {};
	for (const [k, d] of Object.entries(def.args)) {
		withDefaults[k] = args[k] !== undefined ? args[k] : d.default;
	}
	try {
		return await def.run(withDefaults);
	} catch (e) {
		return { error: e instanceof Error ? e.message : String(e), tool: name };
	}
}
