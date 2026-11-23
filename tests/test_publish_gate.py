"""Publisher-facing identity gate (INVENTORY M2, step 2b).

Runs inside `unittest discover -s tests`, so scripts/verify.sh and `npm run predeploy` gate it.
Every canonical head here is built from the frozen fixture's VERBATIM `header_quote`
(street included, campuses `|`-joined, states sometimes spelled out), i.e. the shape the
source file's own header really has, not a convenient `City, ST ZIP` string. Each class names
the past mistake it guards:

* KnownWrongPairsAtThePublisher - batch-000 / W12 / W13: the production publisher
                         (publish_mini.identity_gate) passes pairs whose state and city equal the
                         registry's and passes street-less / spelled-out-state addresses it
                         cannot parse. The 14 frozen pairs must all be refused here.
* TrueOwnersStillPublish - a gate that refuses everything also passes the wrong-pair test.
* AddressShapes          - the production regex needs a comma before the city; these shapes
                         must still yield a location.
* HoldsAndFailClosed     - an unreadable holds file refuses everything; a hold beats evidence.
* RejectedUrlAndEin      - R0 / R2 ported from production keep their semantics.
* UnprovenPolicy         - parity (warn) vs strict (refuse): a policy switch, tested both ways.
* OneDecision            - the identity verdict is hl_admission.admit, not a second opinion.
"""
import json
import os
import re
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import hl_admission as ha  # noqa: E402
import hl_publish_gate as pg  # noqa: E402

with open(os.path.join(REPO, "tests", "fixtures", "identity", "known_wrong_pairs.json")) as fh:
    FX = json.load(fh)


def _quoted(quote, key):
    m = re.search(key + r"""\s+(?:'([^']*)'|"([^"]*)")""", quote)
    return (m.group(1) or m.group(2)) if m else None


def head_of(pair, url="https://example.invalid/file.json"):
    """A production-shaped canonical head from the pair's verbatim header quote."""
    q = pair["header_quote"]
    si = {"hospital_name": _quoted(q, "hospital_name"),
          "hospital_address": _quoted(q, "hospital_address")}
    if _quoted(q, "location_name"):
        si["location_name"] = _quoted(q, "location_name")
    return {"source_url": url, "source_identity": si}


def reg_of(pair):
    return dict(pair["registry"])


def gate(pair, **kw):
    kw.setdefault("holds", set())
    return pg.identity_gate(pair["ccn"], head_of(pair), reg_of(pair), **kw)


class KnownWrongPairsAtThePublisher(unittest.TestCase):
    def test_every_wrong_pair_is_refused(self):
        passed = []
        for p in FX["wrong_pairs"]:
            reason, _ = gate(p)
            if reason is None:
                passed.append(f"{p['ccn']} ({p['registry']['name']} <- "
                              f"{_quoted(p['header_quote'], 'hospital_name')})")
        self.assertEqual(passed, [], "known-wrong pairs would PUBLISH:\n" + "\n".join(passed))

    def test_refusal_names_the_identity_mismatch(self):
        for p in FX["wrong_pairs"]:
            with self.subTest(ccn=p["ccn"]):
                reason, _ = gate(p)
                self.assertTrue(reason and reason.startswith("identity mismatch"), reason)

    def test_state_and_city_equal_pairs_are_refused_on_the_name(self):
        # 050663 and 184004: state AND city equal the registry's. Only the name separates them.
        for ccn in ("050663", "184004"):
            p = next(p for p in FX["wrong_pairs"] if p["ccn"] == ccn)
            reason, _ = gate(p)
            self.assertIn("does not match the registry name", reason or "")

    def test_strict_policy_refuses_them_too(self):
        for p in FX["wrong_pairs"]:
            with self.subTest(ccn=p["ccn"]):
                self.assertIsNotNone(gate(p, unproven="refuse")[0])

    def test_held_pair_is_refused_on_the_hold(self):
        h = FX["held_pair"]
        reason, _ = pg.identity_gate(h["ccn"], {"source_url": "u", "source_identity": h["source"]},
                                     h["registry"], holds=set(h["holds"]))
        self.assertIn("hold", reason)


