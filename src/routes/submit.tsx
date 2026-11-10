/**
 * GET /submit and GET /submit/status/:id — the hospital file-submission
 * flow. The pages are shells: public/submit.js drives the upload state
 * machine against /api/submissions/*, because the file never passes
 * through this Worker's request body (multipart direct to R2).
 *
 * Copy rule (goal hard constraint): nothing here says or implies that
 * submitting a file to Hospital Ledger makes a hospital CMS-compliant.
 * Compliance is posting the file on the hospital's own website; the
 * copy says exactly that, in exactly those terms.
 */

import type { Context } from "hono";
import { Layout } from "../components/Layout";
import type { Env } from "../index";

const inputCls =
	"w-full rounded-md bg-zinc-900 border border-zinc-700 px-3 py-2 text-zinc-100 placeholder:text-zinc-500 focus:border-emerald-500 focus:outline-none";
const labelCls = "block text-sm font-medium text-zinc-300 mb-1";

export function submitPage(url: string) {
	return (
		<Layout
			title="Submit your hospital's price file — Hospital Ledger"
			description="Hospitals: upload your machine-readable price file in any format. Free processing, a plain-language validation report against the CMS template requirements, and a clean CMS-template file to post on your own website."
			ogTitle="Submit your hospital's price file"
			url={url}
			scriptSrc="/submit.js"
		>
			<main class="mx-auto max-w-3xl px-4 sm:px-6 py-10 space-y-8">
				<header>
					<h1 class="serif text-3xl sm:text-4xl md:text-5xl leading-tight">
						Submit your hospital's price file
					</h1>
					<p class="mt-4 text-zinc-400 max-w-2xl">
						If your hospital's machine-readable file is hard to find, in a
						format patients can't use, or not posted yet, send it to us
						directly. We run it through the same parser that built this database
						and hand you back two things, free:
					</p>
					<ul class="mt-4 space-y-2 text-zinc-300 max-w-2xl list-none">
						<li>
							<span class="text-emerald-400 mr-2">▸</span>A plain-language
							validation report — what in your file is missing or invalid
							against the CMS machine-readable file requirements (45 CFR Part
							180, CMS template version 3.0.0), item by item.
						</li>
						<li>
							<span class="text-emerald-400 mr-2">▸</span>A normalized file in
							the CMS template format, built from your own data, ready to
							download and post.
						</li>
					</ul>
				</header>

				<section class="rounded-xl border border-amber-700/40 bg-amber-950/20 p-4 sm:p-5">
					<h2 class="font-semibold text-amber-200">
						Submitting here does not, by itself, meet the CMS requirement
					</h2>
					<p class="mt-2 text-sm text-zinc-300">
						Federal price transparency rules require the file to be posted on{" "}
						<strong>your hospital's own website</strong>. A file that lives only
						here is not compliant. The file we hand back is built for exactly
						that: download it, post it on your site, and it becomes the file our
						harvester — and everyone else — can find like any other hospital's.
					</p>
				</section>

				<section>
					<h2 class="serif text-2xl mb-3">
						Optional: be listed from your submission
					</h2>
					<p class="text-zinc-400 max-w-2xl">
						Most hospitals reach the Ledger when our harvester finds the file on
						their site. If you'd rather be listed straight from this submission,
						check the opt-in box below and prove the submission is really yours:
						after uploading, we'll give you a one-line token to place in a text
						file on your hospital's website. Before anything is listed, the file
						itself must pass the same gates every harvested file passes — the
						identity inside the file has to match the hospital you claim, and
						the file has to be complete. An unverified or mismatched submission
						is still processed (you get the report and the normalized file), but
						it is never listed.
					</p>
				</section>

				<form id="sub-form" class="space-y-5" novalidate>
					<div>
						<label class={labelCls} for="sub-hospital-name">
							Hospital name (as it appears in your file)
						</label>
						<input
							id="sub-hospital-name"
							class={inputCls}
							type="text"
							autocomplete="organization"
							required
						/>
					</div>
					<div class="grid grid-cols-1 sm:grid-cols-2 gap-5">
						<div>
							<label class={labelCls} for="sub-ccn">
								CMS Certification Number (CCN)
							</label>
							<input
								id="sub-ccn"
								class={inputCls}
								type="text"
								inputmode="numeric"
								placeholder="e.g. 050075"
								required
							/>
							<p class="mt-1 text-xs text-zinc-500">
								Six digits, or five digits plus a letter for distinct-part
								units.
							</p>
						</div>
						<div>
							<label class={labelCls} for="sub-website">
								Hospital website (for the listing opt-in)
							</label>
							<input
								id="sub-website"
								class={inputCls}
								type="text"
								inputmode="url"
								placeholder="yourhospital.org"
							/>
						</div>
					</div>
					<div class="grid grid-cols-1 sm:grid-cols-2 gap-5">
						<div>
							<label class={labelCls} for="sub-contact-name">
								Your name
							</label>
							<input
								id="sub-contact-name"
								class={inputCls}
								type="text"
								autocomplete="name"
								required
							/>
						</div>
						<div>
							<label class={labelCls} for="sub-contact-email">
								Your email
							</label>
							<input
								id="sub-contact-email"
								class={inputCls}
								type="email"
								autocomplete="email"
								required
							/>
						</div>
					</div>
					<div>
						<label class={labelCls} for="sub-file">
							Machine-readable file
						</label>
						<input
							id="sub-file"
							class="w-full text-zinc-300 file:mr-3 file:rounded-md file:border file:border-zinc-700 file:bg-zinc-800 file:px-3 file:py-2 file:text-zinc-100 hover:file:bg-zinc-700"
							type="file"
							accept=".json,.csv,.tsv,.txt,.zip,.xlsx,.xls,.xml"
							required
						/>
						<p class="mt-1 text-xs text-zinc-500">
							CSV, JSON, Excel, XML, or a ZIP of any of those — the same formats
							our parser reads everywhere else. Large files are fine: the upload
							goes in pieces, straight to storage.
						</p>
					</div>
					<label class="flex items-start gap-3 text-sm text-zinc-300">
						<input
							id="sub-optin"
							type="checkbox"
							class="mt-0.5 h-4 w-4 accent-emerald-500"
						/>
						<span>
							List my hospital in the Ledger from this submission (requires the
							website verification step after upload — see above).
						</span>
					</label>
					<div>
						<button
							id="sub-submit"
							type="submit"
							class="rounded-md bg-emerald-500 px-5 py-2.5 font-medium text-zinc-950 hover:bg-emerald-400 transition disabled:opacity-50 disabled:cursor-not-allowed"
						>
							Upload and process my file
						</button>
					</div>
					<div id="sub-progress" class="hidden">
						<div class="h-2 rounded bg-zinc-800 overflow-hidden">
							<div
								id="sub-progress-bar"
								class="h-full bg-emerald-500 transition-all"
								style="width:0%"
							/>
						</div>
						<p id="sub-progress-text" class="mt-2 text-sm text-zinc-400" />
					</div>
					<p
						id="sub-outcome"
						role="status"
						aria-live="polite"
						class="text-sm"
					/>
				</form>

				<section
					id="sub-started"
					class="hidden rounded-xl border border-emerald-700/40 bg-emerald-950/20 p-4 sm:p-5 space-y-3"
				>
					<h2 class="font-semibold text-emerald-200">
						Your submission has started
					</h2>
					<p class="text-sm text-zinc-300">
						<strong>Keep your status link.</strong> It is the only way back to
						your report and your normalized file — we don't send email, and the
						link can't be recovered if you lose it.
					</p>
					<p class="text-sm">
						<a
							id="sub-status-link"
							class="text-emerald-300 underline break-all"
							href="#sub-started"
						>
							Your status link will appear here once the upload starts
						</a>
					</p>
					<div id="sub-verify-box" class="hidden space-y-2">
						<p class="text-sm text-zinc-300">
							To finish the listing opt-in, place this exact line in a text file
							on your hospital's website at{" "}
							<code class="text-emerald-300">
								/.well-known/hospital-ledger-verification.txt
							</code>{" "}
							(or{" "}
							<code class="text-emerald-300">
								/hospital-ledger-verification.txt
							</code>
							), then use the check button on your status page:
						</p>
						<pre
							id="sub-verify-token"
							class="overflow-x-auto rounded bg-zinc-900 border border-zinc-800 p-3 text-xs text-emerald-200"
						/>
					</div>
				</section>
			</main>
		</Layout>
	);
}

