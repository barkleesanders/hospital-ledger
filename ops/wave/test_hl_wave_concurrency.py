#!/usr/bin/env python3
"""Concurrency tests for verify_index_complete (2026-10-28 fix).

Wave 48 false-failed with "index-regression": the old guard demanded
count == pre + new exactly, so a campaign publish landing mid-wave
(4358 -> 4383 while the wave's own update was correct) read as a
regression. The guard now compares SETS:
  a) the wave's updated CCNs are present, priced (n > 0);
  b) no pre-wave CCN is lost or flipped priced -> zero;
  c) extra CCNs (concurrent campaign growth) pass, with a note logged.

Fixtures only: INDEX_PATH is pointed at temp files; the production
index is never touched.

Run: ~/hospital-ledger/.venv/bin/python3 test_hl_wave_concurrency.py
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hl_wave", HERE / "hl_wave.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)

PRE_MAP = {"010001": 100, "020001": 50, "060001": 0}  # 060001 = zero-row entry
PRE_CCNS = set(PRE_MAP)
PRE_COUNT = len(PRE_MAP)
UPDATED = ["030001"]  # a brand-new CCN this wave adds


class VerifyIndexCompleteConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.idx = Path(self.tmp.name) / "index.json"
        self._real_index_path = hw.INDEX_PATH
        hw.INDEX_PATH = self.idx
        self.logs = []
        self._real_log = hw.log
        hw.log = self.logs.append

    def tearDown(self):
        hw.INDEX_PATH = self._real_index_path
        hw.log = self._real_log
        self.tmp.cleanup()

    def post(self, entries):
        """Write the post-wave index fixture: {ccn: n}."""
        self.idx.write_text(json.dumps(
            {"hospitals": [{"ccn": c, "n": n} for c, n in entries.items()]}))

    def verify(self):
        return hw.verify_index_complete(PRE_COUNT, PRE_CCNS, UPDATED,
                                        pre_map=dict(PRE_MAP))

    def test_plain_update_passes(self):
        self.post({**PRE_MAP, "030001": 25})
        ok, count = self.verify()
        self.assertTrue(ok)
        self.assertEqual(count, 4)

    def test_concurrent_campaign_growth_passes_with_note(self):
        # Wave 48's shape: our +1, plus 24 extra CCNs from a campaign
        # publish that landed mid-wave.
        post = {**PRE_MAP, "030001": 25}
        for i in range(24):
            post[f"9{i:05d}"] = 10 + i
        self.post(post)
        ok, count = self.verify()
        self.assertTrue(ok)
        self.assertEqual(count, 28)
        self.assertTrue(any("CONCURRENCY NOTE" in m for m in self.logs),
                        f"expected a concurrency note, got: {self.logs}")

    def test_lost_pre_wave_ccn_fails_closed(self):
        post = {**PRE_MAP, "030001": 25}
        del post["020001"]
        self.post(post)
        ok, _ = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("INDEX REGRESSION" in m for m in self.logs))

    def test_priced_to_zero_flip_fails_closed(self):
        self.post({**PRE_MAP, "020001": 0, "030001": 25})
        ok, _ = self.verify()
        self.assertFalse(ok)

    def test_updated_ccn_missing_fails_closed(self):
        self.post(dict(PRE_MAP))
        ok, _ = self.verify()
        self.assertFalse(ok)

    def test_updated_ccn_zero_fails_closed(self):
        self.post({**PRE_MAP, "030001": 0})
        ok, _ = self.verify()
        self.assertFalse(ok)

    def test_zero_row_entry_staying_zero_is_not_a_regression(self):
        # 060001 was n=0 pre-wave and stays n=0: not priced, not lost.
        self.post({**PRE_MAP, "030001": 25})
        ok, _ = self.verify()
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
