"""Copy gates must compare preview hospitals with the nonempty population."""
import importlib.util
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'predeploy_audit', Path(__file__).resolve().parents[1] / 'scripts/predeploy_audit.py')
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


class PreviewClaimPopulations(unittest.TestCase):
    def test_preview_claim_uses_nonempty_count_not_index_count(self):
        claim = next(c for c in audit.CLAIMS if c.label == 'README: nonempty price-preview hospitals')
        with patch.object(audit, '_summary', return_value={
                'standardized_price_hospitals': 2, 'standardized_price_index_hospitals': 3}):
            self.assertEqual(claim.truth(), '2')

    def test_row_claim_preserves_exact_integer(self):
        claim = next(c for c in audit.CLAIMS if c.label == 'README: standardized price rows')
        with patch.object(audit, '_summary', return_value={'standardized_price_rows': 1234567}):
            self.assertEqual(claim.truth(), '1,234,567')
            match = re.search(claim.pattern,
                              '| Standardized price rows across those hospitals | **1,234,567** |')
            self.assertEqual(match.group(1), claim.truth())

    def test_stage_coverage_uses_nonempty_count(self):
        claim = next(c for c in audit.CLAIMS if c.label == 'README: Stage 4 coverage line')
        with patch.object(audit, '_summary', return_value={
                'standardized_price_hospitals': 2, 'standardized_price_index_hospitals': 3}):
            self.assertEqual(claim.truth(), '2')
            self.assertIsNotNone(re.search(claim.pattern, 'live (2 nonempty on-site price previews;'))


if __name__ == '__main__':
    unittest.main()
