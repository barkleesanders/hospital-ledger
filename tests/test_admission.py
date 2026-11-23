"""Admission (INVENTORY M2, step 2a): the frozen known-wrong pairs must be refused.

Runs inside `unittest discover -s tests`, so scripts/verify.sh and `npm run
predeploy` gate it. Each class names the past mistake it guards:

* KnownWrongPairs      - batch-000 / W12 / W13: name-similarity admitted files to the
                         wrong CCN (`grade_pair`, 2026-11-02). Fails if admission
                         goes back to name overlap or drops the state/city anchor.
* TrueOwnerControls    - the other direction: a gate that refuses everything also
                         "passes" the wrong-pair test, so real owners must ACCEPT.
* OutcomeTable         - three outcomes, never two; ACCEPT only on address evidence.
* CaseNormalisation    - b3e2df9: uppercase registry names never matched because the
                         helper lowered only the header.
* VerdictType          - an ACCEPT with no evidence cannot be constructed.
* SubmissionsGateDelegates - process_submissions.identity_gate is not a second,
                         weaker admission: the W13 141305 shape must not VERIFY.
"""
import copy
import importlib.util
import json
import os
import sqlite3
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import hl_admission as ha  # noqa: E402

FIXTURE = os.path.join(REPO, "tests", "fixtures", "identity", "known_wrong_pairs.json")
with open(FIXTURE) as fh:
    FX = json.load(fh)


def _src(pair):
    s = dict(pair["source"])
    if pair.get("license"):
        s["license"] = pair["license"]
    return s


class KnownWrongPairs(unittest.TestCase):
    def test_fixture_is_the_frozen_set(self):
        # 14 W13 pairs (b84). Shrinking this list to make a check pass is the failure.
        self.assertEqual(len(FX["wrong_pairs"]), 14)
        self.assertEqual(
            sorted(p["ccn"] for p in FX["wrong_pairs"]),
            ["010089", "010101", "050663", "141305", "150082", "150169", "161382",
             "184004", "193300", "210008", "250151", "281349", "380090", "500108"])

    def test_every_wrong_pair_is_refused_for_its_recorded_reason(self):
        failures = []
        for p in FX["wrong_pairs"]:
            v = ha.admit(p["ccn"], p["registry"], _src(p))
            if v.outcome != ha.REFUSE or v.reason != p["expected_reason"]:
                failures.append(
                    f"CCN {p['ccn']} ({p['registry']['name']} vs file "
                    f"'{p['source']['hospital_name']}'): got {v.outcome}/{v.reason}, "
                    f"want REFUSE/{p['expected_reason']}")
        self.assertEqual(failures, [], "known-wrong pairs admitted or mis-refused:\n"
                         + "\n".join(failures))

    def test_held_pair_refuses_on_hold_and_on_name_without_it(self):
        h = FX["held_pair"]
        v = ha.admit(h["ccn"], h["registry"], h["source"], holds=h["holds"])
        self.assertEqual((v.outcome, v.reason), (ha.REFUSE, h["expected_reason_with_hold"]))
        v = ha.admit(h["ccn"], h["registry"], h["source"])
        self.assertEqual((v.outcome, v.reason), (ha.REFUSE, h["expected_reason_without_hold"]))

    def test_name_is_necessary_but_never_sufficient(self):
        # 141305: the file's name is a token superset of the registry name and the
        # state matches. Only the city/zip anchor refuses it.
        p = next(p for p in FX["wrong_pairs"] if p["ccn"] == "141305")
        self.assertTrue(ha.name_consistent(p["source"]["hospital_name"], [p["registry"]["name"]]))
        self.assertEqual(p["source"]["state"], p["registry"]["state"])
        v = ha.admit(p["ccn"], p["registry"], _src(p))
        self.assertEqual(v.outcome, ha.REFUSE)

    def test_state_and_city_equal_still_needs_the_name(self):
        for ccn in ("050663", "184004"):
            p = next(p for p in FX["wrong_pairs"] if p["ccn"] == ccn)
            self.assertEqual(p["source"]["state"], p["registry"]["state"])
            self.assertEqual(p["source"]["city"].upper(), p["registry"]["city"])
            self.assertEqual(ha.admit(ccn, p["registry"], _src(p)).reason, ha.R_NAME_MISMATCH)

    def test_license_number_alone_never_admits(self):
        p = next(p for p in FX["wrong_pairs"] if p["ccn"] == "150082")
        self.assertTrue(_src(p).get("license"))
        self.assertNotEqual(ha.admit(p["ccn"], p["registry"], _src(p)).outcome, ha.ACCEPT)
        # a name-consistent file with a license but NO address is UNPROVEN
        v = ha.admit("1", {"name": "EXAMPLE GENERAL HOSPITAL", "city": "X", "state": "CA", "zip": "90210"},
                     {"hospital_name": "Example General Hospital", "license": "70000048"})
        self.assertEqual((v.outcome, v.reason), (ha.UNPROVEN, ha.R_NO_ADDRESS))


