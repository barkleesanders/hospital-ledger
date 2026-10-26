"""Compliance and pricing have different source populations."""
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys
import os
import shutil
import sqlite3
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from site_data_metrics import COHORT_FIELDS, price_index_metrics

spec = importlib.util.spec_from_file_location(
    'validate_site_data', Path(__file__).resolve().parents[1] / 'scripts/validate_site_data.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
REPO = Path(__file__).resolve().parents[1]


def mixed_hospitals():
    return [dict(ccn=ccn, name=ccn, state='CA', required=required, has_live_mrf=live)
            for ccn, required, live in (
                ('000001', True, True), ('000002', True, True),
                ('000003', True, True), ('000004', False, True),
                ('000005', True, False))]


def mixed_index():
    return [dict(ccn=ccn, n=n, name=ccn, counts={}, cpt_indexed=0, compliance={})
            for ccn, n in (('000001', 10), ('000002', 0),
                           ('000004', 20), ('000005', 30), ('999999', 40))]


def mixed_summary():
    return dict(
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        total_facilities=5, cms_required_total=4, live_mrf_total=4,
        compliant=3, compliance_pct=75.0, missing=1,
        enforcement_actions_total=0, states=[], types=[], worst_offenders=[],
        **price_index_metrics(mixed_hospitals(), mixed_index()))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class CohortJoins(unittest.TestCase):
    def test_repaired_ungraded_price_remains_valid_and_unknown(self):
        # The real repair helper used to omit this required object entirely,
        # blocking publication of recovered ungraded hospital payloads.
        spec = importlib.util.spec_from_file_location(
            'patch_index', REPO / 'scripts/patch_index_from_prices.py')
        repair = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repair)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / '000001.json'
            write_json(path, dict(ccn='000001', hospital_name='Fixture',
                                 items=[dict(code='LOCAL1', type='CDM', gross=25,
                                             cash=None, min=None, max=None)]))
            entry = repair.summary_from_prices_file(path)
            self.assertEqual(entry['compliance'], {})
            write_json(root / 'prices/index.json', {'hospitals': [entry]})
            report = validator.Report()
            validator.check_prices_index(root, report, None)
            self.assertFalse(report.fails, report.fails)
            del entry['compliance']
            write_json(root / 'prices/index.json', {'hospitals': [entry]})
            report = validator.Report()
            validator.check_prices_index(root, report, None)
            self.assertTrue(report.fails, 'Missing compliance must still fail the contract')

    def test_literal_mixed_populations(self):
        self.assertEqual(price_index_metrics(mixed_hospitals(), mixed_index()), {
            'standardized_price_index_hospitals': 5,
            'standardized_price_hospitals': 4, 'standardized_price_rows': 100,
            'cpt_indexed_hospitals': 0, 'cpt_indexed_rows': 0,
            'zero_price_index_entries': 1,
            'required_live_price_hospitals': 1,
            'required_live_zero_price_hospitals': 1,
            'required_live_unindexed_hospitals': 1,
            'price_hospitals_outside_required_live': 3,
        })

    def test_duplicate_ccns_fail_closed_in_both_inputs(self):
        for registry, index in ((mixed_hospitals() + [mixed_hospitals()[0]], mixed_index()),
                                (mixed_hospitals(), mixed_index() + [mixed_index()[0]])):
            with self.subTest(registry=len(registry), index=len(index)):
                with self.assertRaisesRegex(ValueError, 'duplicate CCN 000001'):
                    price_index_metrics(registry, index)

    def test_invalid_ccns_flags_and_row_counts_fail_closed(self):
        mutations = [('ccn', 1), ('ccn', '000001 '), ('n', -1),
                     ('n', True), ('n', '2'), ('n', None), ('cpt_indexed', -1)]
        for field, value in mutations:
            index = mixed_index()
            index[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                price_index_metrics(mixed_hospitals(), index)
        registry = mixed_hospitals()
        registry[0]['required'] = 1
        with self.assertRaisesRegex(ValueError, 'required must be a boolean'):
            price_index_metrics(registry, mixed_index())
        for registry, index in ((None, []), ([], None), ([None], []), ([], [None])):
            with self.subTest(registry=registry, index=index), self.assertRaises(ValueError):
                price_index_metrics(registry, index)

    def test_validator_rejects_duplicate_and_invalid_index_rows_without_crashing(self):
        for mutation in ('duplicate', 'negative', 'invalid_ccn', 'null'):
            index = mixed_index()
            if mutation == 'duplicate':
                index.append(index[0])
            elif mutation == 'negative':
                index[0]['n'] = -1
            elif mutation == 'invalid_ccn':
                index[0]['ccn'] = 1
            else:
                index[0] = None
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_json(root / 'meta/hospitals.json', mixed_hospitals())
                write_json(root / 'prices/index.json', {'hospitals': index})
                report = validator.Report()
                validator.check_prices_index(root, report, mixed_summary())
                self.assertTrue(report.fails)

    def check_contract(self, summary, include_index=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / 'meta/summary.json', summary)
            write_json(root / 'meta/hospitals.json', mixed_hospitals())
            report = validator.Report()
            validator.check_summary(root, report, None)
            validator.check_hospitals(root, report, summary)
            if include_index:
                write_json(root / 'prices/index.json', {'hospitals': mixed_index()})
                validator.check_prices_index(root, report, summary)
            return report

    def test_index_larger_than_compliant_is_valid(self):
        self.assertEqual(self.check_contract(mixed_summary()).fails, [])

    def test_complete_cohorts_and_legacy_both_pass_counts_only(self):
        summary = mixed_summary()
        self.assertEqual(self.check_contract(summary, False).fails, [])
        for field in COHORT_FIELDS:
            summary.pop(field)
        self.assertEqual(self.check_contract(summary, False).fails, [])

    def test_partial_quartet_fails(self):
        summary = mixed_summary()
        del summary['required_live_unindexed_hospitals']
        self.assertTrue(any('must be complete' in failure
                            for failure in self.check_contract(summary, False).fails))

    def test_old_scalar_subtraction_and_balanced_wrong_join_are_rejected(self):
        for priced, unindexed, outside in ((3, -1, 1), (2, 0, 2)):
            summary = mixed_summary()
            summary.update(required_live_price_hospitals=priced,
                           required_live_unindexed_hospitals=unindexed,
                           price_hospitals_outside_required_live=outside)
            failures = self.check_contract(summary).fails
            self.assertTrue(any('registry/index CCN join' in failure for failure in failures), failures)


class RealProducers(unittest.TestCase):
    def test_counts_gate_rejects_balanced_wrong_join_and_accepts_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / 'meta/hospitals.json', mixed_hospitals())
            write_json(root / 'prices/index.json', {'hospitals': mixed_index()})
            for bad in (False, True):
                summary = mixed_summary()
                if bad:
                    summary.update(required_live_price_hospitals=2,
                                   required_live_unindexed_hospitals=0,
                                   price_hospitals_outside_required_live=2)
                else:
                    for key in COHORT_FIELDS:
                        summary.pop(key)
                write_json(root / 'meta/summary.json', summary)
                generated = subprocess.run([
                    sys.executable, str(REPO / 'scripts/make_manifest.py'),
                    str(root), '--producer', 'cohort-regression', '--tier', 'counts'],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(generated.returncode, 0, generated.stderr)
                checked = subprocess.run([
                    sys.executable, str(REPO / 'scripts/validate_site_data.py'),
                    str(root), '--tier', 'counts'],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(checked.returncode, int(bad), checked.stdout + checked.stderr)
                if bad:
                    self.assertIn('summary.required_live_price_hospitals=2 == registry/index CCN join 1', checked.stdout)

    def test_build_then_slim_then_patch_keeps_summary_joined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'scripts'
            scripts.mkdir()
            for name in ('build_site_data.py', 'slim_parsed.py', 'patch_index_from_prices.py',
                         'site_data_metrics.py'):
                shutil.copy2(REPO / 'scripts' / name, scripts / name)
            (root / 'db').mkdir()
            conn = sqlite3.connect(root / 'db/hospital_ledger.db')
            conn.executescript('''
                CREATE TABLE hospitals (ccn, name, address, city, state, zip, county, phone,
                                        hospital_type, ownership, emergency, cms_rating);
                CREATE TABLE mrf_seed (ccn, mrf_url, file_format);
                CREATE TABLE mrf_probe (ccn, mrf_url, alive, http_status, content_length, probed_at);
                CREATE TABLE mrf_rediscovered (ccn, candidate_url, score, alive, head_status,
                                              head_content_length, discovered_at, source_page);
                CREATE TABLE cms_enforcement_match (ccn, case_id);
                CREATE TABLE cms_enforcement (case_id, action, date_of_action);
            ''')
            for h in mixed_hospitals():
                hospital_type = 'Acute Care Hospitals' if h['required'] else 'Psychiatric'
                conn.execute('INSERT INTO hospitals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                             (h['ccn'], h['name'], '', '', 'CA', '', '', '', hospital_type, '', '', ''))
                if h['has_live_mrf']:
                    conn.execute('INSERT INTO mrf_seed VALUES (?, ?, ?)', (h['ccn'], 'https://example.test/mrf', 'csv'))
                    conn.execute('INSERT INTO mrf_probe VALUES (?, ?, 1, 200, 100, ?)',
                                 (h['ccn'], 'https://example.test/mrf', '2026-10-25'))
            conn.commit()
            conn.close()
            public = root / 'public/data'
            write_json(public / 'prices/index.json', {'hospitals': mixed_index()})
            env = {k: v for k, v in os.environ.items()
                   if k not in {'CCNS', 'CCNS_FILE'} and not k.startswith('SLIM_')}

            def run(name, **extra_env):
                result = subprocess.run([sys.executable, str(scripts / name)], cwd=root,
                                        env={**env, **extra_env}, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads((public / 'summary.json').read_text())

            summary = run('build_site_data.py')
            self.assertEqual([summary[k] for k in COHORT_FIELDS], [1, 1, 1, 3])
            write_json(root / 'data/parsed/000002.json', {
                'hospital_name': 'newly priced', 'items': [
                    {'code': '99213', 'code_type': 'CPT', 'description': 'visit', 'gross_charge': 42,
                     'gross_charge_source': {'header': 'STD CHARGE 01/01/19', 'effective_date': '2019-01-01', 'column_1based': 4}},
                    {'code': 'LOCAL1', 'code_type': 'CDM', 'description': 'conflict A', 'gross_charge': 14.56,
                     'gross_charge_source': {'header': 'STD CHARGE 01/01/13', 'effective_date': '2013-01-01', 'column_1based': 5}},
                    {'code': 'LOCAL1', 'code_type': 'CDM', 'description': 'conflict B', 'gross_charge': 1764,
                     'gross_charge_source': {'header': 'STD CHARGE 01/01/13', 'effective_date': '2013-01-01', 'column_1based': 5}},
                    {'code': 'LOCAL2', 'code_type': 'CDM', 'description': 'explicit zero', 'gross_charge': 0,
                     'gross_charge_source': {'header': 'STD CHARGE 09/01/20', 'effective_date': '2020-09-01', 'column_1based': 6}},
                    # A historical duplicate losing payer-based selection still
                    # proves that a later differing amount is ambiguous.
                    {'code': 'LOCAL3', 'code_type': 'CDM', 'gross_charge': 100},
                    {'code': 'LOCAL3', 'code_type': 'CDM', 'gross_charge': 100,
                     'gross_charge_source': {'header': 'STD CHARGE 01/01/13', 'effective_date': '2013-01-01', 'column_1based': 5}},
                    {'code': 'LOCAL3', 'code_type': 'CDM', 'gross_charge': 200,
                     'payer_rates': [{'payer': 'Fixture payer', 'rate_dollar': 150}]}]})
            summary = run('slim_parsed.py', CCNS='000002', SLIM_NO_GZIP='1')
            self.assertEqual([summary[k] for k in COHORT_FIELDS], [2, 0, 1, 3])
            priced = json.loads((public / 'prices/000002.json').read_text())
            self.assertEqual([it['code'] for it in priced['items']], ['99213', 'LOCAL2'])
            self.assertEqual(priced['items'][0]['gross_charge_source']['effective_date'], '2019-01-01')
            self.assertEqual(priced['items'][1]['gross'], 0)
            self.assertEqual(priced['counts']['skipped']['conflicting_historical_gross'], 2)
            write_json(public / 'prices/000003.json', {'hospital_name': 'zero-row', 'items': [],
                                                      'counts': {}, 'compliance': {'score': 0}})
            summary = run('patch_index_from_prices.py', CCNS='000003')
            self.assertEqual([summary[k] for k in COHORT_FIELDS], [2, 1, 0, 3])

            # A malformed preserved index must halt, not become an empty base
            # that silently drops every hospital outside this partial batch.
            previous_summary = (public / 'summary.json').read_bytes()
            (public / 'prices/index.json').write_text('{truncated')
            for name in ('build_site_data.py', 'slim_parsed.py'):
                failed = subprocess.run([sys.executable, str(scripts / name)], cwd=root,
                                        env={**env, 'CCNS': '000002', 'SLIM_NO_GZIP': '1'},
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(failed.returncode, 0, failed.stdout)
                self.assertEqual((public / 'summary.json').read_bytes(), previous_summary)
                self.assertEqual((public / 'prices/index.json').read_text(), '{truncated')

    def test_counts_merge_drops_unjoinable_cohort_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, live = mixed_summary(), mixed_summary()
            fresh['required_live_price_hospitals'] = 999
            write_json(root / 'fresh.json', fresh)
            write_json(root / 'live.json', live)
            result = subprocess.run([sys.executable, str(REPO / 'pipeline/merge_summary.py'),
                                     str(root / 'fresh.json'), str(root / 'live.json'), str(root / 'out.json')],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(any(k in json.loads((root / 'out.json').read_text()) for k in COHORT_FIELDS))


class FullPriceFiles(unittest.TestCase):
    def check(self, records, index_counts=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = index_counts if index_counts is not None else {'000001': 1}
            write_json(root / 'prices/index.json', {'hospitals': [
                {'ccn': ccn, 'n': n} for ccn, n in expected.items()]})
            for ccn, items in records.items():
                write_json(root / f'prices/{ccn}.json', {
                    'ccn': ccn, 'hospital_name': ccn, 'items': items})
            report = validator.Report()
            validator.check_price_files(root, report, list(expected))
            return report

    def item(self):
        return dict(code='99213', type='CPT', gross=42, cash=None, min=None, max=None)

    def test_full_gate_requires_files_for_every_index_entry(self):
        self.assertTrue(self.check({}).fails)
        self.assertTrue(self.check({}, {}).fails)
        self.assertTrue(self.check({'000001': [self.item()]}, {'000001': 1, '000002': 0}).fails)

    def test_full_gate_accepts_complete_positive_and_zero_files(self):
        report = self.check({'000001': [self.item()], '000002': []}, {'000001': 1, '000002': 0})
        self.assertEqual(report.fails, [])

    def test_full_gate_checks_index_count_against_actual_file(self):
        self.assertTrue(self.check({'000001': [self.item()]}, {'000001': 2}).fails)

    def test_full_gate_checks_rows_after_old_200_row_sample(self):
        items = [self.item() for _ in range(201)]
        items[-1]['cash'] = 'not a monetary value'
        self.assertTrue(self.check({'000001': items}, {'000001': 201}).fails)
        items[-1] = dict(self.item(), type='UNKNOWN')
        self.assertTrue(self.check({'000001': items}, {'000001': 201}).fails)

    def test_full_gate_rejects_nonfinite_boolean_and_missing_money(self):
        for value in (float('nan'), float('inf'), True):
            with self.subTest(value=value):
                self.assertTrue(self.check({'000001': [dict(self.item(), gross=value)]}).fails)
        item = self.item()
        del item['gross']
        self.assertTrue(self.check({'000001': [item]}).fails)

    def test_full_gate_preserves_literal_zero_and_finite_negative_source_values(self):
        # A real credit/refund is not a malformed number. This gate checks
        # representation and completeness, not whether a source price is fair.
        self.assertEqual(self.check({'000001': [dict(self.item(), gross=0, cash=-1)]}).fails, [])

    def test_full_gate_rejects_unindexed_files_and_invalid_filenames(self):
        self.assertTrue(self.check({'000001': [self.item()], '000002': []}).fails)
        self.assertTrue(self.check({'000001': [self.item()], '000001EXTRA': []}).fails)


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
