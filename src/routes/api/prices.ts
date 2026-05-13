/**
 * GET /api/prices/:ccn
 *
 * Per-hospital standardized price preview. Tries:
 *   1. R2 prices/{ccn}.json (canonical, pre-built slim payload)
 *   2. ASSETS /data/prices/{ccn}.json (older static fallback)
 *   3. R2 parsed/{ccn}.json (raw parser output, slimmed on the fly)
 *
 * Ports site/functions/api/prices/[ccn].js, including the slimParsedRecord
 * fallback path. JSON shapes must remain byte-identical to production.
 */

import type { Context } from "hono";
import type { Env } from "../../index";
import { badRequest, json } from "../../lib/responses";

const DISPLAY_TYPES = new Set(["CPT", "HCPCS", "DRG", "MS-DRG", "REV", "CDM"]);
const CPT_INDEX_TYPES = new Set(["CPT", "HCPCS"]);

type PayerRate = { payer?: unknown; rate_dollar?: unknown };

type RawItem = {
  code?: unknown;
  code_type?: unknown;
  description?: unknown;
  gross_charge?: unknown;
  cash_discount?: unknown;
  min_negotiated?: unknown;
  max_negotiated?: unknown;
  payer_rates?: unknown;
  billing_class?: unknown;
  setting?: unknown;
};

type SlimPayer = { p: string; r: number | null };

type SlimItem = {
  code: string;
  type: string;
  desc: string;
  gross: number | null;
  cash: number | null;
  min: number | null;
  max: number | null;
  pc: number;
  payers?: SlimPayer[];
  bc?: string;
  s?: string;
};

type SlimRecord = {
  ccn: string;
  hospital_name: string;
  source_url: string;
  fetched_at: string;
  format: string;
  n_total_raw: number;
  n_slim: number;
  counts: {
    raw: number;
    source_rows: number;
    display: number;
    cpt_indexed: number;
    by_type: Record<string, number>;
    skipped: { missing_code: number; unpriced: number; unsupported_type: number };
  };
  items: SlimItem[];
};