class TrueOwnerControls(unittest.TestCase):
    def test_every_true_owner_is_accepted_with_evidence(self):
        for c in FX["true_owner_controls"]:
            with self.subTest(ccn=c["ccn"]):
                v = ha.admit(c["ccn"], c["registry"], _src(c))
                self.assertEqual(v.outcome, ha.ACCEPT, v.detail)
                self.assertTrue(v.evidence)

    def test_registry_in_fixture_matches_the_tracked_db(self):
        # The fixture's registry values are the tracked db rows; if the db moves,
        # the controls must be re-derived (not silently kept stale).
        db_path = os.path.join(REPO, "db", "hospital_ledger.db")
        if not os.path.exists(db_path):
            self.skipTest("db/hospital_ledger.db not present in this checkout")
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for c in FX["true_owner_controls"] + FX["wrong_pairs"]:
                row = db.execute("SELECT name, city, state, zip FROM hospitals WHERE ccn = ?",
                                 (c["ccn"],)).fetchone()
                self.assertIsNotNone(row, f"CCN {c['ccn']} missing from hospitals")
                r = c["registry"]
                self.assertEqual((r["name"], r["city"], r["state"], r["zip"]), tuple(row), c["ccn"])
        finally:
            db.close()

    def test_saint_and_st_are_the_same_place(self):
        reg = {"name": "ST JOSEPH MEDICAL CENTER", "city": "SAINT LOUIS", "state": "MO", "zip": "63101"}
        v = ha.admit("1", reg, {"hospital_name": "Saint Joseph Medical Center",
                                "city": "St. Louis", "state": "MO", "zip": "63110"})
        self.assertEqual(v.outcome, ha.ACCEPT, v.detail)

    def test_same_zip_rescues_a_postal_city_difference(self):
        reg = {"name": "EXAMPLE GENERAL HOSPITAL", "city": "Springfield", "state": "CA", "zip": "90210"}
        v = ha.admit("1", reg, {"hospital_name": "Example General Hospital",
                                "city": "Beverly Hills", "state": "CA", "zip": "90210-1234"})
        self.assertEqual(v.outcome, ha.ACCEPT, v.detail)


