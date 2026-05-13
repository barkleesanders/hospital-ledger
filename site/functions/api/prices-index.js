function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': status === 200 ? 'public, max-age=300' : 'no-store',
    },
  });
}

async function responseFromR2(env) {
  if (!env.HL_MRF_PARSED || typeof env.HL_MRF_PARSED.get !== 'function') {
    return null;
  }
  const object = await env.HL_MRF_PARSED.get('prices/index.json');
  if (!object) {
    return null;
  }
  const text = await object.text();
  return {
    source: 'r2',
    text,
    count: hospitalCount(text),
    etag: object.httpEtag,
    contentType: object.httpMetadata?.contentType || 'application/json; charset=utf-8',
  };
}

async function responseFromAssets(env, request) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') {
    return null;
  }
  const url = new URL(request.url);
  url.pathname = '/data/prices/index.json';
  url.search = '';
  const response = await env.ASSETS.fetch(url.toString());
  const contentType = response.headers.get('content-type') || '';
  if (response.status === 404 || contentType.includes('text/html')) {
    return null;
  }
  const text = await response.text();
  return {
    source: 'assets',
    text,
    count: hospitalCount(text),
    etag: response.headers.get('etag'),
    contentType: contentType || 'application/json; charset=utf-8',
  };
}

function hospitalCount(text) {
  try {
    const payload = JSON.parse(text);
    return Array.isArray(payload?.hospitals) ? payload.hospitals.length : 0;
  } catch {
    return 0;
  }
}

function indexResponse(candidate) {
  const headers = {
    'content-type': candidate.contentType,
    'cache-control': 'public, max-age=300',
    'x-hl-price-index-source': candidate.source,
    'x-hl-price-index-hospitals': String(candidate.count),
  };
  if (candidate.etag) {
    headers.etag = candidate.etag;
  }
  return new Response(candidate.text, { headers });
}

export async function onRequestGet({ env, request }) {
  const [r2, assets] = await Promise.all([
    responseFromR2(env),
    responseFromAssets(env, request),
  ]);
  const best = [r2, assets]
    .filter(Boolean)
    .sort((a, b) => b.count - a.count)[0];
  if (best) {
    return indexResponse(best);
  }
  return json({ error: 'price index not found' }, 404);
}
