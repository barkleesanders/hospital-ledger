#!/usr/bin/env python3
"""Tests for hl_wave.py chunk mode (--chunk-ccns, 2026-10-25).

The contract under test is the streak guarantee: a chunk run -- whether its
CCNs land, fail to parse, fail to publish, or defer -- must leave the loop
state file byte-identical (no_progress_streak neither burned nor reset),
park its failures through the deferred mechanics, and still exit 0. A
stopped loop makes it exit 3 having written nothing; a bad CCN list is a
usage error (exit 2).

All heavy machinery (queue build, reparse, publish phase, API verify) is
stubbed; state/loop/log paths are redirected into a temp dir.

Run: ~/hospital-ledger/.venv/bin/python3 test_hl_wave_chunk.py
"""
import argparse
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

CCNS = ["450001", "450002", "450003", "450004", "450005"]


def row(ccn):
    return {"ccn": ccn, "name": f"Hosp {ccn}",
            "url": f"https://example.org/{ccn}.csv", "host": "example.org"}


class ChunkMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self._saved = {}
        for attr in ("STD_LOOP", "WAVE_LOG", "STATE_PATH", "QUEUE_PATH",
                     "PENDING_PATH", "ROUTE_HEALTH_PATH", "GOAL",
                     "INDEX_PATH", "ROOT"):
            self._saved[attr] = getattr(hw, attr)
        hw.STD_LOOP = t / "loop.json"
        hw.WAVE_LOG = t / "waves.jsonl"
        hw.STATE_PATH = t / "state.json"
        hw.QUEUE_PATH = t / "queue.json"
        hw.PENDING_PATH = t / "pending.json"
        hw.ROUTE_HEALTH_PATH = t / "health.json"
        hw.GOAL = t
        hw.INDEX_PATH = t / "index.json"
        hw.ROOT = t / "root"
        hw.INDEX_PATH.write_text(json.dumps(
            {"hospitals": [{"ccn": f"{i:06d}"} for i in range(3001)]}))
        hw.STATE_PATH.write_text(json.dumps(
            {"pos": 0, "waves_done": 47, "urls": {}, "deferred": [],
             "unpublished": []}))
        self.set_streak(2)
        self.rows = {c: row(c) for c in CCNS}
        hw.load_edge = lambda: None
        hw.load_granted = lambda edge=None: None
        hw.build_queue = lambda granted: list(self.rows.values())
        hw.verify_index_complete = lambda pre, ccns, upd: (True, pre + len(upd))
        hw.verify_ccn_api = lambda ccn: 7

    def tearDown(self):
        for attr, val in self._saved.items():
            setattr(hw, attr, val)
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def set_streak(self, n):
        hw.STD_LOOP.write_text(json.dumps(
            {"status": "active", "goal": hw.GOAL_SLUG, "max_iter": 2000,
             "iter": 78, "predicates": {"target_met": False,
                                        "no_progress_streak": n}}))

    def loop_bytes(self):
        return hw.STD_LOOP.read_bytes()

    def std(self):
        return hw.load_std_loop()

    def state(self):
        return json.loads(hw.STATE_PATH.read_text())

    def last_record(self):
        lines = hw.WAVE_LOG.read_text().strip().splitlines()
        return json.loads(lines[-1])

    def args(self, ccns):
        return argparse.Namespace(chunk_ccns=",".join(ccns),
                                  dry_run=False, no_publish=False)

    def reparse_fail(self, changed, bound, errors, state):
        for ch in changed:
            errors.append({"ccn": ch["ccn"], "class": "parse",
                           "detail": "boom"})
        return [], []

    def reparse_ok(self, changed, bound, errors, state):
        return [ch["ccn"] for ch in changed], []

    # -- the streak contract ----------------------------------------------
    def test_failed_chunk_never_moves_streak(self):
        for n in (1, 3, 5):
            ccns = CCNS[:n]
            hw.reparse_changed = self.reparse_fail
            before = self.loop_bytes()
            rc = hw.run_chunk(self.args(ccns), {}, self.std())
            self.assertEqual(rc, 0)
            self.assertEqual(self.loop_bytes(), before,
                             f"chunk of {n}: loop state file changed")
            rec = self.last_record()
            self.assertEqual(rec["result"], "CHUNK")
            self.assertEqual(rec["no_progress_streak"], 2)
            self.assertEqual(rec["published_ccns"], [])
            self.assertEqual({p["ccn"] for p in rec["parked"]}, set(ccns))
            deferred = {d["ccn"] for d in self.state()["deferred"]}
            self.assertTrue(set(ccns) <= deferred)

    def test_publish_failure_parks_instead_of_striking(self):
        hw.reparse_changed = self.reparse_ok

        def pub_fail(args, env, state, std, updated, carry, idx, pre,
                     errors, health, wave_no):
            return {"status": "failed", "reason": "counts-validation-fail",
                    "detail": "bad counts",
                    "publish_ccns": list(updated) + list(carry)}
        hw.run_publish_phase = pub_fail
        before = self.loop_bytes()
        rc = hw.run_chunk(self.args(CCNS[:3]), {}, self.std())
        self.assertEqual(rc, 0)
        self.assertEqual(self.loop_bytes(), before)
        self.assertTrue(set(CCNS[:3]) <= set(self.state()["unpublished"]))
        pending = json.loads(hw.PENDING_PATH.read_text())
        self.assertEqual(set(pending["publish_ccns"]), set(CCNS[:3]))
        rec = self.last_record()
        self.assertTrue(any("publish-failed-parked" in p["reason"]
                            for p in rec["parked"]))

    def test_publish_deferred_parks_and_keeps_streak(self):
        hw.reparse_changed = self.reparse_ok

        def pub_defer(args, env, state, std, updated, carry, idx, pre,
                      errors, health, wave_no):
            return {"status": "deferred", "reason": "publish-fail",
                    "detail": "transport",
                    "publish_ccns": list(updated) + list(carry)}
        hw.run_publish_phase = pub_defer
        before = self.loop_bytes()
        rc = hw.run_chunk(self.args(CCNS[:2]), {}, self.std())
        self.assertEqual(rc, 0)
        self.assertEqual(self.loop_bytes(), before)
        rec = self.last_record()
        self.assertTrue(any("publish-deferred" in p["reason"]
                            for p in rec["parked"]))

    def test_successful_chunk_does_not_reset_streak(self):
        hw.reparse_changed = self.reparse_ok

        def pub_ok(args, env, state, std, updated, carry, idx, pre,
                   errors, health, wave_no):
            return {"status": "published",
                    "publish_ccns": list(updated) + list(carry)}
        hw.run_publish_phase = pub_ok
        before = self.loop_bytes()
        rc = hw.run_chunk(self.args(CCNS[:4]), {}, self.std())
        self.assertEqual(rc, 0)
        self.assertEqual(self.loop_bytes(), before,
                         "a landed chunk must not reset the streak either")
        rec = self.last_record()
        self.assertEqual(rec["published_ccns"], CCNS[:4])
        self.assertEqual(rec["api_items"], {c: 7 for c in CCNS[:4]})
        self.assertEqual(rec["no_progress_streak"], 2)

    # -- gates and usage ----------------------------------------------------
    def test_stopped_loop_returns_3_and_writes_nothing(self):
        self.set_streak(3)
        hw.reparse_changed = self.reparse_ok
        state_before = hw.STATE_PATH.read_bytes()
        rc = hw.run_chunk(self.args(CCNS[:2]), {}, self.std())
        self.assertEqual(rc, 3)
        self.assertFalse(hw.WAVE_LOG.exists())
        self.assertEqual(hw.STATE_PATH.read_bytes(), state_before)

    def test_usage_errors_write_nothing(self):
        before = self.loop_bytes()
        self.assertEqual(hw.run_chunk(self.args([]), {}, self.std()), 2)
        big = [f"45{i:04d}" for i in range(hw.CHUNK_MAX + 1)]
        self.assertEqual(hw.run_chunk(self.args(big), {}, self.std()), 2)
        self.assertEqual(self.loop_bytes(), before)
        self.assertFalse(hw.WAVE_LOG.exists())

    def test_unknown_ccn_parks_not_in_queue(self):
        hw.reparse_changed = self.reparse_fail
        rc = hw.run_chunk(self.args(["450001", "999999"]), {}, self.std())
        self.assertEqual(rc, 0)
        rec = self.last_record()
        reasons = {p["ccn"]: p["reason"] for p in rec["parked"]}
        self.assertEqual(reasons["999999"], "not-in-queue")
        self.assertIn("450001", reasons)


if __name__ == "__main__":
    unittest.main(verbosity=2)
