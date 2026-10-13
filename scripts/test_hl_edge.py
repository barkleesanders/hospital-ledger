"""Unit tests for scripts/hl_edge.py (run: python3 -m unittest scripts/test_hl_edge.py).

The network is an httpx.MockTransport standing in for the Worker at
/api/fetch, so these check the client's contract without Cloudflare.
"""

import asyncio
import os
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx  # noqa: E402

import hl_edge  # noqa: E402

PROXY = "https://edge.test/api/fetch"
TOKEN = "t" * 40
MRF = "https://hospital.test/mrf.csv"


def worker(seen, upstream):
    """A fake /api/fetch: checks auth, records the request, answers like the Worker."""

    def handler(request):
        # Snapshot: the client later rewrites request.url to the final URL.
        seen.append((httpx.URL(str(request.url)), httpx.Headers(request.headers)))
        if request.url.host == "hospital.test":  # a direct (fallback) fetch
            return httpx.Response(200, content=b"direct-body")
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, headers={"x-hl-proxy-error": "unauthorized"})
        target = urllib.parse.parse_qs(request.url.query.decode())["url"][0]
        return upstream(target, request)

    return httpx.MockTransport(handler)


class EdgeTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["HL_FETCH_PROXY"] = PROXY
        os.environ["HL_FETCH_TOKEN"] = TOKEN
        os.environ.pop("HL_EDGE_DIRECT_HOSTS_FILE", None)
        hl_edge._direct_hosts = None
        self._pick = hl_edge._pick
        self.seen = []

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        hl_edge._pick = self._pick
        hl_edge._direct_hosts = None

    def client(self, upstream, async_=False):
        mock = worker(self.seen, upstream)
        hl_edge._pick = lambda helper, url: mock
        cls = httpx.AsyncClient if async_ else httpx.Client
        return cls(follow_redirects=True, **hl_edge.httpx_kwargs(async_=async_))

    def ok(self, body=b"a,b\n1,2\n", final=MRF, extra=None):
        headers = {"x-hl-proxy": "1", "x-hl-final-url": final, "content-type": "text/csv"}
        headers.update(extra or {})
        # stream= (not content=) so no Content-Length is set, like a Worker's chunked reply.
        return lambda target, req: httpx.Response(200, stream=httpx.ByteStream(body), headers=headers)

    def test_disabled_changes_nothing(self):
        os.environ.pop("HL_FETCH_PROXY")
        self.assertEqual(hl_edge.httpx_kwargs(), {})
        self.assertFalse(hl_edge.enabled())
        argv = ["curl", "-fsSL", MRF]
        self.assertEqual(hl_edge.curl_argv(argv, MRF), argv)

    def test_routes_through_proxy_with_token_and_reports_final_url(self):
        final = "https://cdn.test/final.csv"
        with self.client(self.ok(final=final)) as c:
            r = c.get(MRF)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"a,b\n1,2\n")
        self.assertEqual(str(r.url), final)
        url, headers = self.seen[0]
        self.assertEqual(url.host, "edge.test")
        self.assertEqual(urllib.parse.parse_qs(url.query.decode())["url"], [MRF])
        self.assertEqual(headers["host"], "edge.test")

    def test_restores_content_length_only_for_uncompressed_bodies(self):
        plain = self.ok(extra={"x-hl-upstream-content-length": "999"})
        with self.client(plain) as c:
            with c.stream("GET", MRF) as r:
                self.assertEqual(r.headers.get("content-length"), "999")
        gz = self.ok(extra={"x-hl-upstream-content-length": "999",
                            "x-hl-upstream-content-encoding": "gzip"})
        with self.client(gz) as c:
            with c.stream("GET", MRF) as r:
                self.assertIsNone(r.headers.get("content-length"))

    def test_hospital_statuses_pass_through(self):
        for status in (304, 404, 410, 206):
            up = lambda t, req, s=status: httpx.Response(s, headers={"x-hl-proxy": "1"})
            with self.client(up) as c:
                self.assertEqual(c.get(MRF).status_code, status)

    def test_proxy_failures_map_to_transport_errors(self):
        cases = {"connect": httpx.ConnectError, "timeout": httpx.ReadTimeout,
                 "upstream_challenge": httpx.TransportError, "upstream_tls": httpx.TransportError}
        for kind, exc in cases.items():
            up = lambda t, req, k=kind: httpx.Response(502, headers={"x-hl-proxy-error": k})
            with self.client(up) as c, self.assertRaises(exc):
                c.get(MRF)

    def test_misconfiguration_is_loud_never_a_dead_hospital(self):
        os.environ["HL_FETCH_TOKEN"] = "wrong"
        with self.client(self.ok()) as c, self.assertRaises(hl_edge.EdgeConfigError):
            c.get(MRF)
        os.environ["HL_FETCH_TOKEN"] = TOKEN
        # Something answering on the proxy URL that is not the route (pre-deploy 404).
        up = lambda t, req: httpx.Response(404, text="Not Found")
        with self.client(up) as c, self.assertRaises(hl_edge.EdgeConfigError):
            c.get(MRF)
        os.environ["HL_FETCH_TOKEN"] = ""
        with self.assertRaises(hl_edge.EdgeConfigError):
            hl_edge.httpx_kwargs()

    def test_blocked_host_goes_direct_only_when_already_approved(self):
        blocked = lambda t, req: httpx.Response(502, headers={"x-hl-proxy-error": "upstream_challenge"})
        with self.client(blocked) as c, self.assertRaises(httpx.TransportError):
            c.get(MRF)
        self.assertTrue(all(url.host == "edge.test" for url, _ in self.seen))
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("hospital.test\n")
        os.environ["HL_EDGE_DIRECT_HOSTS_FILE"] = f.name
        hl_edge._direct_hosts = None
        self.seen.clear()
        with self.client(blocked) as c:
            r = c.get(MRF)
        os.remove(f.name)
        self.assertEqual(r.content, b"direct-body")
        url, headers = self.seen[-1]
        self.assertEqual(str(url), MRF)
        self.assertNotIn("authorization", headers)  # the token never leaves for a hospital

    def test_own_domain_is_fetched_directly(self):
        with self.client(self.ok()) as c:
            c.get("https://edge.test/api/manifest")
        self.assertEqual(str(self.seen[0][0]), "https://edge.test/api/manifest")

    def test_async_client(self):
        async def go():
            async with self.client(self.ok(), async_=True) as c:
                return await c.get(MRF)

        r = asyncio.run(go())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(str(r.url), MRF)

    def test_curl_argv_keeps_token_out_of_argv(self):
        argv = hl_edge.curl_argv(["curl", "-fsSL", "-w", "%{url_effective}", MRF], MRF)
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertIn(hl_edge.proxied_url(MRF), argv)
        self.assertIn("%header{x-hl-final-url}", argv)
        hdr = argv[argv.index("-H") + 1]
        self.assertTrue(hdr.startswith("@"))
        self.assertEqual(oct(os.stat(hdr[1:]).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
