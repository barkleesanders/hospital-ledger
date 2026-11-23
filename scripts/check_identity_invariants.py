#!/usr/bin/env python3
"""Identity-path lint (INVENTORY M2, step 2a): admission is never a similarity test.

WHY THIS EXISTS
---------------
2026-11-02, "batch 000": `grade_pair` admitted files to CCNs on a 0.5 token-overlap /
`difflib` ratio, so two different hospitals sharing "Memorial Hospital" or "Baptist
Medical Center" passed (14 wrong pairs hand-adjudicated in W12/W13, frozen in
tests/fixtures/identity/known_wrong_pairs.json). The fix is `hl_admission.admit`:
name is necessary, an address anchor is required. This lint stops the shape from
coming back through a side door.

WHAT IT CHECKS (exit 0 clean / 1 violations / 2 could not measure)
------------------------------------------------------------------
1. IDENTITY PATHS (scripts/hl_admission.py, scripts/process_submissions.py) must not
   import or call fuzzy/similarity matching: `difflib`, `SequenceMatcher`,
   `get_close_matches`, `.ratio()` / `.quick_ratio()`, rapidfuzz / fuzzywuzzy /
   jellyfish / Levenshtein / jaro. Similarity is not identity.
2. NO RAW ACCEPT VERDICT outside the admission-evidence path: no tracked source file
   other than scripts/hl_admission.py (and tests, which assert on it) may carry a
   bare "ACCEPT" string literal. Verdict rows are rendered by `hl_admission.accept_row`,
   which refuses to render one with no evidence.
3. ONE DECISION (step 2b): any non-test module that defines a function named
   `identity_gate` must import hl_admission. scripts/hl_publish_gate.py (the publisher-facing
   gate) and scripts/process_submissions.py both do; a standalone gate with its own logic
   is the production publisher's shape (it passed 11/14 known-wrong pairs).
4. STRICT POLICY (step 2c): hl_publish_gate's `DEFAULT_UNPROVEN` must be `UNPROVEN_REFUSE`
   and the committed gate policy (scripts/hl_publish_gate_policy.json) must exist, be a JSON
   object, and not set `unproven` to anything but "refuse". Unproven data does not publish;
   loosening that is an owner decision made in a PR that edits this lint on purpose.
5. POSITIVE CONTROL: the identity paths exist and parse, hl_admission still defines
   `admit` / `accept_row`, hl_publish_gate still defines `identity_gate`; an empty scan is
   UNMEASURED, never a pass.

Run: `python3 scripts/check_identity_invariants.py [--root DIR]`. `--root` audits any
tree (scripts/prove_admission.sh replays the past-mistake shapes through it).
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
IDENTITY_PATHS = ("scripts/hl_admission.py", "scripts/process_submissions.py",
                  "scripts/hl_publish_gate.py")
ADMISSION_MODULE = "scripts/hl_admission.py"
PUBLISH_GATE_MODULE = "scripts/hl_publish_gate.py"
POLICY_FILE = "scripts/hl_publish_gate_policy.json"
BAD_MODULES = {"difflib", "rapidfuzz", "fuzzywuzzy", "jellyfish", "Levenshtein", "thefuzz", "textdistance"}
BAD_NAMES = {"SequenceMatcher", "get_close_matches", "jaro", "jaro_winkler", "levenshtein",
             "fuzz", "token_set_ratio", "token_sort_ratio", "partial_ratio"}
BAD_ATTRS = BAD_NAMES | {"ratio", "quick_ratio", "real_quick_ratio"}
SKIP_DIRS = {"node_modules", ".git", "dist", ".wrangler", ".beads", "__pycache__", "fixtures"}
ACCEPT_WORD = "ACC" + "EPT"  # built, not spelled, so this file does not trip its own check
BATCH000 = (
    "batch-000 (2026-11-02) admitted wrong CCNs on a name-similarity ratio; identity is "
    "decided only by hl_admission.admit (name necessary + state AND city/zip anchor)"
)


class Unmeasured(Exception):
    """The instrument could not measure; never reported as a pass."""


def _is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") or "/tests/" in rel or name.startswith("test_") or ".test." in name


def tracked_python(root: Path) -> list[str]:
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                  "--exclude-standard"], check=True, capture_output=True).stdout
            files = [p for p in out.decode().split("\0") if p.endswith(".py")]
            return sorted(p for p in files if (root / p).is_file())
        except (OSError, subprocess.CalledProcessError):
            pass
    found = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        found.append(rel)
    return sorted(found)


def parse(root: Path, rel: str) -> ast.AST:
    try:
        return ast.parse((root / rel).read_text(), filename=rel)
    except (OSError, SyntaxError, ValueError) as exc:
        raise Unmeasured(f"cannot parse {rel}: {exc}") from exc


def fuzzy_violations(tree: ast.AST, rel: str) -> list[str]:
    out = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BAD_MODULES:
                    out.append(f"{rel}:{line}: imports `{a.name}` in an identity path. {BATCH000}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BAD_MODULES:
                out.append(f"{rel}:{line}: imports from `{node.module}` in an identity path. {BATCH000}")
            for a in node.names:
                if a.name in BAD_NAMES:
                    out.append(f"{rel}:{line}: imports `{a.name}` in an identity path. {BATCH000}")
        elif isinstance(node, ast.Attribute) and node.attr in BAD_ATTRS:
            out.append(f"{rel}:{line}: uses `.{node.attr}` (similarity matching) in an identity path. {BATCH000}")
        elif isinstance(node, ast.Name) and node.id in BAD_NAMES:
            out.append(f"{rel}:{line}: uses `{node.id}` (similarity matching) in an identity path. {BATCH000}")
    return out


def raw_accept_violations(tree: ast.AST, rel: str) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # bare token, or a TSV cell like "\tACCEPT\t": a verdict being written
            if node.value.strip() == ACCEPT_WORD or f"\t{ACCEPT_WORD}\t" in node.value:
                out.append(
                    f"{rel}:{node.lineno}: raw ACCEPT verdict literal outside the admission-evidence "
                    "path; render it with hl_admission.accept_row (refuses empty evidence). "
                    "Name alone is not identity (batch-000, 2026-11-02).")
    return out


def imports_admission(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[-1] == "hl_admission" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and ((node.module or "").split(".")[-1] == "hl_admission"
                                                 or any(a.name == "hl_admission" for a in node.names)):
            return True
    return False


def second_gate_violations(tree: ast.AST, rel: str) -> list[str]:
    """A module that defines its own `identity_gate` must take the decision from
    hl_admission. One admission function, not a second, independent opinion."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "identity_gate" and not imports_admission(tree):
            out.append(
                f"{rel}:{node.lineno}: defines `identity_gate` without importing hl_admission: a second, "
                "independent identity decision. Delegate to hl_admission.admit (scripts/hl_publish_gate.py "
                "is the publisher-facing gate). The production publisher passed 11/14 known-wrong pairs on "
                "its own logic (INVENTORY M2, step 2b; batch-000, 2026-11-02).")
    return out