class OutcomeTable(unittest.TestCase):
    REG = {"name": "EXAMPLE GENERAL HOSPITAL", "city": "Springfield", "state": "CA", "zip": "90210",
           "aliases": ["Example General Hospital Inc"]}

    def admit(self, **src):
        return ha.admit("050075", self.REG, src)

    def test_accept_requires_name_state_and_city_or_zip(self):
        self.assertEqual(self.admit(hospital_name="Example General Hospital", state="CA",
                                    city="Springfield").outcome, ha.ACCEPT)
        self.assertEqual(self.admit(hospital_name="Example General Hospital", state="CA",
                                    zip="90210").outcome, ha.ACCEPT)

    def test_state_only_is_unproven(self):
        v = self.admit(hospital_name="Example General Hospital", state="CA")
        self.assertEqual((v.outcome, v.reason), (ha.UNPROVEN, ha.R_NO_ADDRESS))

    def test_no_file_name_is_unproven(self):
        self.assertEqual(self.admit(state="CA", city="Springfield").reason, ha.R_NO_FILE_NAME)
        self.assertEqual(self.admit(state="CA", city="Springfield").outcome, ha.UNPROVEN)

    def test_no_registry_row_is_unproven(self):
        v = ha.admit("999999", None, {"hospital_name": "Example General Hospital", "state": "CA"})
        self.assertEqual((v.outcome, v.reason), (ha.UNPROVEN, ha.R_NO_REGISTRY))

    def test_contradictions_refuse_even_when_name_matches(self):
        self.assertEqual(self.admit(hospital_name="Example General Hospital", state="TX",
                                    city="Springfield").reason, ha.R_STATE_CONTRADICTION)
        self.assertEqual(self.admit(hospital_name="Example General Hospital", state="CA",
                                    city="Elsewhere", zip="11111").reason, ha.R_CITY_CONTRADICTION)

    def test_alias_names_count(self):
        v = self.admit(hospital_name="Example General Hospital Inc", state="CA", city="Springfield")
        self.assertEqual(v.outcome, ha.ACCEPT)

    def test_hold_and_quarantine_beat_everything(self):
        good = {"hospital_name": "Example General Hospital", "state": "CA", "city": "Springfield"}
        self.assertEqual(ha.admit("050075", self.REG, good, holds={"050075": "x"}).reason, ha.R_HOLD)
        self.assertEqual(ha.admit("050075", self.REG, good, quarantined={"050075": "x"}).reason,
                         ha.R_QUARANTINED)

    def test_multi_campus_one_anchor_accepts_all_wrong_refuses(self):
        names = {"hospital_name": "Example General Hospital"}
        ok = dict(names, addresses=[{"city": "Elsewhere", "state": "CA", "zip": "11111"},
                                    {"city": "Springfield", "state": "CA", "zip": "90210"}])
        bad = dict(names, addresses=[{"city": "A", "state": "TX", "zip": "1"},
                                     {"city": "B", "state": "NV", "zip": "2"}])
        mixed = dict(names, addresses=[{"city": "A", "state": "TX", "zip": "1"}, {"state": "CA"}])
        self.assertEqual(ha.admit("1", self.REG, ok).outcome, ha.ACCEPT)
        self.assertEqual(ha.admit("1", self.REG, bad).outcome, ha.REFUSE)
        self.assertEqual(ha.admit("1", self.REG, mixed).outcome, ha.UNPROVEN)

    def test_w12_reassignments_without_a_registry_address_are_never_accepted(self):
        u = FX["unproven_controls"]
        for p in u["pairs"] + u["name_differs_known_false_refusal"]:
            with self.subTest(ccn=p["ccn"]):
                v = ha.admit(p["ccn"], p["registry"], p["source"])
                self.assertEqual(v.outcome, p["expected_outcome"], v.detail)
                self.assertNotEqual(v.outcome, ha.ACCEPT)


class CaseNormalisation(unittest.TestCase):
    """b3e2df9: the helper tokenised the header lower-case but the candidate as-given,
    so an UPPERCASE registry name never matched its own file."""

    def test_uppercase_registry_name_matches_mixed_case_file(self):
        self.assertTrue(ha.name_consistent("Example General Hospital", ["EXAMPLE GENERAL HOSPITAL"]))
        self.assertTrue(ha.name_consistent("EXAMPLE GENERAL HOSPITAL", ["Example General Hospital"]))

    def test_uppercase_registry_admits_end_to_end(self):
        reg = {"name": "GOOD SAMARITAN MEDICAL CENTER", "city": "WEST PALM BEACH", "state": "FL",
               "zip": "33401"}
        v = ha.admit("100287", reg, {"hospital_name": "Good Samaritan Medical Center, Inc.",
                                     "city": "West Palm Beach", "state": "fl", "zip": "33401"})
        self.assertEqual(v.outcome, ha.ACCEPT, v.detail)

    def test_single_token_names_never_match(self):
        self.assertFalse(ha.name_consistent("Memorial", ["MEMORIAL"]))


