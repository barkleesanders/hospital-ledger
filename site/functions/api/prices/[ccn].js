const DISPLAY_TYPES = new Set(['CPT', 'HCPCS', 'DRG', 'MS-DRG', 'REV', 'CDM']);
const CPT_INDEX_TYPES = new Set(['CPT', 'HCPCS']);

function json(body, status = 200, cacheControl = null) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': cacheControl || (status === 200 ? 'public, max-age=300' : 'no-store'),
    },
  });
}

async function fetchAsset(env, request, pathname) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') {
    return null;
  }
  const url = new URL(request.url);
  url.pathname = pathname;
  url.search = '';
  const response = await env.ASSETS.fetch(url.toString());
  if (response.status === 404) {
    return null;
  }
  const contentType = response.headers.get('content-type') || '';
  if (pathname.endsWith('.json') && contentType.includes('text/html')) {
    return null;
  }
  return response;
}

async function fetchR2(env, key) {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== 'function') {
    return null;
  }
  const object = await env.HL_MRF_PARSED.get(key);
  if (!object) return null;
  return new Response(object.body, {
    headers: {
      'content-type': object.httpMetadata?.contentType || 'application/json; charset=utf-8',
      'cache-control': 'public, max-age=300',
      etag: object.httpEtag,
    },
  });
}

function numeric(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function displayCodeAndType(item) {
  const desc = String(item?.description || '').trim();
  let code = String(item?.code || '').trim();
  let codeType = String(item?.code_type || '').trim().toUpperCase() || 'CDM';
  const payerRates = Array.isArray(item?.payer_rates) ? item.payer_rates : [];
  const gross = numeric(item?.gross_charge);
  const cash = numeric(item?.cash_discount);
  if (!code && desc && (gross !== null || cash !== null || payerRates.length > 0)) {
    code = desc.slice(0, 48);
    codeType = 'CDM';
  }
  return { code, codeType };
}

function slimParsedRecord(parsed, ccn) {
  const items = Array.isArray(parsed?.items) ? parsed.items : [];
  const seen = new Map();
  let skippedMissingCode = 0;
  let skippedUnpriced = 0;
  let skippedType = 0;

  for (const item of items) {
    const { code, codeType } = displayCodeAndType(item);
    if (!code) {
      skippedMissingCode += 1;
      continue;
    }
    const payerRates = Array.isArray(item?.payer_rates) ? item.payer_rates : [];
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
    const key = JSON.stringify([code, item?.billing_class || '', item?.setting || '']);
    const prev = seen.get(key);
    if (!prev || payerRates.length > (Array.isArray(prev?.payer_rates) ? prev.payer_rates.length : 0)) {
      seen.set(key, item);
    }
  }

  const slim = [];
  const countsByType = {};
  let cptIndexed = 0;
  for (const item of seen.values()) {
    const { code, codeType } = displayCodeAndType(item);
    const payerRates = Array.isArray(item?.payer_rates) ? item.payer_rates : [];
    const payers = payerRates
      .filter((payer) => numeric(payer?.rate_dollar) !== null)
      .sort((a, b) => numeric(b?.rate_dollar) - numeric(a?.rate_dollar))
      .slice(0, 5);

    const rec = {
      code,
      type: codeType,
      desc: String(item?.description || '').slice(0, 100),
      gross: numeric(item?.gross_charge),
      cash: numeric(item?.cash_discount),
      min: numeric(item?.min_negotiated),
      max: numeric(item?.max_negotiated),
      pc: payerRates.length,
    };
    if (payers.length) {
      rec.payers = payers.map((payer) => ({
        p: String(payer?.payer || '').slice(0, 40),
        r: numeric(payer?.rate_dollar),
      }));
    }
    if (item?.billing_class) {
      rec.bc = String(item.billing_class).slice(0, 16);
    }
    if (item?.setting) {
      rec.s = String(item.setting).slice(0, 3).toLowerCase();
    }
    slim.push(rec);
    countsByType[codeType] = (countsByType[codeType] || 0) + 1;
    if (CPT_INDEX_TYPES.has(codeType)) {
      cptIndexed += 1;
    }
  }

  slim.sort((a, b) => (numeric(b.gross) || 0) - (numeric(a.gross) || 0));
  if (!slim.length) {
    return null;
  }

  return {
    ccn,
    hospital_name: String(parsed?.hospital_name || ''),
    source_url: String(parsed?.source_url || ''),
    fetched_at: String(parsed?.fetched_at || ''),
    format: String(parsed?.format_detected || ''),
    n_total_raw: Number(parsed?.row_count || 0),
    n_slim: slim.length,
    counts: {
      raw: items.length,
      source_rows: Number(parsed?.row_count || 0),
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

async function fetchPreviewFromParsedR2(env, ccn) {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== 'function') {
    return null;
  }
  const object = await env.HL_MRF_PARSED.get(`parsed/${ccn}.json`);
  if (!object) {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(await object.text());
  } catch {
    return null;
  }
  const preview = slimParsedRecord(parsed, ccn);
  if (!preview) {
    return null;
  }
  return json(preview, 200, 'public, max-age=120');
}

export async function onRequestGet({ env, params, request }) {
  const ccn = String(params.ccn || '');
  if (!/^\d{6}$/.test(ccn)) {
    return json({ error: 'invalid ccn' }, 400);
  }
  return (
    (await fetchR2(env, `prices/${ccn}.json`)) ||
    (await fetchAsset(env, request, `/data/prices/${ccn}.json`)) ||
    (await fetchPreviewFromParsedR2(env, ccn)) ||
    json({ error: 'price file not found' }, 404)
  );
}

export { slimParsedRecord };
