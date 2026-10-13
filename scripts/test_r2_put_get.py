"""r2_put GETs must never hand a short body to the caller.

Regression for 2026-10-13: the VM's S3 egress leg ended a 1,548,059-byte
prices/index.json at ~1.34 MB twice in a row without an error, and the
truncated JSON reached the parser at publish time.
"""

import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import r2_put  # noqa: E402

BODY = b"x" * (3 * r2_put.CHUNK + 123)


class FakeResp(io.BytesIO):
    def __init__(self, data, content_length):
        super().__init__(data)
        self.headers = {"Content-Length": str(content_length)}


def responses(*bodies):
    """_open_signed stand-in: each call serves the next (data, declared length)."""
    seq = list(bodies)

    @contextlib.contextmanager
    def opener(*_a, **_k):
        data, length = seq.pop(0)
        yield FakeResp(data, length)

    return opener


SHORT = (BODY[: len(BODY) - 200_000], len(BODY))
FULL = (BODY, len(BODY))


class ShortBodyTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(r2_put.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_get_file_retries_a_short_body_and_keeps_only_the_full_one(self):
        mock.patch.object(r2_put, "_open_signed", responses(SHORT, SHORT, FULL)).start()
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "index.json")
            self.assertTrue(r2_put.get_file("prices/index.json", dest))
            self.assertEqual(open(dest, "rb").read(), BODY)
            self.assertFalse(os.path.exists(dest + ".part"))

    def test_get_file_fails_closed_and_leaves_no_file_when_always_short(self):
        mock.patch.object(r2_put, "_open_signed",
                          responses(*[SHORT] * r2_put.LIST_MAX_RETRIES)).start()
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "index.json")
            self.assertFalse(r2_put.get_file("prices/index.json", dest))
            self.assertFalse(os.path.exists(dest))
            self.assertFalse(os.path.exists(dest + ".part"))

    def test_get_bytes_and_sha256_ignore_the_short_attempt(self):
        mock.patch.object(r2_put, "_open_signed", responses(SHORT, FULL, SHORT, FULL)).start()
        self.assertEqual(r2_put.get_bytes("k"), BODY)
        self.assertEqual(r2_put.get_sha256("k"), hashlib.sha256(BODY).hexdigest())

    def test_full_body_first_try_does_not_retry(self):
        mock.patch.object(r2_put, "_open_signed", responses(FULL)).start()
        self.assertEqual(r2_put.get_bytes("k"), BODY)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
