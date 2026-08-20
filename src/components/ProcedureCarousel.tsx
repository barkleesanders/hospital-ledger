/**
 * Editorial procedure carousel — slideable showcase of top U.S. procedures.
 * Replaces the static single-feature card on the home page hero.
 *
 * Pure SSR scroll-snap track + a tiny client controller at /procedure-carousel.js
 * (prev/next buttons, pagination dots, autoplay with pause on hover/interaction,
 * keyboard arrows). Works without JS — degrades to a horizontally scrollable list.
 */

import type { FC } from "hono/jsx";
import { fmtMoney } from "../lib/format";
import { RangeBar } from "./RangeBar";

export type ProcedureSlide = {
	code: string;
	// Eyebrow display name, e.g. "TOTAL HIP REPLACEMENT"
	name: string;
	// Inline noun phrase in the editorial sentence, e.g. "hip replacement"
	short: string;
	min: number;
	median: number;
	max: number;
	// Hospital coverage number shown in the "See all N+" link
	hospitals: number;
	// Optional override for where the median dot sits on the bar (0..1)
	medianFrac?: number;
};

// NOTE: min/median/max are the 5th / 50th / 95th percentile of gross-charge
// across every indexed hospital for the code, computed from /api/cpt-index
// (R2: hl-mrf-parsed/indexes/cpt-index.json). p5/p95 trims junk values like
// $1 placeholders and seven-figure rounding artifacts. `hospitals` is the
// exact distinct-CCN count in the index. Refresh with:
//   curl https://hospitalledger.com/api/cpt-index | python3 scripts/compute_carousel_stats.py
// Last refreshed: 2026-05-18 from production cpt-index.
export const PROCEDURE_SLIDES: ProcedureSlide[] = [
	{
		code: "27130",
		name: "TOTAL HIP REPLACEMENT",
		short: "hip replacement",
		min: 605,
		median: 11048,
		max: 78288,
		hospitals: 1254,
		medianFrac: 0.134,
	},
	{
		code: "27447",
		name: "TOTAL KNEE REPLACEMENT",
		short: "knee replacement",
		min: 1448,
		median: 12519,
		max: 79249,
		hospitals: 1304,
	},
	{
		code: "59409",
		name: "VAGINAL DELIVERY (DELIVERY ONLY)",
		short: "vaginal delivery",
		min: 1085,
		median: 3677,
		max: 15799,
		hospitals: 1409,
	},
	{
		code: "47562",
		name: "GALLBLADDER REMOVAL (LAPAROSCOPIC)",
		short: "gallbladder removal",
		min: 1317,
		median: 10013,
		max: 42610,
		hospitals: 1359,
	},
	{
		code: "45378",
		name: "DIAGNOSTIC COLONOSCOPY",
		short: "colonoscopy",
		min: 487,
		median: 2548,
		max: 9538,
		hospitals: 1544,
	},
	{
		code: "66984",
		name: "CATARACT SURGERY",
		short: "cataract surgery",
		min: 1074,
		median: 5834,
		max: 15651,
		hospitals: 1127,
	},
	{
		code: "70553",
		name: "MRI BRAIN W/ CONTRAST",
		short: "brain MRI",
		min: 705,
		median: 4627,
		max: 11810,
		hospitals: 1677,
	},
	{
		code: "93458",
		name: "CARDIAC CATHETERIZATION",
		short: "cardiac cath",
		min: 2015,
		median: 18065,
		max: 41919,
		hospitals: 1262,
	},
];

export const ProcedureCarousel: FC<{ slides?: ProcedureSlide[] }> = ({
	slides = PROCEDURE_SLIDES,
}) => {
	return (
		<div
			class="procedure-carousel mt-8 relative"
			data-carousel
			data-slide-count={String(slides.length)}
		>
			<section
				class="carousel-track flex overflow-x-auto snap-x snap-mandatory scroll-smooth"
				data-carousel-track
				aria-roledescription="carousel"
				aria-label="Featured procedures"
			>
				{slides.map((s, i) => (
					<a
						href={`/procedure/${s.code}`}
						data-slide-index={String(i)}
						aria-label={`${s.name.toLowerCase()} — see all ${s.hospitals}+ hospitals`}
						class="snap-start shrink-0 basis-full min-w-full block group"
					>
						<div class="rounded-lg border border-zinc-800 bg-zinc-900/40 p-5 hover:border-emerald-700/50 transition mx-px">
							<div class="label-eyebrow text-zinc-500 mb-2">
								FEATURED · {s.name} (CPT {s.code})
							</div>
							<p class="serif text-xl md:text-2xl text-zinc-100 leading-snug max-w-3xl">
								The same {s.short} costs{" "}
								<span class="text-amber-300 tab-num">{fmtMoney(s.min)}</span> at
								one hospital and{" "}
								<span class="text-rose-300 tab-num">{fmtMoney(s.max)}</span> at
								another. Median:{" "}
								<span class="text-emerald-300 tab-num">
									{fmtMoney(s.median)}
								</span>
								.
							</p>
							<RangeBar
								min={s.min}
								median={s.median}
								max={s.max}
								medianFrac={s.medianFrac}
							/>
							<div class="mt-6 text-xs text-zinc-500 group-hover:text-emerald-400 transition">
								See all {s.hospitals.toLocaleString()}+ hospitals →
							</div>
						</div>
					</a>
				))}
			</section>

			<button
				type="button"
				data-carousel-prev
				aria-label="Previous procedure"
				class="hidden sm:flex absolute left-0 top-1/2 -translate-y-[calc(50%+18px)] -translate-x-2 md:-translate-x-4 z-10 h-9 w-9 items-center justify-center rounded-full border border-zinc-700 bg-zinc-900/90 text-zinc-300 hover:border-emerald-500 hover:text-emerald-300 backdrop-blur transition text-lg leading-none"
			>
				‹
			</button>
			<button
				type="button"
				data-carousel-next
				aria-label="Next procedure"
				class="hidden sm:flex absolute right-0 top-1/2 -translate-y-[calc(50%+18px)] translate-x-2 md:translate-x-4 z-10 h-9 w-9 items-center justify-center rounded-full border border-zinc-700 bg-zinc-900/90 text-zinc-300 hover:border-emerald-500 hover:text-emerald-300 backdrop-blur transition text-lg leading-none"
			>
				›
			</button>

			<div
				class="mt-4 flex justify-center gap-1.5"
				data-carousel-dots
				role="tablist"
				aria-label="Procedure slides"
			>
				{slides.map((s, i) => (
					<button
						type="button"
						role="tab"
						data-carousel-dot={String(i)}
						aria-label={`Show ${s.name.toLowerCase()}`}
						aria-selected={i === 0 ? "true" : "false"}
						class={
							"h-1.5 rounded-full transition-all duration-300 cursor-pointer " +
							(i === 0
								? "w-6 bg-emerald-400"
								: "w-1.5 bg-zinc-700 hover:bg-zinc-500")
						}
					/>
				))}
			</div>
		</div>
	);
};
