"""Publisher gate policy (INVENTORY M2, step 2c, owner decision 2026-11-23).

Runs inside `unittest discover -s tests`, so scripts/verify.sh and `npm run predeploy` gate it.

The decision under test: an unproven hospital/owner pair is REFUSED by default; a legitimate
rebrand publishes ONLY through an explicit named override (hospital id, accepted name(s),
reason, plus the exact file and attested location) in the gate configuration. Each class names
the mistake it guards:

* StrictByDefault        - unproven data must not publish; "warn" is an explicit opt-in only.
* OverrideIsNarrow       - an override covers ONE file for ONE CCN and repairs only what an
                           override can repair. It must not admit a file from the wrong place, a
                           different file under the same CCN, a different CCN, or a name the owner
                           did not list.
* LiveShape              - 372004 as the Mini really holds it: no registry row, canonical with no
                           source_identity. A name alias alone could never publish this.
* PolicyFailsClosed      - a broken policy refuses everything; it never degrades to "no
                           overrides" or to the lenient setting (the unmeasured-instrument rule).
* ShippedPolicy          - the committed policy file is the one the publisher will read: strict,
                           valid, and every override in it provably does what its reason says.
* KnownPairsUnderPolicy  - the 14 known-wrong pairs stay refused and the 7 true owners keep
                           publishing under the real shipped policy.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "tests"))

import hl_admission as ha  # noqa: E402
import hl_publish_gate as pg  # noqa: E402
from test_publish_gate import FX, head_of, reg_of  # noqa: E402

URL = "https://example.invalid/812471465_curahealth-oklahoma-llc_standardcharges.csv"
OTHER_URL = "https://example.invalid/some-other-file_standardcharges.csv"
OVERRIDE = {"hospital_id": "372004", "accepted_names": ["Curahealth Oklahoma LLC"],
            "reason": "same facility rebranded (test)", "source_url": URL,
            "anchor": {"city": "Oklahoma City", "state": "OK", "zip": "73103"}}
# 372004 with a registry row that has only a name and a state (the fixture's shape)
REG_PARTIAL = {"name": "Curahealth Oklahoma City", "state": "OK"}
FILE_FULL = {"hospital_name": "Curahealth Oklahoma LLC",
             "hospital_address": "1407 N Robinson Ave, Oklahoma City, OK 73103"}


def policy_of(*overrides, unproven="refuse"):
    return pg.parse_policy({"unproven": unproven, "overrides": [copy.deepcopy(o) for o in overrides]})


def cura(si=None, url=URL, ccn="372004", reg=REG_PARTIAL, policy=None):
    head = {"source_url": url, "source_identity": si if si is not None else FILE_FULL}
    return pg.gate_from_policy(ccn, head, reg, holds=set(),
                               policy=policy if policy is not None else policy_of(OVERRIDE))


class StrictByDefault(unittest.TestCase):
    REG = {"name": "EXAMPLE GENERAL HOSPITAL", "city": "Springfield", "state": "CA", "zip": "90210"}
    HEAD = {"source_url": "", "source_identity": {"hospital_name": "Example General Hospital"}}

    def test_a_policy_with_no_unproven_key_is_strict(self):
        self.assertEqual(pg.parse_policy({}).unproven, pg.UNPROVEN_REFUSE)

    def test_gate_from_policy_refuses_unproven_data(self):
        reason, _ = pg.gate_from_policy("050075", self.HEAD, self.REG, holds=set(), policy=policy_of())
        self.assertIn("unproven", reason)

    def test_warn_is_reachable_only_by_saying_so(self):
        reason, warns = pg.gate_from_policy("050075", self.HEAD, self.REG, holds=set(),
                                            policy=policy_of(unproven="warn"))
        self.assertIsNone(reason)
        self.assertTrue(warns)


class OverrideIsNarrow(unittest.TestCase):
    def test_the_rebrand_is_refused_without_the_override(self):
        reason, _ = cura(policy=policy_of())
        self.assertIn("does not match the registry name", reason)

    def test_the_rebrand_publishes_with_the_override_and_says_so(self):
        reason, warns = cura()
        self.assertIsNone(reason)
        self.assertTrue(any(w.startswith("override applied: 372004") for w in warns), warns)
        self.assertTrue(any(URL in w for w in warns), "the warning must name the covered file")

    def test_a_full_header_attests_nothing(self):
        _, warns = cura()
        self.assertIn("attested by the override: accepted names", " ".join(warns))
        self.assertNotIn("address", " ".join(warns).split("attested by the override:")[1].split("(")[0])

    def test_a_different_file_under_the_same_ccn_gets_no_help(self):
        reason, _ = cura(url=OTHER_URL)
        self.assertIn("does not match the registry name", reason)

    def test_a_wrong_state_file_is_refused_even_with_the_override(self):
        reason, _ = cura(si=dict(FILE_FULL, hospital_address="1 Main St, Dallas, TX 75201"))
        self.assertIn("state", reason)

    def test_a_wrong_city_file_is_refused_even_with_the_override(self):
        reason, _ = cura(si=dict(FILE_FULL, hospital_address="1 Main St, Tulsa, OK 74103"))
        self.assertIsNotNone(reason)
        self.assertIn("city", reason)

    def test_the_override_does_not_extend_to_another_ccn(self):
        reason, _ = cura(ccn="372005")
        self.assertIn("does not match the registry name", reason)

    def test_a_name_the_owner_did_not_list_is_still_refused(self):
        reason, _ = cura(si=dict(FILE_FULL, hospital_name="Some Other Hospital Group"))
        self.assertIn("does not match the registry name", reason)

    def test_the_anchor_never_replaces_a_non_blank_registry_value(self):
        reg = dict(REG_PARTIAL, city="Tulsa", zip="74103")  # registry says Tulsa; anchor says OKC
        reason, _ = cura(reg=reg)
        self.assertIsNotNone(reason)  # the file (Oklahoma City 73103) contradicts the registry

    def test_a_held_ccn_stays_held_whatever_the_override(self):
        head = {"source_url": URL, "source_identity": FILE_FULL}
        reason, _ = pg.gate_from_policy("372004", head, REG_PARTIAL, holds={"372004"},
                                        policy=policy_of(OVERRIDE))
        self.assertIn("hold", reason)

    def test_the_override_is_not_consulted_for_a_contradiction(self):
        # not a second identity decision: with admit() forced to a state contradiction (not an
        # overridable reason), admit is called once and the override never runs.
        refuse = ha.Verdict(ha.REFUSE, ha.R_STATE_CONTRADICTION, detail="forced")
        with mock.patch.object(ha, "admit", return_value=refuse) as m:
            reason, _ = cura()
        self.assertEqual(m.call_count, 1)
        self.assertIn("forced", reason)

    def test_the_override_is_not_consulted_when_plain_admit_already_accepts(self):
        ok = ha.Verdict(ha.ACCEPT, ha.R_ANCHORED, evidence=("e",))
        with mock.patch.object(ha, "admit", return_value=ok) as m:
            reason, warns = cura()
        self.assertEqual(m.call_count, 1)
        self.assertEqual((reason, warns), (None, []))


class LiveShape(unittest.TestCase):
    """372004 exactly as the publisher holds it (measured 2026-11-23): `_registry_identity`
    returns None and the canonical head has a source_url but no source_identity."""

    def test_refused_without_the_override_as_unproven(self):
        reason, _ = cura(si={}, reg=None, policy=policy_of())
        self.assertIn("unproven", reason)

    def test_publishes_with_the_override_and_names_what_was_attested(self):
        reason, warns = cura(si={}, reg=None)
        self.assertIsNone(reason)
        joined = " ".join(warns)
        self.assertIn("attested by the override: name, address, accepted names", joined)

    def test_a_different_file_under_the_ccn_is_still_unproven(self):
        reason, _ = cura(si={}, reg=None, url=OTHER_URL)
        self.assertIn("unproven", reason)

    def test_a_contradicting_header_beats_the_attestation(self):
        # the override fills only what the file does not say; a header that names Texas is judged
        # as written even with no registry row at all.
        reason, _ = cura(si={"hospital_name": "Curahealth Oklahoma LLC",
                             "hospital_address": "1 Main St, Dallas, TX 75201"}, reg=None)
        self.assertIn("state", reason)

    def test_a_name_that_matches_nothing_is_not_rescued_by_the_attestation(self):
        reason, _ = cura(si={"hospital_name": "Totally Different Hospital",
                             "hospital_address": "1 Main St, Oklahoma City, OK 73103"}, reg=None)
        self.assertIn("does not match the registry name", reason)


class PolicyFailsClosed(unittest.TestCase):
    def bad(self, doc, needle):
        with self.assertRaises(pg.PolicyError) as cm:
            pg.parse_policy(doc)
        self.assertIn(needle, str(cm.exception))

    def test_not_an_object(self):
        self.bad([], "JSON object")

    def test_unknown_top_level_key_is_a_typo_not_a_default(self):
        self.bad({"overides": []}, "unknown policy key")

    def test_unknown_unproven_value(self):
        self.bad({"unproven": "maybe"}, "unproven")

    def test_overrides_must_be_a_list(self):
        self.bad({"overrides": {}}, "must be a list")

    def test_override_needs_a_real_ccn(self):
        for bad in ("", "37200", "3720044", "37-004", None, 372004):
            with self.subTest(hospital_id=bad):
                self.bad({"overrides": [dict(OVERRIDE, hospital_id=bad)]}, "hospital_id")

    def test_override_needs_accepted_names(self):
        for bad in ([], None, "Curahealth Oklahoma LLC", [1]):
            with self.subTest(names=bad):
                self.bad({"overrides": [dict(OVERRIDE, accepted_names=bad)]}, "accepted_names")

    def test_a_dead_override_is_rejected_at_load(self):
        # one distinguishing word can never satisfy name_consistent (>= 2 tokens)
        self.bad({"overrides": [dict(OVERRIDE, accepted_names=["Curahealth"])]}, "could never match")

    def test_override_needs_a_reason(self):
        for bad in ("", "   ", None):
            with self.subTest(reason=bad):
                self.bad({"overrides": [dict(OVERRIDE, reason=bad)]}, "reason")
        missing = dict(OVERRIDE)
        missing.pop("reason")
        self.bad({"overrides": [missing]}, "reason")

    def test_override_must_be_pinned_to_a_file(self):
        for bad in ("", None, "not a url", "ftp://x/y", 5):
            with self.subTest(source_url=bad):
                self.bad({"overrides": [dict(OVERRIDE, source_url=bad)]}, "source_url")
        missing = dict(OVERRIDE)
        missing.pop("source_url")
        self.bad({"overrides": [missing]}, "source_url")

    def test_override_must_attest_a_location(self):
        for bad in ({}, {"state": "OK"}, {"city": "Oklahoma City"}, {"state": "", "city": "X"}, None):
            with self.subTest(anchor=bad):
                self.bad({"overrides": [dict(OVERRIDE, anchor=bad)]}, "anchor")
        missing = dict(OVERRIDE)
        missing.pop("anchor")
        self.bad({"overrides": [missing]}, "anchor")

    def test_anchor_may_only_hold_location_strings(self):
        self.bad({"overrides": [dict(OVERRIDE, anchor={"state": "OK", "name": "x"})]}, "anchor")
        self.bad({"overrides": [dict(OVERRIDE, anchor={"state": "OK", "zip": 73103})]}, "anchor")

    def test_duplicate_override(self):
        self.bad({"overrides": [OVERRIDE, OVERRIDE]}, "duplicate")

    def test_unknown_override_key(self):
        self.bad({"overrides": [dict(OVERRIDE, skip_address_check=True)]}, "unknown key")

    def test_unreadable_policy_file_refuses_everything(self):
        c = FX["true_owner_controls"][0]
        reason, _ = pg.gate_from_policy(c["ccn"], head_of(c), reg_of(c), holds=set(),
                                        policy_path="/nonexistent/policy.json")
        self.assertIn("failing closed", reason)

    def test_corrupt_policy_file_refuses_everything(self):
        c = FX["true_owner_controls"][0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.json")
            with open(path, "w") as fh:
                fh.write("{ not json")
            reason, _ = pg.gate_from_policy(c["ccn"], head_of(c), reg_of(c), holds=set(),
                                            policy_path=path)
        self.assertIn("failing closed", reason)

    def test_invalid_policy_file_refuses_even_a_true_owner(self):
        c = FX["true_owner_controls"][0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.json")
            with open(path, "w") as fh:
                json.dump({"unproven": "warn", "overides": []}, fh)
            reason, warns = pg.gate_from_policy(c["ccn"], head_of(c), reg_of(c), holds=set(),
                                                policy_path=path)
        self.assertIn("failing closed", reason)
        self.assertEqual(warns, [])


class ShippedPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = pg.load_policy()  # the committed file, via the default path

    def test_the_default_path_is_the_committed_file(self):
        self.assertTrue(os.path.samefile(pg.DEFAULT_POLICY_PATH,
                                         os.path.join(REPO, "scripts", "hl_publish_gate_policy.json")))

    def test_it_is_strict(self):
        self.assertEqual(self.policy.unproven, pg.UNPROVEN_REFUSE)

    def test_curahealth_is_the_seeded_override(self):
        ov = self.policy.overrides["372004"]
        self.assertIn("Curahealth Oklahoma LLC", ov.accepted_names)
        self.assertTrue(ov.reason)
        self.assertTrue(ov.source_url.endswith("812471465_curahealth-oklahoma-llc_standardcharges.csv"))

    def test_the_only_overrides_are_the_ones_the_owner_named(self):
        # adding a rebrand is an owner decision: it must be added here on purpose.
        self.assertEqual(sorted(self.policy.overrides), ["372004"])

    def test_every_shipped_override_publishes_its_file_in_the_live_shape_and_not_without(self):
        # each override must publish its own pinned file as the Mini holds it (no registry row, no
        # source_identity) and be refused without it, so a dead or mistyped entry fails here
        # instead of silently doing nothing.
        for ccn, ov in self.policy.overrides.items():
            with self.subTest(ccn=ccn):
                head = {"source_url": ov.source_url, "source_identity": {}}
                with_ov, warns = pg.gate_from_policy(ccn, head, None, holds=set(), policy=self.policy)
                without, _ = pg.gate_from_policy(ccn, head, None, holds=set(),
                                                 policy=pg.Policy(unproven=self.policy.unproven))
                self.assertIsNone(with_ov)
                self.assertTrue(any("override applied" in w for w in warns))
                self.assertIsNotNone(without)

    def test_every_shipped_override_is_exercised_by_the_fixture_header(self):
        rebrands = {p["ccn"]: p for p in FX["unproven_controls"]["name_differs_known_false_refusal"]}
        for ccn, ov in self.policy.overrides.items():
            with self.subTest(ccn=ccn):
                self.assertIn(ccn, rebrands, "an override with no fixture case is untested")
                p = rebrands[ccn]
                s = p["source"]
                head = {"source_url": ov.source_url, "source_identity": {
                    "hospital_name": s["hospital_name"], "hospital_address_city": s["city"],
                    "hospital_address_state": s["state"], "hospital_address_zip": s["zip"]}}
                with_ov, _ = pg.gate_from_policy(ccn, head, p["registry"], holds=set(), policy=self.policy)
                without, _ = pg.gate_from_policy(ccn, head, p["registry"], holds=set(),
                                                 policy=pg.Policy(unproven=self.policy.unproven))
                self.assertIsNone(with_ov)
                self.assertIsNotNone(without)


class KnownPairsUnderPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = pg.load_policy()

    def test_all_fourteen_wrong_pairs_are_refused(self):
        leaked = []
        for p in FX["wrong_pairs"]:
            reason, _ = pg.gate_from_policy(p["ccn"], head_of(p), reg_of(p), holds=set(),
                                            policy=self.policy)
            if reason is None:
                leaked.append(p["ccn"])
        self.assertEqual(leaked, [])
        self.assertEqual(len(FX["wrong_pairs"]), 14)

    def test_all_seven_true_owners_still_publish_clean(self):
        for c in FX["true_owner_controls"]:
            with self.subTest(ccn=c["ccn"]):
                self.assertEqual(pg.gate_from_policy(c["ccn"], head_of(c), reg_of(c), holds=set(),
                                                     policy=self.policy), (None, []))
        self.assertEqual(len(FX["true_owner_controls"]), 7)

    def test_no_wrong_pair_is_rescued_by_an_override(self):
        wrong = {p["ccn"] for p in FX["wrong_pairs"]}
        self.assertFalse(wrong & set(self.policy.overrides))

    def test_the_nine_unproven_reassignments_do_not_publish(self):
        # W12 reassignments whose registry rows have no city/zip: unproven, so refused by default.
        for c in FX["unproven_controls"]["pairs"]:
            with self.subTest(ccn=c["ccn"]):
                s = c["source"]
                head = {"source_url": "", "source_identity": {
                    "hospital_name": s["hospital_name"], "hospital_address_city": s["city"],
                    "hospital_address_state": s["state"], "hospital_address_zip": s["zip"]}}
                reason, _ = pg.gate_from_policy(c["ccn"], head, c["registry"], holds=set(),
                                                policy=self.policy)
                self.assertIn("unproven", reason)


if __name__ == "__main__":
    unittest.main()
