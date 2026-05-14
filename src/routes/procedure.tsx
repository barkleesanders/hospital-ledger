/**
 * GET /procedure/:code — SSR procedure page.
 *
 * Renders the editorial hero + 3 narrative number cards + SSR price
 * histogram + filter form + hospital table. The filter/sort interactions
 * still hydrate via /procedure-client.js so users get the SSR view first
 * (and SEO bots see the full content) and the dropdowns still work.
 *
 * Replaces site/_procedure_template.html + site/functions/procedure/[code].js.
 */

import type { Context } from "hono";
import type { Env } from "../index";
import { EditorialHero } from "../components/EditorialHero";
import { Layout, PageHeader } from "../components/Layout";
import { PriceHistogram } from "../components/PriceHistogram";
import { lookupCptName } from "../lib/cpt-names";
import { loadProcedure, type ProcedureData } from "../lib/data";
import { escapeHtml, fmtMoney, titleCase } from "../lib/format";

const VALID_CODE = /^[A-Z0-9][A-Z0-9-]{1,9}$/i;

function notFoundPage(code: string, url: string){
  const fallbackName = lookupCptName(code) ?? `Procedure ${code}`;
  return (
    <Layout
      title={`Procedure ${code} not indexed — Hospital Ledger`}
      description={`No price data available for procedure ${code}.`}
      url={url}
    >
      <PageHeader eyebrow="Procedure" />
      <main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
        <h1 class="serif text-4xl sm:text-5xl md:text-6xl text-zinc-50">{fallbackName}</h1>
        <div class="label-eyebrow mt-3">
          <span class="mono text-zinc-300">{escapeHtml(code)}</span> · not in our top-10,000 index
        </div>
        <div class="mt-8 rounded-lg border border-amber-700/40 bg-amber-900/15 p-4 text-amber-100 max-w-2xl">
          <div class="font-semibold mb-1">No price data for code <span class="mono">{escapeHtml(code)}</span></div>
          <div class="text-sm text-amber-200/90">
            We index the top 10,000 most-coverage CPT/HCPCS codes. This code may be:
            <ul class="list-disc ml-5 mt-1 space-y-0.5">
              <li>A rare or specialty procedure not in our top 10k</li>
              <li>A typo (CPT codes are 5 digits; HCPCS are letter + 4 digits)</li>
              <li>A non-CPT charge code unique to one hospital</li>
            </ul>
            <div class="mt-2">
              <a href="/" class="inline-block rounded-md bg-amber-700/30 hover:bg-amber-700/50 px-3 py-1 text-amber-50">
                ← Search by name on the home page
              </a>
            </div>
          </div>
        </div>
      </main>
    </Layout>
  );
}

function procedureName(data: ProcedureData): string {
  const desc = (data.desc || lookupCptName(data.code) || "").replace(/\s+/g, " ").trim();
  const raw = desc || data.code;
  return titleCase(raw);
}

function ledeMarkup(data: ProcedureData){
  const { cash_min, cash_max, cash_p50 } = data.stats;
  if (!cash_min || !cash_max || !cash_p50) return null;
  const spread = (cash_max / cash_min).toFixed(0);
  return (
    <>
      The same procedure costs <span class="text-amber-300 tab-num">{fmtMoney(cash_min)}</span> at one
      hospital and <span class="text-rose-300 tab-num">{fmtMoney(cash_max)}</span> at another — a{" "}
      <strong class="text-zinc-100">{spread}× spread</strong> for what's billed under the same code.
      Median: <span class="text-emerald-300 tab-num">{fmtMoney(cash_p50)}</span>.
    </>
  );
}