function numeric(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function displayCodeAndType(item: RawItem): { code: string; codeType: string } {
  const desc = String(item?.description ?? "").trim();
  let code = String(item?.code ?? "").trim();
  let codeType = String(item?.code_type ?? "").trim().toUpperCase() || "CDM";
  const payerRates = Array.isArray(item?.payer_rates) ? (item.payer_rates as PayerRate[]) : [];
  const gross = numeric(item?.gross_charge);
  const cash = numeric(item?.cash_discount);
  if (!code && desc && (gross !== null || cash !== null || payerRates.length > 0)) {
    code = desc.slice(0, 48);
    codeType = "CDM";
  }
  return { code, codeType };
}

function slimParsedRecord(parsed: unknown, ccn: string): SlimRecord | null {
  const p = parsed as { items?: unknown; hospital_name?: unknown; source_url?: unknown; fetched_at?: unknown; format_detected?: unknown; row_count?: unknown };
  const items: RawItem[] = Array.isArray(p?.items) ? (p.items as RawItem[]) : [];
  const seen = new Map<string, RawItem>();
  let skippedMissingCode = 0;
  let skippedUnpriced = 0;
  let skippedType = 0;

  for (const item of items) {
    const { code, codeType } = displayCodeAndType(item);
    if (!code) {
      skippedMissingCode += 1;
      continue;
    }
    const payerRates = Array.isArray(item?.payer_rates) ? (item.payer_rates as PayerRate[]) : [];
    const gross = numeric(item?.gross_charge);
    const cash = numeric(item?.cash_discount);
    if (gross === null && cash === null && payerRates.length === 0) {
      skippedUnpriced += 1;
      continue;
    }
    if (!DISPLAY_TYPES.has(codeType)) {
      skippedType += 1;
      continue;
    }
    const key = JSON.stringify([code, item?.billing_class ?? "", item?.setting ?? ""]);
    const prev = seen.get(key);
    const prevPayerLen = prev && Array.isArray(prev.payer_rates) ? (prev.payer_rates as unknown[]).length : 0;
    if (!prev || payerRates.length > prevPayerLen) {
      seen.set(key, item);
    }
  }

  const slim: SlimItem[] = [];
  const countsByType: Record<string, number> = {};
  let cptIndexed = 0;
  for (const item of seen.values()) {
    const { code, codeType } = displayCodeAndType(item);
    const payerRates = Array.isArray(item?.payer_rates) ? (item.payer_rates as PayerRate[]) : [];
    const payers: SlimPayer[] = payerRates
      .filter((payer) => numeric(payer?.rate_dollar) !== null)
      .sort((a, b) => (numeric(b?.rate_dollar) ?? 0) - (numeric(a?.rate_dollar) ?? 0))
      .slice(0, 5)
      .map((payer) => ({
        p: String(payer?.payer ?? "").slice(0, 40),
        r: numeric(payer?.rate_dollar),
      }));

    const rec: SlimItem = {
      code,
      type: codeType,
      desc: String(item?.description ?? "").slice(0, 100),
      gross: numeric(item?.gross_charge),
      cash: numeric(item?.cash_discount),
      min: numeric(item?.min_negotiated),
      max: numeric(item?.max_negotiated),
      pc: payerRates.length,
    };
    if (payers.length) rec.payers = payers;
    if (item?.billing_class) rec.bc = String(item.billing_class).slice(0, 16);
    if (item?.setting) rec.s = String(item.setting).slice(0, 3).toLowerCase();

    slim.push(rec);
    countsByType[codeType] = (countsByType[codeType] ?? 0) + 1;
    if (CPT_INDEX_TYPES.has(codeType)) cptIndexed += 1;
  }

  slim.sort((a, b) => (numeric(b.gross) ?? 0) - (numeric(a.gross) ?? 0));
  if (!slim.length) return null;

  return {
    ccn,
    hospital_name: String(p?.hospital_name ?? ""),
    source_url: String(p?.source_url ?? ""),
    fetched_at: String(p?.fetched_at ?? ""),
    format: String(p?.format_detected ?? ""),
    n_total_raw: Number(p?.row_count ?? 0),
    n_slim: slim.length,
    counts: {
      raw: items.length,
      source_rows: Number(p?.row_count ?? 0),
      display: slim.length,
      cpt_indexed: cptIndexed,
      by_type: countsByType,
      skipped: {
        missing_code: skippedMissingCode,
        unpriced: skippedUnpriced,
        unsupported_type: skippedType,
      },
    },
    items: slim,
  };
}

async function fetchR2(env: Env["Bindings"], key: string): Promise<Response | null> {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== "function") return null;
  const obj = await env.HL_MRF_PARSED.get(key);
  if (!obj) return null;
  return new Response(obj.body, {
    headers: {
      "content-type": obj.httpMetadata?.contentType ?? "application/json; charset=utf-8",
      "cache-control": "public, max-age=300",
      etag: obj.httpEtag,
    },
  });
}

async function fetchAsset(env: Env["Bindings"], request: Request, pathname: string): Promise<Response | null> {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== "function") return null;
  const url = new URL(request.url);
  url.pathname = pathname;
  url.search = "";
  const resp = await env.ASSETS.fetch(url.toString());
  if (resp.status === 404) return null;
  const ct = resp.headers.get("content-type") ?? "";
  if (pathname.endsWith(".json") && ct.includes("text/html")) return null;
  return resp;
}

async function fetchPreviewFromParsedR2(env: Env["Bindings"], ccn: string): Promise<Response | null> {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== "function") return null;
  const obj = await env.HL_MRF_PARSED.get(`parsed/${ccn}.json`);
  if (!obj) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(await obj.text());
  } catch {
    return null;
  }
  const preview = slimParsedRecord(parsed, ccn);
  if (!preview) return null;
  return json(preview, { cacheControl: "public, max-age=120" });
}

export async function pricesHandler(c: Context<Env>): Promise<Response> {
  const ccn = String(c.req.param("ccn") ?? "");
  if (!/^\d{6}$/.test(ccn)) {
    return badRequest("invalid ccn");
  }
  const env = c.env;
  const request = c.req.raw;
  return (
    (await fetchR2(env, `prices/${ccn}.json`)) ||
    (await fetchAsset(env, request, `/data/prices/${ccn}.json`)) ||
    (await fetchPreviewFromParsedR2(env, ccn)) ||
    json({ error: "price file not found" }, { status: 404 })
  );
}

export { slimParsedRecord };
