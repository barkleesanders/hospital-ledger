"""The source file's own header identity (remediation wave 4, lane D).

Runs inside `unittest discover -s tests`, so scripts/verify.sh and `npm run predeploy` gate it.
Every fixture is the SHAPE of a real retained source file, with the hospital it came from named:

* ByteOrderMark      - 031311 Little Colorado (CSV), 054144 UCSF Langley Porter (JSON), 019800
                       UAB (zipped CSV): a UTF-8 byte-order mark hid the header, the fleet reader
                       returned {} and 108 retained, bound, live files stayed "no in-file name".
* HospitalLocation   - 034025 The Healing Place, 261311 Perry: the 2.0 CSV template names the
                       facility column `hospital_location`.
* HeaderRowIsNotData - 224043 TaraVista: `\r\r\n` line ends put a blank line under the header and
                       the reader recorded hospital_name = 'last_updated_on'.
* NeverAGuess        - a web page, a spreadsheet with no CMS keys and a custom layout whose first
                       cell is a hospital's name must all yield {} (the design's sabotage case).
* UnchangedReads     - the forms the fleet reader already read keep reading the same.
"""
import gzip
import io
import os
import sys
import unittest
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import hl_source_identity as si  # noqa: E402

BOM = b"\xef\xbb\xbf"
ATTEST = '"To the best of its knowledge and belief, this hospital has included all applicable standard charge information"'
CSV_V3 = (f"hospital_name,last_updated_on,version,location_name,hospital_address,license_number|AZ,type_2_npi,{ATTEST}\n"
          '"Little Colorado Medical Center",2026-01-01,3.0.0,"Little Colorado Medical Center",'
          '"1501 N Williamson Ave, Winslow, AZ 86047",AZ-123,1234567890,TRUE\n'
          "description,code|1,code|1|type,standard_charge|gross\n"
          "Aspirin,1,CDM,5.00\n").encode()
WANT_V3 = {"hospital_name": "Little Colorado Medical Center", "location_name": "Little Colorado Medical Center",
           "hospital_address": "1501 N Williamson Ave, Winslow, AZ 86047", "license_number": "AZ-123",
           "type_2_npi": "1234567890"}
JSON_V3 = (b'{"hospital_name":"UCSF Langley Porter Psychiatric Hospital","last_updated_on":"2026-04-01",'
           b'"version":"3.0.0","location_name":["UCSF Langley Porter Psychiatric Hospital"],'
           b'"hospital_address":["1600 Divisadero, Flr 7, San Francisco, CA 94115"],'
           b'"license_information":{"license_number":"220000377","state":"CA"},"type_2_npi":["1111111111"],'
           b'"standard_charge_information":[]}')
WANT_JSON = {"hospital_name": "UCSF Langley Porter Psychiatric Hospital",
             "location_name": "UCSF Langley Porter Psychiatric Hospital",
             "hospital_address": "1600 Divisadero, Flr 7, San Francisco, CA 94115",
             "license_number": "220000377", "type_2_npi": "1111111111"}


def zipped(name, body):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, body)
    return buf.getvalue()


class ByteOrderMark(unittest.TestCase):
    def test_csv_header_row_behind_a_byte_order_mark_is_read(self):
        self.assertEqual(si.extract_source_identity(BOM + CSV_V3), WANT_V3)

    def test_json_behind_a_byte_order_mark_is_read_as_json(self):
        self.assertEqual(si.extract_source_identity(BOM + JSON_V3), WANT_JSON)

    def test_zipped_csv_member_behind_a_byte_order_mark_is_read(self):
        self.assertEqual(si.extract_source_identity(zipped("63-6005396_x_standardcharges.csv", BOM + CSV_V3)), WANT_V3)

    def test_zipped_json_member_behind_a_byte_order_mark_is_read(self):
        self.assertEqual(si.extract_source_identity(zipped("x_standardcharges.json", BOM + JSON_V3)), WANT_JSON)

    def test_gzipped_file_behind_a_byte_order_mark_is_read(self):
        self.assertEqual(si.extract_source_identity(gzip.compress(BOM + CSV_V3)), WANT_V3)
        self.assertEqual(si.extract_source_identity(gzip.compress(BOM + JSON_V3)), WANT_JSON)

    def test_quoted_header_cells_behind_a_byte_order_mark(self):
        body = (b'"hospital_name","last_updated_on","version","hospital_location","hospital_address","license_number | AR"\n'
                b'"Perry County Memorial Hospital","2025-01-01","2.0.0","Perry County Memorial Hospital",'
                b'"434 N West St, Perryville, MO 63775","L-1"\n')
        got = si.extract_source_identity(BOM + body)
        self.assertEqual(got["hospital_name"], "Perry County Memorial Hospital")
        self.assertEqual(got["hospital_address"], "434 N West St, Perryville, MO 63775")

    def test_the_mark_is_only_stripped_from_the_front(self):
        body = b"hospital_name,hospital_address\n\xef\xbb\xbfExample General Hospital,\"1 A St, X, CA 90210\"\n"
        self.assertEqual(si.extract_source_identity(body)["hospital_name"], "﻿Example General Hospital")


