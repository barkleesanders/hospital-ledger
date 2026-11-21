#!/usr/bin/env python3
"""Refusal tests for the adjudicated-holds gate in hl_wave.py (2026-11-15).

Wave 62 (2026-11-12) published CCN 31001F -- a Gate-6 federal closure
REJECTed in batch b82 and listed in adjudicated_holds.txt -- because the
wave lane never consulted the holds file. These tests prove the
mechanical guard:

1. load_adjudicated_holds parses the shared file format (comments,
   blanks, first token) and returns None when nothing is readable.
2. publish() refuses a held CCN BEFORE acquiring the publish lock or
   uploading anything, and fails closed when the holds file is
   unreadable.
3. publish() passes a clean CCN through to the locked publish path.
4. split_held partitions exactly.

Run: ~/hospital-ledger/.venv/bin/python3 test_hl_wave_holds.py
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hl_wave", HERE / "hl_wave.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)

HOLDS_TEXT = """\
# Hospital Ledger — adjudicated holds (test fixture)
# Format: one CCN per line; '#' comments and blank lines ignored.

31001F
250038
123456 trailing-token-is-ignored
"""


def _holds_file(text=HOLDS_TEXT):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(text)
    f.close()
    return f.name


class LoadHolds(unittest.TestCase):
    def test_parse_format(self):
        holds = hw.load_adjudicated_holds(_holds_file())
        self.assertEqual(holds, {"31001F", "250038", "123456"})

    def test_unreadable_returns_none(self):
        self.assertIsNone(hw.load_adjudicated_holds("/nonexistent/holds.txt"))

    def test_real_shared_file_loads_and_holds_31001f(self):
        holds = hw.load_adjudicated_holds()
        self.assertIsNotNone(holds, "shared adjudicated_holds.txt must be readable")
        self.assertIn("31001F", holds)


class SplitHeld(unittest.TestCase):
    def test_partition(self):
        kept, held = hw.split_held(["31001F", "999999", "250038"],
                                   {"31001F", "250038"})
        self.assertEqual(kept, ["999999"])
        self.assertEqual(held, ["31001F", "250038"])


class PublishGate(unittest.TestCase):
    def _publish(self, ccns, holds_path):
        """publish() with the lock and the locked body stubbed out; the
        stubs record whether the publish machinery was ever entered."""
        entered = []
        class FakeLock:
            def __init__(self, *a, **k): pass
            def acquire(self): entered.append("lock"); return True
            def release(self): pass
        def fake_locked(out, c, penv):
            entered.append("locked")
            return True, "stubbed"
        with patch.object(hw, "PublishLock", FakeLock), \
             patch.object(hw, "_publish_locked", fake_locked), \
             patch.object(hw, "_holds_candidates", lambda: [holds_path]):
            ok, msg = hw.publish(Path("/tmp"), ccns, {})
        return ok, msg, entered

    def test_held_ccn_refused_before_any_upload(self):
        ok, msg, entered = self._publish(["31001F"], _holds_file())
        self.assertFalse(ok)
        self.assertIn("adjudicated holds", msg)
        self.assertEqual(entered, [], "held CCN reached the publish machinery")

    def test_held_ccn_refused_even_mixed_with_clean(self):
        ok, msg, entered = self._publish(["999999", "31001F"], _holds_file())
        self.assertFalse(ok)
        self.assertIn("31001F", msg)
        self.assertEqual(entered, [])

    def test_unreadable_holds_file_fails_closed(self):
        ok, msg, entered = self._publish(["999999"], "/nonexistent/holds.txt")
        self.assertFalse(ok)
        self.assertIn("unreadable", msg)
        self.assertEqual(entered, [])

    def test_clean_ccn_passes_the_gate(self):
        ok, msg, entered = self._publish(["999999"], _holds_file())
        self.assertTrue(ok)
        self.assertEqual(entered, ["lock", "locked"])


class ParkGate(unittest.TestCase):
    """Wave-65 regression: exception paths park the wave's UNFILTERED
    updated set. park_for_retry is the choke point — held CCNs must
    never enter the pending manifest."""

    def _park(self, ccns, holds_path):
        saved, cleared = [], []
        with patch.object(hw, "save_pending", lambda p: saved.append(p)), \
             patch.object(hw, "clear_pending", lambda: cleared.append(True)), \
             patch.object(hw, "_holds_candidates", lambda: [holds_path]):
            hw.park_for_retry(ccns, 65, "publish-exception", "stub", 0)
        return saved, cleared

    def test_park_strips_held_ccns(self):
        saved, cleared = self._park(["330154", "31001F"], _holds_file())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["publish_ccns"], ["330154"])
        self.assertEqual(cleared, [])

    def test_park_all_held_clears_manifest(self):
        saved, cleared = self._park(["31001F", "250038"], _holds_file())
        self.assertEqual(saved, [])
        self.assertEqual(cleared, [True])

    def test_drop_held_partitions(self):
        with patch.object(hw, "_holds_candidates",
                          lambda: [_holds_file()]):
            self.assertEqual(hw.drop_held(["999999", "31001F"], "test"),
                             ["999999"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
