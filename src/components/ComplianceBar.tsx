/**
 * A-F compliance grade badge (used on the hospital page).
 * Mirrors the GRADE_COLOR map from site/_payer_template.html / _hospital_template.html.
 */

import type { FC } from "hono/jsx";
import { complianceRating, type Grade } from "../lib/compliance";

export type { Grade } from "../lib/compliance";

const GRADE_COLOR: Record<Grade, string> = {
	A: "text-emerald-300 border-emerald-700/40 bg-emerald-900/20",
	B: "text-emerald-200 border-emerald-700/30 bg-emerald-900/10",
	C: "text-amber-300 border-amber-700/30 bg-amber-900/10",
	D: "text-orange-300 border-orange-700/30 bg-orange-900/10",
	F: "text-rose-300 border-rose-700/40 bg-rose-900/15",
};

export const GradeBadge: FC<{
	grade?: string | null;
	score?: number | null;
}> = ({ grade, score }) => {
	const rating = complianceRating({ grade, score });
	const klass = rating
		? GRADE_COLOR[rating.grade]
		: "text-zinc-400 border-zinc-700 bg-zinc-900/20";
	return (
		<span
			class={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs ${klass}`}
			title={
				rating
					? `§ 180 compliance score ${rating.score}/100`
					: "Compliance grade unavailable"
			}
		>
			{rating ? `${rating.grade} · ${rating.score}` : "Not graded"}
		</span>
	);
};