function hospitalCards(data: ProcedureData) {
  const rows = data.hospitals.slice(0, 500);
  if (!rows.length) {
    return <div class="px-3 py-6 text-center text-zinc-500 text-sm">No matches.</div>;
  }
  const med = data.stats.cash_p50 ?? 0;
  const sorted = [...rows].sort((a, b) => {
    const qa = a.quality === "normal" ? 0 : 1;
    const qb = b.quality === "normal" ? 0 : 1;
    if (qa !== qb) return qa - qb;
    const da = a.cash !== null ? Math.abs((a.cash as number) - med) : Infinity;
    const db = b.cash !== null ? Math.abs((b.cash as number) - med) : Infinity;
    return da - db;
  });
  return sorted.map((h) => {
    const isLow = h.quality === "low_outlier";
    const isHigh = h.quality === "high_outlier";
    const cashClass = isLow || isHigh ? "text-amber-200" : "text-emerald-300";
    const flagBadge = isLow ? (
      <span class="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-amber-900/30 text-amber-300 border border-amber-700/40">⚠ check</span>
    ) : isHigh ? (
      <span class="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-amber-900/30 text-amber-300 border border-amber-700/40">⚠ high</span>
    ) : null;
    return (
      <a
        href={`/hospital/${h.ccn}`}
        class={`block rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 hover:border-emerald-700/50 transition ${isLow || isHigh ? "opacity-70" : ""}`}
      >
        <div class="flex items-baseline justify-between gap-2 mb-2 min-w-0">
          <div class="font-medium text-emerald-300 truncate text-sm">{h.name}</div>
          <div class="text-xs text-zinc-500 shrink-0">{h.state}</div>
        </div>
        <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <div class="text-zinc-500">Cash</div>
          <div class={`text-right tab-num text-base font-medium ${cashClass}`}>
            {fmtMoney(h.cash)}
            {flagBadge}
          </div>
          <div class="text-zinc-500">Gross</div>
          <div class="text-right tab-num text-zinc-400">{fmtMoney(h.gross)}</div>
        </div>
      </a>
    );
  });
}

function hospitalRows(data: ProcedureData) {
  const rows = data.hospitals.slice(0, 500);
  if (!rows.length) {
    return (
      <tr>
        <td colspan={7} class="px-3 py-6 text-center text-zinc-500">
          No matches.
        </td>
      </tr>
    );
  }
  const med = data.stats.cash_p50 ?? 0;
  // Server-side default sort matches client default: median-first
  const sorted = [...rows].sort((a, b) => {
    const qa = a.quality === "normal" ? 0 : 1;
    const qb = b.quality === "normal" ? 0 : 1;
    if (qa !== qb) return qa - qb;
    const da = a.cash !== null ? Math.abs((a.cash as number) - med) : Infinity;
    const db = b.cash !== null ? Math.abs((b.cash as number) - med) : Infinity;
    return da - db;
  });
  return sorted.map((h) => {
    const isLow = h.quality === "low_outlier";
    const isHigh = h.quality === "high_outlier";
    const cashClass = isLow || isHigh ? "text-amber-200" : "text-emerald-300 font-medium";
    const flagBadge = isLow ? (
      <span class="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-amber-900/30 text-amber-300 border border-amber-700/40">
        ⚠ check
      </span>
    ) : isHigh ? (
      <span class="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-amber-900/30 text-amber-300 border border-amber-700/40">
        ⚠ high
      </span>
    ) : null;
    return (
      <tr class={`hover:bg-zinc-900/50 ${isLow || isHigh ? "opacity-70" : ""}`}>
        <td class="px-3 py-2">
          <a href={`/hospital/${h.ccn}`} class="text-emerald-300 hover:underline">
            {h.name}
          </a>
        </td>
        <td class="px-3 py-2 text-zinc-400">{h.state}</td>
        <td class="hidden md:table-cell px-3 py-2 text-right tab-num text-zinc-300">{fmtMoney(h.gross)}</td>
        <td class={`px-3 py-2 text-right tab-num ${cashClass}`}>
          {fmtMoney(h.cash)}
          {flagBadge}
        </td>
        <td class="hidden md:table-cell px-3 py-2 text-right tab-num text-zinc-400">{fmtMoney(h.min)}</td>
        <td class="hidden md:table-cell px-3 py-2 text-right tab-num text-zinc-400">{fmtMoney(h.max)}</td>
        <td class="hidden md:table-cell px-3 py-2 text-right tab-num text-zinc-200">
          <span class="text-zinc-500">{(h.payers ?? []).length} insurers</span>
        </td>
      </tr>
    );
  });
}

