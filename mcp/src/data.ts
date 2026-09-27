/**
 * Data-access layer for the HospitalLedger MCP server + CLI.
 *
 * Reads the repo's bundled static datasets from ../public/data (refreshed by
 * the Tier-3 pipeline; see README-tier3-pipeline.md). Per-hospital price
 * payloads live in R2 and are NOT bundled, so get-hospital fetches them from
 * the site's public read-only JSON API — the same endpoint the homepage
 * client uses (GET /api/prices/:ccn).
 *
 * Env overrides:
 *   HL_DATA_DIR  directory holding hospitals.json etc. (default: <repo>/public/data)
 *   HL_API_BASE  base URL for the live API (default: https://hospitalledger.com)
 *   HL_OFFLINE=1 disable all network access; live-only fields report "unavailable"
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CPT_NAMES } from "../../src/lib/cpt-names.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");

export const DATA_DIR: string =
	process.env.HL_DATA_DIR ?? join(REPO_ROOT, "public", "data");
export const API_BASE: string = (
	process.env.HL_API_BASE ?? "https://hospitalledger.com"
).replace(/\/+$/, "");
export const OFFLINE: boolean = process.env.HL_OFFLINE === "1";

export function dataDirStatus(): { dir: string; exists: boolean; files: string[] } {
	const want = [
		"hospitals.json",
		"prices/index.json",
		"cpt-index.json",
		"payers-index.json",
		"summary.json",
	];
	return {
		dir: DATA_DIR,
		exists: existsSync(DATA_DIR),
		files: want.filter((f) => existsSync(join(DATA_DIR, f))),
	};
}

/* ------------------------------------------------------------------ types */

export interface Hospital {
	ccn?: string;
	name?: string;
	city?: string;
	state?: string;
	address?: string;
	zip?: string;
	type?: string;
	ownership?: string;
	emergency?: boolean;
	rating?: string;
	required?: boolean;
	has_live_mrf?: boolean;
}

export interface SlimHospital {
	ccn: string;
	name: string;
	city: string;
	state: string;
	type: string;
	has_live_mrf: boolean;
	rating: string;
}

export interface PriceIndexEntry {
	ccn: string;
	n: number;
	name: string;
	counts?: Record<string, number>;
	cpt_indexed?: number;
	compliance?: {
		score?: number;
		grade?: string;
		elements?: Record<string, boolean>;
		coverage?: Record<string, number>;
		item_count?: number;
	};
}

export interface CptPriceEntry {
	ccn: string;
	gross: number | null;
	cash: number | null;
	min?: number | null;
	max?: number | null;
	type?: string;
	pc?: number;
	payer_max?: number | null;
}

export interface FeaturedPayer {
	slug: string;
	display: string;
	category?: string;
	hospital_count?: number;
	median_rate?: number | null;
}

/* ---------------------------------------------------------------- loaders */

const cache = new Map<string, unknown>();

function loadJson<T>(rel: string): T {
	if (cache.has(rel)) return cache.get(rel) as T;
	const p = join(DATA_DIR, rel);
	if (!existsSync(p)) {
		throw new Error(`dataset not found: ${p} (override with HL_DATA_DIR)`);
	}
	const v = JSON.parse(readFileSync(p, "utf8")) as T;
	cache.set(rel, v);
	return v;
}

export function loadHospitals(): Hospital[] {
	return loadJson<Hospital[]>("hospitals.json");
}

export function loadPriceIndex(): PriceIndexEntry[] {
	const d = loadJson<{ hospitals: PriceIndexEntry[] }>("prices/index.json");
	return d.hospitals ?? [];
}

export function loadCptIndex(): Record<string, CptPriceEntry[]> {
	return loadJson<Record<string, CptPriceEntry[]>>("cpt-index.json");
}

export function loadSummary(): Record<string, unknown> {
	return loadJson<Record<string, unknown>>("summary.json");
}

export function loadPayersIndex(): {
	featured: FeaturedPayer[];
	total?: number;
	long_tail_count?: number;
} {
	return loadJson("payers-index.json");
}

export function loadPayerFile(slug: string): unknown | null {
	const safe = slug.toLowerCase().replace(/[^a-z0-9-]/g, "");
	if (!safe) return null;
	const p = join(DATA_DIR, "payer", `${safe}.json`);
	if (!existsSync(p)) return null;
	return JSON.parse(readFileSync(p, "utf8")) as unknown;
}

let hospitalByCcnCache: Map<string, Hospital> | null = null;
export function hospitalByCcn(ccn: string): Hospital | undefined {
	if (!hospitalByCcnCache) {
		hospitalByCcnCache = new Map();
		for (const h of loadHospitals()) {
			const key = String(h.ccn ?? "").toUpperCase();
			if (key && !hospitalByCcnCache.has(key)) hospitalByCcnCache.set(key, h);
		}
	}
	return hospitalByCcnCache.get(ccn.trim().toUpperCase());
}

