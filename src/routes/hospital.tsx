/**
 * GET /hospital/:ccn — SSR hospital page.
 * Replaces site/_hospital_template.html + site/functions/hospital/[ccn].js.
 */

import type { Context } from "hono";
import type { Env } from "../index";
import { Layout, PageHeader } from "../components/Layout";
import { loadHospital, type HospitalData } from "../lib/data";
import { fmtMoney } from "../lib/format";

const VALID_CCN = /^\d{6}$/;

const ELEMENT_LABELS: Record<string, string> = {
  mrf: "Machine-readable file published",
  gross: "Gross / standard charges",
  cash: "Discounted cash price",
  payer_rates: "Payer-specific negotiated rates",
  min_max: "Min / max negotiated charges",
  free_access: "Free, public, no login required",
};

const GRADE_COLOR: Record<string, string> = {
  A: "text-emerald-300",
  B: "text-emerald-200",
  C: "text-amber-300",
  D: "text-orange-300",
  F: "text-rose-300",
};

function notFoundPage(ccn: string){
  return (
    <Layout title={`Hospital ${ccn} — Hospital Ledger`} description={`No price data published for hospital ${ccn}.`}>
      <PageHeader eyebrow="Hospital" />
      <main class="mx-auto max-w-6xl px-6 py-8">
        <h1 class="text-3xl md:text-4xl font-semibold">Hospital {ccn}</h1>
        <p class="mt-2 text-zinc-400">No price data published.</p>
      </main>
    </Layout>
  );
}

