"""r2_put GETs must never hand a short body to the caller, and must finish
through a proxy that ends every body early.

Regressions from 2026-10-13 on the muse.ai VM: the S3 egress leg ended
prices/index.json (1,386,703 bytes) at ~1.34 MB, the truncated JSON reached
the parser at publish time; once that was caught, 15 of 15 restart-from-zero
attempts still ended at 278-377 KB, so a retry alone never completed.
"""

import contextlib
import hashlib
import http.client
import io
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import r2_put  # noqa: E402

BODY = bytes(range(256)) * 5417  # 1,386,752 bytes, about the real index size


def etag_for(data):
    return '"' + hashlib.md5(data).hexdigest() + '"'


class FakeResp(io.BytesIO):
    def __init__(self, data, status, headers):
        super().__init__(data)
        self.status = status
        self.headers = headers


class FakeR2:
    """_open_signed stand-in serving one object, honouring Range and If-Match.

    cuts[i] bytes are delivered on request i before a clean EOF (None = all).
    """

    def __init__(self, obj, cuts, *, honour_range=True, etag=None, change_to=None,
                 change_after=None):
        self.obj = obj
        self.cuts = list(cuts)
        self.honour_range = honour_range
        self.etag = etag or etag_for(obj)
        self.change_to = change_to
        self.change_after = change_after
        self.calls = []

    @contextlib.contextmanager
    def __call__(self, method, key, *, timeout, extra_headers=None):
        n = len(self.calls)
        self.calls.append(dict(extra_headers or {}))
        if self.change_after is not None and n == self.change_after:
            self.obj, self.etag = self.change_to, etag_for(self.change_to)
        hdr = extra_headers or {}
        if "if-match" in hdr and hdr["if-match"] != self.etag:
            raise urllib.error.HTTPError("u", 412, "Precondition Failed", {}, None)
        start = 0
        m = re.match(r"bytes=(\d+)-", hdr.get("range", ""))
        if m and self.honour_range:
            start = int(m.group(1))
        body = self.obj[start:]
        cut = self.cuts.pop(0) if self.cuts else None
        sent = body if cut is None else body[:cut]
        headers = {"Content-Length": str(len(body)), "ETag": self.etag}
        status = 200
        if start:
            status = 206
            headers["Content-Range"] = f"bytes {start}-{len(self.obj) - 1}/{len(self.obj)}"
        yield FakeResp(sent, status, headers)


PIECE = 300_000  # inside the measured 278-377 KB per-connection range


class GetTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(r2_put.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def use(self, fake):
        mock.patch.object(r2_put, "_open_signed", fake).start()
        return fake

    def test_resumes_a_proxy_that_ends_every_body_early(self):
        fake = self.use(FakeR2(BODY, [PIECE] * 10))
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "index.json")
            self.assertTrue(r2_put.get_file("prices/index.json", dest))
            self.assertEqual(open(dest, "rb").read(), BODY)
            self.assertFalse(os.path.exists(dest + ".part"))
        self.assertEqual(len(fake.calls), 5)  # 5 pieces of <=300 KB
        self.assertEqual(fake.calls[1], {"range": f"bytes={PIECE}-", "if-match": fake.etag})
        self.sleep.assert_not_called()  # progress never costs a backoff

    def test_get_bytes_and_sha256_resume_too(self):
        self.use(FakeR2(BODY, [PIECE] * 10))
        self.assertEqual(r2_put.get_bytes("k"), BODY)
        self.use(FakeR2(BODY, [PIECE] * 10))
        self.assertEqual(r2_put.get_sha256("k"), hashlib.sha256(BODY).hexdigest())

    def test_full_body_first_try_makes_one_request(self):
        fake = self.use(FakeR2(BODY, [None]))
        self.assertEqual(r2_put.get_bytes("k"), BODY)
        self.assertEqual(len(fake.calls), 1)
        self.sleep.assert_not_called()

    def test_server_ignoring_range_restarts_from_zero(self):
        fake = self.use(FakeR2(BODY, [PIECE, None], honour_range=False))
        self.assertEqual(r2_put.get_bytes("k"), BODY)
        self.assertEqual(len(fake.calls), 2)

    def test_object_replaced_mid_download_never_joins_two_versions(self):
        new = b"N" * len(BODY)
        self.use(FakeR2(BODY, [PIECE, None, None], change_to=new, change_after=1))
        self.assertEqual(r2_put.get_bytes("k"), new)

    def test_md5_mismatch_with_etag_fails_closed(self):
        self.use(FakeR2(BODY, [None] * 10, etag='"' + "0" * 32 + '"'))
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "index.json")
            self.assertFalse(r2_put.get_file("k", dest))
            self.assertEqual(os.listdir(d), [])

    def test_multipart_etag_is_not_treated_as_md5(self):
        self.use(FakeR2(BODY, [PIECE] * 10, etag='"' + "a" * 32 + '-3"'))
        self.assertEqual(r2_put.get_bytes("k"), BODY)

    def test_no_progress_fails_closed_and_leaves_no_file(self):
        self.use(FakeR2(BODY, [PIECE] + [0] * 20))
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "index.json")
            self.assertFalse(r2_put.get_file("prices/index.json", dest))
            self.assertEqual(os.listdir(d), [])
        self.assertEqual(self.sleep.call_count, r2_put.LIST_MAX_RETRIES - 1)

    def test_http_error_is_final(self):
        def boom(*_a, **_k):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

        mock.patch.object(r2_put, "_open_signed", boom).start()
        self.assertIsNone(r2_put.get_bytes("k"))
        self.sleep.assert_not_called()

    def test_body_longer_than_declared_is_discarded(self):
        class Lying(FakeR2):
            @contextlib.contextmanager
            def __call__(self, method, key, *, timeout, extra_headers=None):
                self.calls.append(dict(extra_headers or {}))
                if len(self.calls) == 1:
                    yield FakeResp(BODY + b"junk", 200,
                                   {"Content-Length": str(len(BODY)), "ETag": self.etag})
                else:
                    yield FakeResp(BODY, 200,
                                   {"Content-Length": str(len(BODY)), "ETag": self.etag})

        self.use(Lying(BODY, []))
        self.assertEqual(r2_put.get_bytes("k"), BODY)


if __name__ == "__main__":
    unittest.main()