export function submitStatusPage(url: string, id: string) {
	return (
		<Layout
			title="Submission status — Hospital Ledger"
			description="The processing status, validation report, and downloads for a hospital-submitted price file."
			ogTitle="Submission status"
			url={url}
			scriptSrc="/submit.js"
		>
			<main class="mx-auto max-w-3xl px-4 sm:px-6 py-10 space-y-8">
				<header>
					<h1 class="serif text-3xl sm:text-4xl leading-tight">
						Submission status
					</h1>
					<p class="mt-2 text-sm text-zinc-500">
						Submission <code class="text-zinc-400">{id}</code>
					</p>
				</header>
				<div id="status-root" data-submission-id={id}>
					<p id="status-loading" class="text-zinc-400">
						Loading your submission…
					</p>
					<div id="status-body" class="hidden space-y-8">
						<section>
							<h2 class="serif text-2xl mb-2">Where it stands</h2>
							<p id="status-phase" class="text-zinc-300" />
							<p id="status-detail" class="mt-1 text-sm text-zinc-500" />
						</section>
						<section id="status-downloads" class="hidden space-y-2">
							<h2 class="serif text-2xl mb-2">Your files</h2>
							<p class="text-sm text-zinc-400">
								The normalized file below is built from your data in the CMS
								template format. Posting it on your hospital's own website is
								what the CMS rule requires — a file that lives only here does
								not meet it.
							</p>
							<div class="flex flex-wrap gap-3 pt-1">
								<a
									id="dl-normalized"
									class="hidden rounded-md bg-emerald-500 px-4 py-2 text-sm font-medium text-zinc-950 hover:bg-emerald-400 transition"
									href="#status-downloads"
								>
									Download the CMS-template file
								</a>
								<a
									id="dl-report"
									class="hidden rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800 transition"
									href="#status-downloads"
								>
									Download the validation report (JSON)
								</a>
							</div>
						</section>
						<section id="status-findings" class="hidden">
							<h2 class="serif text-2xl mb-2">What the validation found</h2>
							<p id="findings-summary" class="text-sm text-zinc-400 mb-3" />
							<ul id="findings-list" class="space-y-2 text-sm" />
						</section>
						<section
							id="status-verify"
							class="hidden rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 sm:p-5 space-y-3"
						>
							<h2 class="font-semibold text-zinc-100">
								Verify your hospital's website
							</h2>
							<p class="text-sm text-zinc-400">
								Place this exact line in a text file at{" "}
								<code class="text-emerald-300">
									/.well-known/hospital-ledger-verification.txt
								</code>{" "}
								on your hospital's website (or at{" "}
								<code class="text-emerald-300">
									/hospital-ledger-verification.txt
								</code>
								), then check it:
							</p>
							<pre
								id="status-verify-token"
								class="overflow-x-auto rounded bg-zinc-950 border border-zinc-800 p-3 text-xs text-emerald-200"
							/>
							<div class="flex flex-wrap items-center gap-3">
								<input
									id="verify-domain"
									class="rounded-md bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-zinc-100"
									type="text"
									placeholder="yourhospital.org"
								/>
								<button
									id="verify-check"
									type="button"
									class="rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800 transition disabled:opacity-50"
								>
									Check my website
								</button>
							</div>
							<p
								id="verify-outcome"
								role="status"
								aria-live="polite"
								class="text-sm"
							/>
						</section>
						<section id="status-listing" class="hidden">
							<h2 class="serif text-2xl mb-2">Listing</h2>
							<p id="listing-text" class="text-zinc-300" />
						</section>
					</div>
					<p
						id="status-error"
						role="alert"
						class="hidden text-sm text-red-300"
					/>
				</div>
			</main>
		</Layout>
	);
}

export async function submitPageHandler(c: Context<Env>): Promise<Response> {
	return c.html(submitPage(c.req.url));
}

export async function submitStatusPageHandler(
	c: Context<Env>,
): Promise<Response> {
	return c.html(submitStatusPage(c.req.url, c.req.param("id") ?? ""));
}
