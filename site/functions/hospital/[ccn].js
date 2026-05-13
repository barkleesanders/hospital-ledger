// /hospital/{ccn} — serve the patient-facing hospital template.
// See site/functions/procedure/[code].js for why this is a Pages Function
// rather than a _redirects rewrite.

export async function onRequestGet({ env, request }) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') {
    return new Response('ASSETS binding unavailable', { status: 500 });
  }
  const url = new URL(request.url);
  url.pathname = '/_hospital_template.html';
  url.search = '';
  const resp = await env.ASSETS.fetch(url.toString());
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'content-type': resp.headers.get('content-type') || 'text/html; charset=utf-8',
      'cache-control': 'public, max-age=60',
      'x-hl-template': 'hospital',
    },
  });
}
