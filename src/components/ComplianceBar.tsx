/**
 * A-F compliance grade badge (used on the hospital page).
 * Mirrors the GRADE_COLOR map from site/_payer_template.html / _hospital_template.html.
 */

import type { FC } from "hono/jsx";

export type Grade = "A" | "B" | "C" | "D" | "F";

const GRADE_COLOR: Record<Grade, string> = {
  A: "text-emerald-300 border-emerald-700/40 bg-emerald-900/20",
  B: "text-emerald-200 border-emerald-700/30 bg-emerald-900/10",
  C: "text-amber-300 border-amber-700/30 bg-amber-900/10",
  D: "text-orange-300 border-orange-700/30 bg-orange-900/10",
  F: "text-rose-300 border-rose-700/40 bg-rose-900/15",
};

export const GradeBadge: FC<{ grade?: string | null; score?: number | null }> = ({ grade, score }) => {
  const g = ((grade ?? "F").toUpperCase() as Grade) in GRADE_COLOR ? ((grade ?? "F").toUpperCase() as Grade) : "F";
  const s = Number(score ?? 0);
  const klass = GRADE_COLOR[g];
  return (
    <span
      class={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs ${klass}`}
      title={`§ 180 compliance score ${s}/100`}
    >
      {g} · {s}
    </span>
  );
};
