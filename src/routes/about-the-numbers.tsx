/**
 * GET /about-the-numbers — methodology page explaining the count gaps.
 *
 * Reader question: "Why does the homepage say 3,587 hospitals when CMS
 * required 4,625 and 3,986 have a live MRF?" This page answers that with
 * the count definitions, the four-stage pipeline, and the live gap.
 *
 * All numbers are derived from /data/summary.json at build time so they
 * stay in sync with the homepage.
 */

import type { Context } from "hono";
import type { Env } from "../index";
import { Layout } from "../components/Layout";
import summaryJson from "../../public/data/summary.json";

type Summary = {
  generated_at: string;
  total_facilities?: number;
  cms_required_total: number;
  compliant: number;
  compliance_pct: number;
  standardized_price_index_hospitals?: number;
  standardized_price_hospitals?: number;
  standardized_price_rows?: number;
  zero_price_index_entries?: number;
};

const S = summaryJson as Summary;
const totalFacilities = S.total_facilities ?? 5426;
const cmsRequired = S.cms_required_total;
const compliant = S.compliant;
const indexEntries = S.standardized_price_index_hospitals ?? 3768;
const standardized = S.standardized_price_hospitals ?? 3654;
const zeroEntries = S.zero_price_index_entries ?? (indexEntries - standardized);
const notInIndex = compliant - indexEntries;
const totalGap = compliant - standardized;
const updated = S.generated_at.slice(0, 10);
const fmt = (n: number) => n.toLocaleString("en-US");

