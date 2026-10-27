#!/usr/bin/env python3
"""Tests for the file-declared updated-date capture (source_updated).

The site's data-vintage stamp prefers the date the hospital's own file
declares (CMS templates put it at the very top: a JSON top-level
"last_updated_on", or a CSV metadata header row followed by a value row)
over the newest dated charge column. mrf_parse.extract_declared_updated
must find it in both shapes, normalize MM/DD/YYYY to ISO, and return
None — never raise — for files that declare nothing.

Run: ~/hospital-ledger/.venv/bin/python3 test_source_updated.py
"""
import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mrf_parse", HERE / "mrf_parse.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


class DeclaredUpdated(unittest.TestCase):
    def test_json_iso(self):
        raw = b'{"hospital_name":"X","last_updated_on":"2026-04-01","version":"3.0.0"}'
        self.assertEqual(mp.extract_declared_updated(raw), "2026-04-01")

    def test_json_datetime(self):
        raw = b'{"last_updated_on": "2025-11-02T00:00:00Z", "items": []}'
        self.assertEqual(mp.extract_declared_updated(raw), "2025-11-02")

    def test_csv_meta_pair_comma(self):
        raw = (b"hospital_name,last_updated_on,version,hospital_ein\n"
               b"Some Hospital,2026-01-10,2.0.0,123456789\n"
               b"description,code,standard_charge\n")
        self.assertEqual(mp.extract_declared_updated(raw), "2026-01-10")

    def test_csv_meta_pair_pipe_us_date(self):
        raw = (b"hospital_name|last_updated_on|version\n"
               b"Some Hospital|01/10/2026|2.0.0\n")
        self.assertEqual(mp.extract_declared_updated(raw), "2026-01-10")

    def test_plain_csv_no_declared(self):
        raw = (b"description,code,standard_charge\n"
               b"MRI,70551,1200.00\n")
        self.assertIsNone(mp.extract_declared_updated(raw))

    def test_garbage_and_empty(self):
        self.assertIsNone(mp.extract_declared_updated(b""))
        self.assertIsNone(mp.extract_declared_updated(b"\x00\x01\x02 not a date"))

    def test_implausible_year_rejected(self):
        raw = b'{"last_updated_on":"1999-01-01"}'
        self.assertIsNone(mp.extract_declared_updated(raw))

    def test_normalize(self):
        self.assertEqual(mp._normalize_declared_date(" 2026-02-17 "), "2026-02-17")
        self.assertEqual(mp._normalize_declared_date("2/7/2026"), "2026-02-07")
        self.assertIsNone(mp._normalize_declared_date("2026-13-40"))
        self.assertIsNone(mp._normalize_declared_date("not a date"))


if __name__ == "__main__":
    unittest.main()
