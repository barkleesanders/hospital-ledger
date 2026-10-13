import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "batch_ingest.py"
MRF_PARSE = types.ModuleType("mrf_parse")
MRF_PARSE.ingest_one = lambda *args, **kwargs: (False, 0, "", "not used")
MRF_PARSE.normalize_source_url = lambda url: str(url or "").strip()
PREVIOUS_MRF_PARSE = sys.modules.get("mrf_parse")
sys.modules["mrf_parse"] = MRF_PARSE
SPEC = importlib.util.spec_from_file_location("batch_ingest", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
if PREVIOUS_MRF_PARSE is None:
    del sys.modules["mrf_parse"]
else:
    sys.modules["mrf_parse"] = PREVIOUS_MRF_PARSE


class WorklistTests(unittest.TestCase):
    def test_authoritative_url_bypasses_independent_candidate_ranking(self):
        selected = "https://selected.example.test/mrf.json"
        attempted = []
        with (
            mock.patch.object(MODULE, "ranked_mrf_candidates") as ranked,
            mock.patch.object(MODULE, "probe_candidate_url", return_value=(True, "probe_ok")),
            mock.patch.object(
                MODULE,
                "ingest_candidate",
                side_effect=lambda ccn, candidate: (
                    attempted.append(candidate["url"]) or True,
                    12,
                    "json",
                    "ok",
                ),
            ),
        ):
            result = MODULE.ingest_ccn(
                "123456",
                selected_candidate={"url": selected, "name": "Test Hospital"},
            )

        self.assertTrue(result[1])
        self.assertEqual(attempted, [selected])
        ranked.assert_not_called()

    def test_authoritative_url_works_when_ranker_has_no_candidate(self):
        selected = "https://selected.example.test/mrf.csv"
        with (
            mock.patch.object(MODULE, "ranked_mrf_candidates", return_value=[]),
            mock.patch.object(MODULE, "probe_candidate_url", return_value=(True, "probe_ok")),
            mock.patch.object(MODULE, "ingest_candidate", return_value=(True, 7, "csv", "ok")) as ingest,
        ):
            result = MODULE.ingest_ccn("654321", selected_candidate={"url": selected})

        self.assertTrue(result[1])
        self.assertEqual(ingest.call_args.args[1]["url"], selected)

    def test_requested_ccn_missing_from_worklist_is_rejected(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE hospitals(ccn TEXT PRIMARY KEY, name TEXT)")
        with self.assertRaisesRegex(ValueError, "missing 1 requested CCN"):
            MODULE.select_worklist_candidates(
                connection,
                ["123456", "654321"],
                {"123456": {"url": "https://example.test/mrf.json"}},
            )
        connection.close()

    def test_loads_planner_worklist_without_changing_url(self):
        selected = "https://example.test/mrf.json?signature=A%2FB"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worklist.json"
            path.write_text(json.dumps({"hospitals": {"123456": {"url": selected}}}))
            worklist = MODULE.load_worklist(path)
        self.assertEqual(worklist["123456"]["url"], selected)


if __name__ == "__main__":
    unittest.main()
