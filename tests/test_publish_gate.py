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

    def test_no_head_is_a_warning_by_default_and_a_refusal_when_strict(self):
        reason, warns = pg.identity_gate("1", None, {}, holds=set())
        self.assertIsNone(reason)
        self.assertTrue(warns)
        reason, _ = pg.identity_gate("1", None, {}, holds=set(), unproven="refuse")
        self.assertIsNotNone(reason)


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
    REG = {"name": "EXAMPLE GENERAL HOSPITAL", "city": "Springfield", "state": "CA", "zip": "90210"}

    def go(self, si, **kw):
        return pg.identity_gate("050075", {"source_url": "", "source_identity": si}, self.REG,
                                holds=set(), **kw)

    def test_name_without_address_warns_by_default(self):
        reason, warns = self.go({"hospital_name": "Example General Hospital"})
        self.assertIsNone(reason)
        self.assertTrue(any("unproven" in w for w in warns))

    def test_name_without_address_refuses_when_strict(self):
        reason, _ = self.go({"hospital_name": "Example General Hospital"}, unproven="refuse")
        self.assertIn("unproven", reason)

    def test_legacy_canonical_with_no_identity(self):
        self.assertIsNone(self.go({})[0])
        self.assertIsNotNone(self.go({}, unproven="refuse")[0])

    def test_no_registry_row_is_unproven_not_accepted(self):
        reason, warns = pg.identity_gate("9", {"source_url": "", "source_identity": {
            "hospital_name": "Example General Hospital", "hospital_address": "1 A St, X, CA 90210"}},
            None, holds=set())
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
