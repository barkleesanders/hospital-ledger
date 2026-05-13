import { Hono } from "hono";
import { complianceRankingHandler } from "./routes/api/compliance-ranking";
import { cptIndexHandler } from "./routes/api/cpt-index";
import { payerHandler } from "./routes/api/payer";
import { payersIndexHandler } from "./routes/api/payers-index";
import { pricesHandler } from "./routes/api/prices";
import { pricesIndexHandler } from "./routes/api/prices-index";
import { procedureHandler } from "./routes/api/procedure";

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

// Placeholder root — replaced by SSR home in Phase 4.
app.get("/", (c) => c.text("ok"));

export default app;
