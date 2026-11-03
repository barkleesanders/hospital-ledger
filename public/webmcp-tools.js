// webmcp-tools.js — WebMCP tool definitions for hospitalledger.com (Tier 1, /ship Phase 1.4w).
// Loaded before /webmcp.js, which registers these with document.modelContext where available.
// Tools call the SAME data paths the site's own UI uses — no parallel backend:
//   search_hospitals       -> GET /data/hospitals.json   (home-client.js initSearch/render)
//   get_procedure_prices   -> GET /api/procedure/:code   (home-client.js by-proc form)
//   get_hospital_prices    -> GET /api/prices/:ccn       (home-client.js renderHospitalPricePreview)
//   navigate_to_hospital   -> /hospital/:ccn             (the site's own hospital route)
// Spec + rules: shared/webmcp.md in the ship skill. Inventory: WEBMCP.md in this repo.
// Usage beacon opt-in (shared/webmcp.md "Usage beacon"): the registrar POSTs
// anonymous register/execute events (tool names + arg key names, never values)
// to this endpoint; stored as R2 objects under webmcp-beacon/ (see WEBMCP.md).
window.WEBMCP_BEACON_URL = "/api/webmcp-beacon";
window.WEBMCP_TOOLS = [
  {
    name: "search_hospitals",
    description: "Search U.S. hospitals by name, city, or CCN in the Hospital Ledger dataset and return matching hospitals with city, state, type, live price-file status, and their Hospital Ledger URL.",
    webmcpClass: "answer",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Hospital name, city, or CCN fragment, e.g. 'Alaska Native' or 'Anchorage'" },
        state: { type: "string", description: "Optional 2-letter state filter, e.g. 'AK'" },
      },
      required: ["query"],
    },
    execute: async ({ query, state }) => {
      try {
        const q = String(query || "").trim().toLowerCase();
        if (!q) return { error: "query is required" };
        const st = String(state || "").trim().toUpperCase();
        if (st && !/^[A-Z]{2}$/.test(st)) return { error: "state must be a 2-letter state code" };
        // Same fetch + filter logic as public/home-client.js (cached per page, as the UI loads it once).
        if (!window.__HL_HOSPITALS) {
          const r = await fetch("/data/hospitals.json");
          if (!r.ok) return { error: "hospital index unavailable" };
          window.__HL_HOSPITALS = await r.json();
        }
        const hospitals = window.__HL_HOSPITALS;
        let out = hospitals.filter(
          (h) =>
            String(h.name || "").toLowerCase().includes(q) ||
            String(h.city || "").toLowerCase().includes(q) ||
            String(h.ccn || "").toLowerCase().includes(q),
        );
        if (st) out = out.filter((h) => h.state === st);
        const total = out.length;
        const hits = out.slice(0, 10).map((h) => ({
          ccn: h.ccn,
          name: h.name,
          city: h.city,
          state: h.state,
          type: h.type,
          has_live_mrf: !!h.has_live_mrf,
          required: !!h.required,
          url: "/hospital/" + h.ccn,
        }));
        return {
          content: [{
            type: "text",
            text: JSON.stringify({ query: q, state: st || undefined, total_matches: total, hospitals: hits }),
          }],
        };
      } catch { return { error: "hospital search failed" }; }
    },
  },
  {
    name: "get_procedure_prices",
    description: "Compare published prices for one CPT or HCPCS procedure code across U.S. hospitals — gross, cash, and min/max negotiated prices plus site-wide median cash price — from Hospital Ledger's cross-hospital procedure index.",
    webmcpClass: "answer",
    inputSchema: {
      type: "object",
      properties: {
        code: { type: "string", description: "CPT or HCPCS code, e.g. '99213' (office visit) or '45378' (colonoscopy)" },
      },
      required: ["code"],
    },
    execute: async ({ code }) => {
      try {
        const c = String(code || "").trim().toUpperCase();
        if (!/^[A-Z0-9][A-Z0-9-]{1,9}$/.test(c)) return { error: "code must be a CPT/HCPCS code, e.g. 99213" };
        const r = await fetch("/api/procedure/" + encodeURIComponent(c));
        if (r.status === 404) return { error: "procedure " + c + " is not in the indexed top procedures" };
        if (!r.ok) return { error: "procedure index unavailable" };
        const data = await r.json();
        const hospitals = (data.hospitals || []).slice(0, 10).map((h) => ({
          ccn: h.ccn,
          name: h.name,
          city: h.city,
          state: h.state,
          gross: h.gross,
          cash: h.cash,
          min: h.min,
          max: h.max,
          url: "/hospital/" + h.ccn,
        }));
        return {
          content: [{
            type: "text",
            text: JSON.stringify({
              code: data.code || c,
              description: data.desc || "",
              type: data.type || "",
              stats: data.stats || {},
              hospitals_shown: hospitals.length,
              hospitals,
              url: "/procedure/" + c,
            }),
          }],
        };
      } catch { return { error: "procedure price lookup failed" }; }
    },
  },
  {
    name: "get_hospital_prices",
    description: "Return a hospital's published standardized price rows (code, description, gross, cash, and min/max negotiated prices) from Hospital Ledger, by the hospital's 6-character CCN.",
    webmcpClass: "answer",
    inputSchema: {
      type: "object",
      properties: {
        ccn: { type: "string", description: "Hospital CCN, 6 characters, e.g. '020026'" },
      },
      required: ["ccn"],
    },
    execute: async ({ ccn }) => {
      try {
        const id = String(ccn || "").trim().toUpperCase();
        if (!/^[0-9A-Z]{6}$/.test(id)) return { error: "ccn must be 6 alphanumeric characters" };
        const r = await fetch("/api/prices/" + encodeURIComponent(id));
        if (r.status === 404) return { error: "no published price rows found for hospital " + id };
        if (!r.ok) return { error: "hospital price file unavailable" };
        const data = await r.json();
        const items = (data.items || []).slice(0, 15).map((it) => ({
          code: it.code,
          type: it.type,
          description: it.desc,
          gross: it.gross,
          cash: it.cash,
          min: it.min,
          max: it.max,
        }));
        return {
          content: [{
            type: "text",
            text: JSON.stringify({
              ccn: data.ccn || id,
              hospital_name: data.hospital_name || "",
              total_price_rows: data.n_slim ?? (data.items || []).length,
              items_shown: items.length,
              items,
              url: "/hospital/" + id,
            }),
          }],
        };
      } catch { return { error: "hospital price lookup failed" }; }
    },
  },
  {
    name: "navigate_to_hospital",
    description: "Navigate the visitor's browser to a hospital's Hospital Ledger page, by its 6-character CCN. Only internal /hospital/<ccn> paths are used.",
    webmcpClass: "action",
    inputSchema: {
      type: "object",
      properties: {
        ccn: { type: "string", description: "Hospital CCN, 6 characters, e.g. '020026'" },
      },
      required: ["ccn"],
    },
    execute: async ({ ccn }) => {
      const id = String(ccn || "").trim().toUpperCase();
      if (!/^[0-9A-Z]{6}$/.test(id)) return { error: "ccn must be 6 alphanumeric characters" };
      const path = "/hospital/" + id;
      window.location.assign(path);
      return { content: [{ type: "text", text: "Navigating to " + path }] };
    },
  },
];
