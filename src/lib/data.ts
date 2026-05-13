/**
 * Server-side data loaders. SSR pages read R2 first, fall through to ASSETS,
 * then return null (route layer renders the 404 panel).
 */

import { readR2Json } from "./r2";

export type ProcedureStats = {
  hospital_count: number;
  cash_p50: number | null;
  cash_min: number | null;
  cash_max: number | null;
  gross_p50: number | null;
  flagged_low?: number;
  flagged_high?: number;
};

export type ProcedurePayer = { slug?: string; display?: string; rate?: number };

export type ProcedureHospital = {
  ccn: string;
  name: string;
  state: string;
  city?: string;
  gross: number | null;
  cash: number | null;
  min: number | null;
  max: number | null;
  payer_count?: number;
  payers?: ProcedurePayer[];
  quality?: "normal" | "low_outlier" | "high_outlier";
};

export type ProcedureData = {
  code: string;
  desc: string;
  type: string;
  stats: ProcedureStats;
  hospitals: ProcedureHospital[];
};

export type PayerData = {
  payer: {
    slug: string;
    display: string;
    category?: string;
    hospital_count?: number;
    raw_aliases?: string[];
    median_rate?: number | null;
    min_rate?: number | null;
    max_rate?: number | null;
  };
  hospital_count?: number;
  hospitals: Array<{
    ccn: string;
    name: string;
    state: string;
    city?: string;
    n_items_with_payer?: number;
    median_rate?: number | null;
    compliance_grade?: string | null;
    compliance_score?: number | null;
  }>;
};

export type HospitalItem = {
  code: string;
  type?: string;
  desc?: string;
  gross: number | null;
  cash: number | null;
  min: number | null;
  max: number | null;
  pc?: number;
  payers?: Array<{ p: string; r: number | null }>;
};

export type HospitalData = {
  ccn: string;
  hospital_name: string;
  source_url?: string;
  format?: string;
  n_slim?: number;
  compliance?: {
    score?: number | null;
    grade?: string | null;
    elements?: Record<string, boolean>;
  };
  items: HospitalItem[];
};

export type Bindings = {
  HL_MRF_PARSED: R2Bucket;
  ASSETS: Fetcher;
};

async function fetchAssetJson<T>(env: Bindings, request: Request, pathname: string): Promise<T | null> {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== "function") return null;
  const url = new URL(request.url);
  url.pathname = pathname;
  url.search = "";
  const resp = await env.ASSETS.fetch(url.toString());
  const ct = resp.headers.get("content-type") ?? "";
  if (resp.status === 404 || ct.includes("text/html")) return null;
  try {
    return (await resp.json()) as T;
  } catch {
    return null;
  }
}

export async function loadProcedure(env: Bindings, request: Request, code: string): Promise<ProcedureData | null> {
  const upper = code.toUpperCase();
  const r2 = await readR2Json<ProcedureData>(env.HL_MRF_PARSED, `aggregates/cpt-detail/${upper}.json`);
  if (r2) return r2;
  return await fetchAssetJson<ProcedureData>(env, request, `/data/cpt-detail/${upper}.json`);
}

export async function loadPayer(env: Bindings, request: Request, slug: string): Promise<PayerData | null> {
  const lower = slug.toLowerCase();
  const r2 = await readR2Json<PayerData>(env.HL_MRF_PARSED, `aggregates/payer/${lower}.json`);
  if (r2) return r2;
  return await fetchAssetJson<PayerData>(env, request, `/data/payer/${lower}.json`);
}

export async function loadHospital(env: Bindings, request: Request, ccn: string): Promise<HospitalData | null> {
  const r2 = await readR2Json<HospitalData>(env.HL_MRF_PARSED, `prices/${ccn}.json`);
  if (r2) return r2;
  return await fetchAssetJson<HospitalData>(env, request, `/data/prices/${ccn}.json`);
}