class VerdictType(unittest.TestCase):
    def test_accept_without_evidence_cannot_be_constructed(self):
        with self.assertRaises(ha.AdmissionError):
            ha.Verdict(ha.ACCEPT, ha.R_ANCHORED)
        with self.assertRaises(ha.AdmissionError):
            ha.Verdict(ha.ACCEPT, ha.R_ANCHORED, evidence=())

    def test_unknown_outcome_and_empty_reason_cannot_be_constructed(self):
        with self.assertRaises(ha.AdmissionError):
            ha.Verdict("MAYBE", "x")
        with self.assertRaises(ha.AdmissionError):
            ha.Verdict(ha.REFUSE, "")

    def test_refuse_and_unproven_need_no_evidence(self):
        self.assertEqual(ha.Verdict(ha.REFUSE, "x").outcome, ha.REFUSE)
        self.assertEqual(ha.Verdict(ha.UNPROVEN, "x").outcome, ha.UNPROVEN)

    def test_accept_row_refuses_to_render_without_evidence(self):
        with self.assertRaises(ha.AdmissionError):
            ha.accept_row("050075", "https://example.org", "detail", evidence=("", None))
        row = ha.accept_row("050075", "https://example.org", "detail", evidence=("why",))
        self.assertEqual(row, "050075\thttps://example.org\tACCEPT\tdetail\n")


class SubmissionsGateDelegates(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "process_submissions_adm", os.path.join(REPO, "scripts", "process_submissions.py"))
        self.ps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ps)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE hospitals(ccn TEXT PRIMARY KEY, name TEXT, address TEXT,"
                        " city TEXT, state TEXT, zip TEXT)")
        self.db.execute("CREATE TABLE mrf_seed(ccn TEXT, entity_name_legal TEXT,"
                        " entity_name_common TEXT, mrf_url TEXT, mrf_page TEXT, state TEXT)")
        for p in FX["wrong_pairs"] + FX["true_owner_controls"]:
            r = p["registry"]
            self.db.execute("INSERT INTO hospitals VALUES (?,?,?,?,?,?)",
                            (p["ccn"], r["name"], "", r["city"], r["state"], r["zip"]))

    def tearDown(self):
        self.db.close()

    def gate(self, p):
        # pm=None: identity no longer consults the Mini's helper copy.
        return self.ps.identity_gate(_src(p), "claimed", self.db, p["ccn"], None)

    def test_no_wrong_pair_is_verified_by_the_submissions_gate(self):
        wrongly = [p["ccn"] for p in FX["wrong_pairs"] if self.gate(p)[0] == "VERIFIED"]
        self.assertEqual(wrongly, [], f"submissions gate VERIFIED known-wrong pairs: {wrongly}")

    def test_141305_alton_memorial_is_a_mismatch_not_verified(self):
        p = next(p for p in FX["wrong_pairs"] if p["ccn"] == "141305")
        verdict, detail = self.gate(p)
        self.assertEqual(verdict, "MISMATCH", detail)
        self.assertIn("Alton", detail)

    def test_true_owners_verify(self):
        for c in FX["true_owner_controls"]:
            with self.subTest(ccn=c["ccn"]):
                self.assertEqual(self.gate(c)[0], "VERIFIED")

    def test_file_naming_another_registry_row_names_that_row(self):
        # the mis-seed signature keeps its explanatory detail: 010089's file is
        # Missouri Baptist, which IS another registry row (260108).
        p = next(p for p in FX["wrong_pairs"] if p["ccn"] == "010089")
        verdict, detail = self.gate(p)
        self.assertEqual(verdict, "MISMATCH")
        self.assertIn("260108", detail)

    def test_license_alone_is_unverifiable(self):
        p = copy.deepcopy(next(c for c in FX["true_owner_controls"] if c["ccn"] == "360131"))
        p["source"] = {"hospital_name": "Alliance Community Hospital"}
        p["license"] = "12345"
        self.assertEqual(self.gate(p)[0], "UNVERIFIABLE")


if __name__ == "__main__":
    unittest.main()