function hospitalPage(ccn: string, data: HospitalData){
  const name = data.hospital_name || `Hospital ${ccn}`;
  const compliance = data.compliance ?? {};
  const grade = (compliance.grade ?? "F").toUpperCase();
  const score = Number(compliance.score ?? 0);
  const colorClass = GRADE_COLOR[grade] ?? GRADE_COLOR["F"]!;
  const elements = compliance.elements ?? {};
  const items = data.items ?? [];
  const cptCount = items.filter((it) => it.type === "CPT" || it.type === "HCPCS").length;
  const payerSet = new Set<string>();
  for (const it of items) {
    for (const p of it.payers ?? []) {
      if (p.p) payerSet.add(p.p);
    }
  }
  const top = items.slice(0, 50);
  const explainer =
    score >= 80
      ? "This hospital published most of what § 180 requires."
      : score >= 60
        ? "This hospital published part of what § 180 requires."
        : "This hospital published little of what § 180 requires.";

  return (
    <Layout title={`${name} — Hospital Ledger`} description={`Prices and compliance for ${name}.`}>
      <header class="border-b border-zinc-800 bg-gradient-to-b from-zinc-900 to-zinc-950">
        <div class="mx-auto max-w-6xl px-6 py-8">
          <div class="flex items-center justify-between">
            <a href="/" class="text-sm text-zinc-400 hover:text-zinc-200">
              ← Hospital Ledger
            </a>
            <div class="text-xs uppercase tracking-widest text-emerald-400">Hospital</div>
          </div>
          <h1 class="mt-4 text-3xl md:text-4xl font-semibold">{name}</h1>
          <p class="mt-2 text-zinc-400">CCN {ccn}</p>
        </div>
      </header>

      <main class="mx-auto max-w-6xl px-6 py-8">
        <section class="rounded-lg border border-zinc-800 bg-zinc-900/40 p-6 mb-6">
          <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
            <div>
              <div class="text-xs uppercase tracking-wider text-zinc-500 mb-1">45 CFR § 180 compliance</div>
              <div class={`text-4xl md:text-5xl font-semibold tab-num ${colorClass}`}>
                {grade} · {score}
              </div>
              <div class="text-sm text-zinc-400 mt-1">{explainer}</div>
            </div>
            <div class="flex flex-col gap-2 text-sm">
              {Object.entries(ELEMENT_LABELS).map(([k, label]) => {
                const ok = !!elements[k];
                return (
                  <div class="flex items-center gap-2">
                    <span class={ok ? "text-emerald-400" : "text-zinc-600"}>{ok ? "●" : "○"}</span>
                    <span class={ok ? "text-zinc-200" : "text-zinc-500"}>{label}</span>
                  </div>
                );
              })}
            </div>
          </div>
        </section>

        <section class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
          <div class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
            <div class="text-xs uppercase tracking-wider text-zinc-500">Procedures listed</div>
            <div class="mt-1 text-2xl font-semibold tab-num text-zinc-100">{items.length.toLocaleString("en-US")}</div>
          </div>
          <div class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
            <div class="text-xs uppercase tracking-wider text-zinc-500">Insurances with rates</div>
            <div class="mt-1 text-2xl font-semibold tab-num text-emerald-300">{payerSet.size.toLocaleString("en-US")}</div>
          </div>
          <div class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
            <div class="text-xs uppercase tracking-wider text-zinc-500">CPT / HCPCS codes</div>
            <div class="mt-1 text-2xl font-semibold tab-num text-zinc-100">{cptCount.toLocaleString("en-US")}</div>
          </div>
          <div class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
            <div class="text-xs uppercase tracking-wider text-zinc-500">Source MRF</div>
            <div class="mt-1 text-sm">
              {data.source_url ? (
                <a
                  href={data.source_url}
                  target="_blank"
                  rel="noopener"
                  class="text-emerald-300 hover:underline break-all"
                >
                  {data.format ?? "file"} ↗
                </a>
              ) : (
                "—"
              )}
            </div>
          </div>
        </section>

        <section class="mb-8">
          <h2 class="text-lg font-semibold mb-2">Most expensive procedures (gross)</h2>
          <div class="overflow-x-auto rounded-lg border border-zinc-800">
            <table class="w-full text-sm">
              <thead class="bg-zinc-900 text-xs uppercase tracking-wider text-zinc-400">
                <tr>
                  <th class="px-3 py-2 text-left">Code</th>
                  <th class="px-3 py-2 text-left">Description</th>
                  <th class="px-3 py-2 text-right">Gross</th>
                  <th class="px-3 py-2 text-right">Cash</th>
                  <th class="px-3 py-2 text-right">Min payer</th>
                  <th class="px-3 py-2 text-right">Max payer</th>
                  <th class="px-3 py-2 text-center"># insurers</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-zinc-800">
                {top.map((it) => (
                  <tr class="hover:bg-zinc-900/50">
                    <td class="px-3 py-2 mono text-zinc-300">{it.code}</td>
                    <td class="px-3 py-2 text-zinc-200">{it.desc ?? ""}</td>
                    <td class="px-3 py-2 text-right tab-num text-zinc-300">{fmtMoney(it.gross)}</td>
                    <td class="px-3 py-2 text-right tab-num text-emerald-300">{fmtMoney(it.cash)}</td>
                    <td class="px-3 py-2 text-right tab-num text-zinc-400">{fmtMoney(it.min)}</td>
                    <td class="px-3 py-2 text-right tab-num text-zinc-400">{fmtMoney(it.max)}</td>
                    <td class="px-3 py-2 text-center text-zinc-300">{it.pc ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div class="mt-2 text-xs text-zinc-500">
            Showing top {Math.min(50, items.length)} of {items.length.toLocaleString("en-US")} priced procedures, sorted
            by gross charge.
          </div>
        </section>

        <p class="text-xs text-zinc-500">
          Data straight from this hospital's federally-mandated machine-readable file (45 CFR § 180). The compliance
          grade reflects how completely the hospital published the six required data elements, not the quality of care.
        </p>
      </main>
    </Layout>
  );
}

export async function hospitalPageHandler(c: Context<Env>): Promise<Response> {
  const ccn = String(c.req.param("ccn") ?? "");
  if (!VALID_CCN.test(ccn)) {
    return c.html(notFoundPage(ccn), 400);
  }
  const data = await loadHospital(c.env, c.req.raw, ccn);
  if (!data) {
    return c.html(notFoundPage(ccn), 404);
  }
  return c.html(hospitalPage(ccn, data), 200, {
    "cache-control": "public, max-age=300",
    "x-hl-template": "hospital-ssr",
  });
}
