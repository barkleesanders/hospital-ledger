// /procedure/{code} — serve the patient-facing procedure template.
// Uses a Pages Function (not _redirects) because CF Pages auto-308s
// /procedure/{anything} to /procedure when /procedure.html exists.
// Functions take precedence over path normalization, preserving the
// original URL so the inline JS can parse the {code} from location.pathname.

export async function onRequestGet({ env, request }) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') {
    return new Response('ASSETS binding unavailable', { status: 500 });
  }
  const url = new URL(request.url);
  url.pathname = '/_procedure_template.html';
  url.search = '';
  const resp = await env.ASSETS.fetch(url.toString());
  // Pass through the body but normalize headers — short cache so updates roll fast.
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'content-type': resp.headers.get('content-type') || 'text/html; charset=utf-8',
      'cache-control': 'public, max-age=60',
      'x-hl-template': 'procedure',
    },
  });
}
