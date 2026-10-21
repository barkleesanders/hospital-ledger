/**
 * Site-level counts, R2-first.
 *
 * The muse.ai refresh (Nova) publishes `meta/summary.json` to HL_MRF_PARSED on
 * every wave, so the live counts move without a Worker deploy. The copy bundled
 * at build time (public/data/summary.json) is only the fallback for when R2 has
 * nothing usable, or the binding is absent (tests, build-time FAQ corpus).
 *
 * Measured 2026-10-13: R2 meta/summary.json generated_at 2026-10-14T00:56:57Z,
 * 3,831 price-index hospitals; the bundled copy was 2026-09-27, 3,810.
 */

import bundledSummaryJson from "../../public/data/summary.json";
import { readR2Json } from "./r2";

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

export type SiteDataSource = "r2" | "bundled";

export const bundledSummary: Summary = bundledSummaryJson;

type Bindings = { HL_MRF_PARSED?: R2Bucket };

// Per-isolate memo so a burst of page views does not re-read the same 6 KB
// object. 60 s is under the pages' 300 s edge cache, so it never makes a page
// staler than the cache already allows.
const MEMO_TTL_MS = 60_000;
let memo: { at: number; summary: Summary; source: SiteDataSource } | null =
	null;

/** Test hook: forget the per-isolate memo. */
export function resetSummaryMemo(): void {
	memo = null;
}

/**
 * Trust an R2 summary only when it carries every field the pages render
 * unconditionally; a partial upload must not take the home page down.
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
	if (memo && now - memo.at < MEMO_TTL_MS) {
		return { summary: memo.summary, source: memo.source };
	}
	const fromR2 = await readR2Json<unknown>(
		env.HL_MRF_PARSED,
		"meta/summary.json",
	);
	const hit = isValidSummary(fromR2)
		? { summary: fromR2, source: "r2" as const }
		: { summary: bundledSummary, source: "bundled" as const };
	memo = { at: now, ...hit };
	return hit;
}
