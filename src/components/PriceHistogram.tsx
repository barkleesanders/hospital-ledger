/**
 * SSR price histogram — log-scale, 32 bins, median bar in emerald, outlier
 * dot strip below. Direct port of the inline drawHistogram() function in
 * site/_procedure_template.html lines ~318-395. Output is plain SVG so
 * it works without JS and the page is already useful before hydration.
 */

import type { FC } from "hono/jsx";
import { fmtMoney } from "../lib/format";

const NBINS = 32;
const W = 1100;
const H = 220;
const PADDING = { l: 50, r: 30, t: 20, b: 38 };
const innerW = W - PADDING.l - PADDING.r;
const innerH = H - PADDING.t - PADDING.b;

export type HospitalPoint = {
  cash: number | null;
  name: string;
  quality?: string | null;
};

export type PriceHistogramProps = {
  hospitals: HospitalPoint[];
  cashMedian: number | null;
};

type Tick = { x: number; label: string };

function buildTicks(logLo: number, logHi: number): Tick[] {
  const ticks: Tick[] = [];
  const range = logHi - logLo;
  if (!Number.isFinite(range) || range <= 0) return ticks;
  for (let p = Math.ceil(logLo); p <= Math.floor(logHi); p++) {
    const v = 10 ** p;
    const x = PADDING.l + ((p - logLo) / range) * innerW;
    const label = "$" + (v >= 1000 ? `${(v / 1000).toLocaleString("en-US")}K` : v.toLocaleString("en-US"));
    ticks.push({ x, label });
  }
  return ticks;
}

export const PriceHistogram: FC<PriceHistogramProps> = ({ hospitals, cashMedian }) => {
  const cashVals = hospitals
    .filter((h) => h.quality === "normal" && h.cash !== null && h.cash > 0)
    .map((h) => h.cash as number);

  if (cashVals.length < 5 || cashMedian === null || !Number.isFinite(cashMedian)) {
    return (
      <div class="text-zinc-500 text-sm py-8 text-center">Not enough data to chart.</div>
    );
  }

  const lo = Math.max(1, Math.min(...cashVals));
  const hi = Math.max(...cashVals);
  const logLo = Math.log10(lo);
  const logHi = Math.log10(hi);
  const step = (logHi - logLo) / NBINS;
  const bins = new Array<number>(NBINS).fill(0);
  for (const v of cashVals) {
    const i = Math.min(NBINS - 1, Math.floor((Math.log10(v) - logLo) / step));
    bins[i]! += 1;
  }
  const maxCount = Math.max(...bins);
  const medBin = Math.min(NBINS - 1, Math.floor((Math.log10(cashMedian) - logLo) / step));

  const xFor = (i: number): number => PADDING.l + (i / NBINS) * innerW;
  const xWidth = innerW / NBINS - 2;
  const yFor = (c: number): number => PADDING.t + innerH - (c / maxCount) * innerH;

  const lowOutliers = hospitals.filter((h) => h.quality === "low_outlier" && h.cash !== null) as Array<HospitalPoint & { cash: number }>;
  const highOutliers = hospitals.filter((h) => h.quality === "high_outlier" && h.cash !== null) as Array<HospitalPoint & { cash: number }>;
  const allValues = [...cashVals, ...lowOutliers.map((h) => h.cash), ...highOutliers.map((h) => h.cash)];
  const fullLo = Math.max(1, Math.min(...allValues));
  const fullHi = Math.max(...allValues);
  const fullLogLo = Math.log10(fullLo);
  const fullLogHi = Math.log10(fullHi);
  const fullRange = fullLogHi - fullLogLo;
  const xForVal = (v: number): number => {
    if (!Number.isFinite(fullRange) || fullRange <= 0) return PADDING.l;
    return PADDING.l + ((Math.log10(Math.max(1, v)) - fullLogLo) / fullRange) * innerW;
  };

  const ticks = buildTicks(logLo, logHi);
  const medX = xForVal(cashMedian);
  const stripY = PADDING.t + innerH + 14;
  const axisY = PADDING.t + innerH;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      style="width:100%;height:auto;font-family:'Inter',sans-serif"
    >
      <line x1={PADDING.l} y1={axisY} x2={W - PADDING.r} y2={axisY} stroke="#3f3f46" stroke-width="1" />
      {bins.map((c, i) => {
        if (c === 0) return null;
        const isMed = i === medBin;
        return (
          <rect
            x={xFor(i)}
            y={yFor(c)}
            width={xWidth}
            height={(c / maxCount) * innerH}
            fill={isMed ? "#6ee7b7" : "#52525b"}
            opacity={isMed ? 1 : 0.85}
          />
        );
      })}
      <line
        x1={medX}
        y1={PADDING.t}
        x2={medX}
        y2={axisY + 6}
        stroke="#6ee7b7"
        stroke-width="1"
        stroke-dasharray="3,3"
        opacity="0.7"
      />
      <text
        x={medX}
        y={PADDING.t - 6}
        text-anchor="middle"
        fill="#6ee7b7"
        font-size="11"
        font-family="'JetBrains Mono',monospace"
      >
        median {fmtMoney(cashMedian)}
      </text>
      {lowOutliers.map((h) => (
        <circle cx={xForVal(h.cash)} cy={stripY} r="2.5" fill="#fcd34d" opacity="0.7">
          <title>{`${h.name} — ${fmtMoney(h.cash)} (low outlier)`}</title>
        </circle>
      ))}
      {highOutliers.map((h) => (
        <circle cx={xForVal(h.cash)} cy={stripY} r="2.5" fill="#fda4af" opacity="0.7">
          <title>{`${h.name} — ${fmtMoney(h.cash)} (high outlier)`}</title>
        </circle>
      ))}
      {ticks.map((t) => (
        <>
          <line x1={t.x} y1={axisY} x2={t.x} y2={axisY + 4} stroke="#52525b" stroke-width="1" />
          <text
            x={t.x}
            y={axisY + 28}
            text-anchor="middle"
            fill="#71717a"
            font-size="11"
            font-family="'JetBrains Mono',monospace"
          >
            {t.label}
          </text>
        </>
      ))}
      <text
        x={PADDING.l}
        y={PADDING.t - 6}
        fill="#71717a"
        font-size="10"
        font-family="'Inter',sans-serif"
        letter-spacing="0.1em"
      >
        HOSPITALS →
      </text>
    </svg>
  );
};