class TrueOwnersStillPublish(unittest.TestCase):
    def test_every_true_owner_publishes_clean(self):
        for c in FX["true_owner_controls"]:
            with self.subTest(ccn=c["ccn"]):
                self.assertEqual(gate(c), (None, []))

    def test_every_true_owner_publishes_under_the_strict_policy(self):
        # strict must not trade away real owners: each carries a full header.
        for c in FX["true_owner_controls"]:
            with self.subTest(ccn=c["ccn"]):
                self.assertEqual(gate(c, unproven="refuse"), (None, []))


class AddressShapes(unittest.TestCase):
    def test_quote_parses_to_the_fixtures_own_city_and_state(self):
        # independent cross-check: the verbatim quote and the hand-structured `source` agree.
        for p in FX["wrong_pairs"] + FX["true_owner_controls"]:
            with self.subTest(ccn=p["ccn"]):
                got = pg.parse_addresses(_quoted(p["header_quote"], "hospital_address"))
                self.assertTrue(got, p["header_quote"])
                s = p["source"]
                want = s.get("addresses") or [s]
                self.assertEqual(
                    [(a["city"].lower().replace("st.", "saint"), a["state"]) for a in got],
                    [(a["city"].lower().replace("st.", "saint"), a["state"]) for a in want])

    def test_street_less_address_still_yields_a_location(self):
        # production's regex needs a comma BEFORE the city and finds nothing here.
        self.assertEqual(pg.parse_addresses("Alton, IL 62002"),
                         [{"city": "Alton", "state": "IL", "zip": "62002"}])

    def test_spelled_out_state(self):
        self.assertEqual(pg.parse_addresses("1325 N. Highland, Aurora, Illinois 60506"),
                         [{"city": "Aurora", "state": "IL", "zip": "60506"}])
        self.assertEqual(pg.parse_addresses("1 Main St, Winston-Salem, North Carolina 27101"),
                         [{"city": "Winston-Salem", "state": "NC", "zip": "27101"}])

    def test_multi_campus_pipe_joined(self):
        got = pg.parse_addresses("150 New Providence Rd, Mountainside, NJ 07092 | "
                                 "200 Somerset St., New Brunswick, NJ 08901")
        self.assertEqual([a["city"] for a in got], ["Mountainside", "New Brunswick"])

    def test_zip_plus_four_and_no_zip(self):
        self.assertEqual(pg.parse_addresses("1 A St, Aurora, IL 60506-1234")[0]["zip"], "60506")
        self.assertEqual(pg.parse_addresses("1 A St, Aurora, IL")[0]["zip"], "")

    def test_unparseable_is_dropped_never_guessed(self):
        self.assertEqual(pg.parse_addresses("somewhere near the river"), [])
        self.assertEqual(pg.parse_addresses(None), [])

    def test_explicit_city_state_fields_are_honoured(self):
        si = {"hospital_name": "Example General Hospital", "hospital_address_city": "Springfield",
              "hospital_address_state": "California", "hospital_address_zip": "90210"}
        reg = {"name": "EXAMPLE GENERAL HOSPITAL", "city": "Springfield", "state": "CA", "zip": "90210"}
        self.assertEqual(pg.identity_gate("1", {"source_url": "", "source_identity": si}, reg,
                                          holds=set()), (None, []))

    def test_street_less_wrong_city_is_refused(self):
        # 141305 with the street stripped: still refused (production sees no location, passes).
        p = next(p for p in FX["wrong_pairs"] if p["ccn"] == "141305")
        head = head_of(p)
        head["source_identity"]["hospital_address"] = "Alton, IL 62202"
        reason, _ = pg.identity_gate(p["ccn"], head, reg_of(p), holds=set())
        self.assertIn("city", reason or "")