function procedurePage(data: ProcedureData, url: string){
  const name = procedureName(data);
  const flaggedLow = data.stats.flagged_low ?? 0;
  const cashMinNote =
    flaggedLow > 0
      ? `${flaggedLow} entries below $50 already filtered. Remaining low values usually reflect partial-cost line items (professional fee only), not full bills.`
      : "Lowest published price across all reporting hospitals.";

  // SEO meta: keep description under ~160 chars and include hospital count + code.
  const seoDesc = `Compare ${name} (${data.code}) prices across ${data.stats.hospital_count} U.S. hospitals — gross, cash, negotiated rates by insurance.`;

  // Build the eyebrow with HTML so we can keep mono spans + tab-num styling.
  const eyebrow = `${escapeHtml(data.type || "CPT")} <span class="mono text-zinc-300">${escapeHtml(
    data.code,
  )}</span> · <span class="tab-num">${data.stats.hospital_count.toLocaleString("en-US")}</span> hospitals reporting`;

  // Distinct states + payer slugs for filter dropdowns (SSR; client-side hydration extends).
  const states = new Set<string>();
  const payers = new Map<string, string>();
  for (const h of data.hospitals) {
    if (h.state) states.add(h.state);
    for (const p of h.payers ?? []) {
      if (p.slug) payers.set(p.slug, p.display ?? p.slug);
    }
  }

  return (
    <Layout
      title={`${name} (${data.code}) — Hospital Ledger`}
      description={seoDesc}
      ogTitle={`${name} (${data.code}) — Hospital Ledger`}
      url={url}
    >
      <PageHeader eyebrow="Procedure" />
      <main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
        <EditorialHero eyebrow={eyebrow} title={name}>
          {ledeMarkup(data)}
        </EditorialHero>

        <section class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8 md:mb-12">
          <div>
            <div class="label-eyebrow">Cheapest published</div>
            <div class="mt-2 serif text-3xl sm:text-4xl md:text-5xl text-amber-300">{fmtMoney(data.stats.cash_min)}</div>
            <div class="mt-2 text-sm text-zinc-400 leading-snug">{cashMinNote}</div>
          </div>
          <div>
            <div class="label-eyebrow">
              Median across <span class="tab-num">{data.stats.hospital_count.toLocaleString("en-US")}</span> hospitals
            </div>
            <div class="mt-2 serif text-3xl sm:text-4xl md:text-5xl text-emerald-300">{fmtMoney(data.stats.cash_p50)}</div>
            <div class="mt-2 text-sm text-zinc-400 leading-snug">
              The realistic middle of the cash-price distribution. Use this as your benchmark.
            </div>
          </div>
          <div>
            <div class="label-eyebrow">Most expensive</div>
            <div class="mt-2 serif text-3xl sm:text-4xl md:text-5xl text-rose-300">{fmtMoney(data.stats.cash_max)}</div>
            <div class="mt-2 text-sm text-zinc-400 leading-snug">
              Highest published price. Often a gross-charge "list price" rarely actually paid.
            </div>
          </div>
        </section>

        <section class="mb-8 md:mb-12">
          <div class="flex items-baseline justify-between mb-4 gap-2">
            <h2 class="serif text-2xl sm:text-3xl text-zinc-100">Price distribution</h2>
            <div class="label-eyebrow">
              {data.hospitals.filter((h) => h.quality === "normal" && h.cash !== null && (h.cash as number) > 0).length.toLocaleString("en-US")}{" "}
              hospitals · log-scale x-axis
            </div>
          </div>
          <div class="rounded-md border border-zinc-800 bg-zinc-900/30 p-4">
            <PriceHistogram hospitals={data.hospitals} cashMedian={data.stats.cash_p50} />
          </div>
          <p class="mt-3 text-xs text-zinc-500 leading-relaxed">
            Each bar = number of hospitals charging in that price range.{" "}
            <span class="text-emerald-300">Emerald</span> bar contains the median.{" "}
            <span class="text-amber-300">Amber</span> dots = entries we flagged as likely partial-cost line items
            (excluded from the table below).
          </p>
        </section>

        <section
          class="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4 mb-4"
          data-states={JSON.stringify([...states].sort())}
          data-payers={JSON.stringify([...payers.entries()].sort((a, b) => a[1].localeCompare(b[1])))}
        >
          <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <label class="text-xs uppercase tracking-wider text-zinc-500" for="filter-state">
                Filter by state
              </label>
              <select
                id="filter-state"
                class="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              >
                <option value="">All states</option>
                {[...states].sort().map((s) => (
                  <option value={s}>{s}</option>
                ))}
              </select>
            </div>
            <div>
              <label class="text-xs uppercase tracking-wider text-zinc-500" for="filter-payer">
                Filter by your insurance
              </label>
              <select
                id="filter-payer"
                class="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              >
                <option value="">All insurers</option>
                {[...payers.entries()]
                  .sort((a, b) => a[1].localeCompare(b[1]))
                  .map(([slug, name]) => (
                    <option value={slug}>{name}</option>
                  ))}
              </select>
            </div>
            <div>
              <label class="text-xs uppercase tracking-wider text-zinc-500" for="sort-by">
                Sort
              </label>
              <select
                id="sort-by"
                class="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              >
                <option value="median-first">Most representative (near median)</option>
                <option value="state">By state</option>
                <option value="cash-asc">Cheapest cash first ⚠</option>
                <option value="cash-desc">Most expensive cash first</option>
                <option value="gross-asc">Cheapest gross first</option>
                <option value="payer-asc">Lowest your-insurer rate first</option>
              </select>
            </div>
          </div>
        </section>

        <details class="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/30 p-3 text-sm text-zinc-400">
          <summary class="cursor-pointer text-zinc-300 hover:text-emerald-300">
            Why some prices look unrealistically low or high
          </summary>
          <div class="mt-3 space-y-2 leading-relaxed">
            <p>
              Hospitals publish their machine-readable files (MRFs) the way they choose. A single procedure code can
              appear with very different prices depending on what's being charged:
            </p>
            <ul class="list-disc pl-5 space-y-1">
              <li>
                <strong class="text-zinc-300">Global price</strong> — facility + implant + anesthesia + surgeon. This is
                what you'd actually be billed.
              </li>
              <li>
                <strong class="text-zinc-300">Professional fee only</strong> — surgeon's portion. Often $500–$2,500 for
                a major procedure that costs $20K+ globally.
              </li>
              <li>
                <strong class="text-zinc-300">Facility fee only</strong> — operating room and recovery, no implant or
                surgeon.
              </li>
              <li>
                <strong class="text-zinc-300">Per-unit / per-minute charges</strong> — e.g. anesthesia time billed at
                $0.68/min appearing under a CPT code.
              </li>
              <li>
                <strong class="text-zinc-300">Rate multipliers</strong> — values like 0.85 meaning "85% of Medicare"
                rather than dollars.
              </li>
            </ul>
            <p>
              We filter the most obvious artifacts (under $50, or 25× above the median), but rows in the $500–$2K range
              for a major procedure are typically professional-only and we can't always tell. Use the median and
              high-end as the realistic range; treat very low rows as <em>partial-cost line items</em>, not full bills.
            </p>
          </div>
        </details>

        <section>
          <div id="status" class="text-sm text-zinc-400 mb-2">
            {data.hospitals.length.toLocaleString("en-US")} hospitals reporting
          </div>
          {/* Mobile: card-stacked list (visible 0-767px) */}
          <div class="md:hidden space-y-2" id="results-mobile">
            {hospitalCards(data)}
          </div>
          {/* Desktop: table (visible 768px+) */}
          <div class="hidden md:block overflow-x-auto rounded-lg border border-zinc-800">
            <table class="w-full text-sm">
              <thead class="bg-zinc-900 text-xs uppercase tracking-wider text-zinc-400">
                <tr>
                  <th class="px-3 py-2 text-left">Hospital</th>
                  <th class="px-3 py-2 text-left">State</th>
                  <th class="px-3 py-2 text-right">Gross</th>
                  <th class="px-3 py-2 text-right">Cash price</th>
                  <th class="px-3 py-2 text-right">Min negotiated</th>
                  <th class="px-3 py-2 text-right">Max negotiated</th>
                  <th class="px-3 py-2 text-right">Your insurer</th>
                </tr>
              </thead>
              <tbody id="results" class="divide-y divide-zinc-800">
                {hospitalRows(data)}
              </tbody>
            </table>
          </div>
        </section>

        <p class="mt-6 text-xs text-zinc-500">
          Data straight from each hospital's federally-mandated machine-readable file (45 CFR § 180). Prices reflect
          what the hospital published; what you actually pay depends on your specific plan, deductible, and other
          factors. "Cash price" is the discounted self-pay rate hospitals are required to publish for uninsured
          patients.
        </p>
      </main>
    </Layout>
  );
}

export async function procedurePageHandler(c: Context<Env>): Promise<Response> {
  const code = String(c.req.param("code") ?? "").toUpperCase();
  const canonicalUrl = `https://hospitalledger.com/procedure/${code}`;
  if (!VALID_CODE.test(code)) {
    return c.html(notFoundPage(code, canonicalUrl), 400);
  }
  const data = await loadProcedure(c.env, c.req.raw, code);
  if (!data) {
    return c.html(notFoundPage(code, canonicalUrl), 404);
  }
  return c.html(procedurePage(data, canonicalUrl), 200, {
    "cache-control": "public, max-age=300",
    "x-hl-template": "procedure-ssr",
  });
}
