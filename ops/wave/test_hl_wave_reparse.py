"""Regressions for real per-row reparse outcomes observed in chunk continuation."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('hl_wave', Path(__file__).with_name('hl_wave.py'))
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)


class ReparseIsolation(unittest.TestCase):
    def test_empty_preview_cannot_poison_valid_peer(self):
        self.exercise('empty')

    def test_parse_timeout_cannot_strand_valid_peer(self):
        self.exercise('parse-timeout')

    def test_slim_timeout_restores_index_and_keeps_valid_peer(self):
        self.exercise('slim-timeout')

    def test_failed_pending_preview_is_restored_byte_for_byte(self):
        for failure in ('empty', 'slim-timeout', 'slim-error', 'invalid', 'missing'):
            with self.subTest(failure=failure):
                self.exercise(failure, existing=True)

    def test_failed_new_preview_is_removed(self):
        for failure in ('empty', 'slim-timeout', 'slim-error', 'invalid', 'missing'):
            with self.subTest(failure=failure):
                self.exercise(failure)

    def exercise(self, failure, existing=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prices = root / 'public/data/prices'
            prices.mkdir(parents=True)
            index = prices / 'index.json'
            index.write_text(json.dumps({'hospitals': [{'ccn': '010001', 'n': 1}]}))
            failed_price = prices / '149803.json'
            original = b'{"items": [{"code": "99212", "type": "CPT"}]}\n'
            if existing:
                failed_price.write_bytes(original)
            rows = [{'ccn': c, 'name': c, 'url': 'https://example.org/' + c}
                    for c in ('149803', '132002')]
            current = None

            def run(argv, **kwargs):
                nonlocal current
                if argv[1] == 'scripts/mrf_parse.py':
                    current = argv[argv.index('--ccn') + 1]
                    # Preview decoding must not survive into the next parser.
                    import inspect
                    self.assertNotIn('items', inspect.currentframe().f_back.f_locals)
                    if current == '149803' and failure == 'parse-timeout':
                        raise subprocess.TimeoutExpired(argv, 1800)
                else:
                    ccn = kwargs['env']['CCNS']
                    doc = json.loads(index.read_text())
                    items = [] if ccn == '149803' else [{'code': '99213', 'type': 'CPT'}]
                    doc['hospitals'].append({'ccn': ccn, 'n': len(items)})
                    index.write_text(json.dumps(doc))
                    (prices / (ccn + '.json')).write_text(json.dumps({'items': items}))
                    if ccn == '149803' and failure == 'slim-timeout':
                        raise subprocess.TimeoutExpired(argv, 1800)
                    if ccn == '149803' and failure == 'slim-error':
                        return subprocess.CompletedProcess(argv, 1, '', 'slim failed')
                    if ccn == '149803' and failure == 'invalid':
                        failed_price.write_text('{')
                    if ccn == '149803' and failure == 'missing':
                        failed_price.unlink()
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch.object(hw, 'ROOT', root), patch.object(hw, 'INDEX_PATH', index), \
                    patch.object(hw.subprocess, 'run', run):
                errors = []
                updated, deferred = hw.reparse_changed(rows, 2, errors, {'urls': {}})
            self.assertEqual(updated, ['132002'])
            self.assertEqual(deferred, [])
            self.assertEqual([e['ccn'] for e in errors], ['149803'])
            self.assertEqual(json.loads(index.read_text()), {'hospitals': [
                {'ccn': '010001', 'n': 1}, {'ccn': '132002', 'n': 1}]})
            if existing:
                self.assertEqual(failed_price.read_bytes(), original)
            else:
                self.assertFalse(failed_price.exists())


if __name__ == '__main__':
    unittest.main()
