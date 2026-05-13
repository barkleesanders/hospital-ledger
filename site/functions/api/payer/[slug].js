// /api/payer/{slug}
// Per-payer page: hospitals that have a negotiated rate with this payer,
// median rates, links to the per-hospital detail. Powers /payer/{slug}.

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': status === 200 ? 'public, max-age=300' : 'no-store',
    },
  });
}

const VALID_SLUG = /^[a-z0-9][a-z0-9-]{0,80}$/;

async function fromR2(env, slug) {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== 'function') return null;
  const obj = await env.HL_MRF_PARSED.get(`aggregates/payer/${slug}.json`);
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

async function fromAssets(env, request, slug) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') return null;
  const url = new URL(request.url);
  url.pathname = `/data/payer/${slug}.json`;
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
  const slug = String(params?.slug || '').toLowerCase();
  if (!VALID_SLUG.test(slug)) {
    return json({ error: 'invalid payer slug' }, 400);
  }
  return (await fromR2(env, slug)) || (await fromAssets(env, request, slug)) || json({ error: 'payer not found', slug }, 404);
}
