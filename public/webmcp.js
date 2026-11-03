// webmcp.js — canonical WebMCP drop-in for Barklee's Cloudflare Worker sites.
// Copy into the site's public/ (or bundle it), load with <script src="/webmcp.js" defer>,
// and define this site's tools in window.WEBMCP_TOOLS below / an adjacent file.
// Rules (shared/webmcp.md): feature-detect, no-op where unsupported, tools call
// the SAME functions the page's buttons call, sensitive tools confirm in-page first.
(function () {
  var mc = (typeof document !== "undefined" && document.modelContext) ||
           (typeof navigator !== "undefined" && navigator.modelContext);
  if (!mc || typeof mc.registerTool !== "function") return; // unsupported browser: normal UI untouched

  var tools = window.WEBMCP_TOOLS || [];
  var ctl = ("AbortController" in window) ? new AbortController() : null;
  var opts = ctl ? { signal: ctl.signal } : undefined;

  // SPA navigation: unregister this document's tools when the app tears down.
  window.addEventListener("pagehide", function () { if (ctl) ctl.abort(); }, { once: true });

  // ===== WebMCP usage beacon (optional — see shared/webmcp.md "Usage beacon") =====
  // A site opts in by setting window.WEBMCP_BEACON_URL (e.g. "/api/webmcp-beacon")
  // in its tools file, loaded BEFORE this registrar. Unset = no beaconing.
  // Fires ONE { event: "register", tools: [names], path } after registration
  // settles, and one { event: "execute", tool, argKeys, path } per tool call.
  // NEVER argument values — key names only. Transport: navigator.sendBeacon,
  // fallback fetch(keepalive). Every failure is swallowed: tool behavior,
  // outputs and errors are identical whether or not the endpoint exists.
  var BEACON_URL = (typeof window !== "undefined" && window.WEBMCP_BEACON_URL) || null;
  function pagePath() { try { return window.location.pathname || ""; } catch (e) { return ""; } }
  function beacon(payload) {
    if (!BEACON_URL) return;
    try {
      var body = JSON.stringify(payload);
      var sent = false;
      if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
        try {
          sent = navigator.sendBeacon(BEACON_URL, new Blob([body], { type: "application/json" }));
        } catch (e) { sent = false; }
      }
      if (!sent && typeof fetch === "function") {
        fetch(BEACON_URL, {
          method: "POST",
          body: body,
          headers: { "Content-Type": "application/json" },
          keepalive: true
        }).catch(function () {});
      }
    } catch (e) { /* the beacon must never break the page */ }
  }
  function instrument(tool) {
    if (!BEACON_URL || !tool || typeof tool.execute !== "function") return tool;
    var copy = {};
    for (var k in tool) copy[k] = tool[k];
    var orig = tool.execute;
    copy.execute = function (args) {
      beacon({
        event: "execute",
        tool: tool.name,
        argKeys: (args && typeof args === "object") ? Object.keys(args) : [],
        path: pagePath()
      });
      return orig.apply(this, arguments); // untouched pass-through: same args, same return/throw
    };
    return copy;
  }
  // ===== end beacon block =====

  var registeredNames = [];
  var registrations = tools.map(function (tool) {
    var t = instrument(tool);
    // registerTool returns a Promise on Chrome 151+, threw synchronously before: await+catch covers both.
    return Promise.resolve()
      .then(function () { return mc.registerTool(t, opts); })
      .then(function () { registeredNames.push(t.name); })
      .catch(function () { /* duplicate name / policy rejection: page keeps working */ });
  });

  if (BEACON_URL && registrations.length) {
    var fireRegister = function () {
      if (registeredNames.length) {
        beacon({ event: "register", tools: registeredNames.slice(), path: pagePath() });
      }
    };
    if (typeof Promise.allSettled === "function") Promise.allSettled(registrations).then(fireRegister);
    else Promise.all(registrations).then(fireRegister, fireRegister);
  }
})();

// --- Per-site tool definitions go in the repo, e.g.: ---
// window.WEBMCP_BEACON_URL = "/api/webmcp-beacon"; // opt-in usage beacon (see above)
// window.WEBMCP_TOOLS = [{
//   name: "search_site",
//   description: "Search this site and return matching pages with titles and URLs.",
//   webmcpClass: "answer",
//   inputSchema: { type: "object", properties: { query: { type: "string", description: "Search terms" } }, required: ["query"] },
//   execute: async ({ query }) => {
//     try {
//       const r = await fetch("/api/search?q=" + encodeURIComponent(String(query || "")));
//       const data = await r.json();
//       return { content: [{ type: "text", text: JSON.stringify(data) }] };
//     } catch (e) { return { error: "search failed" }; } // return {error}, never throw — messages are discarded
//   }
// }];
