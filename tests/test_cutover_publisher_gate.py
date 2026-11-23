"""The publisher cut-over tool (INVENTORY M2, step 2c): backup, verified install, rollback.

Runs inside `unittest discover -s tests`. Everything happens in a scratch fleet directory built
here; the real ~/hl-fleet is only READ (one test checks the patch applies to its real shape, and is
reported as a skip, never a pass, when that file is absent, e.g. in CI).

* BackupIsVerified     - the install must refuse without a backup that re-hashes clean.
* InstallPatchesAndDelegates - the patched publisher's `identity_gate` really is the repo gate
                         under the policy: wrong pair refused, true owner published, missing gate
                         REFUSES (never falls back to the legacy gate), idempotent.
* RollbackIsExact      - the pre-cut-over bytes come back and the files the install added go away.
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
from test_publish_gate import FX, head_of, reg_of  # noqa: E402

SCRIPTS = os.path.join(REPO, "scripts")
REAL_PUBLISHER = os.path.expanduser("~/hl-fleet/publish_mini.py")

# A publisher with the same anchors and helper names as the real one, and a legacy gate that
# says PUBLISH for everything so a delegation failure is visible.
SYNTHETIC = '''#!/usr/bin/env python3
"""synthetic publish_mini"""
import os, sys
HOME = os.path.expanduser("~")
FLEET = os.environ.get("TEST_FLEET") or os.path.join(HOME, "hl-fleet")
STORE = os.path.join(FLEET, "results")
VERDICTS_DIR = os.path.join(FLEET, "verdicts")
HEADS = {}
REGISTRY = {}
HOLDS = set()
ROWS = {}


def load_holds(path=None):
    return None if path == "UNREADABLE" else set(HOLDS)


def _canonical_record_head(ccn, store_dir):
    return HEADS.get(ccn)


def _registry_identity(ccn):
    return REGISTRY.get(ccn)


def _all_verdict_rows(ccn, verdicts_dir):
    return ROWS.get(ccn, [])


def identity_gate(ccn, store_dir=None, verdicts_dir=None,
                  holds_path=None):
    return None, ["legacy gate: publishes everything"]


def main():
    return 0
'''


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def put(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def get(path):
    with open(path) as fh:
        return fh.read()


def make_fleet(tmp):
    fleet = os.path.join(tmp, "hl-fleet")
    os.makedirs(os.path.join(fleet, "verdicts"))
    with open(os.path.join(fleet, "publish_mini.py"), "w") as fh:
        fh.write(SYNTHETIC)
    os.chmod(os.path.join(fleet, "publish_mini.py"), 0o751)
    for name, body in (("process_submissions.py", "# ps\n"), ("adjudicated_holds.txt", "150042\n"),
                       ("publish-state.json", "{}\n"), ("publish_b99.sh", "python3 publish_mini.py $1\n"),
                       ("unrelated.sh", "echo hi\n"), ("verdicts/d1_verdicts_b1.tsv", "ccn\turl\n")):
        with open(os.path.join(fleet, name), "w") as fh:
            fh.write(body)
    return fleet


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cutover-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fleet = make_fleet(self.tmp)
        self.backups = os.path.join(self.tmp, "backups")
        os.makedirs(self.backups)
        self.orig = sha(os.path.join(self.fleet, "publish_mini.py"))

    def backup(self):
        return cg.cmd_backup(self.fleet, self.backups, "T1")

    def install(self, bk):
        with mock.patch.object(cg, "publish_running", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            return cg.cmd_install(self.fleet, SCRIPTS, bk)


class BackupIsVerified(Base):
    def test_backup_covers_publisher_runners_holds_and_verdicts(self):
        bk = self.backup()
        rels = {f["rel"] for f in cg.load_manifest(bk)["files"]}
        self.assertEqual(rels, {"publish_mini.py", "process_submissions.py", "adjudicated_holds.txt",
                                "publish-state.json", "publish_b99.sh", "verdicts/d1_verdicts_b1.tsv"})
        self.assertNotIn("unrelated.sh", rels)
        self.assertEqual(cg.cmd_verify(bk), len(rels))

    def test_backup_path_is_timestamped_and_never_overwritten(self):
        bk = self.backup()
        self.assertTrue(os.path.basename(bk).startswith("pr2c-"))
        with self.assertRaises(cg.CutoverError):
            self.backup()

    def test_a_tampered_backup_fails_verification(self):
        bk = self.backup()
        with open(os.path.join(bk, "publish_mini.py"), "a") as fh:
            fh.write("# tampered\n")
        with self.assertRaises(cg.CutoverError):
            cg.cmd_verify(bk)

    def test_install_refuses_a_tampered_backup_and_writes_nothing(self):
        bk = self.backup()
        with open(os.path.join(bk, "publish_mini.py"), "a") as fh:
            fh.write("# tampered\n")
        with self.assertRaises(cg.CutoverError):
            self.install(bk)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), self.orig)
        self.assertFalse(os.path.exists(os.path.join(self.fleet, "gate")))

    def test_an_empty_manifest_is_never_a_verified_backup(self):
        bk = self.backup()
        m = cg.load_manifest(bk)
        m["files"] = []
        put(os.path.join(bk, "MANIFEST.json"), json.dumps(m))
        with self.assertRaises(cg.CutoverError):
            cg.cmd_verify(bk)

    def test_install_refuses_under_a_running_publish(self):
        bk = self.backup()
        with mock.patch.object(cg, "publish_running", return_value="publish.lock exists"):
            with self.assertRaises(cg.CutoverError):
                cg.cmd_install(self.fleet, SCRIPTS, bk)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), self.orig)

    def test_publish_lock_file_counts_as_running(self):
        put(os.path.join(self.fleet, "publish.lock"), "")
        self.assertIn("publish.lock", cg.publish_running(self.fleet))

    def test_the_restore_script_restores_byte_for_byte_without_the_tool(self):
        bk = self.backup()
        self.install(bk)
        import subprocess
        r = subprocess.run(["bash", os.path.join(bk, "RESTORE.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), self.orig)


class InstallPatchesAndDelegates(Base):
    def load_patched(self):
        spec = importlib.util.spec_from_file_location("pm_patched", os.path.join(self.fleet, "publish_mini.py"))
        pm = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"TEST_FLEET": self.fleet}):
            os.environ.pop("HL_PUBLISH_GATE_DIR", None)
            os.environ.pop("HL_PUBLISH_GATE_POLICY", None)
            spec.loader.exec_module(pm)
        assert pm.GATE_DIR == os.path.join(self.fleet, "gate"), pm.GATE_DIR  # never the real fleet
        return pm

    def run_in_clean_modules(self, fn):
        # the repo's own hl_publish_gate/hl_admission are already imported by sibling tests; the patched
        # publisher must be tested against the INSTALLED copies, so hide the cached ones.
        with mock.patch.dict(sys.modules):
            for name in ("hl_publish_gate", "hl_admission"):
                sys.modules.pop(name, None)
            return fn()

    def test_install_adds_the_gate_files_and_patches_the_publisher(self):
        bk = self.backup()
        added = self.install(bk)
        self.assertEqual(sorted(added), sorted(f"gate/{g}" for g in cg.GATE_FILES))
        for g in cg.GATE_FILES:
            self.assertEqual(sha(os.path.join(self.fleet, "gate", g)), sha(os.path.join(SCRIPTS, g)))
        src = get(os.path.join(self.fleet, "publish_mini.py"))
        self.assertIn(cg.MARKER, src)
        self.assertIn("def _legacy_identity_gate(", src)
        self.assertEqual(src.count("def identity_gate("), 1)
        self.assertEqual(os.stat(os.path.join(self.fleet, "publish_mini.py")).st_mode & 0o7777, 0o751)
        self.assertTrue(cg.cmd_status(self.fleet)["patched"])

    def test_install_is_idempotent_and_refuses_a_second_patch(self):
        bk = self.backup()
        self.install(bk)
        patched = sha(os.path.join(self.fleet, "publish_mini.py"))
        with self.assertRaises(cg.CutoverError):
            self.install(bk)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), patched)

    def test_a_publisher_of_an_unexpected_shape_is_not_touched(self):
        put(os.path.join(self.fleet, "publish_mini.py"), "def main():\n    return 0\n")
        bk = self.backup()
        before = sha(os.path.join(self.fleet, "publish_mini.py"))
        with self.assertRaises(cg.CutoverError):
            self.install(bk)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), before)
        self.assertFalse(os.path.exists(os.path.join(self.fleet, "gate")))

    def test_the_patched_identity_gate_is_the_repo_gate(self):
        self.install(self.backup())

        def go():
            pm = self.load_patched()
            wrong = FX["wrong_pairs"][0]
            owner = FX["true_owner_controls"][0]
            for p in (wrong, owner):
                pm.HEADS[p["ccn"]] = head_of(p)
                pm.REGISTRY[p["ccn"]] = reg_of(p)
            return pm.identity_gate(wrong["ccn"]), pm.identity_gate(owner["ccn"])

        (w_reason, _), (o_reason, o_warns) = self.run_in_clean_modules(go)
        self.assertIn("identity mismatch", w_reason or "")  # the legacy stub would have published it
        self.assertEqual((o_reason, o_warns), (None, []))

    def test_unproven_is_refused_through_the_publisher(self):
        self.install(self.backup())

        def go():
            pm = self.load_patched()
            pm.HEADS["050075"] = {"source_url": "", "source_identity": {}}
            pm.REGISTRY["050075"] = {"name": "EXAMPLE GENERAL HOSPITAL", "city": "Springfield",
                                     "state": "CA", "zip": "90210"}
            return pm.identity_gate("050075")

        reason, _ = self.run_in_clean_modules(go)
        self.assertIn("unproven", reason or "")

    def test_a_hold_and_an_unreadable_holds_file_still_refuse(self):
        self.install(self.backup())

        def go():
            pm = self.load_patched()
            c = FX["true_owner_controls"][0]
            pm.HEADS[c["ccn"]], pm.REGISTRY[c["ccn"]] = head_of(c), reg_of(c)
            pm.HOLDS.add(c["ccn"])
            return pm.identity_gate(c["ccn"]), pm.identity_gate(c["ccn"], holds_path="UNREADABLE")

        (held, _), (unreadable, _) = self.run_in_clean_modules(go)
        self.assertIn("hold", held or "")
        self.assertIn("failing closed", unreadable or "")

    def test_the_curahealth_override_publishes_its_pinned_file_only(self):
        self.install(self.backup())
        pol = json.loads(get(os.path.join(SCRIPTS, "hl_publish_gate_policy.json")))["overrides"][0]

        def go():
            pm = self.load_patched()
            pm.REGISTRY["372004"] = None  # the live shape
            pm.HEADS["372004"] = {"source_url": pol["source_url"], "source_identity": {}}
            ok = pm.identity_gate("372004")
            pm.HEADS["372004"] = {"source_url": "https://example.invalid/other.csv", "source_identity": {}}
            other = pm.identity_gate("372004")
            return ok, other

        (ok_reason, ok_warns), (other_reason, _) = self.run_in_clean_modules(go)
        self.assertIsNone(ok_reason)
        self.assertTrue(any("override applied: 372004" in w for w in ok_warns), ok_warns)
        self.assertIn("unproven", other_reason or "")

    def test_a_missing_gate_directory_refuses_and_does_not_fall_back_to_the_legacy_gate(self):
        self.install(self.backup())

        def go():
            pm = self.load_patched()
            pm.GATE_DIR = os.path.join(self.tmp, "nonexistent-gate-dir")
            c = FX["true_owner_controls"][0]
            pm.HEADS[c["ccn"]], pm.REGISTRY[c["ccn"]] = head_of(c), reg_of(c)
            return pm.identity_gate(c["ccn"])

        reason, warns = self.run_in_clean_modules(go)
        self.assertIn("failing closed", reason or "")
        self.assertNotIn("legacy gate", " ".join(warns))

    def test_a_broken_policy_file_refuses_through_the_publisher(self):
        self.install(self.backup())
        put(os.path.join(self.fleet, "gate", "hl_publish_gate_policy.json"), "{ not json")

        def go():
            pm = self.load_patched()
            c = FX["true_owner_controls"][0]
            pm.HEADS[c["ccn"]], pm.REGISTRY[c["ccn"]] = head_of(c), reg_of(c)
            return pm.identity_gate(c["ccn"])

        reason, _ = self.run_in_clean_modules(go)
        self.assertIn("failing closed", reason or "")

    @unittest.skipUnless(os.path.isfile(REAL_PUBLISHER), "the real ~/hl-fleet/publish_mini.py is not on this machine")
    def test_the_patch_applies_to_the_real_publisher_shape(self):
        out = cg.patched_source(get(REAL_PUBLISHER))
        self.assertIn(cg.MARKER, out)
        self.assertEqual(out.count("def identity_gate("), 1)


class RollbackIsExact(Base):
    def test_rollback_restores_the_original_bytes_and_removes_what_install_added(self):
        bk = self.backup()
        before = {f: sha(os.path.join(self.fleet, f)) for f in
                  ("publish_mini.py", "process_submissions.py", "adjudicated_holds.txt", "publish_b99.sh")}
        self.install(bk)
        self.assertNotEqual(sha(os.path.join(self.fleet, "publish_mini.py")), self.orig)
        with contextlib.redirect_stdout(io.StringIO()):
            cg.cmd_rollback(self.fleet, bk)
        for f, h in before.items():
            self.assertEqual(sha(os.path.join(self.fleet, f)), h, f)
        self.assertFalse(os.path.exists(os.path.join(self.fleet, "gate")))
        self.assertFalse(cg.cmd_status(self.fleet)["patched"])

    def test_rollback_after_the_patched_publisher_ran_leaves_no_gate_dir(self):
        # the dress rehearsal on a copy of the real fleet found this: importing the installed gate
        # makes Python write gate/__pycache__, which used to stop the empty-dir cleanup.
        bk = self.backup()
        self.install(bk)
        os.makedirs(os.path.join(self.fleet, "gate", "__pycache__"))
        put(os.path.join(self.fleet, "gate", "__pycache__", "hl_publish_gate.cpython-313.pyc"), "x")
        with contextlib.redirect_stdout(io.StringIO()):
            cg.cmd_rollback(self.fleet, bk)
        self.assertFalse(os.path.exists(os.path.join(self.fleet, "gate")))

    def test_rollback_keeps_a_preexisting_gate_dirs_pycache(self):
        os.makedirs(os.path.join(self.fleet, "gate", "__pycache__"))
        put(os.path.join(self.fleet, "gate", "keep.txt"), "mine\n")
        bk = self.backup()
        self.install(bk)
        with contextlib.redirect_stdout(io.StringIO()):
            cg.cmd_rollback(self.fleet, bk)
        self.assertTrue(os.path.isdir(os.path.join(self.fleet, "gate", "__pycache__")))

    def test_rollback_refuses_a_tampered_backup(self):
        bk = self.backup()
        self.install(bk)
        with open(os.path.join(bk, "publish_mini.py"), "a") as fh:
            fh.write("# tampered\n")
        patched = sha(os.path.join(self.fleet, "publish_mini.py"))
        with self.assertRaises(cg.CutoverError):
            cg.cmd_rollback(self.fleet, bk)
        self.assertEqual(sha(os.path.join(self.fleet, "publish_mini.py")), patched)

    def test_rollback_keeps_a_gate_dir_that_existed_before_the_install(self):
        os.makedirs(os.path.join(self.fleet, "gate"))
        put(os.path.join(self.fleet, "gate", "keep.txt"), "mine\n")
        bk = self.backup()
        self.install(bk)
        with contextlib.redirect_stdout(io.StringIO()):
            cg.cmd_rollback(self.fleet, bk)
        self.assertEqual(get(os.path.join(self.fleet, "gate", "keep.txt")), "mine\n")


if __name__ == "__main__":
    unittest.main()
