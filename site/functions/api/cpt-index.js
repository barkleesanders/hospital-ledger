function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    },
  });
}

export async function onRequestGet({ env, request }) {
  if (env.HL_MRF_PARSED && typeof env.HL_MRF_PARSED.get === 'function') {
    const object = await env.HL_MRF_PARSED.get('indexes/cpt-index.json');
    if (object) {
      return new Response(object.body, {
        headers: {
          'content-type': object.httpMetadata?.contentType || 'application/json; charset=utf-8',
          'cache-control': 'public, max-age=300',
          etag: object.httpEtag,
        },
      });
    }
  }
  if (env.ASSETS && typeof env.ASSETS.fetch === 'function') {
    const url = new URL(request.url);
    url.pathname = '/data/cpt-index.json';
    url.search = '';
    const response = await env.ASSETS.fetch(url.toString());
    const contentType = response.headers.get('content-type') || '';
    if (response.status !== 404 && !contentType.includes('text/html')) {
      return response;
    }
  }
  return json({ error: 'cpt index not found' }, 404);
}