class HospitalLocation(unittest.TestCase):
    V2 = (b"hospital_name,last_updated_on,version,hospital_location,hospital_address,license_number | AZ\n"
          b'The Healing Place LLC,2025-06-01,2.0.0,The Healing Place,"1 Recovery Way, Prescott, AZ 86301",BH-9\n')

    def test_hospital_location_is_stored_as_location_name(self):
        got = si.extract_source_identity(BOM + self.V2)
        self.assertEqual(got, {"hospital_name": "The Healing Place LLC", "location_name": "The Healing Place",
                               "hospital_address": "1 Recovery Way, Prescott, AZ 86301", "license_number": "BH-9"})
        self.assertNotIn("hospital_location", got)

    def test_a_header_with_both_spellings_keeps_its_own_location_name(self):
        body = (b"hospital_name,hospital_location,location_name,hospital_address\n"
                b'Example Health Inc,From The Old Column,Example General Hospital,"1 A St, X, CA 90210"\n')
        self.assertEqual(si.extract_source_identity(body)["location_name"], "Example General Hospital")

    def test_json_and_meta_row_forms(self):
        self.assertEqual(si.extract_source_identity(
            b'{"hospital_name":"Example Health Inc","hospital_location":["Example General Hospital"]}')["location_name"],
            "Example General Hospital")
        self.assertEqual(si.extract_source_identity(
            b"hospital_name,Example Health Inc\nhospital_location,Example General Hospital\n")["location_name"],
            "Example General Hospital")


class HeaderRowIsNotData(unittest.TestCase):
    HEADER = b"hospital_name,last_updated_on,version,location_name,hospital_address,license_number|MA,type_2_npi"

    def test_a_blank_line_under_the_header_does_not_hide_the_value_row(self):
        body = (self.HEADER + b"\r\r\n"
                b'TARAVISTA BEHAVIORAL HEALTH CENTER,3/2/2026,3.0.0,TARAVISTA BEHAVIORAL HEALTH CENTER,'
                b'"85 PATTON ROAD, DEVENS, MA 01434",V123,1999999999\r\r\n')
        got = si.extract_source_identity(body)
        self.assertEqual(got["hospital_name"], "TARAVISTA BEHAVIORAL HEALTH CENTER")
        self.assertEqual(got["hospital_address"], "85 PATTON ROAD, DEVENS, MA 01434")

    def test_a_header_with_no_value_row_yields_nothing_not_the_next_column_name(self):
        self.assertEqual(si.extract_source_identity(self.HEADER + b"\n"), {})
        self.assertEqual(si.extract_source_identity(self.HEADER + b"\n\n\n"), {})

    def test_a_blank_hospital_name_value_is_not_replaced_by_a_column_name(self):
        body = self.HEADER + b'\n,3/2/2026,3.0.0,Example Campus,"1 A St, X, MA 01434",V1,1\n'
        got = si.extract_source_identity(body)
        self.assertNotIn("hospital_name", got)
        self.assertEqual(got["location_name"], "Example Campus")


class NeverAGuess(unittest.TestCase):
    def test_a_web_page_yields_nothing(self):
        for page in (b"<!DOCTYPE html><html><head><title>Memorial Hospital - Price Transparency</title></head>"
                     b"<body><h1>hospital_name</h1><p>Memorial Hospital, 1 Main St, Springfield, IL 62701</p></body></html>",
                     BOM + b"<html><body>hospital_name,Memorial Hospital</body></html>",
                     b"<?xml version=\"1.0\"?><Error><Code>AccessDenied</Code></Error>"):
            with self.subTest(page=page[:30]):
                self.assertEqual(si.extract_source_identity(page), {})

    def test_a_custom_layout_whose_first_cell_is_a_hospital_name_yields_nothing(self):
        # 264004 Center for Behavioral Medicine, 172004 KPC Promise: no CMS keys anywhere.
        body = (b"Center for Behavioral Medicine,,,,,,\r\nComprehensive Machine-Readable File,,,,,,\r\n"
                b"Version 1,,,,,,\r\nProcedure Code / Modifier,Description,Gross Charges\r\n")
        self.assertEqual(si.extract_source_identity(body), {})

    def test_a_spreadsheet_with_no_cms_keys_yields_nothing(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
            zf.writestr("xl/sharedStrings.xml", "<sst><si><t>Howard Medical Center</t></si><si><t>Cash Price</t></si></sst>")
        self.assertEqual(si.extract_source_identity(buf.getvalue()), {})

    def test_empty_and_garbage_input(self):
        for raw in (b"", b"\xef\xbb\xbf", b"PK\x03\x04 not really a zip", b"\x1f\x8b not really gzip", b"\x00\x01\x02"):
            with self.subTest(raw=raw):
                self.assertEqual(si.extract_source_identity(raw), {})


class UnchangedReads(unittest.TestCase):
    def test_plain_csv_and_json_without_a_mark(self):
        self.assertEqual(si.extract_source_identity(CSV_V3), WANT_V3)
        self.assertEqual(si.extract_source_identity(JSON_V3), WANT_JSON)

    def test_meta_row_form(self):
        body = (b"hospital_name,Example General Hospital\nlast_updated_on,2025-01-01\n"
                b'hospital_address,"1 A St, Springfield, CA 90210"\nhospital_ein,12-3456789\nnpi,1000000001\n')
        self.assertEqual(si.extract_source_identity(body), {
            "hospital_name": "Example General Hospital", "hospital_address": "1 A St, Springfield, CA 90210",
            "ein": "12-3456789", "type_2_npi": "1000000001"})

    def test_json_list_values_are_pipe_joined_and_cut_at_200_characters(self):
        body = ('{"hospital_name":"' + "A" * 300 + '","location_name":["One","Two"]}').encode()
        got = si.extract_source_identity(body)
        self.assertEqual(len(got["hospital_name"]), 200)
        self.assertEqual(got["location_name"], "One | Two")

    def test_xlsx_shared_strings(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
            zf.writestr("xl/sharedStrings.xml", "<sst><si><t>hospital_name</t></si><si><t>Example General Hospital</t></si>"
                                                "<si><t>hospital_location</t></si><si><t>Example Campus</t></si></sst>")
        self.assertEqual(si.extract_source_identity(buf.getvalue()),
                         {"hospital_name": "Example General Hospital", "location_name": "Example Campus"})


if __name__ == "__main__":
    unittest.main()
