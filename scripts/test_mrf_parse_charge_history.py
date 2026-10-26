"""Sparse historical STD CHARGE columns are one dated gross-charge measure."""
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('mrf_parse', HERE / 'mrf_parse.py')
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)

HEADERS = ['MNEMONIC', 'NAME/DESCRIPTION', 'CPT-4 01/01/20',
           'STD CHARGE 01/01/19', 'STD CHARGE 09/01/20', 'STD CHARGE 01/01/13',
           'STD CHARGE 02/30/25', 'ACA 01/01/30 RATE CHARGE',
           'cash price', 'minimum negotiated charge', 'maximum negotiated charge']
ROWS = [
    ['140080210', 'CCU/SEMI ROOM BOARD', '', '750', '', '', '9999', '8888', '50', '20', '100'],
    ['140090110', 'MED PED UNIT/PVT ROOM BOARD', '', '', '', '358', '', '8888', '', '', ''],
    ['01234567890', 'NEWER ZERO', '99213', '100', '0', '50', '', '8888', '', '', ''],
    ['250000001', 'LATEST VALID', '', '2.24', '$3.50', '1', '', '8888', '', '', ''],
    ['250000002', 'INVALID NEWEST AMOUNT', '', '2', 'NaN', '', '', '8888', '', '', ''],
    ['250000003', 'ONLY PAYER', '', '', '', '', '9999', '8888', '', '', ''],
]


def csv_bytes(headers=HEADERS, rows=ROWS):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode()


class DatedChargeHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_alias = mp.HEADER_ALIASES_PATH
        mp.HEADER_ALIASES_PATH = str(Path(self.tmp.name) / 'aliases.json')

    def tearDown(self):
        mp.HEADER_ALIASES_PATH = self.old_alias
        self.tmp.cleanup()

    def assert_history(self, items):
        self.assertEqual(len(items), 6)
        self.assertEqual([x['code'] for x in items], [row[0] for row in ROWS])
        self.assertEqual([x['code_type'] for x in items], ['CDM'] * 6)
        self.assertEqual([x['description'] for x in items], [row[1] for row in ROWS])
        self.assertEqual([x['gross_charge'] for x in items], [750, 358, 0, 3.5, 2, None])
        self.assertEqual(items[0]['cash_discount'], 50)
        self.assertEqual(items[0]['min_negotiated'], 20)
        self.assertEqual(items[0]['max_negotiated'], 100)
        self.assertEqual(items[0]['gross_charge_source'], {
            'header': 'STD CHARGE 01/01/19', 'effective_date': '2019-01-01', 'column_1based': 4})
        self.assertEqual(items[2]['gross_charge_source']['effective_date'], '2020-09-01')
        self.assertNotIn('gross_charge_source', items[-1])

    def test_csv_actual_history_layout(self):
        self.assert_history(mp.parse_csv_wide(csv_bytes()))

    def test_xlsx_delegates_to_same_history_selection(self):
        wb = Workbook()
        ws = wb.active
        ws.title = '660686840-DCHSan Juan'
        ws.append(HEADERS)
        for row in ROWS:
            ws.append(row)
        data = io.BytesIO()
        wb.save(data)
        wb.close()
        fmt, items = mp.parse(data.getvalue(), 'https://example.test/DCHSanjuancharges.xlsx')
        self.assertEqual(fmt, 'xlsx')
        self.assert_history(items)

    def test_tall_and_streaming_paths_share_selection(self):
        self.assert_history(mp.parse_csv_tall(csv_bytes()))
        for tall in (False, True):
            headers = HEADERS + (['payer_name', 'negotiated_rate'] if tall else [])
            rows = [row + (['Aetna', '7.50'] if tall else []) for row in ROWS]
            response = MagicMock()
            response.url = 'https://example.test/history.csv'
            response.headers = {'content-type': 'text/csv'}
            response.iter_bytes.return_value = iter([csv_bytes(headers, rows)])
            client = MagicMock()
            client.stream.return_value.__enter__.return_value = response
            with self.subTest(tall=tall), patch.object(mp.httpx, 'Client', return_value=client):
                items, _, _ = mp.stream_parse_large_csv('https://example.test/history.csv')
                self.assert_history(items)
                if tall:
                    self.assertEqual(items[0]['payer_rates'], [
                        {'payer': 'Aetna', 'plan': '', 'rate_dollar': 7.5}])

    def test_stale_alias_cannot_restore_arbitrary_historical_column(self):
        mapping, _ = mp.resolve_csv_columns(HEADERS, ROWS)
        mapping.update(code=2, gross=5)
        Path(mp.HEADER_ALIASES_PATH).write_text(json.dumps({
            mp.header_fingerprint(HEADERS): {'mapping': mapping}}))
        self.assert_history(mp.parse_csv_wide(csv_bytes()))

    def test_unsorted_dates_zero_blank_and_invalid_money(self):
        headers = ['MNEMONIC', 'description', 'STD CHARGE 01/01/13',
                   'STD CHARGE 09/01/20', 'STD CHARGE 01/01/19']
        for newest in ('', '-1', 'inf', 'not money'):
            with self.subTest(newest=newest):
                item = mp.parse_csv_wide(csv_bytes(headers, [['000001', 'service', '5', newest, '10']]))[0]
                self.assertEqual(item['gross_charge'], 10)
        item = mp.parse_csv_wide(csv_bytes(headers, [['000001', 'service', '5', '0', '10']]))[0]
        self.assertEqual(item['gross_charge'], 0)

    def test_unrelated_dated_measures_remain_separate(self):
        headers = ['code', 'description', 'gross charge', 'inpatient STD CHARGE 01/01/25',
                   'STD CHARGE 01/01/25 outpatient', 'cash STD CHARGE 01/01/25',
                   'STD CHARGE 02/30/25', 'ACA 01/01/30 RATE CHARGE']
        item = mp.parse_csv_wide(csv_bytes(headers, [['A100', 'service', '15', '80', '90', '10', '99', '101']]))[0]
        self.assertEqual(item['gross_charge'], 15)
        self.assertNotIn('gross_charge_source', item)

    def test_canonical_current_gross_is_not_replaced_by_history(self):
        # Pipe-style CMS and extended gross headers were recognized before
        # history support. Their current prices must never become old charges.
        for header in ('gross charge', 'standard_charge|gross',
                       'gross charge amount', 'gross charge ($)', 'total charge'):
            headers = ['code', 'description', header, 'STD CHARGE 01/01/20']
            for parse in (mp.parse_csv_wide, mp.parse_csv_tall):
                with self.subTest(header=header, parser=parse.__name__):
                    item = parse(csv_bytes(headers, [['A100', 'service', '15', '8']]))[0]
                    self.assertEqual(item['gross_charge'], 15)
                    self.assertNotIn('gross_charge_source', item)
            rows = [['A100', 'service', '15', '8']]
            mapping, _ = mp.resolve_csv_columns(headers, rows)
            mapping['gross'] = 3  # a learned mapping from before the fix
            Path(mp.HEADER_ALIASES_PATH).write_text(json.dumps({
                mp.header_fingerprint(headers): {'mapping': mapping}}))
            item = mp.parse_csv_wide(csv_bytes(headers, rows))[0]
            self.assertEqual(item['gross_charge'], 15)
            self.assertNotIn('gross_charge_source', item)
            wb = Workbook()
            wb.active.append(headers)
            wb.active.append(rows[0])
            data = io.BytesIO()
            wb.save(data)
            wb.close()
            _, items = mp.parse(data.getvalue(), 'https://example.test/current.xlsx')
            self.assertEqual(items[0]['gross_charge'], 15)
            self.assertNotIn('gross_charge_source', items[0])
            response = MagicMock()
            response.url = 'https://example.test/current.csv'
            response.headers = {'content-type': 'text/csv'}
            response.iter_bytes.return_value = iter([csv_bytes(headers, rows)])
            client = MagicMock()
            client.stream.return_value.__enter__.return_value = response
            with patch.object(mp.httpx, 'Client', return_value=client):
                items, _, _ = mp.stream_parse_large_csv(response.url)
            self.assertEqual(items[0]['gross_charge'], 15)
            self.assertNotIn('gross_charge_source', items[0])

    def test_mnemonic_is_literal_cdm_even_when_it_looks_like_a_code(self):
        headers = ['MNEMONIC', 'description', 'STD CHARGE 01/01/20']
        item = mp.parse_csv_wide(csv_bytes(headers, [['CPT-99213', 'service', '15']]))[0]
        self.assertEqual((item['code'], item['code_type']), ('CPT-99213', 'CDM'))

    def test_nonhistory_clinical_codes_keep_priority_over_mnemonic(self):
        for clinical_header in ('CPT HCPCS', 'CPT'):
            headers = ['MNEMONIC', clinical_header, 'description', 'gross charge']
            with self.subTest(clinical_header=clinical_header):
                item = mp.parse_csv_wide(csv_bytes(headers, [['LOCAL1', '99213', 'visit', '15']]))[0]
                self.assertEqual((item['code'], item['code_type']), ('99213', 'CPT'))
                # A learned nonhistory mapping must not be overridden simply
                # because a MNEMONIC attribute accompanies the clinical code.
                item = mp.parse_csv_wide(csv_bytes(headers, [['LOCAL1', '99213', 'visit', '15']]))[0]
                self.assertEqual((item['code'], item['code_type']), ('99213', 'CPT'))

    def test_mnemonic_fallback_without_clinical_code(self):
        headers = ['MNEMONIC', 'description', 'gross charge']
        item = mp.parse_csv_wide(csv_bytes(headers, [['LOCAL1', 'visit', '15']]))[0]
        self.assertEqual((item['code'], item['code_type']), ('LOCAL1', 'CDM'))


if __name__ == '__main__':
    unittest.main()
