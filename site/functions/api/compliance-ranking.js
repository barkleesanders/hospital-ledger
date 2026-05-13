// /api/compliance-ranking
// Sortable hospital ranking by 45 CFR § 180 compliance score.
// Reads compliance-ranking.json from R2 (canonical) → ASSETS fallback.

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': status === 200 ? 'public, max-age=300' : 'no-store',
    },
  });
}

async function fromR2(env) {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== 'function') return null;
  const obj = await env.HL_MRF_PARSED.get('aggregates/compliance-ranking.json');
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

async function fromAssets(env, request) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') return null;
  const url = new URL(request.url);
  url.pathname = '/data/compliance-ranking.json';
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

export async function onRequestGet({ env, request }) {
  return (await fromR2(env)) || (await fromAssets(env, request)) || json({ error: 'compliance ranking not built yet' }, 404);
}
