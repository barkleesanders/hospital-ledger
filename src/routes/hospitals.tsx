/**
 * GET /hospitals and /hospitals/:state — SSR hospital directory.
 *
 * Crawl hub + human browse page: every hospital with published price data,
 * grouped by state. The home page search is client-side, so without this page
 * crawlers had no link path to most /hospital/:ccn pages.
 */

import type { Context } from "hono";
import { Layout, PageHeader } from "../components/Layout";
import type { Env } from "../index";
import type { Bindings } from "../lib/data";
import { readR2Json } from "../lib/r2";
import { titleCase } from "../lib/format";
// Bundled CCN → [city, state] snapshot (see hospital.tsx). R2 gives us the live
// hospital list; this gives us the geography to group it by.
import HOSPITAL_GEO from "../../public/data/hospital-geo.json";

type HospitalEntry = { ccn: string; name: string; city: string; state: string };

const GEO = HOSPITAL_GEO as unknown as Record<string, [string, string]>;
const VALID_STATE = /^[A-Z]{2}$/;

async function loadEntries(
	env: Bindings,
	req: Request,
): Promise<HospitalEntry[] | null> {
	const pricesIndex = await readR2Json<{
		hospitals?: { ccn?: string; name?: string }[];
	}>(env.HL_MRF_PARSED, "prices/index.json");
	let list = pricesIndex?.hospitals;
	if (!list?.length && typeof env.ASSETS?.fetch === "function") {
		// Local/dev or R2 outage fallback: the bundled CMS snapshot, filtered
		// to hospitals with a verified live MRF (same fallback as the sitemap).
		try {
			const url = new URL(req.url);
			url.pathname = "/data/hospitals.json";
			url.search = "";
			const resp = await env.ASSETS.fetch(url.toString());
			if (resp.ok) {
				const arr = (await resp.json()) as Array<{
					ccn?: string;
					name?: string;
					has_live_mrf?: boolean;
				}>;
				list = arr.filter((h) => h.has_live_mrf);
			}
		} catch {
			// fall through to null
		}
	}
	if (!list?.length) return null;
	const entries: HospitalEntry[] = [];
	for (const h of list) {
		const ccn = String(h.ccn ?? "");
		if (!ccn) continue;
		const g = GEO[ccn];
		entries.push({
			ccn,
			name: h.name || `Hospital ${ccn}`,
			city: g?.[0] ?? "",
			state: g?.[1] ?? "",
		});
	}
	return entries;
}

const stateName: Record<string, string> = {
	AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California",
	CO: "Colorado", CT: "Connecticut", DE: "Delaware", DC: "District of Columbia",
	FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois",
	IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana",
	ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan",
	MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana",
	NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey",
	NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota",
	OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania",
	RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota",
	TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia",
	WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
	PR: "Puerto Rico", GU: "Guam", VI: "Virgin Islands",
	AS: "American Samoa", MP: "Northern Mariana Islands",
};

function byState(entries: HospitalEntry[]): Map<string, HospitalEntry[]> {
	const m = new Map<string, HospitalEntry[]>();
	for (const e of entries) {
		const st = e.state || "UNKNOWN";
		if (!m.has(st)) m.set(st, []);
		m.get(st)!.push(e);
	}
	for (const list of m.values()) {
		list.sort((a, b) => a.name.localeCompare(b.name));
	}
	return m;
}

export async function hospitalsIndexHandler(c: Context<Env>) {
	const url = "https://hospitalledger.com/hospitals";
	const entries = await loadEntries(c.env, c.req.raw);
	if (!entries) {
		return c.html(
			<Layout
				title="Hospitals — Hospital Ledger"
				description="Browse every U.S. hospital with published price transparency data."
				url={url}
			>
				<PageHeader eyebrow="Directory" />
				<main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
					<h1 class="text-2xl font-semibold">Hospital directory</h1>
					<p class="mt-2 text-zinc-400">Directory temporarily unavailable.</p>
				</main>
			</Layout>,
			503,
		);
	}
	const grouped = byState(entries);
	const states = [...grouped.keys()].sort();
	const total = entries.length;
	return c.html(
		<Layout
			title={`All ${total.toLocaleString("en-US")} hospitals with price data — Hospital Ledger`}
			description={`Browse all ${total.toLocaleString("en-US")} U.S. hospitals with published price transparency data, by state. Free, no signup.`}
			url={url}
		>
			<PageHeader eyebrow="Directory" />
			<main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
				<h1 class="text-2xl sm:text-3xl md:text-4xl font-semibold">
					Hospitals with published prices
				</h1>
				<p class="mt-2 text-zinc-400">
					{total.toLocaleString("en-US")} hospitals · grouped by state · data from
					each hospital's federally-mandated machine-readable file
				</p>
				<div class="mt-8 grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
					{states.map((st) => (
						<a
							href={`/hospitals/${st}`}
							class="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4 hover:border-emerald-700 hover:bg-zinc-900"
						>
							<div class="font-semibold">{st === "UNKNOWN" ? "Unknown state" : (stateName[st] ?? st)}</div>
							<div class="text-sm text-zinc-400">
								{(grouped.get(st) ?? []).length.toLocaleString("en-US")}{" "}
								hospitals
							</div>
						</a>
					))}
				</div>
			</main>
		</Layout>,
		200,
		{ "cache-control": "public, max-age=3600" },
	);
}

