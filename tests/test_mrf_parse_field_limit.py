"""Oversized CSV fields vs the stdlib csv 128 KiB field limit.

The stdlib csv module refuses any field longer than 131,072 chars. Real
MRF generators exceed that two ways: legitimately long notes fields, and
-- the case that forced this, CCN 231305 (Ascension Standish Hospital,
2026-11-02) -- a single unbalanced double-quote in the source file,
which flips csv quote parity and swallows thousands of ordinary rows
into one 1,014,794-char "field" of plain chargemaster data in
code_3_msdrg. Under the default limit the whole file died in
delimited_rows (_csv.Error: field larger than field limit) and the
hospital stayed live on another hospital's file. mrf_parse therefore
raises the limit at import to a bounded 64 MiB (CSV_FIELD_SIZE_LIMIT).
These tests pin both the limit and the parse outcomes.
"""
import csv
import importlib.util
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("mrf_parse", SCRIPTS / "mrf_parse.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)

META = (
    "hospital_name,last_updated_on,version,hospital_location,license_number\r\n"
    "Ascension Standish Hospital,2024-01-23,1.1.1,"
    "St. Mary's of Michigan Standish Hospital,060020|MI\r\n"
)
TALL_HEADER = (
    "description,code,code_1_hcpcs,code_2_rc,code_3_msdrg,code_4_aprdrg,"
    "code_5_cdm,billing_class,setting,drug_unit_of_measurement,"
    "drug_type_of_measurement,modifiers,standard_charge_gross,"
    "standard_charge_discounted_cash,payer_name,plan_name,"
    "standard_charge_negotiated_dollar,standard_charge_negotiated_percent,"
    "standard_charge_min,standard_charge_max,"
    "standard_charge_contracting_method,additional_generic_notes\r\n"
)
WIDE_HEADER = (
    "description,code,code_1_hcpcs,billing_class,setting,"
    "standard_charge_gross,standard_charge_discounted_cash,"
    "additional_generic_notes\r\n"
)


def tall_row(desc, code, gross, cash, payer, plan, rate, notes=""):
    return (
        f"{desc},{code},{code},0,,,,Facility,Both,,,,{gross},{cash},"
        f"{payer},{plan},{rate},,,,Fee Schedule,{notes}\r\n"
    )


class FieldLimitTest(unittest.TestCase):
    def test_limit_raised_at_import(self):
        self.assertEqual(mp.CSV_FIELD_SIZE_LIMIT, 64 * 1024 * 1024)
        self.assertEqual(csv.field_size_limit(), mp.CSV_FIELD_SIZE_LIMIT)
        self.assertGreater(csv.field_size_limit(), 131072)

    def test_tall_csv_with_oversized_notes_field(self):
        big_notes = "N" * 200_000
        text = META + TALL_HEADER
        text += tall_row("INSERT CATH", "51701", "107.00", "51.36",
                         "Medicaid HMO", "1334 Plan", "55.00")
        text += tall_row("DRUG ASSAY CLOZAPINE", "80159", "11.00", "5.28",
                         "Health Plus", "668 Plan", "13.54", notes=big_notes)
        fmt, items = mp.parse(text.encode(), "https://example.invalid/x.csv",
                              hospital_name="Ascension Standish Hospital",
                              ccn="231305")
        self.assertEqual(fmt, "csv")
        by_code = {it["code"]: it for it in items}
        self.assertIn("51701", by_code)
        self.assertIn("80159", by_code)
        rates = by_code["80159"]["payer_rates"]
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0]["rate_dollar"], 13.54)

    def test_wide_csv_with_oversized_description_field(self):
        big_desc = "LONG DESCRIPTION " + ("x" * 200_000)
        text = META + WIDE_HEADER
        text += "SHORT ARM SPLINT,29125,29125,Facility,Both,126.00,60.48,\r\n"
        text += f'"{big_desc}",80159,80159,Facility,Both,11.00,5.28,\r\n'
        fmt, items = mp.parse(text.encode(), "https://example.invalid/x.csv")
        self.assertEqual(fmt, "csv")
        by_code = {it["code"]: it for it in items}
        self.assertIn("29125", by_code)
        self.assertIn("80159", by_code)
        # The oversized field survived the reader (item descriptions are
        # truncated to 300 chars by the parser, by design).
        self.assertEqual(len(by_code["80159"]["description"]), 300)
        self.assertEqual(by_code["80159"]["gross_charge"], 11.0)

    def test_unbalanced_quote_span_still_parses(self):
        # The 231305 shape: an opening quote with no close for >128 KiB
        # swallows many physical lines into one field; rows before and
        # after the span must still parse.
        filler_line = ("ASSAY OF DESIPRAMINE,80160,80160,0,,,,Facility,Both,,,,"
                       "14.10,6.77,Health Plus,668 Plan,13.54,,,,\r\n")
        span = filler_line * 4000  # ~360 KB swallowed into one field
        text = META + WIDE_HEADER
        text += "SHORT ARM SPLINT,29125,29125,Facility,Both,126.00,60.48,\r\n"
        text += f'"BROKEN ROW START {span}END",99999,99999,Facility,Both,1.00,0.50,\r\n'
        text += "INSERT CATH,51701,51701,Facility,Both,107.00,51.36,\r\n"
        fmt, items = mp.parse(text.encode(), "https://example.invalid/x.csv")
        self.assertEqual(fmt, "csv")
        codes = {it["code"] for it in items}
        self.assertIn("29125", codes)
        self.assertIn("51701", codes)


if __name__ == "__main__":
    unittest.main()
