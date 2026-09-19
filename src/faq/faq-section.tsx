/**
 * "Ask anything" row — the server-rendered half of the infinite-FAQ drop-in
 * (~/.claude/skills/infinite-faq). Placements on this site:
 *
 *   hero       — the home page, directly under the hero's lead paragraph
 *                (src/routes/home.tsx), the first interactive thing on the page.
 *   hospital   — /hospital/:ccn, under the compliance card, carrying the CCN as a
 *                hidden `context` so the answer is about THAT hospital
 *                (src/faq/faq-corpus.ts recordContextDoc re-reads it from R2/ASSETS).
 *   procedure  — /procedure/:code, under the three price cards, with the code.
 *   payer      — /payer/:slug, under the stat cards, with the slug.
 *
 * The markup is a real <form method="post"> so it works with JavaScript off:
 * POST /api/faq/ask answers a form submit as text/plain. With JS on,
 * public/faq-island.js (built from src/faq/faq-island.tsx) finds #faq-ask and
 * replaces the form with the streaming version, reading its copy from the
 * data-* attributes.
 */

import type { FC } from "hono/jsx";
import type { RecordContext } from "./faq-route";

export const FAQ_ASK_ACTION = "/api/faq/ask";

export const FAQ_ASK_HOST_ID = "faq-ask";

export type FaqAskRowProps = {
	variant: "hero" | "hospital" | "procedure" | "payer";
	/** Detail placements only: the record on the page, as its URL names it. */
	context?: RecordContext;
};

const COPY: Record<
	FaqAskRowProps["variant"],
	{ placeholder: string; loading: string }
> = {
	hero: {
		// Not "charity care": the site has no charity-care data (0 hits in src/ and
		// README on 2026-09-18), so a placeholder naming it would promise answers the
		// corpus cannot give.
		placeholder: "Ask anything about a hospital's prices or this site",
		loading: "Reading the site",
	},
	hospital: {
		placeholder: "Ask anything about this hospital's prices or compliance",
		loading: "Reading this hospital's page",
	},
	procedure: {
		placeholder: "Ask anything about this procedure's prices",
		loading: "Reading this procedure's page",
	},
	payer: {
		placeholder: "Ask anything about this insurance's negotiated rates",
		loading: "Reading this plan's page",
	},
};

/** The #faq-ask host + no-JS form. Link /faq.css and load /faq-island.js on the page. */
export const FaqAskRow: FC<FaqAskRowProps> = ({ variant, context }) => {
	const copy = COPY[variant];
	// Only a record the page itself put here travels with the question; the route
	// validates the shape again and re-reads the record from the store.
	const ctx =
		variant !== "hero" && context
			? JSON.stringify({ kind: context.kind, id: context.id })
			: "";

	return (
		<div
			id={FAQ_ASK_HOST_ID}
			class={`faq-ask faq-ask--${variant}`}
			data-action={FAQ_ASK_ACTION}
			data-placeholder={copy.placeholder}
			data-loading={copy.loading}
			data-send="Send"
			data-stop="Stop"
			data-context={ctx || undefined}
		>
			<form class="faq-ask-form" method="post" action={FAQ_ASK_ACTION}>
				<label for="faq-ask-input" class="faq-sr-only">
					{copy.placeholder}
				</label>
				<input
					id="faq-ask-input"
					class="faq-ask-input"
					type="text"
					name="question"
					placeholder={copy.placeholder}
					maxlength={600}
					autocomplete="off"
					required
				/>
				{ctx ? <input type="hidden" name="context" value={ctx} /> : null}
				<button type="submit" class="faq-ask-btn is-ready" aria-label="Send">
					<svg
						width="16"
						height="16"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="3"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M5 12h14" />
						<path d="m12 5 7 7-7 7" />
					</svg>
				</button>
			</form>
		</div>
	);
};
