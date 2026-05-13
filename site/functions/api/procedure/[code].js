// /api/procedure/{code}
// Cross-hospital comparison for a single CPT/HCPCS code. Includes gross,
// cash, min/max negotiated, and top-5 payer rates per hospital.

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': status === 200 ? 'public, max-age=300' : 'no-store',
    },
  });
}

// CPT codes are 5 digits. HCPCS codes are 1 letter + 4 digits.
// Allow both, plus DRG variants. Reject anything weirder.
const VALID_CODE = /^[A-Z0-9][A-Z0-9-]{1,9}$/i;

async function fromR2(env, code) {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== 'function') return null;
  const obj = await env.HL_MRF_PARSED.get(`aggregates/cpt-detail/${code}.json`);
  if (!obj) return null;
  return new Response(obj.body, {
    headers: {
      'content-type': obj.httpMetadata?.contentType || 'application/json; charset=utf-8',
      'cache-control': 'public, max-age=300',
      etag: obj.httpEtag,
      'x-hl-source': 'r2',
    },
  });
}

async function fromAssets(env, request, code) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') return null;
  const url = new URL(request.url);
  url.pathname = `/data/cpt-detail/${code}.json`;
  url.search = '';
  const resp = await env.ASSETS.fetch(url.toString());
  const ct = resp.headers.get('content-type') || '';
  if (resp.status === 404 || ct.includes('text/html')) return null;
  return new Response(resp.body, {
    headers: {
      'content-type': ct || 'application/json; charset=utf-8',
      'cache-control': 'public, max-age=300',
      'x-hl-source': 'assets',
    },
  });
}

export async function onRequestGet({ env, request, params }) {
  const code = String(params?.code || '').toUpperCase();
  if (!VALID_CODE.test(code)) {
    return json({ error: 'invalid procedure code' }, 400);
  }
  return (await fromR2(env, code)) || (await fromAssets(env, request, code)) || json({ error: 'procedure not indexed', code }, 404);
}