def strict_policy_violations(root: Path, gate_tree: ast.AST) -> list[str]:
    """Step 2c: the strict default must survive in code AND in the shipped policy file."""
    out = []
    strict_default = False
    for node in ast.walk(gate_tree):
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFAULT_UNPROVEN"
                                                 for t in node.targets)
                and isinstance(node.value, ast.Name) and node.value.id == "UNPROVEN_REFUSE"):
            strict_default = True
    if not strict_default:
        out.append(f"{PUBLISH_GATE_MODULE}: `DEFAULT_UNPROVEN` is not `UNPROVEN_REFUSE`. The owner's "
                   "policy (2026-11-23) is that an unproven hospital/owner pair is refused by default; "
                   "rebrands go through the named override list in "
                   f"{POLICY_FILE}, not through a lenient default.")
    policy_path = root / POLICY_FILE
    if not policy_path.is_file():
        out.append(f"{POLICY_FILE} is missing: the publisher gate policy (strict default + named "
                   "overrides) must be committed beside the gate")
        return out
    try:
        doc = json.loads(policy_path.read_text())
    except (OSError, ValueError) as exc:
        out.append(f"{POLICY_FILE} is not valid JSON: {exc}")
        return out
    if not isinstance(doc, dict):
        out.append(f"{POLICY_FILE} must be a JSON object")
    elif doc.get("unproven", "refuse") != "refuse":
        out.append(f"{POLICY_FILE}: `unproven` is {doc.get('unproven')!r}; the owner's policy is "
                   "\"refuse\" (unproven data does not publish)")
    return out


def audit(root: Path) -> list[str]:
    problems: list[str] = []
    for rel in IDENTITY_PATHS:
        if not (root / rel).is_file():
            raise Unmeasured(f"identity path {rel} not found; refusing to report an empty scan as clean")
    adm = parse(root, ADMISSION_MODULE)
    defined = {n.name for n in ast.walk(adm) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for needed in ("admit", "accept_row", "Verdict"):
        if needed not in defined:
            problems.append(f"{ADMISSION_MODULE} no longer defines `{needed}`: the admission function moved or was removed")
    gate_tree = parse(root, PUBLISH_GATE_MODULE)
    if "identity_gate" not in {n.name for n in ast.walk(gate_tree) if isinstance(n, ast.FunctionDef)}:
        problems.append(f"{PUBLISH_GATE_MODULE} no longer defines `identity_gate`: the publisher-facing gate moved or was removed")
    problems += strict_policy_violations(root, gate_tree)
    for rel in IDENTITY_PATHS:
        problems += fuzzy_violations(parse(root, rel), rel)
    scanned = 0
    for rel in tracked_python(root):
        if rel == ADMISSION_MODULE or _is_test(rel):
            continue
        scanned += 1
        tree = parse(root, rel)
        problems += raw_accept_violations(tree, rel)
        problems += second_gate_violations(tree, rel)
    if scanned == 0:
        raise Unmeasured("no non-test python sources scanned for raw ACCEPT verdicts")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        problems = audit(root)
    except Unmeasured as exc:
        print(f"IDENTITY INVARIANTS: UNMEASURED - {exc}", file=sys.stderr)
        return 2
    if problems:
        for p in problems:
            print(f"IDENTITY INVARIANTS: FAIL - {p}", file=sys.stderr)
        print(f"IDENTITY INVARIANTS: {len(problems)} problem(s) in {root}", file=sys.stderr)
        return 1
    print("IDENTITY INVARIANTS: PASS - no similarity matching in identity paths; "
          "no raw ACCEPT verdict outside hl_admission")
    return 0


if __name__ == "__main__":
    sys.exit(main())