class LocationNameIsOneOfTheFilesOwnNames(unittest.TestCase):
    """Remediation Wave 4, lane C1. A header's `hospital_name` is often the legal entity and its
    `location_name` the facility the registry knows (010108: "The Health Care Authority for
    Baptist Health ..." / "Prattville Baptist Hospital"). Reading only `hospital_name` refused 105
    true owners as name mismatches. A location is still only a NAME: the address test is
    unchanged and is run on that location's own address, never on a sibling campus's."""
    REG = {"name": "PRATTVILLE BAPTIST HOSPITAL", "city": "PRATTVILLE", "state": "AL", "zip": "36067"}
    ENTITY = "THE HEALTH CARE AUTHORITY FOR BAPTIST HEALTH AN AFFILIATE OF UABHS"
    QUAIL = {"name": "QUAIL CREEK SURGICAL HOSPITAL", "city": "AMARILLO", "state": "TX", "zip": "79124"}

    def go(self, si, reg=None):
        return pg.identity_gate("010108", {"source_url": "", "source_identity": si},
                                reg or self.REG, holds=set())

    def si(self, **kw):
        base = {"hospital_name": self.ENTITY, "location_name": "Prattville Baptist Hospital",
                "hospital_address": "124 South Memorial Drive, Prattville, AL 36067"}
        base.update(kw)
        return {k: v for k, v in base.items() if v is not None}

    def test_legal_entity_header_with_the_facility_as_its_location_publishes(self):
        self.assertEqual(self.go(self.si()), (None, []))

    def test_the_facility_may_be_any_one_of_several_locations(self):
        si = self.si(location_name="Prattville Baptist Hospital | Prattville Baptist Hospital Geriatric Behavioral Health",
                     hospital_address="124 South Memorial Drive, Prattville, AL 36067 | 124 South Memorial Drive, Prattville, AL 36067")
        self.assertEqual(self.go(si), (None, []))

    def test_right_location_name_wrong_city_is_refused(self):
        reason, _ = self.go(self.si(hospital_address="100 Main Street, Nowhereville, AL 00000"))
        self.assertIn("identity mismatch", reason or "")
        self.assertIn("city", reason)

    def test_right_location_name_wrong_state_is_refused(self):
        reason, _ = self.go(self.si(hospital_address="100 Main Street, Prattville, TX 36067"))
        self.assertIn("identity mismatch", reason or "")
        self.assertIn("state", reason)

    def test_right_location_name_with_no_address_is_unproven_not_accepted(self):
        reason, _ = self.go(self.si(hospital_address=None))
        self.assertIn("identity unproven", reason or "")

    def test_a_location_that_does_not_name_the_registry_hospital_is_still_a_mismatch(self):
        reason, _ = self.go(self.si(location_name="Montgomery Baptist Medical Center South"))
        self.assertIn("does not match the registry name", reason or "")

    def test_a_location_is_anchored_by_its_own_address_not_a_siblings(self):
        # Quail Creek's own (second) address is Lubbock; the sibling's address is the registry's
        # Amarillo. A gate that let any campus vouch would publish this.
        si = {"hospital_name": "Physicians Surgical Hospitals LLC",
              "location_name": "Panhandle Surgical Hospital | Quail Creek Surgical Hospital",
              "hospital_address": "7100 SW 9th Ave, Amarillo, TX 79124 | 1 Plum Creek Drive, Lubbock, TX 79401"}
        reason, _ = self.go(si, self.QUAIL)
        self.assertIn("identity mismatch", reason or "")
        self.assertIn("Lubbock", reason)

    def test_the_paired_address_publishes_when_it_is_the_registrys(self):
        si = {"hospital_name": "Physicians Surgical Hospitals LLC",
              "location_name": "Panhandle Surgical Hospital | Quail Creek Surgical Hospital",
              "hospital_address": "7100 SW 9th Ave, Amarillo, TX 79106 | 6819 Plum Creek Drive, Amarillo, TX 79124"}
        self.assertEqual(self.go(si, self.QUAIL), (None, []))

    def test_a_paired_location_whose_own_address_does_not_parse_is_unproven(self):
        si = {"hospital_name": "Physicians Surgical Hospitals LLC",
              "location_name": "Panhandle Surgical Hospital | Quail Creek Surgical Hospital",
              "hospital_address": "7100 SW 9th Ave, Amarillo, TX 79124 | somewhere near the river"}
        reason, _ = self.go(si, self.QUAIL)
        self.assertIn("identity unproven", reason or "")

    def test_locations_and_addresses_in_different_numbers_cannot_be_paired(self):
        si = {"hospital_name": "Physicians Surgical Hospitals LLC",
              "location_name": "Panhandle Surgical Hospital | Quail Creek Surgical Hospital",
              "hospital_address": "1 A St, Amarillo, TX 79124 | 2 B St, Amarillo, TX 79124 | 3 C St, Amarillo, TX 79124"}
        reason, _ = self.go(si, self.QUAIL)
        self.assertIn("identity unproven", reason or "")
        self.assertIn("cannot be paired", reason)

    def test_one_location_with_several_addresses_may_use_any_of_them(self):
        si = self.si(hospital_address="1 Other St, Montgomery, AL 36116 | 124 South Memorial Drive, Prattville, AL 36067")
        self.assertEqual(self.go(si), (None, []))

    def test_a_matching_hospital_name_is_judged_exactly_as_before(self):
        si = self.si(hospital_name="Prattville Baptist Hospital", location_name="Some Other Campus | And Another",
                     hospital_address="1 Other St, Montgomery, AL 36116 | 124 South Memorial Drive, Prattville, AL 36067")
        self.assertEqual(self.go(si), (None, []))

    def test_the_wrong_pairs_stay_refused_when_the_header_also_lists_the_registry_name(self):
        # The strongest name forgery: the wrong file's header gains the registry's own name as a
        # location. Every pair whose address contradicts the registry must still be refused.
        # (050663 and 184004 share the registry's city; for those a header that itself names the
        # registry hospital at the registry's city IS the standard's proof, so they are excluded
        # here and stay covered, with their real headers, by KnownWrongPairsAtThePublisher.)
        passed = []
        for p in FX["wrong_pairs"]:
            if p["ccn"] in ("050663", "184004"):
                continue
            head = head_of(p)
            head["source_identity"]["location_name"] = p["registry"]["name"]
            reason, _ = pg.identity_gate(p["ccn"], head, reg_of(p), holds=set())
            if reason is None:
                passed.append(p["ccn"])
        self.assertEqual(passed, [])

    def test_admit_ignores_malformed_location_entries(self):
        reg = dict(self.REG)
        src = {"hospital_name": self.ENTITY, "addresses": [{"city": "Prattville", "state": "AL", "zip": "36067"}],
               "locations": ["Prattville Baptist Hospital", {"name": ""}, None]}
        self.assertEqual(ha.admit("010108", reg, src).outcome, ha.REFUSE)


