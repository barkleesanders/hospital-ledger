import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_incremental_refresh.py"
SPEC = importlib.util.spec_from_file_location("prepare_incremental_refresh", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "db").mkdir()
        (root / "data" / "parsed").mkdir(parents=True)
        MODULE.DB = root / "db" / "hospital_ledger.db"
        MODULE.STATE = root / "data" / "cloud_refresh_state.json"
        MODULE.STAGED = root / "data" / "cloud_refresh_probe.json"
        MODULE.PLAN = root / "data" / "cloud_refresh_plan.json"
        MODULE.CHANGED = root / "data" / "cloud_refresh_changed_ccns.txt"
        MODULE.WORKLIST = root / "data" / "cloud_refresh_worklist.json"
        MODULE.PARSED = root / "data" / "parsed"
        connection = sqlite3.connect(MODULE.DB)
        connection.executescript(
            """
            CREATE TABLE mrf_probe(
              ccn TEXT, mrf_url TEXT, probed_at TEXT, http_status INTEGER,
              content_type TEXT, content_length INTEGER, final_url TEXT,
              alive INTEGER, PRIMARY KEY(ccn, mrf_url, probed_at)
            );
            """
        )
        connection.close()
        self.original_candidates = MODULE.candidates
        self.original_probe = MODULE.probe
        self.original_probe_url = MODULE.probe_url
        MODULE.candidates = lambda: {"123456": "https://example.test/mrf.json"}
        MODULE.probe = lambda item: (
            item[0],
            {
                "url": item[1],
                "checked_at": "2026-08-03T00:00:00Z",
                "status": 200,
                "final_url": item[1],
                "etag": '"v1"',
                "last_modified": "Sun, 02 Aug 2026 00:00:00 GMT",
                "content_length": "100",
            },
        )

    def tearDown(self):
        MODULE.candidates = self.original_candidates
        MODULE.probe = self.original_probe
        MODULE.probe_url = self.original_probe_url
        self.temporary.cleanup()

    def test_probe_uses_ranked_fallback_url(self):
        MODULE.probe_url = lambda url: {
            "url": url,
            "status": 503 if url.endswith("first") else 200,
            "checked_at": "2026-08-03T00:00:00Z",
        }
        ccn, result = self.original_probe(
            ("123456", ["https://example.test/first", "https://example.test/second"])
        )
        self.assertEqual(ccn, "123456")
        self.assertEqual(result["url"], "https://example.test/second")
        self.assertEqual(result["fallback_attempts"], 1)

    def test_new_hospital_is_explicitly_planned_without_deleting_last_good_data(self):
        cached = MODULE.PARSED / "123456.json"
        cached.write_text('{"old":true}')
        plan = MODULE.plan_refresh(workers=1)
        self.assertEqual(plan["changed"], ["123456"])
        self.assertEqual(MODULE.CHANGED.read_text(), "123456\n")
        worklist = json.loads(MODULE.WORKLIST.read_text())
        self.assertEqual(
            worklist["hospitals"]["123456"]["url"],
            "https://example.test/mrf.json",
        )
        self.assertTrue(cached.exists())

    def test_worklist_records_the_exact_reachable_fallback_selected_by_probe(self):
        first = "https://example.test/first"
        selected = "https://example.test/selected"
        MODULE.candidates = lambda: {"123456": [first, selected]}
        MODULE.probe = lambda item: (
            item[0],
            {
                "url": selected,
                "checked_at": "2026-08-03T00:00:00Z",
                "status": 200,
                "final_url": "https://cdn.example.test/signed",
                "etag": '"v1"',
                "last_modified": "Sun, 02 Aug 2026 00:00:00 GMT",
                "content_length": "100",
            },
        )

        MODULE.plan_refresh(workers=1)

        worklist = json.loads(MODULE.WORKLIST.read_text())
        self.assertEqual(worklist["hospitals"]["123456"]["url"], selected)
        self.assertNotEqual(worklist["hospitals"]["123456"]["url"], first)

    def test_checkpoint_advances_only_for_processed_hospitals(self):
        MODULE.plan_refresh(workers=1)
        empty = MODULE.CHANGED.parent / "empty.txt"
        empty.write_text("")
        result = MODULE.commit_state(
            processed_file=empty,
            force_after_days=35,
            force_spread_days=28,
        )
        self.assertEqual(result["processed"], 0)
        state = json.loads(MODULE.STATE.read_text())
        self.assertNotIn("123456", state["hospitals"])

        processed = MODULE.CHANGED.parent / "processed.txt"
        processed.write_text("123456\n")
        result = MODULE.commit_state(
            processed_file=processed,
            force_after_days=35,
            force_spread_days=28,
        )
        self.assertEqual(result["processed"], 1)
        state = json.loads(MODULE.STATE.read_text())
        self.assertIn("next_force_at", state["hospitals"]["123456"])

        next_plan = MODULE.plan_refresh(workers=1)
        self.assertEqual(next_plan["changed"], [])

    def test_transient_failure_does_not_invalidate_last_good_state(self):
        MODULE.plan_refresh(workers=1)
        processed = MODULE.CHANGED.parent / "processed.txt"
        processed.write_text("123456\n")
        MODULE.commit_state(
            processed_file=processed,
            force_after_days=35,
            force_spread_days=28,
        )
        cached = MODULE.PARSED / "123456.json.gz"
        cached.write_bytes(b"old")
        MODULE.probe = lambda item: (
            item[0],
            {
                "url": item[1],
                "checked_at": "2026-08-10T00:00:00Z",
                "status": 503,
                "error": "HTTP 503",
            },
        )
        plan = MODULE.plan_refresh(workers=1)
        self.assertEqual(plan["changed"], [])
        self.assertEqual(plan["unavailable"], 1)
        self.assertTrue(cached.exists())

    def test_rotating_redirect_url_alone_does_not_force_reprocessing(self):
        MODULE.plan_refresh(workers=1)
        processed = MODULE.CHANGED.parent / "processed.txt"
        processed.write_text("123456\n")
        MODULE.commit_state(
            processed_file=processed,
            force_after_days=35,
            force_spread_days=28,
        )
        MODULE.probe = lambda item: (
            item[0],
            {
                "url": item[1],
                "checked_at": "2026-08-10T00:00:00Z",
                "status": 200,
                "final_url": item[1] + "?signature=rotated",
                "etag": '"v1"',
                "last_modified": "Sun, 02 Aug 2026 00:00:00 GMT",
                "content_length": "100",
            },
        )
        plan = MODULE.plan_refresh(workers=1)
        self.assertEqual(plan["changed"], [])


if __name__ == "__main__":
    unittest.main()
