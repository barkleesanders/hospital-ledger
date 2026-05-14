/**
 * Min / median / max horizontal range bar. Used on the home featured spread
 * (the "$3,124 ⟷ $182,439" hip replacement viz). Pure SSR — no JS needed.
 */

import type { FC } from "hono/jsx";
import { fmtMoney } from "../lib/format";

export type RangeBarProps = {
  min: number;
  median: number;
  max: number;
  // 0..1 fraction of the bar at which to draw the median dot.
  // Defaults to a log-scaled position when omitted.
  medianFrac?: number;
};

function logFrac(min: number, median: number, max: number): number {
  if (max <= min) return 0;
  const lo = Math.log10(Math.max(1, min));
  const hi = Math.log10(Math.max(1, max));
  const md = Math.log10(Math.max(1, median));
  if (hi <= lo) return 0;
  return Math.max(0, Math.min(1, (md - lo) / (hi - lo)));
}

export const RangeBar: FC<RangeBarProps> = ({ min, median, max, medianFrac }) => {
  const frac = medianFrac ?? logFrac(min, median, max);
  const leftPct = (frac * 100).toFixed(1);
  return (
    <>
      <div class="mt-5 relative h-10">
        <div class="absolute inset-x-0 top-1/2 h-px bg-zinc-700" />
        <div class="absolute left-0 top-1/2 -translate-y-1/2 w-2.5 h-2.5 rounded-full bg-amber-300 ring-2 ring-amber-300/30" />
        <div class="absolute right-0 top-1/2 -translate-y-1/2 w-2.5 h-2.5 rounded-full bg-rose-300 ring-2 ring-rose-300/30" />
        <div
          class="absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-emerald-300 ring-2 ring-emerald-300/30"
          style={`left: ${leftPct}%;`}
        />
        <div class="absolute left-0 top-full mt-1 text-[10px] tab-num text-amber-300/80">{fmtMoney(min)}</div>
        {/* Median label: hidden on mobile (collides with min/max), inline on sm+ */}
        <div
          class="hidden sm:block absolute top-full mt-1 text-[10px] tab-num text-emerald-300/90 -translate-x-1/2"
          style={`left: ${leftPct}%;`}
        >
          {fmtMoney(median)} · median
        </div>
        <div class="absolute right-0 top-full mt-1 text-[10px] tab-num text-rose-300/80">{fmtMoney(max)}</div>
      </div>
      {/* Mobile: median below the bar so labels don't collide */}
      <div class="sm:hidden mt-6 text-center text-[11px] tab-num text-emerald-300/90">
        {fmtMoney(median)} · median
      </div>
    </>
  );
};
