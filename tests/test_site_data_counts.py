"""Compliance and pricing have different source populations."""
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'validate_site_data', Path(__file__).resolve().parents[1] / 'scripts/validate_site_data.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class CountPopulations(unittest.TestCase):
    def check(self, priced):
        # One required facility and one psychiatric price source: the latter
        # belongs in the price index but not in the required/live numerator.
        summary = dict(
            generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            total_facilities=2, cms_required_total=1, live_mrf_total=2,
            compliant=1, compliance_pct=100.0, missing=0,
            enforcement_actions_total=0, standardized_price_index_hospitals=2,
            standardized_price_hospitals=priced, standardized_price_rows=10,
            cpt_indexed_hospitals=1, cpt_indexed_rows=5,
            states=[], types=[], worst_offenders=[])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'meta').mkdir()
            (root / 'meta/summary.json').write_text(json.dumps(summary))
            report = validator.Report()
            validator.check_summary(root, report, None)
        return report

    def test_nonrequired_price_source_does_not_fail_compliance_bound(self):
        self.assertEqual(self.check(2).fails, [])

    def test_positive_prices_still_cannot_exceed_index(self):
        report = self.check(3)
        self.assertEqual(len(report.fails), 1)
        self.assertIn('standardized_price_hospitals <= standardized_price_index_hospitals',
                      report.fails[0])

    def test_negative_price_count_still_fails(self):
        self.assertIn('0 <= standardized_price_hospitals <= standardized_price_index_hospitals',
                      self.check(-1).fails)


if __name__ == '__main__':
    unittest.main()
