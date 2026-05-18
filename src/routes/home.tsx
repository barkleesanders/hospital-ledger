/**
 * GET / — SSR home page.
 *
 * SSR portion: editorial hero, featured spread (hip replacement), trust strip,
 * 4 patient KPIs, "find a price" cards, "how it works", "why this exists",
 * methodology. The dynamic tables (worst offenders, state compliance, facility
 * type bars, cross-hospital CPT search, hospital list) hydrate from the
 * client-side bundle at /home-client.js, which fetches /data/summary.json,
 * /data/hospitals.json, /api/prices-index, and /api/cpt-index lazily.
 *
 * Replaces site/index.html.
 */

import type { Context } from "hono";
import type { Env } from "../index";
import { Layout } from "../components/Layout";
import { ProcedureCarousel } from "../components/ProcedureCarousel";

function homePage(url: string) {
  return (
    <Layout
      title="Hospital Ledger — what the law required, what hospitals delivered"
      description="A free public database of every U.S. hospital's federally-mandated price transparency machine-readable file. Built from CMS data + live verification. CC0 licensed."
      ogTitle="Hospital Ledger"
      url={url}
      scriptSrc={["/cpt-names.js", "/home-client.js", "/procedure-carousel.js"]}
    >
      <header class="border-b border-zinc-800 bg-gradient-to-b from-zinc-900 to-zinc-950">
        <div class="mx-auto max-w-6xl px-4 sm:px-6 pt-8 pb-10 sm:pt-10 sm:pb-12 md:pt-14 md:pb-16">
          <div class="flex items-center gap-3 text-xs uppercase tracking-widest text-emerald-400">
            <span class="inline-block w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
            <span>Hospital Ledger</span>
            <span class="text-zinc-600">·</span>
            <span class="text-zinc-500">Free forever · No signup · No tracking</span>
          </div>

          <h1 class="mt-5 serif text-4xl sm:text-5xl md:text-6xl lg:text-7xl leading-[1.05] tracking-tight max-w-4xl">
            What does your hospital <em class="text-emerald-300">actually</em> charge?
          </h1>
          <p class="mt-5 text-base sm:text-lg md:text-xl text-zinc-300 max-w-2xl leading-snug">
            Compare the real price of a procedure across{" "}
            <span id="hero-hospital-count" class="font-semibold text-white tab-num">
              3,699
            </span>{" "}
            U.S. hospitals — straight from each hospital's own federally-mandated price file.
          </p>

          <ProcedureCarousel />

          <div class="mt-6 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs text-zinc-400">
            <div class="flex items-start gap-2">
              <svg class="w-4 h-4 mt-0.5 text-emerald-400 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span>Free forever, no signup</span>
            </div>
            <div class="flex items-start gap-2">
              <svg class="w-4 h-4 mt-0.5 text-emerald-400 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span>We don't track or store anything</span>
            </div>
            <div class="flex items-start gap-2">
              <svg class="w-4 h-4 mt-0.5 text-emerald-400 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span>Built from each hospital's MRF (45 CFR § 180)</span>
            </div>
            <div class="flex items-start gap-2">
              <svg class="w-4 h-4 mt-0.5 text-emerald-400 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <path d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span>Open source · CC0 public data</span>
            </div>
          </div>

          <div class="mt-8 grid grid-cols-2 md:grid-cols-4 gap-3">
            <div class="rounded-lg bg-zinc-900 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">Hospitals · with prices</div>
              <div id="kpi-patient-hospitals" class="mt-1 text-3xl font-semibold tab-num text-emerald-300">
                —
              </div>
              <div class="text-xs text-zinc-500 mt-1">readable, indexed prices</div>
            </div>
            <div class="rounded-lg bg-zinc-900 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">Procedures</div>
              <div id="kpi-patient-procedures" class="mt-1 text-3xl font-semibold tab-num text-emerald-300">
                10,000
              </div>
              <div class="text-xs text-zinc-500 mt-1">comparable across hospitals</div>
            </div>
            <div class="rounded-lg bg-zinc-900 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">Insurance plans</div>
              <div id="kpi-patient-payers" class="mt-1 text-3xl font-semibold tab-num text-emerald-300">
                200+
              </div>
              <div class="text-xs text-zinc-500 mt-1">with negotiated rates</div>
            </div>
            <div class="rounded-lg bg-zinc-900 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">Cost</div>
              <div class="mt-1 text-3xl font-semibold tab-num text-emerald-300">$0</div>
              <div class="text-xs text-zinc-500 mt-1">always free, public benefit</div>
            </div>
          </div>
          <div class="hidden">
            <span id="kpi-compliance">—</span>
            <span id="kpi-missing">—</span>
            <span id="kpi-enforcement">—</span>
            <span id="kpi-actions">—</span>
            <span id="hero-required">4,625</span>
          </div>

          <div class="mt-8 flex flex-wrap gap-3 text-sm">
            <a href="#for-patients" class="rounded-md bg-emerald-500 px-5 py-2.5 font-medium text-zinc-950 hover:bg-emerald-400 transition">
              Find a price →
            </a>
            <a href="#how-it-works" class="rounded-md border border-zinc-700 px-5 py-2.5 text-zinc-200 hover:bg-zinc-800 transition">
              How it works
            </a>
            <a href="#methodology" class="rounded-md border border-zinc-700 px-5 py-2.5 text-zinc-200 hover:bg-zinc-800 transition">
              Where this data comes from
            </a>
          </div>
        </div>
      </header>

      <main class="mx-auto max-w-6xl px-4 sm:px-6 py-8 sm:py-10 space-y-10 sm:space-y-12">
        <section
          id="for-patients"
          class="rounded-xl border border-emerald-700/30 bg-gradient-to-br from-emerald-950/30 to-zinc-900/40 p-4 sm:p-6 fade-in"
        >
          <div class="section-rule">
            <span class="num">01</span>
            <span class="label">Find a price</span>
            <span class="line" />
          </div>
          <h2 class="serif text-2xl sm:text-3xl md:text-4xl mb-2">Three ways to start.</h2>
          <p class="text-zinc-400 mb-5 max-w-2xl">
            Search by what you need, your insurance, or the hospital you're going to. Every price is straight from
            that hospital's own published file.
          </p>
          <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
            <form id="by-proc-form" class="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 lift min-w-0" autocomplete="off">
              <div class="flex items-center gap-2 mb-1">
                <span class="num-step text-xs">A.</span>
                <span class="text-xs uppercase tracking-wider text-zinc-400">What you need</span>
              </div>
              <label for="by-proc-input" class="block serif text-xl mb-1 text-zinc-100">
                A procedure or test
              </label>
              <div class="text-sm text-zinc-400 mb-3">
                Type the name (<em>"colonoscopy"</em>) or the CPT/HCPCS code (
                <code class="mono text-emerald-300">45378</code>).
              </div>
              <div class="flex gap-2 min-w-0">
                <input
                  id="by-proc-input"
                  type="search"
                  name="procedure_query"
                  list="proc-suggestions"
                  placeholder="colonoscopy, MRI, 99213…"
                  required
                  autocomplete="off"
                  autocorrect="off"
                  autocapitalize="off"
                  spellcheck={false}
                  inputmode="search"
                  data-1p-ignore="true"
                  data-lpignore="true"
                  data-form-type="other"
                  class="flex-1 min-w-0 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
                />
                <button class="shrink-0 rounded-md bg-emerald-600 hover:bg-emerald-500 px-3 py-2 text-sm font-medium">
                  Compare
                </button>
              </div>
              <datalist id="proc-suggestions" />
              <div id="by-proc-msg" class="mt-2 text-xs text-zinc-500 min-h-4" />
            </form>
            <form id="by-payer-form" class="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 lift min-w-0">
              <div class="flex items-center gap-2 mb-1">
                <span class="num-step text-xs">B.</span>
                <span class="text-xs uppercase tracking-wider text-zinc-400">What you have</span>
              </div>
              <label class="block serif text-xl mb-1 text-zinc-100">Your insurance</label>
              <div class="text-sm text-zinc-400 mb-3">
                See every hospital that has a negotiated rate with your plan.
              </div>
              <div class="flex gap-2 min-w-0">
                <select id="by-payer-select" required class="flex-1 min-w-0 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm">
                  <option value="">Loading insurers…</option>
                </select>
                <button class="shrink-0 rounded-md bg-emerald-600 hover:bg-emerald-500 px-3 py-2 text-sm font-medium">Go</button>
              </div>
            </form>
            <form id="by-hosp-form" class="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 lift min-w-0">
              <div class="flex items-center gap-2 mb-1">
                <span class="num-step text-xs">C.</span>
                <span class="text-xs uppercase tracking-wider text-zinc-400">Where you're going</span>
              </div>
              <label class="block serif text-xl mb-1 text-zinc-100">A specific hospital</label>
              <div class="text-sm text-zinc-400 mb-3">Get its compliance grade plus its top procedure prices.</div>
              <div class="flex gap-2 min-w-0">
                <input
                  id="by-hosp-input"
                  type="text"
                  placeholder="Hospital name or 6-digit CCN"
                  class="flex-1 min-w-0 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
                />
                <button class="shrink-0 rounded-md bg-emerald-600 hover:bg-emerald-500 px-3 py-2 text-sm font-medium">
                  Find
                </button>
              </div>
            </form>
          </div>
          <p class="mt-4 text-xs text-zinc-500">
            Every price comes straight from the hospital's federally-mandated machine-readable file. The "cash price"
            is the self-pay discount they're required to publish. Your actual bill depends on your plan, deductible,
            and copay.
          </p>
        </section>

        <section id="how-it-works">
          <div class="section-rule">
            <span class="num">02</span>
            <span class="label">How it works</span>
            <span class="line" />
          </div>
          <h2 class="serif text-2xl sm:text-3xl md:text-4xl mb-6">Three steps. No fees, no signup.</h2>
          <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4 md:gap-6">
            <div>
              <div class="num-step text-2xl mb-2">01</div>
              <h3 class="serif text-2xl mb-2">Search</h3>
              <p class="text-zinc-400">
                Type a procedure name (<em>"MRI knee"</em>), a CPT code (<code class="mono text-emerald-300">73721</code>),
                an insurer, or a hospital. We match it against every U.S. hospital's published price file.
              </p>
            </div>
            <div>
              <div class="num-step text-2xl mb-2">02</div>
              <h3 class="serif text-2xl mb-2">Compare</h3>
              <p class="text-zinc-400">
                See the gross price, the cash discount, and the rate every insurance plan negotiated — side by side
                across hundreds of hospitals.
              </p>
            </div>
            <div>
              <div class="num-step text-2xl mb-2">03</div>
              <h3 class="serif text-2xl mb-2">Decide</h3>
              <p class="text-zinc-400">
                Pick the hospital that costs less for the procedure you need. Bring the price to your appointment if
                there's a dispute.{" "}
                <span class="text-zinc-500">(Always confirm directly — we publish what they published.)</span>
              </p>
            </div>
          </div>
        </section>

        <section id="why" class="grid md:grid-cols-3 gap-4 md:gap-6">
          <div class="md:col-span-2">
            <div class="section-rule">
              <span class="num">03</span>
              <span class="label">Why this exists</span>
              <span class="line" />
            </div>
            <h2 class="serif text-2xl sm:text-3xl md:text-4xl mb-3">
              The law's been on the books since 2021. The database wasn't.
            </h2>
            <ul class="space-y-3 text-zinc-300 leading-relaxed">
              <li class="flex gap-3">
                <span class="text-emerald-400 mt-1">▸</span>
                <span>
                  Under{" "}
                  <a
                    href="https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180"
                    class="text-emerald-300 underline"
                  >
                    45 CFR § 180
                  </a>
                  , every U.S. hospital must publish a machine-readable price file — gross charges, cash prices,
                  negotiated rates, and min/max — free, public, no signup.
                </span>
              </li>
              <li class="flex gap-3">
                <span class="text-emerald-400 mt-1">▸</span>
                <span>
                  Most hospitals technically comply by uploading a giant unreadable file. We parsed all 4,625 of them
                  so you don't have to.
                </span>
              </li>
              <li class="flex gap-3">
                <span class="text-emerald-400 mt-1">▸</span>
                <span>
                  Mark Cuban built{" "}
                  <a href="https://costplusdrugs.com" class="text-emerald-300 underline">
                    Cost Plus Drugs
                  </a>{" "}
                  for the same reason — pricing transparency in healthcare. We're doing it for hospitals.
                </span>
              </li>
            </ul>
          </div>
          <aside class="rounded-lg border border-zinc-800 bg-zinc-900 p-5">
            <div class="text-xs uppercase text-zinc-500 tracking-wider">Penalty for non-compliance</div>
            <div class="mt-2 text-zinc-100">
              <strong>$300/day</strong> for hospitals ≤30 beds.
              <br />
              <strong>Up to $5,500/day</strong> for hospitals &gt;550 beds.
              <br />
              <span class="text-zinc-400 text-sm">42 USC § 300gg-18 · 45 CFR § 180.90</span>
            </div>
            <hr class="my-4 border-zinc-800" />
            <div class="text-xs uppercase text-zinc-500 tracking-wider">CMS escalation ladder</div>
            <ol class="mt-2 text-sm text-zinc-300 space-y-1 list-decimal pl-4">
              <li>Warning Notice</li>
              <li>Corrective Action Plan (CAP) Request</li>
              <li>Civil Monetary Penalty (CMP)</li>
              <li>Closure Notice</li>
            </ol>
          </aside>
        </section>

        <section
          id="how-we-built-this"
          class="rounded-xl border border-emerald-700/30 bg-gradient-to-br from-emerald-950/30 to-zinc-900/40 p-4 sm:p-6 fade-in"
        >
          <div class="section-rule">
            <span class="num">04</span>
            <span class="label">How we built this</span>
            <span class="line" />
          </div>
          <h2 class="serif text-2xl sm:text-3xl md:text-4xl mb-3">
            Three days. Two AI agents.{" "}
            <em class="text-emerald-300">~3 person-years</em> of human work.
          </h2>
          <p class="text-zinc-300 max-w-3xl leading-relaxed">
            Hospital Ledger was built between May 12–15, 2026 by two AI coding agents —{" "}
            <a class="text-emerald-300 underline" href="https://www.anthropic.com/claude-code">
              Claude Code
            </a>{" "}
            and{" "}
            <a class="text-emerald-300 underline" href="https://github.com/openai/codex">
              OpenAI Codex
            </a>{" "}
            — working in parallel under one operator. The headline number isn't a vibe — it's a sum of
            real per-step rates a competent analyst with Excel, Power Query, and Python would actually
            hit. Every hospital labels "gross charge", "cash price", "negotiated rate", and "CPT code"
            differently; the CMS rule mandates the data points, not the column names. So every file is a
            new schema-mapping problem.
          </p>

          <div class="mt-5 rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 sm:p-5">
            <div class="text-xs uppercase tracking-wider text-emerald-400 mb-3">
              Show the math (per step, defensible rates)
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-sm tab-num">
                <thead class="text-zinc-500 text-left">
                  <tr class="border-b border-zinc-800">
                    <th class="py-2 pr-3 font-medium">Step</th>
                    <th class="py-2 pr-3 font-medium">Volume</th>
                    <th class="py-2 pr-3 font-medium">Per-unit rate (human)</th>
                    <th class="py-2 pr-3 font-medium text-right">Hours</th>
                  </tr>
                </thead>
                <tbody class="text-zinc-300 divide-y divide-zinc-800/60">
                  <tr>
                    <td class="py-2 pr-3">Parse each MRF (download · open · map columns · normalize · QA)</td>
                    <td class="py-2 pr-3">3,699 files</td>
                    <td class="py-2 pr-3">~77 min avg (90% × 66 min clean + 10% × 180 min hard)</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">4,747</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Find candidate MRF URLs from CMS + transparency pages</td>
                    <td class="py-2 pr-3">4,625 hospitals</td>
                    <td class="py-2 pr-3">~5 min / hospital</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">385</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Probe URLs for liveness (curl / browser open)</td>
                    <td class="py-2 pr-3">7,191 URLs</td>
                    <td class="py-2 pr-3">~1 min / URL</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">120</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Rediscover dead links by hunting each hospital's site</td>
                    <td class="py-2 pr-3">~1,000 dead/missing</td>
                    <td class="py-2 pr-3">~15 min each</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">250</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Canonicalize payer names (Aetna vs AETNA vs Aetna Health Inc.)</td>
                    <td class="py-2 pr-3">74,747 raw rows</td>
                    <td class="py-2 pr-3">~250 wpm reading + ~500 unique merges</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">50</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Validate CPT / HCPCS codes against AMA reference</td>
                    <td class="py-2 pr-3">10,000 codes</td>
                    <td class="py-2 pr-3">~3 codes / min spot-check + cleanup</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">30</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Ingest + link CMS enforcement actions to CCNs</td>
                    <td class="py-2 pr-3">8,642 records</td>
                    <td class="py-2 pr-3">structured CSV, mostly automated</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">10</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Build the cross-hospital price index (joins, dedup, pivots)</td>
                    <td class="py-2 pr-3">~660 K rows</td>
                    <td class="py-2 pr-3">Excel/Power Query at solo-analyst pace</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">40</td>
                  </tr>
                  <tr>
                    <td class="py-2 pr-3">Design + build the SSR site, API, search, charts</td>
                    <td class="py-2 pr-3">11 pages, 8 endpoints</td>
                    <td class="py-2 pr-3">solo full-stack dev</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">300</td>
                  </tr>
                  <tr class="bg-emerald-950/20 font-semibold">
                    <td class="py-2 pr-3">Total</td>
                    <td class="py-2 pr-3"></td>
                    <td class="py-2 pr-3 text-zinc-400">≈ 2,080 hr / person-year (full-time)</td>
                    <td class="py-2 pr-3 text-right text-emerald-300">~5,930</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="mt-3 text-xs text-zinc-500">
              ≈ <span class="text-emerald-300">2.85 person-years</span> at 40 hr/week fully-utilized, or
              roughly <span class="text-emerald-300">5–6 calendar years</span> for one analyst working a
              sustainable 20 hr/week on the side. The 99 GB of raw MRF data is too large to retype —
              nobody types it; they normalize. The cost is mapping, not keystrokes. Reading speed
              (~250 wpm) and typing speed (~40 wpm) only show up inside the per-MRF column-mapping pass
              and the payer-name canonicalization row.
            </p>
          </div>

          <div class="mt-6 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
            <div class="rounded-lg bg-zinc-950/50 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">Raw data processed</div>
              <div class="mt-1 text-2xl font-semibold tab-num text-emerald-300">99 GB</div>
              <div class="text-xs text-zinc-500 mt-1">MRFs downloaded, parsed, normalized</div>
            </div>
            <div class="rounded-lg bg-zinc-950/50 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">MRFs parsed</div>
              <div class="mt-1 text-2xl font-semibold tab-num text-emerald-300">3,699</div>
              <div class="text-xs text-zinc-500 mt-1">across 6 schema variants → one schema</div>
            </div>
            <div class="rounded-lg bg-zinc-950/50 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">URLs probed</div>
              <div class="mt-1 text-2xl font-semibold tab-num text-emerald-300">7,191</div>
              <div class="text-xs text-zinc-500 mt-1">TPAFS seed + email-domain + Wayback</div>
            </div>
            <div class="rounded-lg bg-zinc-950/50 border border-zinc-800 p-4">
              <div class="text-xs uppercase tracking-wider text-zinc-500">Enforcement records</div>
              <div class="mt-1 text-2xl font-semibold tab-num text-emerald-300">8,642</div>
              <div class="text-xs text-zinc-500 mt-1">CMS warning · CAP · CMP · closure</div>
            </div>
          </div>

          <div class="mt-6 grid md:grid-cols-2 gap-4">
            <div class="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
              <div class="text-xs uppercase tracking-wider text-emerald-400 mb-3">
                The 11-stage pipeline
              </div>
              <ol class="space-y-1.5 text-sm text-zinc-300 list-decimal pl-4">
                <li>Seed the CMS hospital universe (5,426 facilities)</li>
                <li>Load TPAFS MRF URL seeds, probe each for liveness</li>
                <li>Rediscover dead URLs from each hospital's transparency page</li>
                <li>Exa web search fallback for the still-missing</li>
                <li>Claude / Codex agent loop on the hardest holdouts</li>
                <li>CMS-HPT marker files + email-domain expansion</li>
                <li>Wayback Machine fallback for vanished URLs</li>
                <li>Fetch + parse CSV / JSON / XLSX / ZIP into one unified schema</li>
                <li>Ingest CMS enforcement (warning / CAP / CMP / closure)</li>
                <li>Build cross-hospital CPT / HCPCS price index</li>
                <li>SSR site + Cloudflare Pages + JSON API</li>
              </ol>
            </div>
            <div class="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
              <div class="text-xs uppercase tracking-wider text-emerald-400 mb-3">
                Why this didn't exist before
              </div>
              <ul class="space-y-2 text-sm text-zinc-300">
                <li class="flex gap-2">
                  <span class="text-emerald-400 mt-0.5">▸</span>
                  <span>Commercial aggregators (Turquoise, PayerPrice, Serif) lock the data behind NDAs.</span>
                </li>
                <li class="flex gap-2">
                  <span class="text-emerald-400 mt-0.5">▸</span>
                  <span>CMS publishes the rule but doesn't aggregate or verify anything.</span>
                </li>
                <li class="flex gap-2">
                  <span class="text-emerald-400 mt-0.5">▸</span>
                  <span>Every hospital uses a different format, vendor, and URL convention.</span>
                </li>
                <li class="flex gap-2">
                  <span class="text-emerald-400 mt-0.5">▸</span>
                  <span>13.8% of files sit behind bot defenses; "compliance" ≠ "reachable".</span>
                </li>
                <li class="flex gap-2">
                  <span class="text-emerald-400 mt-0.5">▸</span>
                  <span>
                    Until LLM agents got good at parsing arbitrary CSV / JSON shapes, this scale of
                    normalization wasn't cost-effective for one person.
                  </span>
                </li>
              </ul>
            </div>
          </div>

          <p class="mt-6 text-sm text-zinc-400 max-w-3xl">
            Built in the open at{" "}
            <a
              class="text-emerald-300 underline"
              href="https://github.com/barkleesanders/hospital-ledger"
            >
              github.com/barkleesanders/hospital-ledger
            </a>
            {" "}— 36 pipeline scripts, ~10,700 lines of Python, ~3,000 lines of TypeScript. AGPLv3 code,
            CC0 data. <span class="text-zinc-500">No tracking. No signup. Free forever.</span>
          </p>
        </section>

        <section id="search">
          <h2 class="text-2xl font-semibold tracking-tight">Search every hospital</h2>
          <p class="text-sm text-zinc-400 mt-1">
            Real-time filter across all <span id="hosp-count">5,426</span> facilities
          </p>
          <div class="mt-4 grid md:grid-cols-[1fr_180px_160px] gap-3">
            <input
              id="q"
              type="search"
              placeholder="Hospital name, city, or CCN…"
              class="rounded-md bg-zinc-900 border border-zinc-700 text-zinc-100 placeholder-zinc-500 px-4 py-3 text-base focus:border-emerald-500 focus:outline-none"
            />
            <select id="state" class="rounded-md bg-zinc-900 border border-zinc-700 text-zinc-100 px-3 py-3" />
            <select id="status" class="rounded-md bg-zinc-900 border border-zinc-700 text-zinc-100 px-3 py-3">
              <option value="">All statuses</option>
              <option value="compliant">✓ Compliant (live MRF)</option>
              <option value="missing">⚠ Missing MRF</option>
              <option value="enforcement">⛔ Under CMS enforcement</option>
              <option value="missing-enforcement">🚨 Missing + Cited</option>
            </select>
          </div>
          <div id="results-count" class="mt-3 text-sm text-zinc-500" />
          <div id="results" class="mt-4 space-y-2" />
        </section>

        <section id="worst">
          <h2 class="text-2xl font-semibold tracking-tight">Top 25 worst offenders</h2>
          <p class="text-sm text-zinc-400 mt-1">
            CMS-required hospitals with the most enforcement actions <em>and</em> no findable MRF.
          </p>
          <div class="mt-4 rounded-lg border border-zinc-800 overflow-x-auto">
            <table class="min-w-full text-sm">
              <thead class="bg-zinc-900 text-zinc-400">
                <tr>
                  <th class="px-4 py-2 text-left font-medium">CCN</th>
                  <th class="px-4 py-2 text-left font-medium">Hospital</th>
                  <th class="px-4 py-2 text-left font-medium">City, State</th>
                  <th class="px-4 py-2 text-right font-medium">CMS actions</th>
                </tr>
              </thead>
              <tbody id="worst-tbody" class="divide-y divide-zinc-800" />
            </table>
          </div>
        </section>

        <section id="states" class="grid md:grid-cols-2 gap-8">
          <div>
            <h2 class="text-2xl font-semibold tracking-tight">Worst states</h2>
            <p class="text-sm text-zinc-400 mt-1">
              By share of CMS-required hospitals with a live MRF.
            </p>
            <div class="mt-4 rounded-lg border border-zinc-800 overflow-x-auto">
              <table class="min-w-full text-sm">
                <thead class="bg-zinc-900 text-zinc-400">
                  <tr>
                    <th class="px-4 py-2 text-left font-medium">State</th>
                    <th class="px-4 py-2 text-right font-medium">Total</th>
                    <th class="px-4 py-2 text-right font-medium">Live</th>
                    <th class="px-4 py-2 text-right font-medium">% live</th>
                  </tr>
                </thead>
                <tbody id="state-bottom-tbody" class="divide-y divide-zinc-800" />
              </table>
            </div>
          </div>
          <div>
            <h2 class="text-2xl font-semibold tracking-tight">By facility type</h2>
            <p class="text-sm text-zinc-400 mt-1">Rural Emergency Hospitals (the newest category) lag furthest.</p>
            <div class="mt-4 space-y-3" id="type-bars" />
          </div>
        </section>

        <section id="methodology" class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4 sm:p-6">
          <div class="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
            <div>
              <h2 class="text-2xl font-semibold tracking-tight">Sources / methodology</h2>
              <p class="text-sm text-zinc-400 mt-1">
                The inputs, checks, and limits are listed here on the site, not just in the repo.
              </p>
            </div>
            <a
              href="/data/summary.json"
              class="text-sm text-emerald-400 underline underline-offset-4 hover:text-emerald-300"
            >
              Open the current summary JSON
            </a>
          </div>
          <div class="mt-6 grid gap-4 md:grid-cols-3">
            <div class="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
              <div class="text-xs uppercase tracking-wider text-emerald-400">Primary sources</div>
              <ul class="mt-3 space-y-2 text-sm text-zinc-300">
                <li>
                  <a
                    href="https://data.cms.gov/provider-data/dataset/xubh-q36u"
                    class="underline hover:text-zinc-100"
                  >
                    CMS Hospital General Information
                  </a>{" "}
                  supplies the hospital universe and CCNs.
                </li>
                <li>
                  <a href="https://github.com/TPAFS/transparency-data" class="underline hover:text-zinc-100">
                    TPAFS/transparency-data
                  </a>{" "}
                  supplies the initial MRF seed URLs.
                </li>
                <li>
                  <a
                    href="https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/hospital-price-transparency-enforcement-activities-and-outcomes"
                    class="underline hover:text-zinc-100"
                  >
                    CMS Enforcement Activities
                  </a>{" "}
                  supplies warning, CAP, CMP, and closure records.
                </li>
                <li>
                  <a
                    href="https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180"
                    class="underline hover:text-zinc-100"
                  >
                    45 CFR § 180
                  </a>{" "}
                  defines what hospitals are required to publish.
                </li>
              </ul>
            </div>
            <div class="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
              <div class="text-xs uppercase tracking-wider text-emerald-400">How this was built</div>
              <ol class="mt-3 space-y-2 text-sm text-zinc-300 list-decimal pl-4">
                <li>Start with all CMS-required hospitals.</li>
                <li>Join known MRF links from the seed datasets.</li>
                <li>Probe URLs live and follow redirects to verify which files actually respond.</li>
                <li>Rediscover missing files from hospital transparency pages when the seed is dead.</li>
                <li>Parse live CSV, JSON, XLSX, ZIP, and wrapper-page formats into one schema.</li>
                <li>Generate the on-site standardized price previews from those parsed files.</li>
              </ol>
              <p class="mt-3 text-xs text-zinc-500">
                Current on-site preview coverage:{" "}
                <span id="methodology-price-count" class="tab-num text-zinc-300">
                  —
                </span>{" "}
                hospitals with standardized price rows.
              </p>
            </div>
            <div class="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
              <div class="text-xs uppercase tracking-wider text-emerald-400">What the numbers mean</div>
              <ul class="mt-3 space-y-2 text-sm text-zinc-300">
                <li>
                  <strong>Compliant</strong> means a live machine-readable file was found and verified.
                </li>
                <li>
                  <strong>Missing</strong> means no live public MRF was found after automated discovery and probing.
                </li>
                <li>
                  <strong>Under CMS enforcement</strong> means the hospital appears in CMS's public enforcement record.
                </li>
                <li>
                  <strong>Standardized price preview</strong> appears when parsed rows were actually generated for that
                  hospital.
                </li>
              </ul>
              <p class="mt-3 text-xs text-zinc-500">
                Current limits: some hospitals are behind bot-defense or publish malformed files. Missing rows can mean
                non-compliance, anti-bot blocking, or broken vendor output.
              </p>
            </div>
          </div>
        </section>

        <section id="prices" class="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 sm:p-6">
          <h2 class="text-2xl font-semibold tracking-tight">🩺 Live prices — standardized, on-site</h2>
          <p class="text-sm text-zinc-400 mt-1">
            Parsed directly from each hospital's MRF into a unified schema. No external links — search prices by CPT
            or HCPCS code below.
          </p>
          <div class="mt-4 grid grid-cols-1 gap-3 border-y border-zinc-800 py-4 text-sm md:grid-cols-3">
            <div>
              <div class="text-xs uppercase tracking-wider text-zinc-500">Hospital previews</div>
              <div id="price-hospital-count" class="mt-1 text-2xl font-semibold tab-num text-emerald-300">
                —
              </div>
            </div>
            <div>
              <div class="text-xs uppercase tracking-wider text-zinc-500">Standardized rows</div>
              <div id="price-row-count" class="mt-1 text-2xl font-semibold tab-num text-zinc-100">
                —
              </div>
            </div>
            <div>
              <div class="text-xs uppercase tracking-wider text-zinc-500">CPT / HCPCS codes</div>
              <div id="price-code-count" class="mt-1 text-2xl font-semibold tab-num text-zinc-100">
                —
              </div>
            </div>
          </div>
          <div class="mt-4 grid md:grid-cols-[1fr_180px] gap-3">
            <input
              id="cpt-q"
              type="search"
              placeholder="CPT/HCPCS code or procedure (e.g. 99213, MRI, colonoscopy)…"
              class="rounded-md bg-zinc-900 border border-zinc-700 text-zinc-100 placeholder-zinc-500 px-4 py-3 text-base focus:border-emerald-500 focus:outline-none"
            />
            <select id="cpt-sort" class="rounded-md bg-zinc-900 border border-zinc-700 text-zinc-100 px-3 py-3">
              <option value="coverage">Most hospitals</option>
              <option value="code">By code</option>
            </select>
          </div>
          <div id="cpt-status" class="mt-2 text-xs text-zinc-500" />
          <div id="cpt-results" class="mt-3" />
        </section>

        <section id="api" class="rounded-lg border border-zinc-800 bg-zinc-900 p-4 sm:p-6">
          <h2 class="text-2xl font-semibold tracking-tight">Use it</h2>
          <p class="text-zinc-300 mt-2">
            Everything here is <strong>CC0 / public domain</strong>. No attribution required. Build whatever you want
            on top.
          </p>
          <div class="mt-4 grid md:grid-cols-2 gap-3 text-sm">
            <a href="/data/hospitals.json" class="rounded-md border border-zinc-700 hover:bg-zinc-800 p-3">
              <div class="mono text-emerald-400">/data/hospitals.json</div>
              <div class="text-zinc-400 mt-1">Full dataset · 3.1 MB · 5,426 hospitals</div>
            </a>
            <a href="/data/summary.json" class="rounded-md border border-zinc-700 hover:bg-zinc-800 p-3">
              <div class="mono text-emerald-400">/data/summary.json</div>
              <div class="text-zinc-400 mt-1">Aggregate stats · 5.5 KB</div>
            </a>
            <a href="/api/cpt-index" class="rounded-md border border-zinc-700 hover:bg-zinc-800 p-3">
              <div class="mono text-emerald-400">/api/cpt-index</div>
              <div class="text-zinc-400 mt-1">Cross-hospital CPT price index when generated</div>
            </a>
            <a href="/api/prices/100072" class="rounded-md border border-zinc-700 hover:bg-zinc-800 p-3">
              <div class="mono text-emerald-400">/api/prices/&lt;ccn&gt;</div>
              <div class="text-zinc-400 mt-1">Per-hospital standardized prices via asset or R2 fallback</div>
            </a>
          </div>
        </section>

        <footer class="border-t border-zinc-800 pt-8 text-sm text-zinc-500 space-y-2">
          <p>
            <strong>License:</strong> Data CC0 1.0 Universal · Code AGPLv3 · Built from CMS Hospital General
            Information (public domain) + TPAFS/transparency-data (CC BY-SA 4.0, seed only) + live verification + CMS
            Hospital Price Transparency Enforcement Activities.
          </p>
          <p>
            <strong>Method:</strong> 11 stages (TPAFS seed → page parsing → Exa search → Claude agents → CMS-HPT marker
            files → email-domain expansion → Wayback fallback). 86.2% live MRFs found via automated probing. Updated
            quarterly.
          </p>
          <p>
            <strong>Limits:</strong> Some MRFs require browser bot-bypass (Akamai-walled hospitals). We don't claim 100%
            — and the 13.8% gap IS the policy artifact: federally-required, federally-cited, still not public.
          </p>
          <p class="text-zinc-600">
            Not affiliated with CMS, HHS, Cost Plus Drugs, or any commercial transparency vendor. No warranty.
          </p>
        </footer>
      </main>

      <div
        id="modal"
        class="fixed inset-0 bg-zinc-950/90 backdrop-blur-sm hidden z-50 p-4 overflow-y-auto"
      >
        <div class="mx-auto max-w-3xl rounded-xl bg-zinc-900 border border-zinc-800 p-4 sm:p-6 mt-8 sm:mt-12">
          <div class="flex items-start justify-between gap-4">
            <div id="modal-content" class="flex-1" />
            <button class="text-zinc-500 hover:text-zinc-200 text-2xl leading-none" data-close-modal>
              ✕
            </button>
          </div>
        </div>
      </div>
    </Layout>
  );
}

export async function homePageHandler(c: Context<Env>): Promise<Response> {
  return c.html(homePage("https://hospitalledger.com/"), 200, {
    "cache-control": "public, max-age=300",
    "x-hl-template": "home-ssr",
  });
}
