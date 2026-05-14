import { Hono } from "hono";
import { CPT_NAMES } from "./lib/cpt-names";
import { readR2Json } from "./lib/r2";
import { complianceRankingHandler } from "./routes/api/compliance-ranking";
import { cptIndexHandler } from "./routes/api/cpt-index";
import { payerHandler } from "./routes/api/payer";
import { payersIndexHandler } from "./routes/api/payers-index";
import { pricesHandler } from "./routes/api/prices";
import { pricesIndexHandler } from "./routes/api/prices-index";
import { procedureHandler } from "./routes/api/procedure";
import { homePageHandler } from "./routes/home";
import { hospitalPageHandler } from "./routes/hospital";
import { payerPageHandler } from "./routes/payer";
import { procedurePageHandler } from "./routes/procedure";

export type Env = {
  Bindings: {
    HL_MRF_PARSED: R2Bucket;
    HL_MRF_RAW: R2Bucket;
    ASSETS: Fetcher;
    SITE_NAME: string;
  };
};

const app = new Hono<Env>();

// Security headers — ports site/_headers (defense-in-depth at the edge).
app.use("*", async (c, next) => {
  await next();
  const h = c.res.headers;
  if (!h.has("X-Frame-Options")) h.set("X-Frame-Options", "DENY");
  if (!h.has("X-Content-Type-Options")) h.set("X-Content-Type-Options", "nosniff");
  if (!h.has("Referrer-Policy")) h.set("Referrer-Policy", "strict-origin-when-cross-origin");
  if (!h.has("Permissions-Policy")) h.set("Permissions-Policy", "interest-cohort=()");
  if (!h.has("Strict-Transport-Security")) {
    h.set("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload");
  }
});

// URL normalization: 301-redirect trailing-slash and uppercase entity paths
// to a single canonical form. Skips /api/* (case-/slash-strict by contract).
app.use("*", async (c, next) => {
  const url = new URL(c.req.url);
  if (url.pathname.startsWith("/api/")) return next();
  // Trailing slash → no slash (except root)
  if (url.pathname.length > 1 && url.pathname.endsWith("/")) {
    return c.redirect(url.pathname.slice(0, -1) + url.search, 301);
  }
  // Lowercase only the entity prefix paths (the params themselves are already canonical)
  const lowered = url.pathname.toLowerCase();
  if (lowered !== url.pathname && /^\/(procedure|payer|hospital)\//i.test(url.pathname)) {
    return c.redirect(lowered + url.search, 301);
  }
  return next();
});

// API routes (preserve byte-similar shapes with the legacy Pages Functions).
app.get("/api/prices-index", pricesIndexHandler);
app.get("/api/cpt-index", cptIndexHandler);
app.get("/api/payers-index", payersIndexHandler);
app.get("/api/compliance-ranking", complianceRankingHandler);
app.get("/api/prices/:ccn", pricesHandler);
app.get("/api/payer/:slug", payerHandler);
app.get("/api/procedure/:code", procedureHandler);

// Legacy /api/{hospitals,summary}.json shorthands → /data fallback.
app.get("/api/hospitals.json", (c) => c.redirect("/data/hospitals.json", 301));
app.get("/api/summary.json", (c) => c.redirect("/data/summary.json", 301));

// XML sitemap — top procedures, top payers, top hospitals, plus the home page.
app.get("/sitemap.xml", async (c) => {
  const SITE = "https://hospitalledger.com";
  const lastmod = new Date().toISOString().slice(0, 10);

  // Hospitals: try R2 prices/index.json first; fall back to static
  // /data/hospitals.json filtered to has_live_mrf=true.
  let hospitalCcns: string[] = [];
  const pricesIndex = await readR2Json<{ hospitals?: { ccn?: string }[] }>(
    c.env.HL_MRF_PARSED,
    "prices/index.json",
  );
  if (pricesIndex?.hospitals?.length) {
    hospitalCcns = pricesIndex.hospitals
      .map((h) => String(h.ccn ?? ""))
      .filter(Boolean)
      .slice(0, 200);
  } else if (c.env.ASSETS && typeof c.env.ASSETS.fetch === "function") {
    const url = new URL(c.req.url);
    url.pathname = "/data/hospitals.json";
    url.search = "";
    const resp = await c.env.ASSETS.fetch(url.toString());
    if (resp.ok) {
      const arr = (await resp.json()) as Array<{ ccn?: string; has_live_mrf?: boolean }>;
      hospitalCcns = arr
        .filter((h) => h.has_live_mrf && h.ccn)
        .map((h) => String(h.ccn))
        .slice(0, 200);
    }
  }

  // Payers: R2 only (no static fallback); skip silently if missing.
  let payerSlugs: string[] = [];
  const payersIndex = await readR2Json<{ featured?: { slug?: string }[]; payers?: { slug?: string }[] }>(
    c.env.HL_MRF_PARSED,
    "aggregates/payers-index.json",
  );
  const payerList = payersIndex?.featured ?? payersIndex?.payers ?? [];
  if (payerList.length) {
    payerSlugs = payerList
      .map((p) => String(p.slug ?? ""))
      .filter(Boolean)
      .slice(0, 100);
  }

  // CPT codes: full curated map (103 entries).
  const cptCodes = Object.keys(CPT_NAMES);

  const urls: string[] = [
    `<url><loc>${SITE}/</loc><lastmod>${lastmod}</lastmod><priority>1.0</priority></url>`,
  ];
  for (const code of cptCodes) {
    urls.push(
      `<url><loc>${SITE}/procedure/${code}</loc><lastmod>${lastmod}</lastmod><priority>0.9</priority></url>`,
    );
  }
  for (const slug of payerSlugs) {
    urls.push(
      `<url><loc>${SITE}/payer/${slug}</loc><lastmod>${lastmod}</lastmod><priority>0.8</priority></url>`,
    );
  }
  for (const ccn of hospitalCcns) {
    urls.push(
      `<url><loc>${SITE}/hospital/${ccn}</loc><lastmod>${lastmod}</lastmod><priority>0.7</priority></url>`,
    );
  }

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.join("\n")}
</urlset>`;

  return new Response(xml, {
    headers: {
      "content-type": "application/xml; charset=utf-8",
      "cache-control": "public, max-age=3600",
    },
  });
});

// SSR pages — preserve original URLs (/procedure/:code, /payer/:slug, /hospital/:ccn).
app.get("/procedure/:code", procedurePageHandler);
app.get("/payer/:slug", payerPageHandler);
app.get("/hospital/:ccn", hospitalPageHandler);

// SSR home page.
app.get("/", homePageHandler);

export default app;