export async function hospitalsStateHandler(c: Context<Env>) {
	const st = String(c.req.param("state") ?? "").toUpperCase();
	const url = `https://hospitalledger.com/hospitals/${st}`;
	if (!VALID_STATE.test(st)) {
		return c.html(
			<Layout
				title="Hospitals — Hospital Ledger"
				description="Browse every U.S. hospital with published price transparency data."
				url={url}
			>
				<PageHeader eyebrow="Directory" />
				<main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
					<h1 class="text-2xl font-semibold">Unknown state</h1>
					<p class="mt-2 text-zinc-400">
						<a href="/hospitals" class="underline">Browse all states</a>
					</p>
				</main>
			</Layout>,
			404,
		);
	}
	const entries = await loadEntries(c.env, c.req.raw);
	if (!entries) {
		return c.html(
			<Layout
				title="Hospitals — Hospital Ledger"
				description="Browse every U.S. hospital with published price transparency data."
				url={url}
			>
				<PageHeader eyebrow="Directory" />
				<main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
					<h1 class="text-2xl font-semibold">Hospital directory</h1>
					<p class="mt-2 text-zinc-400">Directory temporarily unavailable.</p>
				</main>
			</Layout>,
			503,
		);
	}
	const list = byState(entries).get(st) ?? [];
	const label = st === "UNKNOWN" ? "Unknown state" : (stateName[st] ?? st);
	if (!list.length) {
		return c.html(
			<Layout
				title={`${label} hospitals — Hospital Ledger`}
				description={`Hospitals in ${label} with published price transparency data.`}
				url={url}
			>
				<PageHeader eyebrow="Directory" />
				<main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
					<h1 class="text-2xl font-semibold">No hospitals in {label}</h1>
					<p class="mt-2 text-zinc-400">
						<a href="/hospitals" class="underline">Browse all states</a>
					</p>
				</main>
			</Layout>,
			404,
		);
	}
	return c.html(
		<Layout
			title={`${label}: ${list.length.toLocaleString("en-US")} hospitals with published prices — Hospital Ledger`}
			description={`Every hospital in ${label} with published price transparency data — compare prices, cash rates, and compliance grades. Free, no signup.`}
			url={url}
		>
			<PageHeader eyebrow="Directory" />
			<main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
				<div class="text-sm text-zinc-400">
					<a href="/hospitals" class="hover:text-zinc-200">← All states</a>
				</div>
				<h1 class="mt-2 text-2xl sm:text-3xl md:text-4xl font-semibold">
					{label} hospitals
				</h1>
				<p class="mt-2 text-zinc-400">
					{list.length.toLocaleString("en-US")} hospitals with published price
					transparency data
				</p>
				<ul class="mt-8 grid grid-cols-1 sm:grid-cols-2 gap-2">
					{list.map((e) => (
						<li>
							<a
								href={`/hospital/${e.ccn}`}
								class="block rounded-lg border border-zinc-800 bg-zinc-900/40 px-4 py-3 hover:border-emerald-700 hover:bg-zinc-900"
							>
								<div class="font-medium">{titleCase(e.name)}</div>
								{e.city ? (
									<div class="text-sm text-zinc-400">{titleCase(e.city)}</div>
								) : null}
							</a>
						</li>
					))}
				</ul>
			</main>
		</Layout>,
		200,
		{ "cache-control": "public, max-age=3600" },
	);
}
