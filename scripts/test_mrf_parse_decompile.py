"""Regression tests for the decompile-wave-1 parser fixes (2026-10-25).

Fingerprints: ~/workspace/hl-gap-research/decompile-wave1.md

1. Select Medical chargemaster extracts (CHARGE_CODE,...,CPT_CODE sparse):
   the code column must resolve to CHARGE_CODE, not the sparse CPT_CODE;
   and a sparsity swap must rescue the same shape when the populated code
   column is not a known priority hint.
2. IU Health tall layout: a bare `Payer` header column is tall evidence
   (the substring probe misses ',Payer,'); MinPrice/MaxPrice resolve to
   the min/max roles.
3. Non-SpreadsheetML XML MRFs: UTF-16 BOM is decoded before detection;
   CMS-template-field XML (<hospital> root) parses through the JSON v2
   expansion; vendor <root><facility> schemas parse via the row finder;
   XML error documents yield no items; SpreadsheetML/HTML routing is
   unchanged.

Run: ~/hospital-ledger/.venv/bin/python3 test_mrf_parse_decompile.py
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mrf_parse", HERE / "mrf_parse.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)

SELECT_HEADER = ["CHARGE_CODE", "CHARGE_DESCRIPTION", "CHARGE",
                 "REVENUE_CODE", "CPT_CODE", "PHARMACY_NDC"]
SELECT_CSV = (
    "CHARGE_CODE,CHARGE_DESCRIPTION,CHARGE,REVENUE_CODE,CPT_CODE,PHARMACY_NDC\r\n"
    "1124127,RISPERDAL M-TAB 1MG,.01,250,,\r\n"
    "3510007,ROOM & BOARD,4296.95,120,,\r\n"
    "26999,KEESLER CREAM,175,250,A4369,\r\n"
).encode()

IU_HEADER = ["CDMCode", "CDMDesc", "ProcCode", "ProcCodeType", "RevCode",
             "UniformPrice", "MinPrice", "MaxPrice", "DiscountedCashPrice",
             "Payer", "PayerPlanCode"]
IU_CSV = (
    "CDMCode,CDMDesc,ProcCode,ProcCodeType,RevCode,UniformPrice,MinPrice,"
    "MaxPrice,DiscountedCashPrice,Payer,PayerPlanCode\n"
    "100001,ROOM BOARD,99213,CPT,100,1000.00,400.00,900.00,800.00,Aetna,PPO\n"
    "100001,ROOM BOARD,99213,CPT,100,1000.00,400.00,900.00,800.00,Cigna,PPO\n"
    "200002,LAB TEST,85025,CPT,300,50.00,20.00,45.00,40.00,Aetna,PPO\n"
).encode()

CMS_XML = """<?xml version="1.0" encoding="utf-16"?>
<hospital xmlns="http://www.shepherd.org">
<location_name>Shepherd Center</location_name>
<last_updated_on>2023-04-01</last_updated_on>
<version>2.0.0</version>
<standard_charge_information>
<item><description>ROOM AND BOARD</description>
<code_information><code>100</code><type>REV</type></code_information>
<standard_charges><setting>outpatient</setting><gross_charge>500.00</gross_charge><discounted_cash>300.00</discounted_cash><minimum>150.00</minimum><maximum>450.00</maximum>
<payers_information><payer_name>Aetna</payer_name><plan_name>PPO</plan_name><standard_charge_dollar>275.00</standard_charge_dollar><methodology>fee schedule</methodology></payers_information>
</standard_charges></item>
<item><description>OFFICE VISIT</description>
<code_information><code>99213</code><type>CPT</type></code_information>
<standard_charges><setting>outpatient</setting><gross_charge>200.00</gross_charge><discounted_cash>120.00</discounted_cash><minimum>90.00</minimum><maximum>180.00</maximum></standard_charges></item>
</standard_charge_information>
</hospital>"""

VENDOR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root><facility><name>Wickenburg Community Hospital</name>
<charges>
<charge><code>99213</code><description>OFFICE VISIT</description><gross_charge>200.00</gross_charge></charge>
<charge><code>100</code><description>ROOM AND BOARD</description><gross_charge>500.00</gross_charge></charge>
<charge><code>85025</code><description>CBC</description><gross_charge>45.50</gross_charge></charge>
</charges></facility></root>"""


