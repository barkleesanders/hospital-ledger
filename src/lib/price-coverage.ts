import { z } from "zod";
import { HOSPITAL_ID_PATTERN } from "./hospital-id";

const count = z.number().int().nonnegative();

export const PriceCoverageSchema = z.object({
	required_live_price_hospitals: count,
	required_live_zero_price_hospitals: count,
	required_live_unindexed_hospitals: count,
	price_hospitals_outside_required_live: count,
});

export type PriceCoverage = z.infer<typeof PriceCoverageSchema>;

const registrySchema = z.array(
	z.object({
		ccn: z.string().regex(HOSPITAL_ID_PATTERN),
		required: z.boolean(),
		has_live_mrf: z.boolean(),
	}),
);

const indexSchema = z.object({
	hospitals: z.array(
		z.object({
			ccn: z.string().regex(HOSPITAL_ID_PATTERN),
			n: count,
		}),
	),
});

/** Join identifiers, never subtract totals belonging to different populations. */
export function derivePriceCoverage(registry: unknown, index: unknown) {
	const hospitals = registrySchema.safeParse(registry);
	const prices = indexSchema.safeParse(index);
	if (!hospitals.success || !prices.success) return null;
	const requiredLive = new Set<string>();
	const registryIds = new Set<string>();
	for (const h of hospitals.data) {
		if (registryIds.has(h.ccn)) return null;
		registryIds.add(h.ccn);
		if (h.required && h.has_live_mrf) requiredLive.add(h.ccn);
	}
	const indexed = new Map<string, number>();
	let positive = 0;
	let outside = 0;
	let covered = 0;
	let zero = 0;
	for (const p of prices.data.hospitals) {
		if (indexed.has(p.ccn)) return null;
		indexed.set(p.ccn, p.n);
		if (p.n > 0) {
			positive++;
			if (requiredLive.has(p.ccn)) covered++;
			else outside++;
		} else if (requiredLive.has(p.ccn)) zero++;
	}
	return {
		required_live_price_hospitals: covered,
		required_live_zero_price_hospitals: zero,
		required_live_unindexed_hospitals: requiredLive.size - covered - zero,
		price_hospitals_outside_required_live: outside,
		registry_hospitals: registryIds.size,
		required_live_hospitals: requiredLive.size,
		index_hospitals: indexed.size,
		price_hospitals: positive,
	};
}

/** An absent or contradictory breakdown is unknown, never a zero-sized gap. */
export function priceCoverage(
	summary: Partial<PriceCoverage> & {
		compliant: number;
		standardized_price_hospitals?: number;
		standardized_price_index_hospitals?: number;
		zero_price_index_entries?: number;
	},
): PriceCoverage | null {
	const parsed = PriceCoverageSchema.safeParse(summary);
	if (!parsed.success) return null;
	const c = parsed.data;
	if (
		c.required_live_price_hospitals +
			c.required_live_zero_price_hospitals +
			c.required_live_unindexed_hospitals !==
			summary.compliant ||
		c.required_live_price_hospitals +
			c.price_hospitals_outside_required_live !==
			summary.standardized_price_hospitals
	)
		return null;
	const indexed = summary.standardized_price_index_hospitals;
	const priced = summary.standardized_price_hospitals;
	if (
		indexed !== undefined &&
		priced !== undefined &&
		(!Number.isSafeInteger(indexed) ||
			indexed < priced ||
			c.required_live_zero_price_hospitals > indexed - priced)
	)
		return null;
	if (
		summary.zero_price_index_entries !== undefined &&
		(!Number.isSafeInteger(summary.zero_price_index_entries) ||
			c.required_live_zero_price_hospitals > summary.zero_price_index_entries)
	)
		return null;
	return c;
}
