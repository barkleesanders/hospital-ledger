/**
 * GET /payer/:slug — SSR payer page.
 * Replaces site/_payer_template.html + site/functions/payer/[slug].js.
 */

import type { Context } from "hono";
import type { Env } from "../index";
import { GradeBadge } from "../components/ComplianceBar";
import { Layout, PageHeader } from "../components/Layout";
import { loadPayer, type PayerData } from "../lib/data";
import { fmtMoney } from "../lib/format";

const VALID_SLUG = /^[a-z0-9][a-z0-9-]{0,80}$/;

function notFoundPage(slug: string, url: string){
  return (
    <Layout title={`Insurance ${slug} — Hospital Ledger`} description={`No data for insurance ${slug} yet.`} url={url}>
      <PageHeader eyebrow="Insurance" />
      <main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
        <h1 class="text-2xl sm:text-3xl md:text-4xl font-semibold">{slug}</h1>
        <p class="mt-2 text-zinc-400">No data for "{slug}" yet.</p>
      </main>
    </Layout>
  );
}

function payerPage(slug: string, data: PayerData, url: string){
  const p = data.payer ?? { slug, display: slug };
  const display = p.display ?? slug;
  const hospitalCount = data.hospital_count ?? data.hospitals.length;
  const states = [...new Set(data.hospitals.map((h) => h.state).filter(Boolean))].sort();

  // SSR default sort: items-desc.
  const rows = [...data.hospitals]
    .sort((a, b) => (b.n_items_with_payer ?? 0) - (a.n_items_with_payer ?? 0))
    .slice(0, 500);

  const aliases = (p.raw_aliases ?? []).slice(0, 5);

  return (
    <Layout
      title={`${display} prices — Hospital Ledger`}
      description={`Hospitals that have negotiated rates with ${display}.`}
      url={url}
    >
      <header class="border-b border-zinc-800 bg-gradient-to-b from-zinc-900 to-zinc-950">
        <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
          <div class="flex items-center justify-between gap-2">
            <a href="/" class="text-sm text-zinc-400 hover:text-zinc-200">
              ← Hospital Ledger
            </a>
            <div class="text-xs uppercase tracking-widest text-emerald-400">{(p.category ?? "insurance").replace("-", " ")}</div>
          </div>
          <h1 class="mt-4 text-2xl sm:text-3xl md:text-4xl font-semibold">{display}</h1>
          <p class="mt-2 text-zinc-400">
            {hospitalCount.toLocaleString("en-US")} hospitals have negotiated rates with this insurance.
          </p>
        </div>
      </header>

      <main class="mx-auto max-w-6xl px-4 sm:px-6 py-8">
        <section class="grid grid-cols-2 md:grid-cols-3 gap-3 mb-6">
          <div class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
            <div class="text-xs uppercase tracking-wider text-zinc-500">Hospitals with this insurance</div>
            <div class="mt-1 text-2xl font-semibold tab-num text-emerald-300">{hospitalCount.toLocaleString("en-US")}</div>
          </div>
          <div class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
            <div class="text-xs uppercase tracking-wider text-zinc-500">Median negotiated rate</div>
            <div class="mt-1 text-2xl font-semibold tab-num text-zinc-100">{fmtMoney(p.median_rate)}</div>
          </div>
          <div class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
            <div class="text-xs uppercase tracking-wider text-zinc-500">Plan name aliases on MRFs</div>
            <div class="mt-1 text-sm text-zinc-300 leading-snug">
              {aliases.length
                ? aliases.map((a, i) => (
                    <>
                      {i > 0 ? <br /> : null}
                      {a}
                    </>
                  ))
                : "—"}
            </div>
          </div>
        </section>

        <section class="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4 mb-4">
          <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label class="text-xs uppercase tracking-wider text-zinc-500" for="filter-state">
                Filter hospitals by state
              </label>
              <select id="filter-state" class="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm">
                <option value="">All states</option>
                {states.map((s) => (
                  <option value={s}>{s}</option>
                ))}
              </select>
            </div>
            <div>
              <label class="text-xs uppercase tracking-wider text-zinc-500" for="sort-by">
                Sort
              </label>
              <select id="sort-by" class="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm">
                <option value="items-desc">Most procedures negotiated first</option>
                <option value="rate-asc">Lowest median rate first</option>
                <option value="grade-desc">Best compliance grade first</option>
                <option value="state">By state</option>
              </select>
            </div>
          </div>
        </section>

        <section>
          <div id="status" class="text-sm text-zinc-400 mb-2">
            {rows.length.toLocaleString("en-US")} hospitals
          </div>
          {/* Mobile: card-stacked list (visible 0-767px) */}
          <div class="md:hidden space-y-2">
            {rows.map((h) => (
              <a
                href={`/hospital/${h.ccn}`}
                class="block rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 hover:border-emerald-700/50 transition"
              >
                <div class="flex items-baseline justify-between gap-2 mb-2 min-w-0">
                  <div class="font-medium text-emerald-300 truncate text-sm">{h.name}</div>
                  <div class="text-xs text-zinc-500 shrink-0">{h.state}</div>
                </div>
                <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                  <div class="text-zinc-500">Median rate</div>
                  <div class="text-right tab-num text-base font-medium text-emerald-300">{fmtMoney(h.median_rate)}</div>
                  <div class="text-zinc-500">Procedures w/ rate</div>
                  <div class="text-right tab-num text-zinc-300">{(h.n_items_with_payer ?? 0).toLocaleString("en-US")}</div>
                  <div class="text-zinc-500">Compliance</div>
                  <div class="text-right">
                    <GradeBadge grade={h.compliance_grade} score={h.compliance_score} />
                  </div>
                </div>
              </a>
            ))}
          </div>
          {/* Desktop: table (visible 768px+) */}
          <div class="hidden md:block overflow-x-auto rounded-lg border border-zinc-800">
            <table class="w-full text-sm">
              <thead class="bg-zinc-900 text-xs uppercase tracking-wider text-zinc-400">
                <tr>
                  <th class="px-3 py-2 text-left">Hospital</th>
                  <th class="px-3 py-2 text-left">State</th>
                  <th class="px-3 py-2 text-right">Procedures with rate</th>
                  <th class="px-3 py-2 text-right">Median rate</th>
                  <th class="px-3 py-2 text-center">Compliance</th>
                </tr>
              </thead>
              <tbody id="results" class="divide-y divide-zinc-800">
                {rows.map((h) => (
                  <tr class="hover:bg-zinc-900/50">
                    <td class="px-3 py-2">
                      <a href={`/hospital/${h.ccn}`} class="text-emerald-300 hover:underline">
                        {h.name}
                      </a>
                    </td>
                    <td class="px-3 py-2 text-zinc-400">{h.state}</td>
                    <td class="px-3 py-2 text-right tab-num text-zinc-200">
                      {(h.n_items_with_payer ?? 0).toLocaleString("en-US")}
                    </td>
                    <td class="px-3 py-2 text-right tab-num text-emerald-300 font-medium">{fmtMoney(h.median_rate)}</td>
                    <td class="px-3 py-2 text-center">
                      <GradeBadge grade={h.compliance_grade} score={h.compliance_score} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <p class="mt-6 text-xs text-zinc-500">
          "Negotiated rate" is what each hospital agreed to accept from this insurance for a specific procedure. Your
          actual cost depends on your plan's deductible, copay, coinsurance, and out-of-network rules. Some plans
          hospitals call "Aetna PPO" or "Aetna HMO" lump under <em>Aetna</em> here — see plan-name aliases above.
        </p>
      </main>
    </Layout>
  );
}

export async function payerPageHandler(c: Context<Env>): Promise<Response> {
  const slug = String(c.req.param("slug") ?? "").toLowerCase();
  const canonicalUrl = `https://hospitalledger.com/payer/${slug}`;
  if (!VALID_SLUG.test(slug)) {
    return c.html(notFoundPage(slug, canonicalUrl), 400);
  }
  const data = await loadPayer(c.env, c.req.raw, slug);
  if (!data) {
    return c.html(notFoundPage(slug, canonicalUrl), 404);
  }
  return c.html(payerPage(slug, data, canonicalUrl), 200, {
    "cache-control": "public, max-age=300",
    "x-hl-template": "payer-ssr",
  });
}
