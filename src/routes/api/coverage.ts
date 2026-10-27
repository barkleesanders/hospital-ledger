import type { Context } from "hono";
import type { Env } from "../../index";
import { priceCoverage } from "../../lib/price-coverage";
import { loadSummary } from "../../lib/site-data";

export async function coverageHandler(c: Context<Env>): Promise<Response> {
	const { summary, source } = await loadSummary(c.env);
	const coverage = priceCoverage(summary);
	if (!coverage)
		return c.json(
			{
				status: "unavailable",
				reason:
					"A consistent registry and price-index breakdown is not available.",
			},
			503,
			{
				"cache-control": "no-store",
			},
		);
	return c.json(
		{
			status: "available",
			generated_at: summary.generated_at,
			source,
			cms_required_live_hospitals: summary.compliant,
			all_price_hospitals: summary.standardized_price_hospitals,
			...coverage,
		},
		200,
		{ "cache-control": "public, max-age=60" },
	);
}
