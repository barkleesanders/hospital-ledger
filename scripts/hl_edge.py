"""Route the data pipeline's hospital fetches through hospitalledger.com/api/fetch.

Why: the muse.ai VM that runs the waves asks a human to approve every new
destination domain. The MRF seed spans 1,136 hospital hosts (mrf_seed,
measured 2026-10-13), so a wave raised one approval per hospital. With
HL_FETCH_PROXY set, every hospital request goes to the one Worker domain the
VM already talks to, and Cloudflare fetches the hospital (src/routes/api/
fetch-proxy.ts). With it unset, nothing here changes behaviour.

Environment:
  HL_FETCH_PROXY  e.g. https://hospitalledger.com/api/fetch   (unset = direct)
  HL_FETCH_TOKEN  the Worker's FETCH_PROXY_TOKEN secret       (required with it)
  HL_EDGE_DIRECT_HOSTS_FILE  optional newline list of hosts the VM is already
                  approved for. When the edge cannot get a usable answer from
                  a host (a bot shield challenging Cloudflare's egress, an
                  invalid origin certificate, an origin dropping Cloudflare's
                  IPs; all measured 2026-10-13) it is fetched directly instead,
                  but only if it is on this list, so the fallback never raises
                  a new approval.

httpx callers:   httpx.Client(..., **hl_edge.httpx_kwargs())
                 httpx.AsyncClient(..., **hl_edge.httpx_kwargs(async_=True, limits=...))
curl callers:    argv = hl_edge.curl_argv(argv, url)

What the caller sees is the hospital's response: its status (200/206/304/404),
its body, the final URL after redirects in `response.url`, and its
Content-Length when the hospital sent an uncompressed body. Proxy-side
failures become httpx.ConnectError / httpx.ReadTimeout (the errors the callers
already treat as "host unreachable"); a missing or rejected token raises
EdgeConfigError so a misconfiguration can never read as "every hospital is dead".
"""

from __future__ import annotations

import atexit
import os
import tempfile
import urllib.parse

ENV_URL = "HL_FETCH_PROXY"
ENV_TOKEN = "HL_FETCH_TOKEN"


class EdgeConfigError(RuntimeError):
    """The proxy refused the pipeline itself (unset secret, wrong token, bad URL)."""


_EdgeBlocked = None


def _blocked_error():
    """One class for the process (httpx is imported lazily, so it is built on first use)."""
    global _EdgeBlocked
    if _EdgeBlocked is None:
        import httpx

        class EdgeBlocked(httpx.TransportError):
            """The hospital's bot shield answered Cloudflare with a challenge page."""

        _EdgeBlocked = EdgeBlocked
    return _EdgeBlocked


_direct_hosts = None


def direct_hosts() -> frozenset:
    global _direct_hosts
    if _direct_hosts is None:
        path = (os.environ.get("HL_EDGE_DIRECT_HOSTS_FILE") or "").strip()
        hosts = set()
        if path and os.path.exists(path):
            with open(path) as f:
                hosts = {l.strip().lower() for l in f if l.strip()}
        _direct_hosts = frozenset(hosts)
    return _direct_hosts


def _config():
    base = (os.environ.get(ENV_URL) or "").strip()
    if not base:
        return None
    token = (os.environ.get(ENV_TOKEN) or "").strip()
    if not token:
        raise EdgeConfigError(f"{ENV_URL} is set but {ENV_TOKEN} is empty")
    return base, token


def enabled() -> bool:
    return _config() is not None


def proxied_url(url: str) -> str:
    base, _ = _config()
    return f"{base}?url={urllib.parse.quote(url, safe='')}"


def _rewrite(request):
    """Point an httpx request at the proxy; returns the original target URL."""
    import httpx

    _, token = _config()
    target = str(request.url)
    request.url = httpx.URL(proxied_url(target))
    request.headers["authorization"] = f"Bearer {token}"
    # The Host header was computed for the hospital; let the transport recompute it.
    request.headers["host"] = request.url.netloc.decode("ascii")
    return target


def _restore(request, target, headers):
    """Undo _rewrite so the request can go straight to the hospital."""
    import httpx

    request.url = httpx.URL(target)
    request.headers.clear()
    request.headers.update(headers)


def _settle(request, response, target):
    """Map proxy semantics back onto the caller's request/response, or raise."""
    import httpx

    EdgeBlocked = _blocked_error()

    kind = response.headers.get("x-hl-proxy-error")
    if kind:
        request.url = httpx.URL(target)
        if kind == "timeout":
            raise httpx.ReadTimeout(f"edge fetch timed out: {target}", request=request)
        if kind == "connect":
            raise httpx.ConnectError(f"edge fetch could not connect: {target}", request=request)
        if kind in ("upstream_challenge", "upstream_tls"):
            raise EdgeBlocked(f"edge blocked ({kind}): {target}", request=request)
        raise EdgeConfigError(f"edge proxy refused ({response.status_code} {kind}) for {target}")
    if response.headers.get("x-hl-proxy") != "1":
        # Anything else answering on the proxy URL (Cloudflare error page, a
        # route that does not exist yet) is not the hospital's response.
        raise EdgeConfigError(
            f"no edge proxy at {_config()[0]} (HTTP {response.status_code}); deploy the /api/fetch route first"
        )
    # response.url is read from response.request.url, which the client sets to
    # this same request object after the transport returns.
    request.url = httpx.URL(response.headers.get("x-hl-final-url") or target)
    headers = response.headers
    upstream_len = headers.get("x-hl-upstream-content-length")
    if upstream_len and "content-length" not in headers and not headers.get(
        "x-hl-upstream-content-encoding"
    ):
        headers["content-length"] = upstream_len
    return response