class HoldsAndFailClosed(unittest.TestCase):
    def setUp(self):
        self.c = FX["true_owner_controls"][0]

    def test_unreadable_holds_refuses_everything(self):
        reason, _ = pg.identity_gate(self.c["ccn"], head_of(self.c), reg_of(self.c), holds=None)
        self.assertIn("failing closed", reason)

    def test_a_hold_beats_clean_evidence(self):
        reason, _ = pg.identity_gate(self.c["ccn"], head_of(self.c), reg_of(self.c),
                                     holds={self.c["ccn"]})
        self.assertIn("hold", reason)

    def test_other_ccns_on_the_hold_list_do_not_block(self):
        self.assertEqual(gate(self.c, holds={"999999"}), (None, []))

    def test_no_head_is_a_refusal_by_default_and_a_warning_only_when_asked(self):
        # step 2c: unproven data does not publish. The lenient value is explicit, never default.
        reason, _ = pg.identity_gate("1", None, {}, holds=set())
        self.assertIsNotNone(reason)
        reason, warns = pg.identity_gate("1", None, {}, holds=set(), unproven="warn")
        self.assertIsNone(reason)
        self.assertTrue(warns)


class RejectedUrlAndEin(unittest.TestCase):
    def setUp(self):
        self.p = FX["true_owner_controls"][1]  # 260108, would otherwise publish
        self.url = "https://Host.example/files/260108%5Ffile.json"

    def go(self, rows, url=None):
        return pg.identity_gate(self.p["ccn"], head_of(self.p, url=url or self.url),
                                reg_of(self.p), holds=set(), verdict_rows=rows)

    def test_exact_rejected_file_is_refused(self):
        reason, _ = self.go([(80, "https://host.example/files/260108_file.json", "REJECT", "x")])
        self.assertIn("rejected", reason)

    def test_a_later_accept_of_the_same_file_rescues_it(self):
        rows = [(80, self.url, "REJECT", "x"), (84, self.url, "ACCEPT", "y")]
        self.assertEqual(self.go(rows), (None, []))

    def test_a_different_rejected_file_does_not_block(self):
        self.assertEqual(self.go([(80, "https://host.example/other.json", "REJECT", "x")]), (None, []))

    def test_ein_contradiction_is_refused(self):
        rows = [(84, "https://h.example/111111111_a_standardcharges.json", "ACCEPT", "y")]
        reason, _ = self.go(rows, url="https://h.example/222222222_b_standardcharges.json")
        self.assertIn("EIN", reason)

    def test_same_ein_passes_and_dashed_ein_is_normalised(self):
        rows = [(84, "https://h.example/12-3456789_a_standardcharges.json", "ACCEPT", "y")]
        self.assertEqual(self.go(rows, url="https://h.example/123456789_b_standardcharges.json"),
                         (None, []))

    def test_highest_batch_wins_and_later_row_wins_a_tie(self):
        self.assertEqual(pg.winning_verdict([(84, "a", "ACCEPT", ""), (83, "b", "REJECT", "")])[1], "a")
        self.assertEqual(pg.winning_verdict([(84, "a", "REJECT", ""), (84, "b", "ACCEPT", "")])[1], "b")
        self.assertIsNone(pg.winning_verdict([]))


