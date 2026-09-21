import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh_resume.py"
SPEC = importlib.util.spec_from_file_location("refresh_resume", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RefreshResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data" / "parsed").mkdir(parents=True)
        (self.root / "db").mkdir()
        (self.root / "db" / "hospital_ledger.db").write_bytes(b"sqlite-after-planner")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "pipeline.py").write_text("pipeline-v1\n")
        probe = {"generated_at": "2026-08-03T00:00:00Z", "hospitals": {"123456": {}}}
        plan = {"generated_at": "2026-08-03T00:00:00Z", "changed": ["123456", "654321"]}
        worklist = {
            "schema_version": 1,
            "hospitals": {"123456": {"url": "https://a.test"}, "654321": {"url": "https://b.test"}},
        }
        (self.root / "data" / "cloud_refresh_probe.json").write_text(json.dumps(probe))
        (self.root / "data" / "cloud_refresh_plan.json").write_text(json.dumps(plan))
        (self.root / "data" / "cloud_refresh_worklist.json").write_text(json.dumps(worklist))
        (self.root / "data" / "cloud_refresh_changed_ccns.txt").write_text("654321\n123456\n")
        self.resume = self.root / "resume"

    def tearDown(self):
        self.temporary.cleanup()

    def test_resume_restores_immutable_inputs_and_subtracts_completed(self):
        MODULE.initialize(self.root, self.resume, "run-1")
        checkpoint = self.root / "data" / "parsed" / "123456.json.gz"
        checkpoint.write_bytes(b"gzip-checkpoint")
        additions = self.root / "additions.txt"
        additions.write_text("123456\n")
        MODULE.complete(self.root, self.resume, additions)

        for relative in MODULE.INPUTS.values():
            (self.root / relative).unlink()
        checkpoint.unlink()
        completed = self.root / "completed.txt"
        remaining = self.root / "remaining.txt"
        result = MODULE.restore(self.root, self.resume, completed, remaining)

        self.assertEqual(result, {"total": 2, "completed": 1, "remaining": 1})
        self.assertEqual(completed.read_text(), "123456\n")
        self.assertEqual(remaining.read_text(), "654321\n")
        self.assertEqual(checkpoint.read_bytes(), b"gzip-checkpoint")
        self.assertEqual((self.root / "db" / "hospital_ledger.db").read_bytes(), b"sqlite-after-planner")
        self.assertEqual(json.loads((self.root / "data" / "cloud_refresh_plan.json").read_text())["changed"], ["123456", "654321"])

    def test_repository_change_refuses_resume(self):
        MODULE.initialize(self.root, self.resume, "run-1")
        (self.root / "scripts" / "pipeline.py").write_text("pipeline-v2\n")
        with self.assertRaisesRegex(SystemExit, "repository digest mismatch"):
            MODULE.validate_resume(self.root, self.resume)

    def test_tampered_input_or_checkpoint_refuses_resume(self):
        MODULE.initialize(self.root, self.resume, "run-1")
        (self.resume / "inputs" / "plan.json").write_text("{}")
        with self.assertRaisesRegex(SystemExit, "digest validation"):
            MODULE.validate_resume(self.root, self.resume)

        self.resume = self.root / "resume-2"
        MODULE.initialize(self.root, self.resume, "run-2")
        checkpoint = self.root / "data" / "parsed" / "123456.json.gz"
        checkpoint.write_bytes(b"good")
        additions = self.root / "additions.txt"
        additions.write_text("123456\n")
        MODULE.complete(self.root, self.resume, additions)
        (self.resume / "parsed" / "123456.json.gz").write_bytes(b"tampered")
        with self.assertRaisesRegex(SystemExit, "checkpoint failed validation"):
            MODULE.validate_resume(self.root, self.resume)

    def test_missing_ready_marker_refuses_partial_initial_staging(self):
        MODULE.initialize(self.root, self.resume, "run-1")
        (self.resume / "ready.json").unlink()
        with self.assertRaisesRegex(SystemExit, "invalid JSON file"):
            MODULE.validate_resume(self.root, self.resume)

    def test_zero_remaining_is_a_valid_resume(self):
        MODULE.initialize(self.root, self.resume, "run-1")
        additions = self.root / "additions.txt"
        additions.write_text("123456\n654321\n")
        for ccn in ("123456", "654321"):
            (self.root / "data" / "parsed" / f"{ccn}.json.gz").write_bytes(ccn.encode())
        MODULE.complete(self.root, self.resume, additions)
        completed = self.root / "completed.txt"
        remaining = self.root / "remaining.txt"
        result = MODULE.restore(self.root, self.resume, completed, remaining)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(remaining.read_text(), "")

    def test_only_nonfinal_status_is_resumable(self):
        status = self.root / "status.json"
        status.write_text('{"status":"failed","phase":"ingesting","resume_id":"run-1"}')
        self.assertEqual(MODULE.resumable_status(status), "run-1")
        status.write_text('{"status":"published","phase":"complete","resume_id":"run-1"}')
        self.assertEqual(MODULE.resumable_status(status), "")
        self.assertEqual(MODULE.completed_staging_status(status), "run-1")
        status.write_text('{"status":"committed","phase":"committed","resume_id":"run-1"}')
        self.assertEqual(MODULE.completed_staging_status(status), "run-1")
        status.write_text('{"status":"failed","phase":"ingesting","resume_id":"run-1"}')
        self.assertEqual(MODULE.completed_staging_status(status), "")
        status.write_text('{"status":"failed","phase":"committed","resume_id":"run-1"}')
        self.assertEqual(MODULE.resumable_status(status), "")
        self.assertEqual(MODULE.completed_staging_status(status), "run-1")

    def test_shell_keeps_zero_remaining_resume_on_the_publication_path(self):
        shell = (SCRIPT.parent / "cloud_refresh.sh").read_text()
        self.assertIn('split -l "${INGEST_SHARD_SIZE:-50}" -d -a 4 "$INGEST_FILE"', shell)
        self.assertIn('if [ "$CHANGED_COUNT" -eq 0 ]; then', shell)
        self.assertNotIn('if [ "$REMAINING_COUNT" -eq 0 ]; then', shell)

        canonical = shell.index('"_pipeline/parsed/" data/parsed "$SUCCESS_FILE"')
        commit = shell.index('scripts/prepare_incremental_refresh.py --commit --processed-file "$SUCCESS_FILE"')
        cleanup = shell.index("if ! cleanup_staging; then")
        self.assertLess(canonical, commit)
        self.assertLess(commit, cleanup)

        ready = shell.index('"$STAGING_PREFIX/ready.json" "$RESUME_DIR/ready.json"')
        exposed = shell.index('RESUME_ID="$CANDIDATE_RESUME_ID"')
        self.assertLess(ready, exposed)
        self.assertIn('if [ "$STATE_COMMITTED" = 1 ]; then', shell)


if __name__ == "__main__":
    unittest.main()
