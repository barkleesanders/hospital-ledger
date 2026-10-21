import argparse
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from ingest_capacity import validate_workers, workers_per_shard


def load_tuner():
    # The planner is not under test; importing the real parser would load optional
    # ingestion dependencies. No hospital/network/database operation is performed.
    batch = types.ModuleType("batch_ingest")
    batch.DB = "unused"
    batch.ROOT = str(SCRIPTS.parent)
    batch.get_target_ccns = lambda *args, **kwargs: []
    batch.parsed_ok = lambda ccn: False
    spec = importlib.util.spec_from_file_location("capacity_tuner_test", SCRIPTS / "tune_ingest_workers.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"batch_ingest": batch}):
        spec.loader.exec_module(module)
    return module


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tuner = load_tuner()
        self.args = argparse.Namespace(allow_parser_failures=True, max_failure_rate=0.35, fallback_workers=4)

    def sample(self, workers=4, code=0, success=10, failed=0, rate=1):
        return dict(workers=workers, returncode=code, succeeded=success,
                    failed=failed, attempted=success + failed, rate_per_second=rate)

    def test_budget_is_shared_instead_of_multiplied(self):
        for total in range(1, 33):
            for shards in range(1, total + 1):
                per_shard = workers_per_shard(total, shards)
                self.assertLessEqual(per_shard * shards, total)
                self.assertGreaterEqual(per_shard, 1)

    def test_invalid_and_over_budget_workers_rejected(self):
        for total, shards in [(0, 1), (4, 0), (4, 5)]:
            with self.assertRaises(ValueError):
                workers_per_shard(total, shards)
        for workers in [0, -1, 5, 128]:
            with self.assertRaises(ValueError):
                validate_workers(workers, 4)

    def test_resource_guard_failure_is_never_a_stable_parser_failure(self):
        for code in [137, 138, -9, 124]:
            self.assertFalse(self.tuner.is_stable(self.sample(code=code), self.args))

    def test_no_stable_sample_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no stable"):
            self.tuner.choose_recommendation([self.sample(code=137, rate=100)], self.args)
        with self.assertRaises(RuntimeError):
            self.tuner.choose_recommendation([self.sample(success=0, failed=10)], self.args)

    def test_faster_unstable_sample_cannot_replace_stable_sample(self):
        result = self.tuner.choose_recommendation([
            self.sample(workers=2), self.sample(workers=128, code=137, rate=100)
        ], self.args)
        self.assertEqual(result, 2)

    def test_tied_samples_choose_lower_concurrency(self):
        self.assertEqual(self.tuner.choose_recommendation([
            self.sample(workers=2), self.sample(workers=4)
        ], self.args), 2)

    def test_default_steps_do_not_infer_capacity_from_cpus(self):
        with mock.patch.object(os, "cpu_count", return_value=128):
            self.assertEqual(self.tuner.cpu_default_max(), 4)
        self.assertEqual(self.tuner.parse_worker_steps(None, 4), [1, 2, 4])
        with self.assertRaises(argparse.ArgumentTypeError):
            self.tuner.parse_worker_steps("8,16", 4)

    def test_probe_command_enforces_memory_disk_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            with (mock.patch.object(self.tuner, "DATA_DIR", directory),
                  mock.patch.object(self.tuner, "STATUS_FILE", str(Path(directory) / "status.json")),
                  mock.patch.object(self.tuner.subprocess, "run") as run):
                run.return_value = argparse.Namespace(returncode=137, stdout="", stderr="guard")
                result = self.tuner.run_probe(4, ["123456"], argparse.Namespace(item_timeout_seconds=1))
                command = run.call_args.args[0]
                self.assertEqual(command[0], "bash")
                self.assertTrue(command[1].endswith("scripts/mem_guard.sh"))
                self.assertEqual(result["returncode"], 137)

    def test_full_runner_passes_divided_budget_to_actual_orchestrator(self):
        batch = types.ModuleType("batch_ingest")
        batch.DB = "unused"
        batch.get_target_ccns = lambda *args, **kwargs: []
        spec = importlib.util.spec_from_file_location("capacity_full_test", SCRIPTS / "full_standardize.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"batch_ingest": batch}):
            spec.loader.exec_module(module)
        with (mock.patch.object(sys, "argv", ["full_standardize.py", "--workers", "4", "--shards", "2"]),
              mock.patch.object(module, "run_sharded_ingest") as shards,
              mock.patch.object(module, "run")):
            module.main()
            self.assertEqual(shards.call_args.args[0].workers, 2)
        with (mock.patch.object(sys, "argv", ["full_standardize.py", "--max-workers", "0", "--auto-workers"]),
              mock.patch.object(module, "auto_tune_workers") as tuner,
              mock.patch.object(module, "run_sharded_ingest")):
            with self.assertRaises(SystemExit):
                module.main()
            tuner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