class AliasIsolation(unittest.TestCase):
    """resolve_csv_columns persists learned mappings; keep tests hermetic."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = mp.HEADER_ALIASES_PATH
        mp.HEADER_ALIASES_PATH = os.path.join(self._tmp.name, "aliases.json")

    def tearDown(self):
        mp.HEADER_ALIASES_PATH = self._old
        self._tmp.cleanup()


class SelectMedicalLayout(AliasIsolation):
    def test_resolve_prefers_charge_code(self):
        rows = [["1124127", "RISPERDAL M-TAB 1MG", ".01", "250", "", ""],
                ["3510007", "ROOM & BOARD", "4296.95", "120", "", ""]]
        mapping, _notes = mp.resolve_csv_columns(SELECT_HEADER, rows)
        self.assertEqual(SELECT_HEADER[mapping["code"]], "CHARGE_CODE")
        self.assertEqual(SELECT_HEADER[mapping["desc"]], "CHARGE_DESCRIPTION")
        self.assertEqual(SELECT_HEADER[mapping["gross"]], "CHARGE")

    def test_parse_items_carry_charge_codes(self):
        fmt, items = mp.parse(SELECT_CSV, "https://resources.selectmedical.com/x_standardcharges.csv")
        self.assertEqual(fmt, "csv")
        self.assertEqual(len(items), 3)
        by_desc = {i["description"]: i for i in items}
        self.assertEqual(by_desc["ROOM & BOARD"]["code"], "3510007")
        self.assertEqual(by_desc["ROOM & BOARD"]["gross_charge"], 4296.95)
        self.assertEqual(by_desc["RISPERDAL M-TAB 1MG"]["code"], "1124127")

    def test_code_sparsity_swap(self):
        # Same failure shape, but the populated code column (ITEM_CODE) is
        # not a priority hint: the sparse CPT pick must be swapped out.
        cols = ["ITEM_CODE", "CPT_CODE", "DESCRIPTION", "CHARGE"]
        rows = [["A100", "99213", "OFFICE VISIT", "200"],
                ["A101", "", "LAB", "50"],
                ["A102", "", "XRAY", "75"]]
        mapping, notes = mp.resolve_csv_columns(cols, rows)
        self.assertEqual(cols[mapping["code"]], "ITEM_CODE")
        self.assertTrue(any("code-sparsity-swap" in n for n in notes))


class IUHealthLayout(AliasIsolation):
    def test_bare_payer_is_tall_evidence(self):
        self.assertTrue(mp.header_has_bare_payer(IU_CSV))
        wide = b"Description,Code,Gross,Aetna,BlueCross\nVISIT,99213,100,80,85\n"
        self.assertFalse(mp.header_has_bare_payer(wide))

    def test_min_max_price_hints(self):
        rows = [["100001", "ROOM BOARD", "99213", "CPT", "100", "1000.00",
                 "400.00", "900.00", "800.00", "Aetna", "PPO"]]
        mapping, _notes = mp.resolve_csv_columns(IU_HEADER, rows)
        self.assertEqual(IU_HEADER[mapping["min"]], "MinPrice")
        self.assertEqual(IU_HEADER[mapping["max"]], "MaxPrice")

    def test_tall_parse_groups_payers_and_populates_min_max(self):
        fmt, items = mp.parse(IU_CSV, "https://cdn.iuhealth.org/x_standardcharges22.csv")
        self.assertEqual(fmt, "csv")
        self.assertEqual(len(items), 2)  # two codes; payers grouped per code
        room = next(i for i in items if i["code"] == "100001")
        self.assertEqual(room["gross_charge"], 1000.00)
        self.assertEqual(room["cash_discount"], 800.00)
        self.assertEqual(room["min_negotiated"], 400.00)
        self.assertEqual(room["max_negotiated"], 900.00)


class XmlMrf(AliasIsolation):
    def test_detect_utf16_cms_xml(self):
        fmt, _ = mp.detect_format(CMS_XML.encode("utf-16"),
                                  "https://www.shepherd.org/docs/x.xml")
        self.assertEqual(fmt, "xml-mrf")

    def test_parse_utf16_cms_xml(self):
        fmt, items = mp.parse(CMS_XML.encode("utf-16"),
                              "https://www.shepherd.org/docs/x.xml")
        self.assertEqual(fmt, "xml-mrf")
        self.assertEqual(len(items), 2)
        room = next(i for i in items if i["code"] == "100")
        self.assertEqual(room["gross_charge"], 500.00)
        self.assertEqual(room["min_negotiated"], 150.00)
        self.assertEqual(room["max_negotiated"], 450.00)
        self.assertEqual(len(room["payer_rates"]), 1)
        self.assertEqual(room["payer_rates"][0]["payer"], "Aetna")
        self.assertEqual(room["payer_rates"][0]["rate_dollar"], 275.00)

    def test_vendor_root_facility_rows(self):
        fmt, items = mp.parse(VENDOR_XML.encode(),
                              "https://www.wickhosp.com/x_standardcharges.xml")
        self.assertEqual(fmt, "xml-mrf")
        self.assertEqual(len(items), 3)
        visit = next(i for i in items if i["code"] == "99213")
        self.assertEqual(visit["gross_charge"], 200.00)

    def test_xml_error_document_yields_no_items(self):
        err = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
               b'<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>')
        fmt, items = mp.parse(err, "https://cdn.example.org/x.xml")
        self.assertEqual(items, [])

    def test_spreadsheetml_routing_unchanged(self):
        ssml = (b'<?xml version="1.0"?><?mso-application progid="Excel.Sheet"?>'
                b'<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"></Workbook>')
        fmt, _ = mp.detect_format(ssml, "https://x.org/f.xml")
        self.assertEqual(fmt, "spreadsheetml")

    def test_html_routing_unchanged(self):
        fmt, _ = mp.detect_format(b"<!doctype html><html><body>404</body></html>",
                                  "https://x.org/f.xml")
        self.assertEqual(fmt, "html")


if __name__ == "__main__":
    unittest.main(verbosity=2)