function aboutPage(url: string) {
  return (
    <Layout
      title="About the numbers — Hospital Ledger"
      description={`Why the homepage says ${fmt(standardized)} hospitals when CMS required ${fmt(cmsRequired)} and ${fmt(compliant)} have a live MRF. Pipeline, count definitions, and the live gap.`}
      ogTitle="About the numbers — Hospital Ledger"
      url={url}
    >
      <main class="mx-auto max-w-3xl px-4 sm:px-6 py-10 sm:py-14">
        <p class="text-xs uppercase tracking-widest text-emerald-400">Methodology</p>
        <h1 class="mt-3 serif text-3xl sm:text-4xl md:text-5xl tracking-tight leading-tight">
          About the numbers
        </h1>
        <p class="mt-4 text-zinc-400 text-sm">
          Updated <span class="tab-num">{updated}</span> · all figures derived from{" "}
          <a href="/data/summary.json" class="underline hover:text-zinc-200">
            /data/summary.json
          </a>
          .
        </p>

        <section class="mt-10">
          <h2 class="text-xl font-semibold tracking-tight">The four counts that matter</h2>
          <div class="mt-4 overflow-x-auto">
            <table class="w-full text-sm">
              <thead class="text-zinc-500 text-xs uppercase tracking-wider">
                <tr class="border-b border-zinc-800">
                  <th class="text-left py-2 pr-3">Stage</th>
                  <th class="text-right py-2 pr-3">Count</th>
                  <th class="text-left py-2">Definition</th>
                </tr>
              </thead>
              <tbody class="text-zinc-300">
                <tr class="border-b border-zinc-900">
                  <td class="py-3 pr-3 font-medium">All US hospitals</td>
                  <td class="py-3 pr-3 text-right tab-num">{fmt(totalFacilities)}</td>
                  <td class="py-3 text-zinc-400">Rows in CMS Hospital General Information.</td>
                </tr>
                <tr class="border-b border-zinc-900">
                  <td class="py-3 pr-3 font-medium">CMS-required</td>
                  <td class="py-3 pr-3 text-right tab-num">{fmt(cmsRequired)}</td>
                  <td class="py-3 text-zinc-400">
                    Acute care, critical access, children's, and rural emergency hospitals — the set 45 CFR § 180 binds.
                  </td>
                </tr>
                <tr class="border-b border-zinc-900">
                  <td class="py-3 pr-3 font-medium">Live MRF (compliant)</td>
                  <td class="py-3 pr-3 text-right tab-num">{fmt(compliant)}</td>
                  <td class="py-3 text-zinc-400">CMS-required hospitals where we verified a live, downloadable MRF URL.</td>
                </tr>
                <tr>
                  <td class="py-3 pr-3 font-medium text-emerald-300">Standardized prices</td>
                  <td class="py-3 pr-3 text-right tab-num text-emerald-300">{fmt(standardized)}</td>
                  <td class="py-3 text-zinc-400">
                    Live MRFs we successfully parsed into <code class="mono text-zinc-300">n &gt; 0</code> standardized
                    rows. This is the homepage headline.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="mt-12">
          <h2 class="text-xl font-semibold tracking-tight">
            The {fmt(totalGap)}-hospital gap, explained
          </h2>
          <p class="mt-3 text-zinc-300 leading-relaxed">
            Between "live MRF" ({fmt(compliant)}) and "standardized prices" ({fmt(standardized)}) sits a coverage
            hole of <span class="tab-num font-semibold text-white">{fmt(totalGap)}</span> hospitals. Two failure
            modes:
          </p>
          <ul class="mt-4 space-y-3 text-zinc-300 leading-relaxed">
            <li class="flex gap-3">
              <span class="text-emerald-400 mt-1">▸</span>
              <span>
                <span class="tab-num font-semibold text-white">{fmt(zeroEntries)}</span> hospitals parsed to{" "}
                <span class="tab-num">0</span> rows. Their MRF is downloadable but the file is empty, placeholder,
                or in a format the parser produced no usable rows from (e.g. an XML/PDF that didn't yield CDM /
                CPT / DRG / HCPCS rows). These show up in{" "}
                <a href="/data/prices/index.json" class="underline hover:text-zinc-200">
                  /data/prices/index.json
                </a>{" "}
                with <code class="mono text-zinc-300">n = 0</code>.
              </span>
            </li>
            <li class="flex gap-3">
              <span class="text-emerald-400 mt-1">▸</span>
              <span>
                <span class="tab-num font-semibold text-white">{fmt(notInIndex)}</span> hospitals aren't in the
                price index at all yet. They have a live MRF (we can fetch the URL) but ingest has either failed
                (decompression, schema, encoding) or hasn't been attempted in the current build. These are queued
                for the next ingest pass.
              </span>
            </li>
          </ul>
          <p class="mt-4 text-sm text-zinc-400 leading-relaxed">
            We surface only <code class="mono text-zinc-300">n &gt; 0</code> entries on the homepage because zero-row
            outputs aren't useful for comparison. They count toward the parse-pipeline yield, not toward the patient-
            facing count.
          </p>
        </section>

        <section class="mt-12">
          <h2 class="text-xl font-semibold tracking-tight">The pipeline</h2>
          <ol class="mt-4 space-y-3 list-decimal list-inside text-zinc-300 leading-relaxed">
            <li>
              <span class="font-medium">Seed</span> — start with CMS hospital list (
              <span class="tab-num">{fmt(totalFacilities)}</span>) and the CMS HPT seed.
            </li>
            <li>
              <span class="font-medium">Discover</span> — sitemap walker + email-domain expansion + Wayback fallback
              to locate each hospital's MRF URL.
            </li>
            <li>
              <span class="font-medium">Probe</span> — HEAD / partial GET to confirm the URL is alive and serves a
              non-HTML, non-XML, non-PDF body. <span class="tab-num">{fmt(compliant)}</span> of{" "}
              <span class="tab-num">{fmt(cmsRequired)}</span> CMS-required hospitals pass this gate.
            </li>
            <li>
              <span class="font-medium">Parse → standardize</span> — extract CDM / CPT / DRG / HCPCS rows into a
              uniform schema.{" "}
              <span class="tab-num">{fmt(indexEntries)}</span> hospitals have an index entry;{" "}
              <span class="tab-num font-semibold text-emerald-300">{fmt(standardized)}</span> of those have at least
              one usable row.
            </li>
            <li>
              <span class="font-medium">Publish</span> — write per-hospital JSON to R2 +{" "}
              <a href="/data/prices/index.json" class="underline hover:text-zinc-200">
                /data/prices/index.json
              </a>{" "}
              for cross-hospital comparison.
            </li>
          </ol>
        </section>

        <section class="mt-12">
          <h2 class="text-xl font-semibold tracking-tight">Closing the gap</h2>
          <p class="mt-3 text-zinc-300 leading-relaxed">
            The closeout pipeline (<code class="mono text-zinc-300">scripts/coverage_closeout.py</code>) replays the
            ingest pass against the <span class="tab-num">{fmt(notInIndex)}</span> not-in-index CCNs and the{" "}
            <span class="tab-num">{fmt(zeroEntries)}</span> zero-row CCNs with auto-tuned parallelism. Two open
            issues:
          </p>
          <ul class="mt-4 space-y-2 text-zinc-300 leading-relaxed text-sm">
            <li>
              <span class="text-emerald-400 mr-2">▸</span> A few host clusters (e.g. <code class="mono">apps.para-hcfs.com</code>,{" "}
              <code class="mono">sthpiprd.blob.core.windows.net</code>) account for most of the parse failures —
              format quirks rather than transient errors.
            </li>
            <li>
              <span class="text-emerald-400 mr-2">▸</span> Zero-row outputs are mostly placeholder files with no
              price content (CMS technically counts them as "available" even though they teach you nothing). The
              law's hole, not ours.
            </li>
          </ul>
        </section>

        <section class="mt-12 pt-6 border-t border-zinc-800">
          <h2 class="text-xl font-semibold tracking-tight">Re-derive everything yourself</h2>
          <pre class="mt-3 rounded-lg bg-zinc-900 border border-zinc-800 p-4 text-xs leading-relaxed overflow-x-auto">
            <code class="mono text-zinc-300">{`# Live summary
curl -s https://hospitalledger.com/data/summary.json | jq '{compliant, standardized_price_hospitals, standardized_price_index_hospitals, zero_price_index_entries}'

# Price index (full)
curl -s https://hospitalledger.com/data/prices/index.json | jq '.hospitals | length'

# Hospitals with n > 0
curl -s https://hospitalledger.com/data/prices/index.json | jq '[.hospitals[] | select(.n > 0)] | length'`}</code>
          </pre>
        </section>

        <p class="mt-12 text-sm text-zinc-500">
          <a href="/" class="text-emerald-300 underline">
            ← Back to Hospital Ledger
          </a>
        </p>
      </main>
    </Layout>
  );
}

export async function aboutNumbersPageHandler(c: Context<Env>): Promise<Response> {
  return c.html(aboutPage("https://hospitalledger.com/about-the-numbers"), 200, {
    "cache-control": "public, max-age=300",
    "x-hl-template": "about-the-numbers-ssr",
  });
}
