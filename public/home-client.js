// Home page client islands. SSR ships the chrome; this file hydrates the
// dynamic tables (worst offenders, state compliance, facility type bars,
// hospital list, cross-hospital CPT search) and wires the patient-facing
// search forms. Direct port of the inline <script> block from the legacy
// site/index.html, with one cleanup: modal close uses [data-close-modal]
// instead of an inline onclick handler (CSP-friendlier).
(() => {
  const $ = (id) => document.getElementById(id);
  let hospitals = [];
  let summary = null;
  let pricesIndex = null;
  let cptIndex = null;
  let cptIndexLoading = false;
  let cptIndexPromise = null;

  async function load() {
    try {
      summary = await (await fetch("/data/summary.json")).json();
      renderSummary();
    } catch (e) {
      console.error("summary load", e);
    }
    try {
      hospitals = await (await fetch("/data/hospitals.json")).json();
      initSearch();
      renderWorst();
      renderStates();
      renderTypes();
      await loadPriceIndexes();
      initCptSearch();
      if (location.hash.startsWith("#h/")) openCCN(decodeURIComponent(location.hash.slice(3)));
    } catch (e) {
      console.error("hospitals load", e);
    }
  }

  async function fetchOptionalJson(url) {
    try {
      const res = await fetch(url);
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      console.warn("optional data load failed", url, e);
      return null;
    }
  }

  async function loadPriceIndexes() {
    const priceData = await fetchOptionalJson("/api/prices-index");
    pricesIndex = priceData || null;
    renderPriceCoverage();
  }

  function renderSummary() {
    if (!summary) return;
    if ($("hero-required")) $("hero-required").textContent = summary.cms_required_total.toLocaleString();
    if ($("hero-required-visible")) $("hero-required-visible").textContent = summary.cms_required_total.toLocaleString();
    if ($("hero-live-mrf-count")) $("hero-live-mrf-count").textContent = summary.compliant.toLocaleString();
    if ($("methodology-live-mrf-count")) $("methodology-live-mrf-count").textContent = summary.compliant.toLocaleString();
    const standardizedHospitals =
      summary.standardized_price_hospitals || summary.standardized_price_index_hospitals || null;
    if (standardizedHospitals) {
      const text = standardizedHospitals.toLocaleString();
      if ($("hero-hospital-count")) $("hero-hospital-count").textContent = text;
      if ($("kpi-patient-hospitals")) $("kpi-patient-hospitals").textContent = text;
      if ($("methodology-price-count")) $("methodology-price-count").textContent = text;
      if ($("price-hospital-count")) $("price-hospital-count").textContent = text;
    }
    if (summary.standardized_price_rows && $("price-row-count")) {
      $("price-row-count").textContent = summary.standardized_price_rows.toLocaleString();
    }
    if ($("kpi-compliance")) $("kpi-compliance").textContent = summary.compliance_pct + "%";
    if ($("kpi-missing")) $("kpi-missing").textContent = summary.missing.toLocaleString();
    if ($("kpi-enforcement")) $("kpi-enforcement").textContent = summary.under_enforcement.toLocaleString();
    if ($("kpi-actions")) $("kpi-actions").textContent = summary.enforcement_actions_total.toLocaleString();
    const tb = $("worst-tbody");
    if (tb && summary.worst_offenders) {
      summary.worst_offenders.forEach((h) => {
        const tr = document.createElement("tr");
        tr.className = "hover:bg-zinc-900/70 cursor-pointer";
        tr.addEventListener("click", () => openCCN(h.ccn));
        tr.innerHTML = `
          <td class="px-4 py-2 mono text-xs text-zinc-500">${h.ccn}</td>
          <td class="px-4 py-2">${escapeHtml(h.name)}</td>
          <td class="px-4 py-2 text-zinc-400">${escapeHtml(h.city)}, ${h.state}</td>
          <td class="px-4 py-2 text-right tab-num text-rose-400">${h.enforcement_count}</td>`;
        tb.appendChild(tr);
      });
    }
    const ws = (summary.states || []).slice(0, 12);
    const stb = $("state-bottom-tbody");
    if (stb) {
      ws.forEach((s) => {
        const tr = document.createElement("tr");
        const cls = s.pct < 70 ? "text-rose-400" : s.pct < 85 ? "text-amber-400" : "text-emerald-400";
        tr.innerHTML = `
          <td class="px-4 py-2 mono">${s.state}</td>
          <td class="px-4 py-2 text-right tab-num">${s.total}</td>
          <td class="px-4 py-2 text-right tab-num">${s.live}</td>
          <td class="px-4 py-2 text-right tab-num ${cls}">${s.pct}%</td>`;
        stb.appendChild(tr);
      });
    }
    const tb2 = $("type-bars");
    if (tb2 && summary.types) {
      summary.types.forEach((t) => {
        const color = t.pct < 70 ? "bg-rose-500" : t.pct < 85 ? "bg-amber-500" : "bg-emerald-500";
        tb2.innerHTML += `
          <div>
            <div class="flex justify-between text-sm">
              <span>${escapeHtml(t.type)}</span>
              <span class="tab-num text-zinc-400">${t.live.toLocaleString()} / ${t.total.toLocaleString()} <span class="text-zinc-500">(${t.pct}%)</span></span>
            </div>
            <div class="mt-1 h-2 rounded-full bg-zinc-800 overflow-hidden">
              <div class="h-full ${color}" style="width:${t.pct}%"></div>
            </div>
          </div>`;
      });
    }
  }

  function initSearch() {
    if ($("hosp-count")) $("hosp-count").textContent = hospitals.length.toLocaleString();
    const states = [...new Set(hospitals.map((h) => h.state))].sort();
    const sel = $("state");
    if (sel) {
      sel.innerHTML =
        '<option value="">All states</option>' + states.map((s) => `<option value="${s}">${s}</option>`).join("");
    }
    if ($("q")) $("q").addEventListener("input", debounce(render, 80));
    if ($("state")) $("state").addEventListener("change", render);
    if ($("status")) $("status").addEventListener("change", render);
    render();
  }

  function initCptSearch() {
    const q = $("cpt-q");
    const sort = $("cpt-sort");
    if (!q || !sort) return;
    q.addEventListener("focus", () => {
      if (!cptIndex && !cptIndexLoading) ensureCptIndex();
    });
    q.addEventListener(
      "input",
      debounce(() => {
        if (q.value.trim() && !cptIndex && !cptIndexLoading) ensureCptIndex();
        renderCptSearch();
      }, 120),
    );
    sort.addEventListener("change", () => {
      if (!cptIndex && !cptIndexLoading) ensureCptIndex();
      renderCptSearch();
    });
    renderCptSearch();
  }

  function getPriceIndexHospitals() {
    if (!pricesIndex) return [];
    if (Array.isArray(pricesIndex)) return pricesIndex;
    if (Array.isArray(pricesIndex.hospitals)) return pricesIndex.hospitals;
    return [];
  }

  function getPreviewHospitals() {
    return getPriceIndexHospitals().filter((h) => Number(h.n || h.count || h.items || 0) > 0);
  }

  function renderPriceCoverage() {
    const previewHospitals = getPreviewHospitals();
    const rows = previewHospitals.reduce((sum, h) => sum + Number(h.n || h.count || h.items || 0), 0);
    const codeCount = cptIndex ? Object.keys(cptIndex).length : null;
    const hospitalText = previewHospitals.length ? previewHospitals.length.toLocaleString() : "—";
    const rowText = rows ? rows.toLocaleString() : "—";
    const codeText = codeCount === null ? "Load on search" : codeCount.toLocaleString();
    if ($("price-hospital-count")) $("price-hospital-count").textContent = hospitalText;
    if ($("methodology-price-count")) $("methodology-price-count").textContent = hospitalText;
    if ($("price-row-count")) $("price-row-count").textContent = rowText;
    if ($("price-code-count")) $("price-code-count").textContent = codeText;
    if ($("kpi-patient-hospitals")) $("kpi-patient-hospitals").textContent = hospitalText;
    if ($("hero-hospital-count")) $("hero-hospital-count").textContent = hospitalText;
  }

  async function ensureCptIndex() {
    if (cptIndex || cptIndexLoading) return cptIndexPromise;
    cptIndexLoading = true;
    renderCptSearch();
    cptIndexPromise = fetchOptionalJson("/api/cpt-index")
      .then((data) => {
        cptIndex = data || null;
        cptIndexLoading = false;
        renderPriceCoverage();
        renderCptSearch();
        return cptIndex;
      })
      .catch((e) => {
        cptIndexLoading = false;
        console.warn("cpt index load failed", e);
        renderCptSearch();
        return null;
      });
    return cptIndexPromise;
  }

  function getHospital(ccn) {
    return hospitals.find((h) => String(h.ccn) === String(ccn));
  }

  function isCodeQuery(q) {
    return /^[A-Z]?\d[A-Z0-9]{1,6}$/.test(q);
  }

  function renderCptSearch() {
    const qEl = $("cpt-q");
    const status = $("cpt-status");
    const results = $("cpt-results");
    if (!qEl || !status || !results) return;
    const q = qEl.value.trim().toUpperCase();
    const sort = $("cpt-sort").value;
    const indexedHospitals = getPreviewHospitals();
    const codeCount = cptIndex ? Object.keys(cptIndex).length : 0;
    const hospitalCount = indexedHospitals.length;

    if (!cptIndex) {
      status.textContent = cptIndexLoading
        ? `${hospitalCount.toLocaleString()} hospitals have standardized price previews. Loading the cross-hospital CPT/HCPCS comparison index…`
        : `${hospitalCount.toLocaleString()} hospitals have standardized price previews. Enter a CPT/HCPCS code to load cross-hospital comparisons.`;
      results.innerHTML = "";
      return;
    }
    if (!q) {
      status.textContent = `${codeCount.toLocaleString()} CPT/HCPCS codes indexed across ${hospitalCount.toLocaleString()} hospital${hospitalCount === 1 ? "" : "s"}. Enter a code such as 99213, J9271, or A9543.`;
      results.innerHTML = renderTopCptCodes(sort);
      return;
    }
    if (!isCodeQuery(q)) {
      status.textContent =
        "This compact CPT index currently supports exact or prefix CPT/HCPCS code searches. Try a code like 99213, J9271, or A9543.";
      results.innerHTML = "";
      return;
    }
    const matches = Object.keys(cptIndex).filter((code) => code.startsWith(q));
    if (sort === "coverage") {
      matches.sort((a, b) => (cptIndex[b] || []).length - (cptIndex[a] || []).length || a.localeCompare(b));
    } else {
      matches.sort((a, b) => a.localeCompare(b));
    }
    if (!matches.length) {
      status.textContent = `No standardized price rows found for code prefix ${q}.`;
      results.innerHTML = "";
      return;
    }
    status.textContent = `${matches.length.toLocaleString()} matching code${matches.length === 1 ? "" : "s"} for ${q}. Showing hospital price rows from the standardized index.`;
    results.innerHTML = matches.slice(0, 20).map((code) => renderCptCodeResult(code, cptIndex[code])).join("");
  }

  function renderTopCptCodes(sort) {
    const codes = Object.keys(cptIndex || {});
    if (sort === "coverage") {
      codes.sort((a, b) => (cptIndex[b] || []).length - (cptIndex[a] || []).length || a.localeCompare(b));
    } else {
      codes.sort((a, b) => a.localeCompare(b));
    }
    return codes.slice(0, 10).map((code) => renderCptCodeResult(code, cptIndex[code], true)).join("");
  }

  function renderCptCodeResult(code, rows, compact = false) {
    const safeCode = escapeHtml(code);
    const shown = (rows || []).slice(0, compact ? 5 : 25);
    const body = shown
      .map((row) => {
        const h = getHospital(row.ccn) || {};
        const name = h.name || row.name || `CCN ${row.ccn}`;
        const place = [h.city, h.state].filter(Boolean).join(", ");
        return `
        <tr class="border-t border-zinc-800 hover:bg-zinc-900/70 cursor-pointer" data-open-ccn="${escapeHtml(row.ccn)}">
          <td class="px-3 py-2 mono text-xs text-zinc-500">${escapeHtml(row.ccn)}</td>
          <td class="px-3 py-2">
            <div class="font-medium text-zinc-100">${escapeHtml(name)}</div>
            <div class="text-xs text-zinc-500">${escapeHtml(place || h.type || "")}</div>
          </td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(row.gross)}</td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(row.cash)}</td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(row.min)}</td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(row.max)}</td>
          <td class="px-3 py-2 text-right tab-num">
            ${formatMoney(firstValue(row.payer_max, row.payerMax))}
            ${Number(row.pc || row.payer_count || 0) ? `<div class="text-xs text-zinc-600">${Number(row.pc || row.payer_count).toLocaleString()} rates</div>` : ""}
          </td>
        </tr>`;
      })
      .join("");
    const more =
      (rows || []).length > shown.length
        ? `<div class="px-3 py-2 text-xs text-zinc-500">+ ${((rows || []).length - shown.length).toLocaleString()} more hospital${(rows || []).length - shown.length === 1 ? "" : "s"} for this code.</div>`
        : "";
    return `
      <div class="rounded-md border border-zinc-800 bg-zinc-950/40 overflow-hidden mb-3">
        <div class="px-3 py-2 flex items-center justify-between gap-3 bg-zinc-900/80">
          <div>
            <div class="mono text-emerald-300">${safeCode}</div>
            <div class="text-xs text-zinc-500">${(rows || []).length.toLocaleString()} hospital${(rows || []).length === 1 ? "" : "s"} with standardized prices</div>
          </div>
        </div>
        <div class="overflow-x-auto">
          <table class="min-w-full text-sm">
            <thead class="text-xs uppercase tracking-wider text-zinc-500">
              <tr><th class="px-3 py-2 text-left font-medium">CCN</th><th class="px-3 py-2 text-left font-medium">Hospital</th><th class="px-3 py-2 text-right font-medium">Gross</th><th class="px-3 py-2 text-right font-medium">Cash</th><th class="px-3 py-2 text-right font-medium">Min</th><th class="px-3 py-2 text-right font-medium">Max</th><th class="px-3 py-2 text-right font-medium">Payer max</th></tr>
            </thead>
            <tbody>${body}</tbody>
          </table>
        </div>
        ${more}
      </div>`;
  }

  function render() {
    const q = ($("q")?.value || "").toLowerCase().trim();
    const st = $("state")?.value || "";
    const status = $("status")?.value || "";
    let out = hospitals;
    if (q) {
      out = out.filter((h) =>
        h.name.toLowerCase().includes(q) || h.city.toLowerCase().includes(q) || h.ccn.includes(q),
      );
    }
    if (st) out = out.filter((h) => h.state === st);
    if (status === "compliant") out = out.filter((h) => h.has_live_mrf);
    else if (status === "missing") out = out.filter((h) => !h.has_live_mrf && h.required);
    else if (status === "enforcement") out = out.filter((h) => (h.enforcement_count || 0) > 0);
    else if (status === "missing-enforcement")
      out = out.filter((h) => !h.has_live_mrf && (h.enforcement_count || 0) > 0);

    if ($("results-count")) {
      $("results-count").textContent = `${out.length.toLocaleString()} hospital${out.length === 1 ? "" : "s"}`;
    }
    const container = $("results");
    if (!container) return;
    container.innerHTML = "";
    out.slice(0, 200).forEach((h) => container.appendChild(card(h)));
    if (out.length > 200) {
      const more = document.createElement("div");
      more.className = "text-center text-sm text-zinc-500 py-3";
      more.textContent = `+ ${(out.length - 200).toLocaleString()} more — refine your search`;
      container.appendChild(more);
    }
  }

  function card(h) {
    const div = document.createElement("div");
    div.className =
      "rounded-md border border-zinc-800 bg-zinc-900/50 p-3 flex items-center gap-3 hover:border-zinc-700 cursor-pointer fade-in";
    div.addEventListener("click", () => openCCN(h.ccn));
    div.innerHTML = `
      <div class="flex-1 min-w-0">
        <div class="font-medium text-zinc-100 truncate">${escapeHtml(h.name)}</div>
        <div class="text-xs text-zinc-500 mt-0.5">
          <span class="mono">${h.ccn}</span> · ${escapeHtml(h.city)}, ${h.state} · ${escapeHtml(h.type)}
        </div>
      </div>
      <div class="flex flex-col items-end gap-1 shrink-0">${badge(h)}</div>`;
    return div;
  }

  function badge(h) {
    const parts = [];
    if (h.has_live_mrf) {
      parts.push(
        `<span class="text-xs px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">✓ Live MRF</span>`,
      );
    } else if (h.required) {
      parts.push(
        `<span class="text-xs px-2 py-0.5 rounded-full bg-rose-500/15 text-rose-300 border border-rose-500/30">✗ Missing</span>`,
      );
    }
    if ((h.enforcement_count || 0) > 0) {
      parts.push(
        `<span class="text-xs px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30">${h.enforcement_count} CMS action${h.enforcement_count === 1 ? "" : "s"}</span>`,
      );
    }
    return parts.join(" ");
  }

  function renderWorst() {}
  function renderStates() {}
  function renderTypes() {}

  function openCCN(ccn) {
    const h = hospitals.find((x) => x.ccn === ccn);
    if (!h) return;
    history.replaceState(null, "", "#h/" + ccn);
    const enfHtml = (h.enforcement_actions || [])
      .map(
        (a) => `<li class="py-1 border-l-2 border-amber-500/40 pl-3 text-sm">
       <span class="text-zinc-400 mono text-xs">${a.date}</span>
       <span class="ml-2">${escapeHtml(a.action)}</span>
     </li>`,
      )
      .join("");
    const mrfHtml = h.has_live_mrf
      ? `
      <div class="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-4 mt-4">
        <div class="text-xs uppercase tracking-wider text-emerald-400">Live machine-readable file</div>
        <a href="${escapeHtml(h.mrf_url)}" target="_blank" rel="noopener" class="mono text-sm text-emerald-300 hover:underline break-all block mt-1">${escapeHtml(h.mrf_url)}</a>
        <div class="text-xs text-zinc-500 mt-2">
          Source: ${escapeHtml(h.mrf_source || "unknown")} · Last verified ${(h.mrf_verified || "").slice(0, 10) || "—"}
          ${h.mrf_bytes ? " · " + formatBytes(h.mrf_bytes) : ""}
        </div>
      </div>`
      : `
      <div class="rounded-md border border-rose-500/30 bg-rose-500/5 p-4 mt-4">
        <div class="text-xs uppercase tracking-wider text-rose-400">No live MRF found</div>
        <p class="text-sm text-zinc-300 mt-1">
          This hospital is required by <a href="https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180" class="underline text-rose-300">45 CFR § 180</a> to publish a machine-readable file.
          After exhaustive automated probing (CMS-HPT marker files, page parsing, Exa search, Claude agents, Wayback Machine), no live file was found.
        </p>
      </div>`;
    if ($("modal-content")) {
      $("modal-content").innerHTML = `
        <div class="text-xs uppercase tracking-widest text-zinc-500">CCN <span class="mono">${escapeHtml(h.ccn)}</span></div>
        <h3 class="text-2xl font-semibold tracking-tight mt-1">${escapeHtml(h.name)}</h3>
        <div class="text-zinc-400 mt-1">${escapeHtml(h.city)}, ${escapeHtml(h.state)} · ${escapeHtml(h.type)} · ${escapeHtml(h.ownership || "")}</div>
        ${mrfHtml}
        <div id="price-preview" class="rounded-md border border-zinc-800 bg-zinc-950/40 p-4 mt-4">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="text-xs uppercase tracking-wider text-emerald-400">Standardized price preview</div>
              <div class="text-sm text-zinc-400 mt-1">Loading on-site price rows…</div>
            </div>
          </div>
        </div>
        ${enfHtml ? `<div class="mt-5"><div class="text-xs uppercase tracking-wider text-amber-400">CMS enforcement history</div><ol class="mt-2 space-y-0">${enfHtml}</ol></div>` : ""}
        <div class="mt-5 text-xs text-zinc-500">
          <a href="https://npiregistry.cms.hhs.gov/search?keyword=${encodeURIComponent(h.name)}&state=${encodeURIComponent(h.state)}" target="_blank" class="underline hover:text-zinc-300">NPI Registry</a> ·
          <a href="https://data.cms.gov/provider-data/dataset/xubh-q36u" target="_blank" class="underline hover:text-zinc-300">CMS Hospital Compare</a> ·
          <a href="https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/hospital-price-transparency-enforcement-activities-and-outcomes" target="_blank" class="underline hover:text-zinc-300">CMS Enforcement record</a>
        </div>`;
    }
    $("modal")?.classList.remove("hidden");
    renderHospitalPricePreview(ccn);
  }

  function closeModal() {
    $("modal")?.classList.add("hidden");
    history.replaceState(null, "", location.pathname);
  }

  document.addEventListener("keydown", (e) => e.key === "Escape" && closeModal());

  // Modal close button + outside-click
  document.addEventListener("click", (e) => {
    const t = e.target;
    if (t && t.matches && t.matches("[data-close-modal]")) closeModal();
    if (t === $("modal")) closeModal();
    // Delegated open-CCN from CPT search rows
    const tr = t && t.closest && t.closest("[data-open-ccn]");
    if (tr) openCCN(tr.getAttribute("data-open-ccn"));
  });

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }
  function formatBytes(b) {
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
    return (b / 1024 / 1024).toFixed(1) + " MB";
  }
  function formatMoney(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return "$" + n.toLocaleString(undefined, { maximumFractionDigits: n < 100 ? 2 : 0 });
  }
  function firstValue(...values) {
    return values.find((v) => v !== undefined && v !== null);
  }

  async function renderHospitalPricePreview(ccn) {
    const el = $("price-preview");
    if (!el) return;
    const data = await fetchOptionalJson(`/api/prices/${encodeURIComponent(ccn)}`);
    if (!el || location.hash !== "#h/" + ccn) return;
    if (!el || !data || !Array.isArray(data.items) || !data.items.length) {
      el.innerHTML = `
        <div class="text-xs uppercase tracking-wider text-zinc-500">Standardized price preview</div>
        <p class="text-sm text-zinc-400 mt-1">No standardized price rows have been generated for this hospital yet.</p>`;
      return;
    }
    const rows = data.items.slice(0, 25).map((item) => {
      const payers = Array.isArray(item.payers) ? item.payers.slice(0, 3) : [];
      const payerHtml = payers.length
        ? payers.map((p) => `<div class="truncate">${escapeHtml(p.p || p.payer || "Payer")} <span class="text-zinc-500">${formatMoney(firstValue(p.r, p.rate, p.rate_dollar))}</span></div>`).join("")
        : '<span class="text-zinc-600">—</span>';
      return `
        <tr class="border-t border-zinc-800 align-top">
          <td class="px-3 py-2 mono text-xs text-emerald-300">${escapeHtml(item.code)}</td>
          <td class="px-3 py-2 text-xs text-zinc-500">${escapeHtml(item.type || item.code_type || "")}</td>
          <td class="px-3 py-2 min-w-64">${escapeHtml(item.desc || item.description || "")}</td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(firstValue(item.gross, item.gross_charge))}</td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(firstValue(item.cash, item.cash_discount))}</td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(firstValue(item.min, item.min_negotiated))}</td>
          <td class="px-3 py-2 text-right tab-num">${formatMoney(firstValue(item.max, item.max_negotiated))}</td>
          <td class="px-3 py-2 text-xs text-zinc-400 max-w-56">${payerHtml}</td>
        </tr>`;
    }).join("");
    el.innerHTML = `
      <div class="flex items-start justify-between gap-3">
        <div>
          <div class="text-xs uppercase tracking-wider text-emerald-400">Standardized price preview</div>
          <div class="text-sm text-zinc-400 mt-1">${escapeHtml(data.hospital_name || "")} · ${firstValue(data.n_slim, data.items.length).toLocaleString()} standardized row${firstValue(data.n_slim, data.items.length) === 1 ? "" : "s"}</div>
        </div>
        <div class="text-xs text-zinc-500 shrink-0">First ${Math.min(25, data.items.length).toLocaleString()}</div>
      </div>
      <div class="mt-3 overflow-x-auto rounded-md border border-zinc-800">
        <table class="min-w-full text-sm">
          <thead class="bg-zinc-900 text-xs uppercase tracking-wider text-zinc-500">
            <tr><th class="px-3 py-2 text-left font-medium">Code</th><th class="px-3 py-2 text-left font-medium">Type</th><th class="px-3 py-2 text-left font-medium">Description</th><th class="px-3 py-2 text-right font-medium">Gross</th><th class="px-3 py-2 text-right font-medium">Cash</th><th class="px-3 py-2 text-right font-medium">Min</th><th class="px-3 py-2 text-right font-medium">Max</th><th class="px-3 py-2 text-left font-medium">Payer examples</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  // Patient-facing entry-point handlers
  const COMMON_PROCEDURES = window.CPT_NAMES || {};

  let datalistBuilt = false;
  const buildDatalist = (filter) => {
    const dl = document.getElementById("proc-suggestions");
    if (!dl) return;
    dl.innerHTML = "";
    const f = (filter || "").trim().toLowerCase();
    let entries = Object.entries(COMMON_PROCEDURES);
    if (f.length >= 2) {
      entries = entries.filter(([code, desc]) => code.toLowerCase().includes(f) || desc.toLowerCase().includes(f));
    }
    entries.slice(0, 25).forEach(([code, desc]) => {
      const opt = document.createElement("option");
      opt.value = code;
      opt.label = `${code} — ${desc}`;
      opt.textContent = `${code} — ${desc}`;
      dl.appendChild(opt);
    });
  };

  const resolveQuery = (raw) => {
    const q = (raw || "").trim();
    if (!q) return { code: null, msg: "" };
    const upper = q.toUpperCase();
    if (/^[A-Z0-9]{4,7}$/.test(upper)) {
      return { code: upper, msg: "" };
    }
    const lower = q.toLowerCase();
    const matches = Object.entries(COMMON_PROCEDURES).filter(([, desc]) => desc.toLowerCase().includes(lower));
    if (matches.length === 1) return { code: matches[0][0], msg: `Matched: ${matches[0][1]}` };
    if (matches.length > 1)
      return {
        code: null,
        msg: `${matches.length} matches — pick one from the dropdown (or type the exact 5-digit CPT code).`,
      };
    return { code: null, msg: `No match for "${q}". Try a CPT/HCPCS code (e.g. 45378) or a common name (e.g. "colonoscopy").` };
  };

  const procForm = document.getElementById("by-proc-form");
  const procInput = document.getElementById("by-proc-input");
  const procMsg = document.getElementById("by-proc-msg");

  if (procInput) {
    procInput.addEventListener("focus", () => {
      if (!datalistBuilt) {
        buildDatalist("");
        datalistBuilt = true;
      }
    });
    procInput.addEventListener("input", () => {
      buildDatalist(procInput.value);
      if (procMsg) procMsg.textContent = "";
    });
  }

  if (procForm) {
    procForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const raw = procInput.value;
      const { code, msg } = resolveQuery(raw);
      if (!code) {
        if (procMsg) procMsg.textContent = msg;
        return;
      }
      if (procMsg) procMsg.textContent = `Checking ${code}…`;
      try {
        const r = await fetch("/api/procedure/" + encodeURIComponent(code), { cache: "no-store" });
        if (r.status === 404) {
          procMsg.textContent = `Code ${code} isn't in our top 5,000 indexed procedures yet. Try a more common procedure or browse hospital prices directly.`;
          return;
        }
        if (!r.ok) {
          procMsg.textContent = `Couldn't load ${code} (HTTP ${r.status}). Try again.`;
          return;
        }
      } catch {
        // network error — destination page handles 404 itself
      }
      location.href = "/procedure/" + encodeURIComponent(code);
    });
  }

  const hospForm = document.getElementById("by-hosp-form");
  if (hospForm) {
    hospForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const v = document.getElementById("by-hosp-input").value.trim();
      if (/^\d{6}$/.test(v)) {
        location.href = "/hospital/" + v;
      } else if (v) {
        const q = document.getElementById("q");
        if (q) {
          q.value = v;
          q.dispatchEvent(new Event("input"));
          document.getElementById("search").scrollIntoView({ behavior: "smooth" });
        }
      }
    });
  }

  const payerForm = document.getElementById("by-payer-form");
  const payerSel = document.getElementById("by-payer-select");
  if (payerForm && payerSel) {
    fetch("/api/payers-index")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data || !data.featured) {
          payerSel.innerHTML = '<option value="">No payer index yet</option>';
          return;
        }
        payerSel.innerHTML =
          '<option value="">Choose your insurance…</option>' +
          data.featured.map((p) => `<option value="${p.slug}">${p.display} (${p.hospital_count.toLocaleString()} hospitals)</option>`).join("");
      })
      .catch(() => {
        payerSel.innerHTML = '<option value="">Failed to load</option>';
      });
    payerForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const slug = payerSel.value;
      if (slug) location.href = "/payer/" + encodeURIComponent(slug);
    });
  }

  load();
})();
