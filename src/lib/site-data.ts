/**
 * Site-level data (summary counts, hospital registry, data manifest).
 *
 * R2-first. Every artifact the site renders from is an object under the
 * `meta/` prefix of HL_MRF_PARSED, so a producer (the muse.ai refresh task)
 * updates the live site by writing R2 objects — no wrangler deploy, no
 * Worker credentials. The JSON bundled at build time is only the fallback
 * for when R2 has nothing yet (or the binding is unavailable in a test).
 *
 * Keys (see ops/data-contract.md):
 *   meta/summary.json    — the counts the home page + /about-the-numbers render
 *   meta/hospitals.json  — the 5,426-facility registry
 *   meta/manifest.json   — contract_version + generated_at + per-artifact hashes
 */

import bundledSummary from "../../public/data/summary.json" with {
	type: "json",
};
import { readR2Json } from "./r2";

/** The build-time copy: what the pages render when R2 has no valid meta/summary.json. */
export const BUNDLED_SUMMARY = bundledSummary as Summary;

export type Summary = {
	generated_at: string;
	total_facilities?: number;
	cms_required_total: number;
	live_mrf_total?: number;
	compliant: number;
	compliance_pct: number;
	missing?: number;
	under_enforcement?: number;
	missing_with_enforcement?: number;
	enforcement_actions_total: number;
	standardized_price_index_hospitals?: number;
	standardized_price_hospitals?: number;
	standardized_price_rows?: number;
	cpt_indexed_hospitals?: number;
	cpt_indexed_rows?: number;
	zero_price_index_entries?: number;
};

export type Manifest = {
	contract_version: string;
	generated_at: string;
	producer?: string;
	artifacts?: Record<string, { bytes?: number; sha256?: string }>;
	/** Full git SHA of the pipeline commit that produced this data. */
	git_commit?: string;
	/** GitHub repo URL, e.g. https://github.com/barkleesanders/hospital-ledger */
	git_repo?: string;
};

export type SiteDataSource = "r2" | "bundled";

export type Bindings = { HL_MRF_PARSED?: R2Bucket };

// Per-isolate memo so a burst of requests doesn't re-read R2 for the same
// 6 KB object. 60 s is well under the 300 s edge cache on the pages that use
// it, so the memo never makes a page staler than the edge already does.
const MEMO_TTL_MS = 60_000;
let summaryMemo: {
	at: number;
	summary: Summary;
	source: SiteDataSource;
} | null = null;

/**
 * A summary is only trusted from R2 when it carries the fields every page
 * renders unconditionally. A partial upload must not take the home page down
 * — fall back to the bundled copy instead.
 */
export function isValidSummary(v: unknown): v is Summary {
	if (!v || typeof v !== "object") return false;
	const s = v as Record<string, unknown>;
	return (
		typeof s.generated_at === "string" &&
		/^\d{4}-\d{2}-\d{2}T/.test(s.generated_at) &&
		typeof s.cms_required_total === "number" &&
		typeof s.compliant === "number" &&
		typeof s.compliance_pct === "number" &&
		typeof s.enforcement_actions_total === "number"
	);
}

export async function loadSummary(
	env: Bindings,
): Promise<{ summary: Summary; source: SiteDataSource }> {
	const now = Date.now();
	if (summaryMemo && now - summaryMemo.at < MEMO_TTL_MS) {
		return { summary: summaryMemo.summary, source: summaryMemo.source };
	}
	const fromR2 = await readR2Json<unknown>(
		env.HL_MRF_PARSED,
		"meta/summary.json",
	);
	const hit = isValidSummary(fromR2)
		? { summary: fromR2, source: "r2" as const }
		: { summary: BUNDLED_SUMMARY, source: "bundled" as const };
	summaryMemo = { at: now, ...hit };
	return hit;
}

export async function loadManifest(env: Bindings): Promise<Manifest> {
	const m = await readR2Json<Manifest>(env.HL_MRF_PARSED, "meta/manifest.json");
	if (m && typeof m.generated_at === "string") return m;
	// No manifest in R2 yet: describe what IS being served.
	return {
		contract_version: "1",
		generated_at: BUNDLED_SUMMARY.generated_at,
		producer: "bundled (build-time public/data/summary.json)",
	};
}
