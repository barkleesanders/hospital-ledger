"""Skip-and-report for the Mini publisher (INVENTORY M2, step 2d).

Runs inside `unittest discover -s tests`. The real ~/hl-fleet is only READ.

Two layers:
* the pure module (`scripts/hl_publish_run.py`): every item is decided, a refusal / a raising
  guard / a malformed guard result refuses THAT item and never stops the loop, and the refusal
  report is built only from refused items;
* the real patched `main()`: the cut-over tool installs stage 2c then stage 2d into a scratch
  fleet and the publisher's own `main` runs in-process with `_publish` recorded, so what is
  measured is the actual control flow (which CCNs reach the upload path, the exit code, the
  report file), not a re-implementation of it.

The properties each have a mutation proof in scripts/prove_skip_report.sh; the test names below
are the ones those proofs expect to fail.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "tests"))

import cutover_publisher_gate as cg  # noqa: E402
import hl_publish_run as run  # noqa: E402
from probe_publisher_gate import bucket as probe_bucket  # noqa: E402
from test_cutover_publisher_gate import SYNTHETIC  # noqa: E402
from test_publish_gate import FX, head_of, reg_of  # noqa: E402

SCRIPTS = os.path.join(REPO, "scripts")
REAL_PUBLISHER = os.path.expanduser("~/hl-fleet/publish_mini.py")
OWNERS = [c["ccn"] for c in FX["true_owner_controls"]]
WRONG = [p["ccn"] for p in FX["wrong_pairs"]]


def ok_guard(ccn):
    return None, []


def refuse_guard(ccn):
    return "refused by test", []


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def slurp(path):
    with open(path) as fh:
        return fh.read()


def jload(path):
    with open(path) as fh:
        return json.load(fh)


POLICY = json.loads(slurp(os.path.join(SCRIPTS, "hl_publish_gate_policy.json")))
CURAHEALTH = POLICY["overrides"][0]


class DecideNeverAborts(unittest.TestCase):
    def test_a_refused_item_mid_set_does_not_stop_later_items(self):
        guards = [("g", lambda c: (("no" if c == "B" else None), []))]
        ds = run.decide_all(["A", "B", "C", "D"], guards)
        self.assertEqual([d.ccn for d in ds], ["A", "B", "C", "D"])
        self.assertEqual(run.accepted_ccns(ds), ["A", "C", "D"])
        self.assertEqual([d.ccn for d in ds if not d.accepted], ["B"])

    def test_first_refusal_wins_and_later_guards_do_not_run(self):
        seen = []

        def later(c):
            seen.append(c)
            return None, []

        d = run.decide("X", [("first", refuse_guard), ("later", later)])
        self.assertFalse(d.accepted)
        self.assertEqual((d.guard, d.reason), ("first", "refused by test"))
        self.assertEqual(seen, [])

    def test_a_guard_that_raises_refuses_only_that_item(self):
        def boom(c):
            if c == "B":
                raise RuntimeError("registry exploded")
            return None, []

        ds = run.decide_all(["A", "B", "C"], [("identity-gate", boom)])
        self.assertEqual(run.accepted_ccns(ds), ["A", "C"])
        b = ds[1]
        self.assertFalse(b.accepted)
        self.assertIn("RuntimeError", b.reason)
        self.assertIn("failing closed", b.reason)

    def test_a_malformed_guard_result_refuses_the_item(self):
        for bad in (lambda c: None, lambda c: ("x", [], "extra"), lambda c: 5):
            d = run.decide("A", [("g", bad)])
            self.assertFalse(d.accepted, bad)
            self.assertIn("failing closed", d.reason)

    def test_an_empty_guard_list_is_never_silently_accepting_a_refused_item(self):
        # the only way to accept is to pass every guard that was supplied
        self.assertTrue(run.decide("A", [("g", ok_guard)]).accepted)
        self.assertFalse(run.decide("A", [("g", ok_guard), ("h", refuse_guard)]).accepted)

    def test_each_item_is_decided_once_in_order(self):
        calls = []

        def g(c):
            calls.append(c)
            return None, []

        ds = run.decide_all(["B", "A", "B", "C", "A"], [("g", g)])
        self.assertEqual([d.ccn for d in ds], ["B", "A", "C"])
        self.assertEqual(calls, ["B", "A", "C"])

    def test_warnings_from_passing_guards_travel_with_the_decision(self):
        d = run.decide("A", [("g1", lambda c: (None, ["w1"])), ("g2", lambda c: (None, ["w2"]))])
        self.assertTrue(d.accepted)
        self.assertEqual(d.warnings, ("w1", "w2"))

    def test_override_status_applied_not_applied_none(self):
        applied = lambda c: (None, [f"override applied: {c} file x accepted under named rebrand"])  # noqa: E731
        self.assertEqual(run.decide("372004", [("g", applied)], {"372004"}).override, run.OVERRIDE_APPLIED)
        refused = run.decide("372004", [("g", refuse_guard)], {"372004"})
        self.assertEqual(refused.override, run.OVERRIDE_NOT_APPLIED)
        self.assertEqual(run.decide("111111", [("g", refuse_guard)], {"372004"}).override, run.OVERRIDE_NONE)
        # an override warning for a DIFFERENT ccn is not this item's override
        other = lambda c: (None, ["override applied: 999999 file x accepted"])  # noqa: E731
        self.assertEqual(run.decide("111111", [("g", other)], {"372004"}).override, run.OVERRIDE_NONE)


class ReportHoldsOnlyRefusals(unittest.TestCase):
    def decisions(self):
        guards = [("identity-gate", lambda c: (("identity unproven: no in-file hospital name" if c in ("R1", "R2") else None), []))]
        return run.decide_all(["A1", "R1", "A2", "R2", "A3"], guards)

    def test_the_refusal_report_never_contains_an_accepted_item(self):
        rep = run.build_report(self.decisions(), True, "explicit")
        refused_ids = {r["hospital_id"] for r in rep["refused"]}
        accepted_ids = {a["hospital_id"] for a in rep["accepted"]}
        self.assertEqual(refused_ids, {"R1", "R2"})
        self.assertEqual(accepted_ids, {"A1", "A2", "A3"})
        self.assertFalse(refused_ids & accepted_ids)

    def test_counts_add_up_and_categories_sum_to_refused(self):
        rep = run.build_report(self.decisions(), False, "all-store")
        c = rep["counts"]
        self.assertEqual((c["requested"], c["accepted"], c["refused"]), (5, 3, 2))
        self.assertEqual(sum(rep["refused_by_category"].values()), c["refused"])
        self.assertEqual(rep["mode"], "live")
        self.assertEqual(rep["scope"], "all-store")

    def test_every_refused_entry_carries_id_reason_and_override_status(self):
        rep = run.build_report(self.decisions(), True, "explicit")
        for r in rep["refused"]:
            self.assertEqual(set(r), {"hospital_id", "guard", "category", "reason", "override"})
            self.assertTrue(r["reason"])

    def test_log_lines_list_every_refusal_and_the_counts(self):
        lines = run.log_lines(run.build_report(self.decisions(), True, "explicit"))
        text = "\n".join(lines)
        self.assertIn("REFUSED R1 ", text)
        self.assertIn("REFUSED R2 ", text)
        self.assertNotIn("REFUSED A1", text)
        self.assertIn("GATE SUMMARY requested=5 accepted=3", text)
        self.assertIn("refused=2", text)

    def test_category_matches_the_pr2c_census_buckets_on_real_gate_reasons(self):
        import hl_publish_gate as pg
        n = 0
        for p in FX["wrong_pairs"] + FX["true_owner_controls"]:
            reason, _ = pg.identity_gate(p["ccn"], head_of(p), reg_of(p), set(), [])
            if reason is not None:
                n += 1
                self.assertEqual(run.category(run.GUARD_IDENTITY, reason), probe_bucket(reason, []), reason)
        self.assertGreater(n, 5)  # the comparison actually ran
        # and the unproven buckets, by their gate wording
        reason, _ = pg.identity_gate("050075", {"source_url": "", "source_identity": {}},
                                     {"name": "EXAMPLE GENERAL HOSPITAL", "city": "X", "state": "CA", "zip": "90210"},
                                     set(), [])
        self.assertEqual(run.category(run.GUARD_IDENTITY, reason), probe_bucket(reason, []))
        self.assertTrue(run.category(run.GUARD_IDENTITY, reason).startswith("unproven:"))

    def test_non_identity_guards_are_their_own_category(self):
        self.assertEqual(run.category("quarantine", "x"), "quarantine")
        self.assertEqual(run.category("key-guard", "x"), "key-guard")


class ReportFileAndStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skip-report-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_report_is_written_atomically_and_never_overwritten(self):
        rep = run.build_report(run.decide_all(["A"], [("g", refuse_guard)]), True, "explicit")
        p1 = run.write_report(rep, os.path.join(self.tmp, "rr"))
        p2 = run.write_report(rep, os.path.join(self.tmp, "rr"))
        self.assertNotEqual(p1, p2)
        self.assertEqual(jload(p1)["refused"][0]["hospital_id"], "A")
        self.assertFalse([f for f in os.listdir(os.path.join(self.tmp, "rr")) if f.endswith(".tmp")])

    def test_an_unwritable_report_dir_returns_none_not_an_exception(self):
        blocker = os.path.join(self.tmp, "afile")
        open(blocker, "w").close()
        rep = run.build_report([], True, "explicit")
        self.assertIsNone(run.write_report(rep, os.path.join(blocker, "sub")))

    def test_enumerate_store_is_sorted_complete_and_drops_nothing_malformed(self):
        for name in ("050663.json.gz", "372004.json.gz", "junk.json.gz", "29002E.json.gz",
                     "050663.json.gz.QUARANTINE-x", "372004.rust-receipt.json", "notes.txt"):
            open(os.path.join(self.tmp, name), "w").close()
        os.mkdir(os.path.join(self.tmp, "dir.json.gz"))
        self.assertEqual(run.enumerate_store(self.tmp), ["050663", "29002E", "372004", "junk"])

    def test_an_unreadable_store_enumerates_empty_so_the_caller_can_refuse_to_call_it_clean(self):
        self.assertEqual(run.enumerate_store(os.path.join(self.tmp, "missing")), [])

    def test_exit_code_contract(self):
        self.assertEqual(run.exit_code(0, 1, 0), 2)    # one refused CCN still exits non-zero
        self.assertEqual(run.exit_code(0, 0, 0), 2)    # nothing accepted is never a success
        self.assertEqual(run.exit_code(3, 0, 0), 0)
        self.assertEqual(run.exit_code(3, 2, 0), 10)   # completed with refusals is never 0
        self.assertEqual(run.exit_code(3, 2, 9), 9)    # a failed publish keeps its own code
        self.assertEqual(run.exit_code(3, 0, 1), 1)


# --------------------------------------------------------------------------------------------
# the real patched main()
# --------------------------------------------------------------------------------------------
EXTRA = '''
import re
TREE = os.path.join(FLEET, "tree")
PUBLISHED = []


def log(msg):
    print(f"[publish-mini {msg}]", flush=True)


def key_refusal(ccn, verdicts_dir=None, store_dir=None):
    if re.fullmatch(r"\\d{6}", ccn):
        return None
    return "not a 6-digit CCN or a registry-ACCEPTED distinct-part key"


class Lock:
    def __init__(self):
        self.path = os.path.join(FLEET, "publish.lock")
        self.acquired = False

    def acquire(self):
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return False
        self.acquired = True
        return True

    def release(self):
        if self.acquired:
            os.remove(self.path)


def _publish(ccns, dry_run):
    PUBLISHED.append((list(ccns), dry_run))
    return 0
'''

SYNTH2 = (SYNTHETIC.replace("\n\ndef main():\n    return 0\n", EXTRA + "\n\ndef main():\n    return 0\n")
          + '\n\nif __name__ == "__main__":\n    sys.exit(main())\n')


class PatchedMain(unittest.TestCase):
    """stage 2c then 2d installed into a scratch fleet; the publisher's own main() runs in-process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skip-main-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fleet = os.path.join(self.tmp, "hl-fleet")
        os.makedirs(os.path.join(self.fleet, "verdicts"))
        os.makedirs(os.path.join(self.fleet, "tree"))
        os.makedirs(os.path.join(self.fleet, "results"))
        with open(os.path.join(self.fleet, "publish_mini.py"), "w") as fh:
            fh.write(SYNTH2)
        for name, body in (("adjudicated_holds.txt", "\n"), ("publish-state.json", "{}\n"),
                           ("verdicts/d1_verdicts_b1.tsv", "ccn\turl\n")):
            with open(os.path.join(self.fleet, name), "w") as fh:
                fh.write(body)
        self.backups = os.path.join(self.tmp, "backups")
        os.makedirs(self.backups)
        self.install_2c()

    def install_2c(self):
        bk = cg.cmd_backup(self.fleet, self.backups, "S1", "pr2c")
        with mock.patch.object(cg, "publish_running", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            cg.cmd_install(self.fleet, SCRIPTS, bk)
        self.pm_2c = slurp(os.path.join(self.fleet, "publish_mini.py"))

    def install_2d(self):
        self.bk2d = cg.cmd_backup(self.fleet, self.backups, "S2", "pr2d")
        with mock.patch.object(cg, "publish_running", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            return cg.cmd_install_skip_report(self.fleet, SCRIPTS, self.bk2d)

    def load(self):
        spec = importlib.util.spec_from_file_location("pm_skip", os.path.join(self.fleet, "publish_mini.py"))
        pm = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"TEST_FLEET": self.fleet}):
            for k in ("HL_PUBLISH_GATE_DIR", "HL_PUBLISH_GATE_POLICY", "HL_PUBLISH_REPORT_DIR"):
                os.environ.pop(k, None)
            spec.loader.exec_module(pm)
        assert pm.FLEET == self.fleet and pm.GATE_DIR == os.path.join(self.fleet, "gate")
        return pm

    def main_of(self, pm, argv):
        """pm.main() with argv, in modules/path that cannot see the repo's cached gate copies."""
        buf = io.StringIO()
        with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(sys, "argv", ["publish_mini.py"] + argv), contextlib.redirect_stdout(buf):
            for name in ("hl_publish_gate", "hl_admission", "hl_publish_run"):
                sys.modules.pop(name, None)
            rc = pm.main()
        return rc, buf.getvalue()

    def seat(self, pm, owners=(), wrong=()):
        for p in FX["true_owner_controls"]:
            if p["ccn"] in owners:
                pm.HEADS[p["ccn"]], pm.REGISTRY[p["ccn"]] = head_of(p), reg_of(p)
        for p in FX["wrong_pairs"]:
            if p["ccn"] in wrong:
                pm.HEADS[p["ccn"]], pm.REGISTRY[p["ccn"]] = head_of(p), reg_of(p)

    def report(self):
        d = os.path.join(self.fleet, "refusal-reports")
        files = sorted(os.listdir(d))
        self.assertEqual(len(files), 1, files)
        return jload(os.path.join(d, files[0]))

    # ---- the property the owner asked for ------------------------------------------------
    def test_a_planted_refused_item_mid_set_does_not_stop_later_accepted_items(self):
        self.install_2d()
        pm = self.load()
        a, b, c = OWNERS[:3]
        planted = WRONG[0]
        self.seat(pm, owners=OWNERS[:3], wrong=[planted])
        rc, out = self.main_of(pm, ["--dry-run", a, planted, b, c])
        self.assertEqual(pm.PUBLISHED, [([a, b, c], True)], "later accepted items must still reach the publish path")
        self.assertEqual(rc, 10)
        rep = self.report()
        self.assertEqual([r["hospital_id"] for r in rep["refused"]], [planted])
        self.assertEqual({x["hospital_id"] for x in rep["accepted"]}, {a, b, c})
        self.assertIn(f"REFUSED {planted} ", out)
        self.assertIn("RUN COMPLETE requested=4 accepted=3 refused=1", out)

    def test_a_planted_accepted_item_never_appears_in_the_refusal_report(self):
        self.install_2d()
        pm = self.load()
        good = OWNERS[0]
        self.seat(pm, owners=[good], wrong=WRONG[:4])
        rc, out = self.main_of(pm, ["--dry-run", WRONG[0], WRONG[1], good, WRONG[2], WRONG[3]])
        rep = self.report()
        self.assertNotIn(good, [r["hospital_id"] for r in rep["refused"]])
        self.assertEqual({r["hospital_id"] for r in rep["refused"]}, set(WRONG[:4]))
        self.assertEqual([x["hospital_id"] for x in rep["accepted"]], [good])
        self.assertNotIn(f"REFUSED {good}", out)
        self.assertEqual(pm.PUBLISHED, [([good], True)])

    def test_refused_items_never_reach_the_publish_path(self):
        self.install_2d()
        pm = self.load()
        self.seat(pm, owners=OWNERS[:2], wrong=WRONG[:3])
        self.main_of(pm, [OWNERS[0], WRONG[0], WRONG[1], OWNERS[1], WRONG[2]])
        (sent, _dry), = pm.PUBLISHED
        self.assertEqual(sent, OWNERS[:2])
        self.assertFalse(set(sent) & set(WRONG))

    def test_a_guard_that_raises_on_one_item_refuses_only_that_item(self):
        self.install_2d()
        pm = self.load()
        a, b, c = OWNERS[:3]
        self.seat(pm, owners=[a, b, c])

        real = pm._registry_identity
        pm._registry_identity = lambda ccn: (_ for _ in ()).throw(OSError("registry db locked")) if ccn == b else real(ccn)
        rc, out = self.main_of(pm, ["--dry-run", a, b, c])
        self.assertEqual(pm.PUBLISHED, [([a, c], True)])
        rep = self.report()
        self.assertEqual([r["hospital_id"] for r in rep["refused"]], [b])
        self.assertIn("failing closed", rep["refused"][0]["reason"])
        self.assertEqual(rc, 10)

    def test_every_refused_run_exits_nonzero_and_publishes_nothing(self):
        self.install_2d()
        pm = self.load()
        self.seat(pm, wrong=[WRONG[0]])
        rc, out = self.main_of(pm, ["--dry-run", WRONG[0]])
        self.assertEqual(rc, 2)  # a single refused CCN: what process_submissions.py relies on
        self.assertEqual(pm.PUBLISHED, [])
        self.assertIn("nothing accepted", out)
        self.assertIn(f"REFUSED {WRONG[0]} ", out)

    def test_all_accepted_exits_zero(self):
        self.install_2d()
        pm = self.load()
        self.seat(pm, owners=OWNERS[:2])
        rc, _ = self.main_of(pm, ["--dry-run", OWNERS[0], OWNERS[1]])
        self.assertEqual(rc, 0)
        self.assertEqual(pm.PUBLISHED, [(OWNERS[:2], True)])

    def test_a_live_run_passes_dry_run_false_to_the_publish_path(self):
        self.install_2d()
        pm = self.load()
        self.seat(pm, owners=[OWNERS[0]])
        self.main_of(pm, [OWNERS[0]])
        self.assertEqual(pm.PUBLISHED, [([OWNERS[0]], False)])
        self.assertEqual(self.report()["mode"], "live")

    # ---- the full set ---------------------------------------------------------------------
    def test_all_enumerates_the_whole_store_including_malformed_keys_and_reports_them(self):
        self.install_2d()
        pm = self.load()
        store = os.path.join(self.fleet, "results")
        keep = OWNERS[:2]
        for ccn in keep + WRONG[:3] + ["junk", "150042"]:
            open(os.path.join(store, f"{ccn}.json.gz"), "w").close()
        self.seat(pm, owners=keep, wrong=WRONG[:3])
        rc, out = self.main_of(pm, ["--dry-run", "--all"])
        rep = self.report()
        self.assertEqual(rep["counts"]["requested"], 7)
        self.assertEqual(rep["scope"], "all-store")
        self.assertEqual(sorted(x["hospital_id"] for x in rep["accepted"]), sorted(keep))
        refused = {r["hospital_id"]: r for r in rep["refused"]}
        self.assertEqual(set(refused), set(WRONG[:3]) | {"junk", "150042"})
        self.assertEqual(refused["junk"]["guard"], "key-guard")
        self.assertEqual(refused["150042"]["guard"], "quarantine")
        self.assertEqual(pm.PUBLISHED, [(sorted(keep), True)])
        self.assertEqual(rc, 10)

    def test_an_empty_or_unreadable_store_is_fatal_not_clean(self):
        self.install_2d()
        pm = self.load()
        rc, out = self.main_of(pm, ["--dry-run", "--all"])
        self.assertEqual(rc, 2)
        self.assertEqual(pm.PUBLISHED, [])
        self.assertIn("empty store is not a clean run", out)

    def test_all_with_explicit_ccns_and_no_args_are_usage_errors(self):
        self.install_2d()
        pm = self.load()
        self.assertEqual(self.main_of(pm, ["--all", OWNERS[0]])[0], 2)
        self.assertEqual(self.main_of(pm, [])[0], 2)
        self.assertEqual(pm.PUBLISHED, [])

    def test_the_curahealth_override_is_reported_applied_and_publishes(self):
        self.install_2d()
        pm = self.load()
        pm.REGISTRY["372004"] = None
        pm.HEADS["372004"] = {"source_url": CURAHEALTH["source_url"], "source_identity": {}}
        rc, out = self.main_of(pm, ["--dry-run", "372004"])
        self.assertEqual(rc, 0)
        self.assertEqual(pm.PUBLISHED, [(["372004"], True)])
        rep = self.report()
        self.assertEqual(rep["accepted"][0]["override"], "applied")
        self.assertEqual(rep["counts"]["accepted_with_override"], 1)
        self.assertIn("override applied: 372004", out)

    def test_a_named_override_that_does_not_apply_is_reported_as_such(self):
        self.install_2d()
        pm = self.load()
        pm.REGISTRY["372004"] = None
        pm.HEADS["372004"] = {"source_url": "https://example.invalid/some-other-file.csv", "source_identity": {}}
        rc, _ = self.main_of(pm, ["--dry-run", "372004"])
        self.assertEqual(rc, 2)
        r = self.report()["refused"][0]
        self.assertEqual((r["hospital_id"], r["override"]), ("372004", "named-override-not-applied"))
        self.assertEqual(pm.PUBLISHED, [])

    def test_the_lock_being_held_is_reported_with_rc_9(self):
        self.install_2d()
        pm = self.load()
        self.seat(pm, owners=[OWNERS[0]])
        open(os.path.join(self.fleet, "publish.lock"), "w").close()
        rc, _ = self.main_of(pm, ["--dry-run", OWNERS[0]])
        self.assertEqual(rc, 9)
        self.assertEqual(pm.PUBLISHED, [])

    # ---- fails closed ---------------------------------------------------------------------
    def test_a_missing_run_module_fails_closed_and_never_publishes(self):
        self.install_2d()
        os.unlink(os.path.join(self.fleet, "gate", cg.RUN_FILE))
        pm = self.load()
        self.seat(pm, owners=[OWNERS[0]])
        rc, out = self.main_of(pm, ["--dry-run", OWNERS[0]])
        self.assertEqual(rc, 2)
        self.assertEqual(pm.PUBLISHED, [])
        self.assertIn("failing closed", out)

    def test_a_broken_gate_policy_refuses_every_item_and_the_run_still_completes(self):
        self.install_2d()
        with open(os.path.join(self.fleet, "gate", "hl_publish_gate_policy.json"), "w") as fh:
            fh.write("{ not json")
        pm = self.load()
        self.seat(pm, owners=OWNERS[:2])
        rc, out = self.main_of(pm, ["--dry-run", OWNERS[0], OWNERS[1]])
        self.assertEqual(rc, 2)
        self.assertEqual(pm.PUBLISHED, [])
        self.assertEqual({r["category"] for r in self.report()["refused"]}, {"fail-closed"})

    def test_the_abort_on_first_refusal_main_is_kept_but_never_called(self):
        self.install_2d()
        src = slurp(os.path.join(self.fleet, "publish_mini.py"))
        self.assertIn("def _legacy_main():", src)
        self.assertEqual(src.count("def main():"), 1)
        pm = self.load()
        self.assertIsNot(pm.main, pm._legacy_main)

    # ---- the tool -------------------------------------------------------------------------
    def test_the_gate_files_and_policy_are_untouched_by_the_skip_report_install(self):
        before = {g: sha(os.path.join(self.fleet, "gate", g)) for g in cg.GATE_FILES}
        added = self.install_2d()
        self.assertEqual(added, [f"gate/{cg.RUN_FILE}"])
        self.assertEqual({g: sha(os.path.join(self.fleet, "gate", g)) for g in cg.GATE_FILES}, before)
        self.assertEqual(sha(os.path.join(self.fleet, "gate", cg.RUN_FILE)), sha(os.path.join(SCRIPTS, cg.RUN_FILE)))
        self.assertTrue(cg.cmd_status(self.fleet)["skip_report"])

    def test_install_refuses_a_publisher_that_is_not_cut_over(self):
        legacy = os.path.join(self.tmp, "fleet2")
        shutil.copytree(self.fleet, legacy)
        with open(os.path.join(legacy, "publish_mini.py"), "w") as fh:
            fh.write(SYNTH2)  # legacy shape, no PR2C marker
        bk = cg.cmd_backup(legacy, self.backups, "L1", "pr2d")
        before = sha(os.path.join(legacy, "publish_mini.py"))
        with mock.patch.object(cg, "publish_running", return_value=""), self.assertRaises(cg.CutoverError):
            cg.cmd_install_skip_report(legacy, SCRIPTS, bk)
        self.assertEqual(sha(os.path.join(legacy, "publish_mini.py")), before)
        self.assertFalse(os.path.exists(os.path.join(legacy, "gate", cg.RUN_FILE)))

    def test_install_refuses_a_second_patch_and_a_tampered_backup(self):
        self.install_2d()
        patched = sha(os.path.join(self.fleet, "publish_mini.py"))
        bk = cg.cmd_backup(self.fleet, self.backups, "S3", "pr2d")
        with mock.patch.object(cg, "publish_running", return_value=""):
            with self.assertRaises(cg.CutoverError):
                cg.cmd_install_skip_report(self.fleet, SCRIPTS, bk)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), patched)
        with open(os.path.join(self.bk2d, "publish_mini.py"), "a") as fh:
            fh.write("# tampered\n")
        with mock.patch.object(cg, "publish_running", return_value=""), self.assertRaises(cg.CutoverError):
            cg.cmd_install_skip_report(self.fleet, SCRIPTS, self.bk2d)

    def test_install_refuses_under_a_running_publish(self):
        bk = cg.cmd_backup(self.fleet, self.backups, "S4", "pr2d")
        before = sha(os.path.join(self.fleet, "publish_mini.py"))
        with mock.patch.object(cg, "publish_running", return_value="publish.lock exists"):
            with self.assertRaises(cg.CutoverError):
                cg.cmd_install_skip_report(self.fleet, SCRIPTS, bk)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), before)

    def test_the_backup_prefix_is_pr2d_and_the_default_is_unchanged(self):
        bk = cg.cmd_backup(self.fleet, self.backups, "P1", "pr2d")
        self.assertTrue(os.path.basename(bk).startswith("pr2d-"))
        self.assertTrue(os.path.basename(cg.cmd_backup(self.fleet, self.backups, "P2")).startswith("pr2c-"))

    def test_rollback_restores_the_cut_over_publisher_byte_for_byte_and_keeps_the_gate(self):
        self.install_2d()
        self.assertNotEqual(slurp(os.path.join(self.fleet, "publish_mini.py")), self.pm_2c)
        with contextlib.redirect_stdout(io.StringIO()):
            cg.cmd_rollback(self.fleet, self.bk2d)
        self.assertEqual(slurp(os.path.join(self.fleet, "publish_mini.py")), self.pm_2c)
        self.assertFalse(os.path.exists(os.path.join(self.fleet, "gate", cg.RUN_FILE)))
        for g in cg.GATE_FILES:
            self.assertTrue(os.path.isfile(os.path.join(self.fleet, "gate", g)), g)
        self.assertFalse(cg.cmd_status(self.fleet)["skip_report"])
        self.assertTrue(cg.cmd_status(self.fleet)["patched"])


class RealPublisherState(unittest.TestCase):
    @unittest.skipUnless(os.path.isfile(REAL_PUBLISHER), "the real ~/hl-fleet/publish_mini.py is not on this machine")
    def test_the_real_publisher_is_in_a_state_stage_2d_understands(self):
        # legacy shape (neither stage applied), 2c only (2d must apply), or 2c+2d (compiles, one live
        # main, refuses a second patch). Never assume one state: the cut-over changes this file.
        src = slurp(REAL_PUBLISHER)
        if cg.MARKER_2D in src:
            compile(src, REAL_PUBLISHER, "exec")
            self.assertEqual(src.count("def main():"), 1)
            self.assertIn("def _legacy_main():", src)
            with self.assertRaises(cg.CutoverError):
                cg.patched_source_2d(src)
        elif cg.MARKER in src:
            out = cg.patched_source_2d(src)
            self.assertIn(cg.MARKER_2D, out)
            self.assertEqual(out.count("def main():"), 1)
        else:
            with self.assertRaises(cg.CutoverError):  # 2d needs 2c first
                cg.patched_source_2d(src)
            out = cg.patched_source_2d(cg.patched_source(src))
            self.assertIn(cg.MARKER_2D, out)


if __name__ == "__main__":
    unittest.main()
