"""Unit tests for scripts/process_submissions.py — the fleet processor
for hospital-submitted MRFs.

These cover the parts that must never regress silently: the identity
gate's VERIFIED / MISMATCH / UNVERIFIABLE trichotomy (the mis-seed
defense), the validation findings for a deliberately broken file, the
normalized CMS-template builder (data out == data in, nothing
invented), and the independent source-entry counter the completeness
gate reconciles against.
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = load("process_submissions", os.path.join(REPO, "scripts", "process_submissions.py"))
mp = load("mrf_parse_repo", os.path.join(REPO, "scripts", "mrf_parse.py"))


class FakePublishHelpers:
    """publish_mini._name_consistent, copied faithfully — INCLUDING its
    lowercase-only candidate tokenization: candidates must be
    pre-lowercased by the caller (publish_mini's own callers pass
    _identity_candidates output, which is lowered). A double that
    lowercased for the caller hid exactly the bug the live E2E caught
    (uppercase registry names never matching)."""

    @staticmethod
    def _name_consistent(header, candidates):
        ht = set(re.findall(r"[a-z0-9]+", (header or "").lower()))
        if len(ht) < 2:
            return False
        for cand in candidates:
            ct = set(re.findall(r"[a-z0-9]+", cand or ""))
            if len(ct) >= 2 and (ct <= ht or ht <= ct):
                return True
        return False


def registry_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE hospitals(ccn TEXT PRIMARY KEY, name TEXT, address TEXT,"
        " city TEXT, state TEXT, zip TEXT)")
    db.execute(
        "CREATE TABLE mrf_seed(ccn TEXT, entity_name_legal TEXT,"
        " entity_name_common TEXT, mrf_url TEXT, mrf_page TEXT, state TEXT)")
    db.execute("INSERT INTO hospitals VALUES ('050075', 'EXAMPLE GENERAL HOSPITAL',"
               " '1 Main St', 'Springfield', 'CA', '90210')")
    db.execute("INSERT INTO hospitals VALUES ('100287', 'GOOD SAMARITAN MEDICAL CENTER',"
               " '1309 N Flagler Dr', 'West Palm Beach', 'FL', '33401')")
    db.execute("INSERT INTO mrf_seed VALUES ('050075', 'EXAMPLE GENERAL HOSPITAL INC',"
               " 'Example General', 'https://example.org/charges.json',"
               " 'https://example.org/pricing', 'CA')")
    return db


class IdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.db = registry_db()

    def tearDown(self):
        self.db.close()

    def gate(self, extracted, ccn="050075"):
        return ps.identity_gate(extracted, "claimed", self.db, ccn,
                                FakePublishHelpers)

    def test_verified_name_and_state(self):
        verdict, detail = self.gate({
            "hospital_name": "EXAMPLE GENERAL HOSPITAL",
            "state": "CA", "city": "Springfield", "license": "70000048"})
        self.assertEqual(verdict, "VERIFIED", detail)

    def test_extract_tenet_shaped_header_verifies(self):
        doc = {
            "hospital_name": "GOOD SAMARITAN MEDICAL CENTER, INC.",
            "hospital_address": ["1309 N Flagler Dr, West Palm Beach, FL 33401"],
            "license_information": {"license_number": "4070", "state": "FL"},
            "version": "3.0.0",
        }
        extracted = ps.extract_identity_json(doc)
        self.assertEqual(extracted.get("state"), "FL")
        self.assertEqual(extracted.get("city"), "West Palm Beach")
        self.assertEqual(extracted.get("license"), "4070")
        verdict, detail = self.gate(extracted, ccn="100287")
        self.assertEqual(verdict, "VERIFIED", detail)

    def test_mismatch_file_names_another_hospital(self):
        verdict, detail = self.gate({
            "hospital_name": "GOOD SAMARITAN MEDICAL CENTER",
            "state": "FL", "city": "West Palm Beach"})
        self.assertEqual(verdict, "MISMATCH", detail)
        self.assertIn("100287", detail)

    def test_mismatch_state_contradiction(self):
        verdict, _ = self.gate({
            "hospital_name": "EXAMPLE GENERAL HOSPITAL", "state": "TX"})
        self.assertEqual(verdict, "MISMATCH")

    def test_unverifiable_no_identity_in_file(self):
        verdict, _ = self.gate({})
        self.assertEqual(verdict, "UNVERIFIABLE")

    def test_unverifiable_name_without_corroboration(self):
        verdict, _ = self.gate({"hospital_name": "EXAMPLE GENERAL HOSPITAL"})
        self.assertEqual(verdict, "UNVERIFIABLE")

    def test_unverifiable_unknown_ccn(self):
        verdict, _ = self.gate(
            {"hospital_name": "EXAMPLE GENERAL HOSPITAL", "state": "CA"},
            ccn="999999")
        self.assertEqual(verdict, "UNVERIFIABLE")


class FindingsTests(unittest.TestCase):
    def test_broken_file_yields_specific_findings(self):
        header = {"version": "2.0.0", "hospital_name": "",
                  "last_updated_on": "2999-01-01"}
        items = [
            {"description": "XRAY", "code": "71045", "code_type": "CPT"},
            {"description": "LAB", "code": "80053", "code_type": "CPT",
             "gross_charge": 100.0, "cash_discount": 80.0,
             "payer_rates": [{"payer": "ACME", "plan": "PPO",
                              "rate_dollar": 90.0, "methodology": ""}]},
        ]
        rules = {f["rule"] for f in ps.fleet_findings(header, items, "json", 5, "note")}
        for expected in ("version-not-current", "hospital-name-missing",
                         "hospital-address-missing", "license-missing",
                         "last-updated-future", "items-without-prices",
                         "completeness-mismatch", "methodology-missing"):
            self.assertIn(expected, rules)
        findings = ps.fleet_findings(header, items, "json", 5, "note")
        mismatch = next(f for f in findings if f["rule"] == "completeness-mismatch")
        self.assertIn("5 entries", mismatch["message"])

    def test_clean_file_has_no_errors(self):
        header = {"version": "3.0.0", "hospital_name": "EXAMPLE GENERAL HOSPITAL",
                  "address": {"city": "Springfield", "state": "CA"},
                  "license": "70000048", "last_updated_on": "2026-09-01"}
        items = [{"description": "XRAY", "code": "71045", "code_type": "CPT",
                  "gross_charge": 100.0, "cash_discount": 80.0,
                  "min_negotiated": 70.0, "max_negotiated": 95.0,
                  "payer_rates": [{"payer": "ACME", "plan": "PPO",
                                   "rate_dollar": 90.0,
                                   "methodology": "fee schedule"}]}]
        findings = ps.fleet_findings(header, items, "json", 1, "")
        self.assertEqual([f for f in findings if f["severity"] == "error"], [])


class NormalizedTests(unittest.TestCase):
    def test_payer_rows_conserved_across_methodologies(self):
        header = {"hospital_name": "EXAMPLE GENERAL HOSPITAL",
                  "last_updated_on": "2026-09-01"}
        base = {"description": "XRAY", "code": "71045", "code_type": "CPT",
                "setting": "outpatient", "gross_charge": 100.0,
                "cash_discount": 80.0}
        items = [
            dict(base, payer_rates=[
                {"payer": "ACME", "plan": "PPO", "rate_dollar": 90.0,
                 "methodology": "fee schedule"},
                {"payer": "ACME", "plan": "PPO", "rate_dollar": None,
                 "methodology": "percentage of charges"},
            ]),
            dict(base, payer_rates=[
                # exact duplicate of the first row -> deduped
                {"payer": "ACME", "plan": "PPO", "rate_dollar": 90.0,
                 "methodology": "fee schedule"},
                # same payer+plan, different rate -> kept
                {"payer": "ACME", "plan": "PPO", "rate_dollar": 70.0,
                 "methodology": "fee schedule"},
            ]),
        ]
        doc = ps.build_normalized(header, items, "REGISTRY NAME", "2026-11-10")
        sci = doc["standard_charge_information"]
        self.assertEqual(len(sci), 1)
        payers = sci[0]["standard_charges"][0]["payers_information"]
        self.assertEqual(len(payers), 3, payers)

    def test_grouping_and_no_invention(self):
        header = {"hospital_name": "EXAMPLE GENERAL HOSPITAL",
                  "reporting_entity_name": "EXAMPLE GENERAL HOSPITAL INC",
                  "address": {"street_address": "1 Main St",
                              "city": "Springfield", "state": "CA",
                              "zip_code": "90210"},
                  "license": "70000048", "license_state": "CA",
                  "last_updated_on": "2026-09-01"}
        items = [
            {"description": "XRAY", "code": "71045", "code_type": "CPT",
             "setting": "outpatient", "gross_charge": 100.0,
             "cash_discount": 80.0,
             "payer_rates": [{"payer": "ACME", "plan": "PPO",
                              "rate_dollar": 90.0, "methodology": "fee schedule"}]},
            {"description": "XRAY", "code": "71045", "code_type": "CPT",
             "setting": "outpatient", "gross_charge": 100.0,
             "cash_discount": 80.0,
             "payer_rates": [{"payer": "BETA", "plan": "HMO",
                              "rate_dollar": 85.0, "methodology": "case rate"}]},
        ]
        doc = ps.build_normalized(header, items, "REGISTRY NAME", "2026-11-10")
        self.assertEqual(doc["version"], "3.0.0")
        self.assertEqual(doc["last_updated_on"], "2026-09-01")
        self.assertEqual(doc["license_number"]["license_number"], "70000048")
        self.assertEqual(len(doc["standard_charge_information"]), 1)
        charge = doc["standard_charge_information"][0]["standard_charges"][0]
        self.assertEqual(charge["gross_charge"], 100.0)
        self.assertEqual(len(charge["payers_information"]), 2)

    def test_missing_header_fields_are_omitted_not_invented(self):
        doc = ps.build_normalized({}, [], "REGISTRY NAME", "2026-11-10")
        self.assertNotIn("hospital_address", doc)
        self.assertNotIn("license_number", doc)
        self.assertEqual(doc["hospital_name"], "REGISTRY NAME")
        self.assertEqual(doc["last_updated_on"], "2026-11-10")


class SourceCountTests(unittest.TestCase):
    def test_json_sci_count(self):
        doc = {"standard_charge_information": [{}, {}, {}]}
        data = json.dumps(doc).encode()
        self.assertEqual(ps.count_source_entries("json", "x.json", data, mp), 3)

    def test_csv_row_count(self):
        text = ("description,code,code_type,setting\n"
                "A,1,CPT,outpatient\nB,2,CPT,outpatient\n\nC,3,CPT,inpatient\n")
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as f:
            f.write(text.encode())
            path = f.name
        try:
            self.assertEqual(ps.count_source_entries("csv", path, None, mp), 3)
        finally:
            os.unlink(path)


class VerdictTests(unittest.TestCase):
    def test_verdict_row_carries_provenance_and_quoted_name(self):
        with tempfile.TemporaryDirectory() as td:
            vdir = os.path.join(td, "verdicts")
            pdir = os.path.join(td, "pending")
            old_v, old_p = ps.VERDICTS_DIR, ps.VERDICTS_PENDING_DIR
            ps.VERDICTS_DIR, ps.VERDICTS_PENDING_DIR = vdir, pdir
            try:
                rec = {"id": "11111111-2222-3333-4444-555555555555",
                       "ccn_claimed": "050075"}
                fname = ps.write_verdict(rec, "state corroborated",
                                         "example.org",
                                         "EXAMPLE GENERAL HOSPITAL")
            finally:
                ps.VERDICTS_DIR, ps.VERDICTS_PENDING_DIR = old_v, old_p
            with open(os.path.join(vdir, fname)) as f:
                content = f.read()
            self.assertIn("hospital-submitted", content)
            self.assertIn("'EXAMPLE GENERAL HOSPITAL'", content)
            self.assertIn("\tACCEPT\t", content)
            self.assertTrue(os.path.exists(os.path.join(pdir, fname)))


class ListingGateTests(unittest.TestCase):
    """The listing path must refuse, with a reason, at every gate short
    of a full pass — and must never touch the store on a refusal."""

    def setUp(self):
        self.db = registry_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_store = ps.STORE_DIR
        ps.STORE_DIR = self.tmp.name

    def tearDown(self):
        ps.STORE_DIR = self.old_store
        self.db.close()
        self.tmp.cleanup()

    def base_rec(self, **kw):
        rec = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
               "ccn_claimed": "050075", "opt_in_listing": True,
               "hospital_name_claimed": "EXAMPLE GENERAL HOSPITAL",
               "verify": {"status": "verified", "domain": "example.org"},
               "_identity_verdict": "VERIFIED", "_identity_detail": "ok",
               "_format_detected": "json"}
        rec.update(kw)
        return rec

    def listing(self, rec, **kw):
        args = dict(header={"hospital_name": "EXAMPLE GENERAL HOSPITAL"},
                    items=[{"gross_charge": 1.0}], priced=1, source_count=1,
                    db=self.db, pm=FakePublishHelpers,
                    workdir=self.tmp.name, dry_run=True, mp=mp)
        args.update(kw)
        return ps.attempt_listing(rec, **args)

    def test_refused_when_unverified(self):
        rec = self.base_rec(verify={"status": "pending", "domain": ""})
        out = self.listing(rec)
        self.assertEqual(out["status"], "awaiting_verification")

    def test_refused_on_identity_mismatch(self):
        rec = self.base_rec(_identity_verdict="MISMATCH",
                            _identity_detail="names another hospital")
        out = self.listing(rec)
        self.assertEqual(out["status"], "refused")
        self.assertIn("MISMATCH", out["reason"])

    def test_refused_on_completeness_mismatch(self):
        out = self.listing(self.base_rec(), source_count=99)
        self.assertEqual(out["status"], "refused")
        self.assertIn("completeness", out["reason"])

    def test_refused_when_domain_not_on_file(self):
        rec = self.base_rec(verify={"status": "verified",
                                    "domain": "evil.example.net"})
        out = self.listing(rec)
        self.assertEqual(out["status"], "refused")
        self.assertIn("not the domain on file", out["reason"])

    def test_refused_when_already_listed(self):
        import gzip
        with gzip.open(os.path.join(self.tmp.name, "050075.json.gz"), "wt") as f:
            f.write("{}")
        out = self.listing(self.base_rec())
        self.assertEqual(out["status"], "refused")
        self.assertIn("already listed", out["reason"])

    def test_quarantined_ccn_refused(self):
        rec = self.base_rec(ccn_claimed="150042")
        out = self.listing(rec)
        self.assertEqual(out["status"], "refused")
        self.assertIn("quarantined", out["reason"])


if __name__ == "__main__":
    unittest.main()
