"""Concatenated multi-document CMS HPT JSON streams.

Some generators (SIH's v3.0.0 export, found 2026-11-02 on CCN 140210,
Harrisburg Medical Center) emit the MRF as many complete JSON documents
back to back -- one standard_charge_information entry per document, each
repeating the hospital metadata -- instead of one document holding the
whole array. Single-document loaders reject the trailing documents
('Extra data'), so the file parsed to zero items while being a perfectly
valid standard-charges file. The parser merges such streams; every other
malformed shape must keep failing exactly as before.
"""
import importlib.util
import json
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("mrf_parse", SCRIPTS / "mrf_parse.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


def hpt_doc(code, desc, gross, rate, hospital="Example General Hospital"):
    """One complete CMS v3.0-shaped document with a single charge entry."""
    return {
        "hospital_name": hospital,
        "last_updated_on": "2026-03-04",
        "version": "3.0.0",
        "location_name": [hospital],
        "hospital_address": ["1 Main St, Example, IL 62946"],
        "standard_charge_information": [
            {
                "description": desc,
                "code_information": [{"code": code, "type": "CPT"}],
                "standard_charges": [
                    {
                        "setting": "outpatient",
                        "gross_charge": gross,
                        "discounted_cash": gross / 2,
                        "minimum": rate,
                        "maximum": rate,
                        "payers_information": [
                            {
                                "payer_name": "Example Payer",
                                "plan_name": "Example Plan",
                                "standard_charge_dollar": rate,
                                "methodology": "fee schedule",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def encode_docs(docs, pretty=True):
    parts = [json.dumps(d, indent=1) if pretty else json.dumps(d) for d in docs]
    return ("\n".join(parts) + "\n").encode()


class MultiDocJson(unittest.TestCase):
    def test_load_single_doc_is_unchanged(self):
        raw = encode_docs([hpt_doc("99213", "OFFICE VISIT", 200.0, 90.0)])
        items = mp.parse_json_v2(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "99213")
        self.assertEqual(items[0]["gross_charge"], 200.0)

    def test_concatenated_docs_merge_all_entries(self):
        raw = encode_docs([
            hpt_doc("99213", "OFFICE VISIT", 200.0, 90.0),
            hpt_doc("99214", "OFFICE VISIT EST", 260.0, 120.0),
            hpt_doc("70450", "CT HEAD WO CONTRAST", 1800.0, 640.0),
        ])
        merged = mp.load_json_bytes_multidoc(raw)
        self.assertIsNotNone(merged)
        self.assertEqual(merged["hospital_name"], "Example General Hospital")
        self.assertEqual(len(merged["standard_charge_information"]), 3)
        items = mp.parse_json_v2(raw)
        self.assertEqual(len(items), 3)
        by_code = {item["code"]: item for item in items}
        self.assertEqual(by_code["70450"]["gross_charge"], 1800.0)
        self.assertEqual(by_code["99214"]["payer_rates"][0]["rate_dollar"], 120.0)

    def test_concatenated_docs_compact_encoding(self):
        raw = encode_docs(
            [hpt_doc("99213", "A", 100.0, 40.0), hpt_doc("99214", "B", 110.0, 44.0)],
            pretty=False,
        )
        self.assertEqual(len(mp.parse_json_v2(raw)), 2)

    def test_valid_doc_plus_garbage_still_fails(self):
        raw = encode_docs([hpt_doc("99213", "OFFICE VISIT", 200.0, 90.0)]) + b'{"truncated": '
        self.assertIsNone(mp.load_json_bytes_multidoc(raw))
        self.assertEqual(mp.parse_json_v2(raw), [])

    def test_single_doc_decodes_to_none_from_multidoc(self):
        raw = encode_docs([hpt_doc("99213", "OFFICE VISIT", 200.0, 90.0)])
        self.assertIsNone(mp.load_json_bytes_multidoc(raw))

    def test_truncated_second_doc_still_fails(self):
        raw = encode_docs([
            hpt_doc("99213", "OFFICE VISIT", 200.0, 90.0),
            hpt_doc("99214", "OFFICE VISIT EST", 260.0, 120.0),
        ])
        self.assertEqual(mp.parse_json_v2(raw[:-40]), [])

    def test_ndjson_flat_rows_not_reclassified(self):
        rows = [{"code": "99213", "gross_charge": 200.0},
                {"code": "99214", "gross_charge": 260.0}]
        raw = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
        self.assertIsNone(mp.load_json_bytes_multidoc(raw))

    def test_metadata_only_docs_do_not_merge(self):
        meta = {"hospital_name": "Example General Hospital", "version": "3.0.0"}
        raw = encode_docs([meta, dict(meta)])
        self.assertIsNone(mp.load_json_bytes_multidoc(raw))

    def test_standard_charges_key_variant_merges(self):
        doc = hpt_doc("99213", "OFFICE VISIT", 200.0, 90.0)
        doc["standard_charges"] = doc.pop("standard_charge_information")
        raw = encode_docs([doc, json.loads(json.dumps(doc))])
        self.assertEqual(len(mp.parse_json_v2(raw)), 2)


if __name__ == "__main__":
    unittest.main()