class UnprovenPolicy(unittest.TestCase):
    """Step 2c: refuse is the DEFAULT; warn is an explicit opt-in, tested both ways."""
    REG = {"name": "EXAMPLE GENERAL HOSPITAL", "city": "Springfield", "state": "CA", "zip": "90210"}

    def go(self, si, **kw):
        return pg.identity_gate("050075", {"source_url": "", "source_identity": si}, self.REG,
                                holds=set(), **kw)

    def test_the_default_is_the_strict_policy(self):
        self.assertEqual(pg.DEFAULT_UNPROVEN, pg.UNPROVEN_REFUSE)

    def test_name_without_address_refuses_by_default(self):
        reason, _ = self.go({"hospital_name": "Example General Hospital"})
        self.assertIn("unproven", reason)

    def test_name_without_address_warns_only_when_asked(self):
        reason, warns = self.go({"hospital_name": "Example General Hospital"}, unproven="warn")
        self.assertIsNone(reason)
        self.assertTrue(any("unproven" in w for w in warns))

    def test_legacy_canonical_with_no_identity(self):
        self.assertIsNotNone(self.go({})[0])
        self.assertIsNone(self.go({}, unproven="warn")[0])

    def test_no_registry_row_is_unproven_not_accepted(self):
        head = {"source_url": "", "source_identity": {
            "hospital_name": "Example General Hospital", "hospital_address": "1 A St, X, CA 90210"}}
        reason, _ = pg.identity_gate("9", head, None, holds=set())
        self.assertIn("unproven", reason)
        reason, warns = pg.identity_gate("9", head, None, holds=set(), unproven="warn")
        self.assertIsNone(reason)
        self.assertTrue(warns)

    def test_a_bad_policy_value_is_an_error_not_a_silent_default(self):
        with self.assertRaises(ValueError):
            self.go({}, unproven="maybe")


class OneDecision(unittest.TestCase):
    def test_the_identity_verdict_is_hl_admission_admit(self):
        c = FX["true_owner_controls"][0]
        refuse = ha.Verdict(ha.REFUSE, "name-mismatch", detail="forced by the test")
        with mock.patch.object(ha, "admit", return_value=refuse) as m:
            reason, _ = gate(c)
        self.assertTrue(m.called)
        self.assertIn("forced by the test", reason)

    def test_accept_from_admit_publishes(self):
        w = FX["wrong_pairs"][0]
        ok = ha.Verdict(ha.ACCEPT, "address-anchored", evidence=("e",))
        with mock.patch.object(ha, "admit", return_value=ok):
            self.assertEqual(gate(w), (None, []))


if __name__ == "__main__":
    unittest.main()
