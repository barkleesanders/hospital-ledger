import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_refresh_output.py"
SPEC = importlib.util.spec_from_file_location("validate_refresh_output", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ValidateRefreshOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "prices").mkdir()
        MODULE.SUMMARY = root / "summary.json"
        MODULE.PRICES_INDEX = root / "prices" / "index.json"
        MODULE.CPT_INDEX = root / "cpt-index.json"
        MODULE.PUBLIC_DATA = root
        self.baseline = root / "baseline.json"
        self.baseline_data = root / "baseline-data"
        self.processed = root / "processed.txt"
        summary = {key: 100 for key in MODULE.GUARDED_METRICS}
        MODULE.SUMMARY.write_text(json.dumps(summary))
        self.baseline.write_text(json.dumps(summary))
        summary["standardized_price_index_hospitals"] = 1
        MODULE.SUMMARY.write_text(json.dumps(summary))
        baseline_summary = dict(summary)
        self.baseline.write_text(json.dumps(baseline_summary))
        MODULE.PRICES_INDEX.write_text(json.dumps({"hospitals": [{"ccn": "123456"}]}))
        MODULE.CPT_INDEX.write_text(json.dumps({str(index): [] for index in range(5000)}))
        (root / "payer").mkdir()
        (root / "cpt-detail").mkdir()
        (root / "payer" / "a.json").write_text("{}")
        (root / "cpt-detail" / "1.json").write_text("{}")
        (root / "compliance-ranking.json").write_text(
            json.dumps({"hospitals": [{"ccn": "123456"}]})
        )
        (root / "payers-index.json").write_text(
            json.dumps({"featured": [{"slug": "a"}], "total": 1})
        )
        self.baseline_data.mkdir()
        (self.baseline_data / "payer").mkdir()
        (self.baseline_data / "cpt-detail").mkdir()
        (self.baseline_data / "payer" / "a.json").write_text("{}")
        (self.baseline_data / "cpt-detail" / "1.json").write_text("{}")
        (self.baseline_data / "compliance-ranking.json").write_text(
            json.dumps({"hospitals": [{"ccn": "123456"}]})
        )
        (self.baseline_data / "payers-index.json").write_text(
            json.dumps({"featured": [{"slug": "a"}], "total": 1})
        )
        (MODULE.PRICES_INDEX.parent / "123456.json").write_text(
            json.dumps({"ccn": "123456", "n_slim": 1})
        )
        self.processed.write_text("123456\n")

    def tearDown(self):
        self.temporary.cleanup()

    def run_main(self):
        argv = [
            str(SCRIPT),
            "--baseline-summary",
            str(self.baseline),
            "--baseline-data-dir",
            str(self.baseline_data),
            "--processed-file",
            str(self.processed),
        ]
        with mock.patch.object(sys, "argv", argv):
            return MODULE.main()

    def test_valid_output_passes(self):
        self.assertEqual(self.run_main(), 0)

    def test_large_metric_regression_fails(self):
        summary = json.loads(MODULE.SUMMARY.read_text())
        summary["standardized_price_rows"] = 90
        MODULE.SUMMARY.write_text(json.dumps(summary))
        with self.assertRaises(SystemExit):
            self.run_main()

    def test_processed_hospital_requires_displayable_output(self):
        (MODULE.PRICES_INDEX.parent / "123456.json").write_text(
            json.dumps({"ccn": "123456", "n_slim": 0})
        )
        with self.assertRaises(SystemExit):
            self.run_main()


if __name__ == "__main__":
    unittest.main()