let priceIndexCache: Map<string, PriceIndexEntry> | null = null;
export function priceIndexEntry(ccn: string): PriceIndexEntry | undefined {
	if (!priceIndexCache) {
		priceIndexCache = new Map();
		for (const e of loadPriceIndex()) {
			const key = String(e.ccn ?? "").toUpperCase();
			if (key) priceIndexCache.set(key, e);
		}
	}
	return priceIndexCache.get(ccn.trim().toUpperCase());
}

export function cptDescription(code: string): string | null {
	return CPT_NAMES[code.trim().toUpperCase()] ?? null;
}

/* ----------------------------------------------------------------- search */

export function slimHospital(h: Hospital): SlimHospital {
	return {
		ccn: String(h.ccn ?? ""),
		name: String(h.name ?? ""),
		city: String(h.city ?? ""),
		state: String(h.state ?? ""),
		type: String(h.type ?? ""),
		has_live_mrf: Boolean(h.has_live_mrf),
		rating: String(h.rating ?? ""),
	};
}

export function searchHospitals(
	query: string,
	state: string | undefined,
	limit: number,
): SlimHospital[] {
	const q = query.trim().toLowerCase();
	const qUpper = query.trim().toUpperCase();
	const st = (state ?? "").trim().toUpperCase();
	const scored: Array<{ h: Hospital; score: number }> = [];
	for (const h of loadHospitals()) {
		if (st && String(h.state ?? "").toUpperCase() !== st) continue;
		const name = String(h.name ?? "").toLowerCase();
		const city = String(h.city ?? "").toLowerCase();
		const ccn = String(h.ccn ?? "").toUpperCase();
		let score = -1;
		if (q && ccn === qUpper) score = 100;
		else if (q && name.startsWith(q)) score = 80;
		else if (q && ccn.toLowerCase().startsWith(q)) score = 70;
		else if (q && name.includes(q)) score = 60;
		else if (q && city.startsWith(q)) score = 40;
		else if (q && city.includes(q)) score = 30;
		else if (!q) score = 1;
		if (score >= 0) scored.push({ h, score });
	}
	scored.sort(
		(a, b) =>
			b.score - a.score || String(a.h.name ?? "").localeCompare(String(b.h.name ?? "")),
	);
	return scored.slice(0, Math.max(1, Math.min(limit, 100))).map(({ h }) => slimHospital(h));
}

export interface ProcedureHit {
	code: string;
	description: string | null;
	hospitals: number;
	gross_min: number | null;
	gross_max: number | null;
}

function priceRange(entries: CptPriceEntry[]): { min: number | null; max: number | null } {
	let lo: number | null = null;
	let hi: number | null = null;
	for (const e of entries) {
		const g = typeof e.gross === "number" && Number.isFinite(e.gross) ? e.gross : null;
		if (g === null) continue;
		if (lo === null || g < lo) lo = g;
		if (hi === null || g > hi) hi = g;
	}
	return { min: lo, max: hi };
}

export function searchProcedures(query: string, limit: number): ProcedureHit[] {
	const q = query.trim().toUpperCase();
	const idx = loadCptIndex();
	const out: Array<ProcedureHit & { score: number }> = [];
	for (const code of Object.keys(idx)) {
		const desc = cptDescription(code);
		let score = -1;
		if (q && code === q) score = 100;
		else if (q && code.startsWith(q)) score = 80;
		else if (q && desc && desc.toUpperCase().includes(q)) score = 60;
		else if (!q) score = 1;
		if (score < 0) continue;
		const entries = idx[code];
		const r = priceRange(entries);
		out.push({
			code,
			description: desc,
			hospitals: entries.length,
			gross_min: r.min,
			gross_max: r.max,
			score,
		});
	}
	out.sort((a, b) => b.score - a.score || b.hospitals - a.hospitals || a.code.localeCompare(b.code));
	return out
		.slice(0, Math.max(1, Math.min(limit, 100)))
		.map(({ score: _s, ...rest }) => rest);
}

export interface ProcedurePriceRow extends CptPriceEntry {
	hospital_name: string;
	hospital_city: string;
	hospital_state: string;
}

