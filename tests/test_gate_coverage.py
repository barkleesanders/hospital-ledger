"""scripts/check_gate_coverage.py and scripts/tracked_tree_fingerprint.sh (INVENTORY G0).

Every failure branch is exercised against a synthetic tree built in a temp dir, so a
regression in the checker itself (not just in the repo) turns this suite red. The real
repo is also audited as a canary. Replays against the real past-mistake commits live in
scripts/prove_gate_coverage.sh.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_gate_coverage", REPO / "scripts" / "check_gate_coverage.py")
cgc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cgc)

GOOD_VERIFY = """#!/usr/bin/env bash
# python3 scripts/test_commented_out.py   <- a comment is not coverage
set -euo pipefail
TREE_BEFORE="$(bash scripts/tracked_tree_fingerprint.sh)"
python3 scripts/check_gate_coverage.py
python3 scripts/check_identity_invariants.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/test_extra.py
python3 scripts/csp_audit.py
npm run lint:observability
npm run test:unit
node --test tests/client.test.js
npm audit --json > "$AUDIT_JSON" || true
TREE_AFTER="$(bash scripts/tracked_tree_fingerprint.sh)"
"""

GOOD_PACKAGE = {
    "scripts": {
        "predeploy": "python3 scripts/predeploy_audit.py && bash scripts/verify.sh",
        "lint:observability": "node scripts/check-observability.mjs",
        "test:unit": "vitest run --config vitest.unit.config.mjs",
    }
}
GOOD_VITEST = 'export default { test: { include: ["src/**/*.test.ts"] } };\n'


def build_tree(root: Path, verify=GOOD_VERIFY, package=None, extra=None, vitest=GOOD_VITEST):
    files = {
        "scripts/verify.sh": verify,
        "package.json": json.dumps(package or GOOD_PACKAGE),
        "vitest.unit.config.mjs": vitest,
        "tests/test_a.py": "",
        "scripts/test_extra.py": "",
        "src/x.test.ts": "",
        "tests/client.test.js": "",
    }
    files.update(extra or {})
    for rel, body in files.items():
        if body is None:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


class AuditCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self, **kw):
        manual = kw.pop("manual", {})
        build_tree(self.root, **kw)
        return cgc.audit(self.root, manual=manual)

    def assertProblem(self, problems, needle):
        self.assertTrue(any(needle in p for p in problems), f"{needle!r} not in {problems}")

    def test_good_tree_is_clean(self):
        self.assertEqual(self.problems(), [])

    def test_orphan_python_test_is_named(self):
        p = self.problems(extra={"scripts/test_orphan.py": ""})
        self.assertProblem(p, "ORPHANED TEST: scripts/test_orphan.py")

    def test_orphan_js_test_is_named(self):
        p = self.problems(extra={"tools/other.test.mjs": ""})
        self.assertProblem(p, "ORPHANED TEST: tools/other.test.mjs")

    def test_commented_out_mention_is_not_coverage(self):
        # GOOD_VERIFY mentions scripts/test_commented_out.py only in a comment.
        p = self.problems(extra={"scripts/test_commented_out.py": ""})
        self.assertProblem(p, "ORPHANED TEST: scripts/test_commented_out.py")

    def test_py_compile_and_cmp_mentions_are_not_coverage(self):
        verify = GOOD_VERIFY + "python3 -m py_compile scripts/test_pc.py\ncmp scripts/test_cmp.py x\n"
        p = self.problems(verify=verify, extra={"scripts/test_pc.py": "", "scripts/test_cmp.py": ""})
        self.assertProblem(p, "ORPHANED TEST: scripts/test_pc.py")
        self.assertProblem(p, "ORPHANED TEST: scripts/test_cmp.py")

    def test_swallowed_test_line_is_flagged_and_not_counted(self):
        verify = GOOD_VERIFY + "python3 scripts/test_swallowed.py || true\n"
        p = self.problems(verify=verify, extra={"scripts/test_swallowed.py": ""})
        self.assertProblem(p, "swallows failure")
        self.assertProblem(p, "ORPHANED TEST: scripts/test_swallowed.py")

    def test_swallowed_non_gate_line_is_allowed(self):
        # `npm audit ... || true` in GOOD_VERIFY is followed by an explicit parse step.
        self.assertEqual(self.problems(), [])

    def test_missing_set_e_is_flagged(self):
        p = self.problems(verify=GOOD_VERIFY.replace("set -euo pipefail", "set -uo pipefail"))
        self.assertProblem(p, "no `set -e`")

    def test_unittest_discover_does_not_cover_nested_dirs(self):
        p = self.problems(extra={"tests/sub/test_nested.py": ""})
        self.assertProblem(p, "ORPHANED TEST: tests/sub/test_nested.py")

    def test_unittest_discover_respects_the_pattern(self):
        p = self.problems(verify=GOOD_VERIFY.replace("'test_*.py'", "'check_*.py'"))
        self.assertProblem(p, "ORPHANED TEST: tests/test_a.py")

    def test_vitest_include_covers_matching_files_only(self):
        p = self.problems(extra={"lib/y.test.ts": ""})
        self.assertProblem(p, "ORPHANED TEST: lib/y.test.ts")

    def test_empty_vitest_config_is_an_empty_check(self):
        p = self.problems(vitest='export default { test: { include: ["nope/**/*.test.ts"] } };\n')
        self.assertProblem(p, "EMPTY CHECK: vitest.unit.config.mjs")
        self.assertProblem(p, "ORPHANED TEST: src/x.test.ts")

    def test_manual_allowlist_exempts_and_goes_stale(self):
        orphan = {"scripts/test_manual.py": ""}
        self.assertEqual(self.problems(extra=orphan, manual={"scripts/test_manual.py": "needs a live VM"}), [])
        p = cgc.audit(self.root, manual={"scripts/test_extra.py": "was manual once"})
        self.assertProblem(p, "STALE EXEMPTION: scripts/test_extra.py")
        p = cgc.audit(self.root, manual={"scripts/test_gone.py": "deleted"})
        self.assertProblem(p, "no such test file exists")
        p = cgc.audit(self.root, manual={"scripts/test_manual.py": "  "})
        self.assertProblem(p, "has no reason")

    def test_missing_csp_audit_is_flagged(self):
        p = self.problems(verify=GOOD_VERIFY.replace("python3 scripts/csp_audit.py\n", ""))
        self.assertProblem(p, "csp_audit.py")

    def test_csp_audit_only_in_a_comment_is_flagged(self):
        p = self.problems(verify=GOOD_VERIFY.replace("python3 scripts/csp_audit.py", "# python3 scripts/csp_audit.py"))
        self.assertProblem(p, "csp_audit.py")

    def test_missing_observability_lint_is_flagged(self):
        p = self.problems(verify=GOOD_VERIFY.replace("npm run lint:observability\n", ""))
        self.assertProblem(p, "lint:observability")

    def test_checker_must_gate_itself(self):
        p = self.problems(verify=GOOD_VERIFY.replace("python3 scripts/check_gate_coverage.py\n", ""))
        self.assertProblem(p, "check_gate_coverage.py")

    def test_missing_identity_lint_is_flagged(self):
        # INVENTORY M2: the identity lint must stay in the battery (batch-000, 2026-11-02).
        p = self.problems(verify=GOOD_VERIFY.replace("python3 scripts/check_identity_invariants.py\n", ""))
        self.assertProblem(p, "check_identity_invariants.py")

    def test_missing_tree_fingerprint_is_flagged(self):
        p = self.problems(verify=GOOD_VERIFY.replace('TREE_AFTER="$(bash scripts/tracked_tree_fingerprint.sh)"\n', ""))
        self.assertProblem(p, "after-battery tracked-tree fingerprint")

    def test_predeploy_that_skips_verify_is_a_split_battery(self):
        pkg = {"scripts": dict(GOOD_PACKAGE["scripts"], predeploy="python3 scripts/csp_audit.py && npm run build")}
        p = self.problems(package=pkg)
        self.assertProblem(p, "SPLIT BATTERY")

    def test_missing_predeploy_is_flagged(self):
        pkg = {"scripts": {k: v for k, v in GOOD_PACKAGE["scripts"].items() if k != "predeploy"}}
        p = self.problems(package=pkg)
        self.assertProblem(p, "no `predeploy` script")

    def test_unknown_npm_script_is_unmeasured_not_a_pass(self):
        with self.assertRaises(cgc.Unmeasured):
            self.problems(verify=GOOD_VERIFY + "npm run does-not-exist\n")

    def test_no_tests_discovered_is_unmeasured_not_clean(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "verify.sh").write_text(GOOD_VERIFY)
        (self.root / "package.json").write_text(json.dumps(GOOD_PACKAGE))
        with self.assertRaises(cgc.Unmeasured):
            cgc.audit(self.root, manual={})

    def test_main_exit_codes_are_three_way(self):
        build_tree(self.root)
        self.assertEqual(cgc.main(["--root", str(self.root)]), 0)
        build_tree(self.root, extra={"scripts/test_orphan.py": ""})
        self.assertEqual(cgc.main(["--root", str(self.root)]), 1)
        (self.root / "scripts" / "verify.sh").unlink()
        self.assertEqual(cgc.main(["--root", str(self.root)]), 2)


class RealRepoCanary(unittest.TestCase):
    def test_this_repo_has_no_orphans_and_one_battery(self):
        self.assertEqual(cgc.audit(REPO), [])


class TreeFingerprint(unittest.TestCase):
    def make_repo(self, tmp: Path):
        (tmp / "scripts").mkdir()
        shutil.copy(REPO / "scripts" / "tracked_tree_fingerprint.sh", tmp / "scripts")
        (tmp / "data.json").write_text("{}\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(tmp), *args], check=True, env=env, capture_output=True)

    def fp(self, tmp: Path) -> str:
        out = subprocess.run(["bash", str(tmp / "scripts" / "tracked_tree_fingerprint.sh")],
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()

    def test_stable_changes_on_tracked_edit_and_reverts(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            self.make_repo(tmp)
            clean = self.fp(tmp)
            self.assertRegex(clean, r"^[0-9a-f]{64}$")
            self.assertEqual(self.fp(tmp), clean)
            (tmp / "data.json").write_text('{"polluted": true}\n')
            self.assertNotEqual(self.fp(tmp), clean)
            (tmp / "data.json").write_text("{}\n")
            self.assertEqual(self.fp(tmp), clean)

    def test_files_listing_names_only_what_changed(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            self.make_repo(tmp)
            script = str(tmp / "scripts" / "tracked_tree_fingerprint.sh")
            run = lambda: subprocess.run(["bash", script, "--files"], check=True,
                                         capture_output=True, text=True).stdout.splitlines()
            self.assertEqual(run(), [])
            (tmp / "data.json").write_text('{"polluted": true}\n')
            (tmp / "untracked.txt").write_text("x")
            rows = run()
            self.assertEqual(len(rows), 1)
            self.assertRegex(rows[0], r"^[0-9a-f]{40} data\.json$")
            (tmp / "data.json").unlink()
            self.assertEqual(run(), ["DELETED data.json"])

    def test_untracked_files_do_not_change_it(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            self.make_repo(tmp)
            clean = self.fp(tmp)
            (tmp / "scratch.log").write_text("noise")
            self.assertEqual(self.fp(tmp), clean)

    def test_outside_a_git_tree_is_unmeasured_never_a_hash(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "scripts").mkdir()
            shutil.copy(REPO / "scripts" / "tracked_tree_fingerprint.sh", tmp / "scripts")
            self.assertEqual(self.fp(tmp), "UNMEASURED")


if __name__ == "__main__":
    unittest.main()