def _pick(helper, url):
    """The transport a stock client would use for `url`, env proxies included."""
    return helper._transport_for_url(url)


def _transport_classes():
    import httpx

    EdgeBlocked = _blocked_error()

    def bypass(target):
        host = (httpx.URL(target).host or "").lower()
        return host == httpx.URL(_config()[0]).host.lower()

    def may_go_direct(target):
        return (httpx.URL(target).host or "").lower() in direct_hosts()

    # Route every hop the way a stock client would, env proxies included (the
    # muse.ai VM egresses through an HTTPS_PROXY), by asking a private stock
    # client for the transport it would pick. The outer client is built with
    # trust_env=False, or its env-proxy mounts would outrank this transport
    # and send hospital URLs straight out, bypassing the edge.

    class EdgeTransport(httpx.BaseTransport):
        def __init__(self, **kw):
            self._edge = httpx.Client(**kw)
            # Hospital certificates: same leniency the callers asked for.
            self._direct = httpx.Client(verify=False, **kw)

        def _send_direct(self, request):
            return _pick(self._direct, request.url).handle_request(request)

        def handle_request(self, request):
            if bypass(str(request.url)):
                return self._send_direct(request)
            saved = httpx.Headers(request.headers)
            target = _rewrite(request)
            response = _pick(self._edge, request.url).handle_request(request)
            try:
                return _settle(request, response, target)
            except (EdgeBlocked, httpx.ConnectError, httpx.ReadTimeout):
                response.close()
                if not may_go_direct(target):
                    raise
                _restore(request, target, saved)
                return self._send_direct(request)
            except BaseException:
                response.close()
                raise

        def close(self):
            self._edge.close()
            self._direct.close()

    class AsyncEdgeTransport(httpx.AsyncBaseTransport):
        def __init__(self, **kw):
            self._edge = httpx.AsyncClient(**kw)
            self._direct = httpx.AsyncClient(verify=False, **kw)

        async def _send_direct(self, request):
            return await _pick(self._direct, request.url).handle_async_request(request)

        async def handle_async_request(self, request):
            if bypass(str(request.url)):
                return await self._send_direct(request)
            saved = httpx.Headers(request.headers)
            target = _rewrite(request)
            response = await _pick(self._edge, request.url).handle_async_request(request)
            try:
                return _settle(request, response, target)
            except (EdgeBlocked, httpx.ConnectError, httpx.ReadTimeout):
                await response.aclose()
                if not may_go_direct(target):
                    raise
                _restore(request, target, saved)
                return await self._send_direct(request)
            except BaseException:
                await response.aclose()
                raise

        async def aclose(self):
            await self._edge.aclose()
            await self._direct.aclose()

    return EdgeTransport, AsyncEdgeTransport


def httpx_kwargs(async_: bool = False, **transport_kw) -> dict:
    """Extra httpx.Client / AsyncClient kwargs: {} when disabled.

    A custom transport makes the client ignore its own `limits`/`verify`, so
    pass `limits` here instead. Env proxies still apply, inside the transport. TLS to the proxy is always verified; the
    callers' verify=False applied to hospital certificates, which Cloudflare
    now checks.
    """
    if not enabled():
        return {}
    sync_cls, async_cls = _transport_classes()
    return {
        "transport": (async_cls if async_ else sync_cls)(**transport_kw),
        "trust_env": False,
    }


_header_file = None


def _auth_header_file() -> str:
    """A 0600 file holding the Authorization header, so the token is not in argv."""
    global _header_file
    if _header_file is None:
        _, token = _config()
        fd, path = tempfile.mkstemp(prefix="hl-edge-", suffix=".hdr")
        with os.fdopen(fd, "w") as f:
            f.write(f"Authorization: Bearer {token}\n")
        atexit.register(lambda: os.path.exists(path) and os.remove(path))
        _header_file = path
    return _header_file


def curl_argv(argv: list[str], url: str) -> list[str]:
    """Rewrite a curl argv so it fetches `url` through the proxy (no-op when disabled).

    `%{url_effective}` in a -w format becomes the hospital's final URL.
    """
    if not enabled():
        return argv
    out = []
    for a in argv:
        if a == url:
            out.append(proxied_url(url))
        else:
            out.append(a.replace("%{url_effective}", "%header{x-hl-final-url}"))
    return [out[0], "-H", f"@{_auth_header_file()}", *out[1:]]


def preflight() -> str:
    """Prove the proxy is reachable and accepts the token, without a real fetch.

    Asks the proxy to fetch an address it always refuses: 400 bad_url means
    the route exists, the secret is set and the token matches.
    """
    import httpx

    if not enabled():
        return "edge proxy disabled (direct fetch)"
    base, token = _config()
    r = httpx.get(
        f"{base}?url=",
        headers={"authorization": f"Bearer {token}"},
        timeout=20,
    )
    kind = r.headers.get("x-hl-proxy-error")
    if r.status_code == 400 and kind == "bad_url":
        return f"edge proxy ok: {base}"
    raise EdgeConfigError(f"edge proxy preflight failed: HTTP {r.status_code} {kind or ''} at {base}")


if __name__ == "__main__":
    print(preflight())