export function getProcedurePrices(
	code: string,
	limit: number,
	offset: number,
): { code: string; description: string | null; total: number; rows: ProcedurePriceRow[] } {
	const c = code.trim().toUpperCase();
	const entries = loadCptIndex()[c] ?? [];
	const rows: ProcedurePriceRow[] = entries.map((e) => {
		const h = hospitalByCcn(String(e.ccn ?? ""));
		return {
			...e,
			hospital_name: String(h?.name ?? ""),
			hospital_city: String(h?.city ?? ""),
			hospital_state: String(h?.state ?? ""),
		};
	});
	rows.sort((a, b) => {
		const ga = typeof a.gross === "number" ? a.gross : Number.POSITIVE_INFINITY;
		const gb = typeof b.gross === "number" ? b.gross : Number.POSITIVE_INFINITY;
		return ga - gb;
	});
	const lim = Math.max(1, Math.min(limit, 200));
	const off = Math.max(0, offset);
	return {
		code: c,
		description: cptDescription(c),
		total: rows.length,
		rows: rows.slice(off, off + lim),
	};
}

export function listStates(): Array<{ state: string; hospitals: number; with_prices: number }> {
	const counts = new Map<string, number>();
	for (const h of loadHospitals()) {
		const st = String(h.state ?? "").toUpperCase() || "UNKNOWN";
		counts.set(st, (counts.get(st) ?? 0) + 1);
	}
	const priced = new Set<string>();
	for (const e of loadPriceIndex()) priced.add(String(e.ccn ?? "").toUpperCase());
	const pricedByState = new Map<string, number>();
	for (const h of loadHospitals()) {
		if (!priced.has(String(h.ccn ?? "").toUpperCase())) continue;
		const st = String(h.state ?? "").toUpperCase() || "UNKNOWN";
		pricedByState.set(st, (pricedByState.get(st) ?? 0) + 1);
	}
	return [...counts.entries()]
		.map(([state, hospitals]) => ({
			state,
			hospitals,
			with_prices: pricedByState.get(state) ?? 0,
		}))
		.sort((a, b) => b.hospitals - a.hospitals || a.state.localeCompare(b.state));
}

export function searchPayers(query: string, limit: number): FeaturedPayer[] {
	const q = query.trim().toLowerCase();
	const { featured } = loadPayersIndex();
	const scored = [];
	for (const p of featured) {
		const d = String(p.display ?? "").toLowerCase();
		const s = String(p.slug ?? "").toLowerCase();
		let score = -1;
		if (q && (d === q || s === q)) score = 100;
		else if (q && d.startsWith(q)) score = 80;
		else if (q && d.includes(q)) score = 60;
		else if (q && s.includes(q)) score = 50;
		else if (!q) score = 1;
		if (score >= 0) scored.push({ p, score });
	}
	scored.sort(
		(a, b) => b.score - a.score || (b.p.hospital_count ?? 0) - (a.p.hospital_count ?? 0),
	);
	return scored.slice(0, Math.max(1, Math.min(limit, 100))).map(({ p }) => p);
}

/* ------------------------------------------------------- live price fetch */

export interface PricePayload {
	source: string;
	url?: string;
	http_status?: number;
	error?: string;
	reason?: string;
	hospital_name?: string;
	fetched_at?: string;
	n_total_raw?: number;
	n_slim?: number;
	counts?: unknown;
	items?: unknown[];
	pagination?: { offset: number; limit: number; total: number };
}

export async function fetchHospitalPrices(
	ccn: string,
	limit: number,
	offset: number,
): Promise<PricePayload> {
	const c = ccn.trim().toUpperCase();
	if (!/^\d{6}$/.test(c)) {
		return {
			source: "unavailable",
			reason: "live price API requires a 6-digit CMS certification number (CCN)",
		};
	}
	if (OFFLINE) {
		return { source: "unavailable", reason: "HL_OFFLINE=1: network disabled" };
	}
	const url = `${API_BASE}/api/prices/${c}`;
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 25000);
	try {
		const r = await fetch(url, {
			signal: ctrl.signal,
			headers: { "user-agent": "hospital-ledger-mcp/1.0.0" },
		});
		if (!r.ok) {
			return { source: "live-api", url, http_status: r.status, error: `API returned HTTP ${r.status}` };
		}
		const data = (await r.json()) as {
			hospital_name?: string;
			fetched_at?: string;
			n_total_raw?: number;
			n_slim?: number;
			counts?: unknown;
			items?: unknown[];
		};
		const items = Array.isArray(data.items) ? data.items : [];
		const lim = Math.max(1, Math.min(limit, 200));
		const off = Math.max(0, offset);
		return {
			source: "live-api",
			url,
			hospital_name: data.hospital_name,
			fetched_at: data.fetched_at,
			n_total_raw: data.n_total_raw,
			n_slim: data.n_slim,
			counts: data.counts,
			items: items.slice(off, off + lim),
			pagination: { offset: off, limit: lim, total: items.length },
		};
	} catch (e) {
		return { source: "live-api", url, error: e instanceof Error ? e.message : String(e) };
	} finally {
		clearTimeout(timer);
	}
}
