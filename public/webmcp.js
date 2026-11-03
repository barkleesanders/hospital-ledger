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

  tools.forEach(function (tool) {
    // registerTool returns a Promise on Chrome 151+, threw synchronously before: await+catch covers both.
    Promise.resolve()
      .then(function () { return mc.registerTool(tool, opts); })
      .catch(function () { /* duplicate name / policy rejection: page keeps working */ });
  });
})();

// --- Per-site tool definitions go in the repo, e.g.: ---
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
