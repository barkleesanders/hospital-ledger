#!/usr/bin/env python3
"""Gate-coverage check: no test file that no gate runs, and one battery on the deploy path.

WHY THIS EXISTS (INVENTORY G0, 2026-11-21)
------------------------------------------
The repo had two disjoint gate batteries and a set of orphaned regression tests:

* `scripts/verify.sh` never ran `csp_audit.py`; `npm run predeploy` never ran the
  Python tests, `test:fetch-proxy`, `test:submissions` or `npm audit`. Evidence:
  2946774 ("CI verify caught what predeploy ... does not run") and d930135
  (`csp_audit.py` red on pristine HEAD "since 5e04acd" because only deploy ran it).
* Seven regression tests added WITH past fixes (8a45aaa CCN 150042 web-asset guard,
  c302ab1 parser layouts, e25340d/3169846 short-body resume, ...) were run by no
  gate at all; 5f15e80 even recorded "test_lessons_gate import error pre-exists on
  main" - a broken test sat unnoticed because nothing ran it. Three more were added
  after the inventory and orphaned the same way.

A test that no gate runs proves nothing about the code it was written for, so every
"fails at the parent of the fix" proof depends on this invariant holding.

WHAT IT CHECKS (exit 0 clean / 1 violations / 2 could not measure)
-------------------------------------------------------------------
1. Every tracked `test_*.py` and `*.test.{ts,tsx,js,mjs}` is COVERED by `scripts/verify.sh`:
     - top-level `tests/test_*.py` via the `unittest discover -s tests -p test_*.py` line;
     - a file matched by an `include` glob of a `vitest.*.config.mjs` that a verify line
       runs (`npm run test:*`, followed transitively through package.json scripts);
     - a file whose path is an EXECUTED command argument in verify.sh
       (`python3 <path>`, `node --test <path>`; `py_compile`, `cmp`, `shasum`, `cat`... do not count);
     - or it is listed in MANUAL below with a reason.
   A verify line that swallows failure (`|| true`) does not count, and verify.sh must
   `set -e`. A vitest config that matches zero files does not count (an empty check is not a pass).
2. MANUAL allowlist entries must exist and must NOT already be covered (no stale exemptions).
3. verify.sh runs `scripts/csp_audit.py`, `npm run lint:observability` and this script, and
   fingerprints tracked files before and after the battery (a suite that writes to the repo
   fails it: three tests rewrote data/header_aliases.json and ops/wave/hl_lessons_debt.json).
4. `package.json` `predeploy` invokes `scripts/verify.sh` (one battery, so the deploy
   path cannot pass while verify is red and the two lists cannot drift apart).

Run: `python3 scripts/check_gate_coverage.py [--root DIR]`. `--root` audits any tree
(used by scripts/prove_gate_coverage.sh to replay the past-mistake commits).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# Test-like files deliberately run by hand. path -> reason. Keep EMPTY unless a test
# truly cannot run offline; every entry is re-checked for staleness on every run.
MANUAL: dict[str, str] = {}

TEST_FILE_RE = re.compile(r"(^|/)test_[^/]*\.py$|\.test\.(ts|tsx|js|mjs)$")
SKIP_DIRS = {"node_modules", ".git", "dist", ".wrangler", ".beads", "__pycache__"}
# Commands whose arguments are mentioned, not executed, so a path after them is NOT coverage.
NON_EXECUTING = {"shasum", "cmp", "cat", "grep", "rg", "echo", "ls", "test", "git", "diff", "wc", "sha256sum"}
REQUIRED_VERIFY_STEPS = (
    ("scripts/csp_audit.py", "csp_audit.py (CSP guard from f32fdc1; deploy-only until d930135 showed it rotting red)"),
    ("scripts/check_gate_coverage.py", "check_gate_coverage.py (this check; it must gate itself)"),
)
REQUIRED_VERIFY_NPM = (
    ("lint:observability", "npm run lint:observability (was predeploy-only)"),
)


# Substrings that must appear in verify.sh's non-comment command text.
REQUIRED_VERIFY_TEXT = (
    ("TREE_BEFORE=", "the before-battery tracked-tree fingerprint (tests must not write to the repo)"),
    ("TREE_AFTER=", "the after-battery tracked-tree fingerprint comparison"),
    ("scripts/tracked_tree_fingerprint.sh", "scripts/tracked_tree_fingerprint.sh"),
)


class Unmeasured(Exception):
    """Raised when the instrument cannot measure; never reported as a pass."""


def discover_tests(root: Path) -> list[str]:
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "ls-files"], check=True, capture_output=True, text=True
            ).stdout.splitlines()
            return sorted(p for p in out if TEST_FILE_RE.search(p))
        except (OSError, subprocess.CalledProcessError):
            pass  # fall through to the walk
    found = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and TEST_FILE_RE.search(path.relative_to(root).as_posix()):
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def verify_commands(text: str) -> list[str]:
    """Logical command lines of verify.sh: comments, heredoc bodies and blanks removed,
    backslash continuations joined."""
    lines: list[str] = []
    heredoc_end: str | None = None
    for raw in text.splitlines():
        if heredoc_end is not None:
            if raw.strip() == heredoc_end:
                heredoc_end = None
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", raw)
        if m:
            heredoc_end = m.group(1)
        lines.append(raw.rstrip())
    joined: list[str] = []
    buf = ""
    for line in lines:
        if line.endswith("\\"):
            buf += line[:-1] + " "
        else:
            joined.append(buf + line)
            buf = ""
    if buf:
        joined.append(buf)
    return joined


def tokenize(line: str) -> list[str]:
    # Drop a trailing ` # comment` (shlex comments=True would also eat `#` inside words).
    try:
        return shlex.split(line, comments=True)
    except ValueError as exc:
        raise Unmeasured(f"cannot tokenize verify.sh line {line!r}: {exc}") from exc


def swallows_failure(tokens: list[str]) -> bool:
    return "||" in tokens


def glob_match(path: str, pattern: str) -> bool:
    """vitest/tinyglobby semantics: `**/` is zero or more directories, `*` never crosses `/`."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.fullmatch(out, path) is not None


def vitest_includes(config_text: str) -> list[str]:
    m = re.search(r"include\s*:\s*\[(.*?)\]", config_text, re.S)
    if not m:
        return []
    return re.findall(r"[\"']([^\"']+)[\"']", m.group(1))


class Battery:
    """What verify.sh actually executes, resolved from the repo's own files."""

    def __init__(self, root: Path):
        self.root = root
        verify = root / "scripts" / "verify.sh"
        pkg = root / "package.json"
        if not verify.is_file():
            raise Unmeasured("scripts/verify.sh not found")
        if not pkg.is_file():
            raise Unmeasured("package.json not found")
        try:
            self.scripts: dict[str, str] = json.loads(pkg.read_text()).get("scripts", {})
        except (ValueError, OSError) as exc:
            raise Unmeasured(f"package.json unreadable: {exc}") from exc
        self.verify_text = verify.read_text()
        self.command_text = "\n".join(verify_commands(self.verify_text))
        self.set_e = bool(re.search(r"^\s*set\s+-[a-zA-Z]*e", self.verify_text, re.M))
        self.executed_paths: set[str] = set()
        self.unittest_dirs: list[tuple[str, str]] = []
        self.vitest_configs: set[str] = set()
        self.npm_scripts_run: set[str] = set()
        self.swallowed: list[str] = []
        for line in verify_commands(self.verify_text):
            self._ingest(line, depth=0)

    def _ingest(self, line: str, depth: int) -> None:
        if depth > 6:
            raise Unmeasured(f"npm script recursion too deep at {line!r}")
        toks = tokenize(line)
        # `a && b` chains (package.json scripts) -> split into simple commands
        simple: list[list[str]] = [[]]
        for tok in toks:
            if tok in {"&&", ";"}:
                simple.append([])
            else:
                simple[-1].append(tok)
        for cmd in simple:
            if not cmd:
                continue
            if swallows_failure(cmd):
                # Only a swallowed GATE is a problem (`npm audit ... || true` is
                # followed by an explicit parse step and is not coverage). Ingest into
                # scratch state: if it would have counted as coverage, flag it and
                # do not count it.
                saved = (set(self.executed_paths), list(self.unittest_dirs),
                         set(self.vitest_configs), set(self.npm_scripts_run))
                self._ingest_simple(cmd[: cmd.index("||")], depth)
                grew = (self.executed_paths, self.unittest_dirs,
                        self.vitest_configs, self.npm_scripts_run) != saved
                (self.executed_paths, self.unittest_dirs,
                 self.vitest_configs, self.npm_scripts_run) = saved
                if grew:
                    self.swallowed.append(" ".join(cmd))
                continue
            self._ingest_simple(cmd, depth)

    def _ingest_simple(self, cmd: list[str], depth: int) -> None:
        # drop redirections (`> file`, `2>&1`, `>&2`): their targets are not executed paths
        kept: list[str] = []
        skip = False
        for tok in cmd:
            if skip:
                skip = False
            elif re.fullmatch(r"\d*(>>?|<)", tok):
                skip = True
            elif re.fullmatch(r"\d*(>>?|<)&?\S*", tok):
                continue
            else:
                kept.append(tok)
        cmd = kept
        # skip leading env assignments
        while cmd and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", cmd[0]):
            cmd = cmd[1:]
        if not cmd:
            return
        head = cmd[0]
        base = head.rsplit("/", 1)[-1]
        if base in NON_EXECUTING:
            return
        if "py_compile" in cmd:
            return
        if base == "npm" and len(cmd) >= 3 and cmd[1] == "run":
            name = cmd[2]
            self.npm_scripts_run.add(name)
            body = self.scripts.get(name)
            if body is None:
                raise Unmeasured(f"verify.sh runs `npm run {name}` but package.json has no such script")
            self._ingest(body, depth + 1)
            return
        if "unittest" in cmd and "discover" in cmd:
            start, pattern = ".", "test*.py"
            if "-s" in cmd:
                start = cmd[cmd.index("-s") + 1]
            if "-p" in cmd:
                pattern = cmd[cmd.index("-p") + 1]
            self.unittest_dirs.append((start.rstrip("/"), pattern))
            return
        if base in {"vitest"} or (len(cmd) > 1 and cmd[1] == "vitest") or "vitest" in cmd[:3]:
            if "--config" in cmd:
                self.vitest_configs.add(cmd[cmd.index("--config") + 1])
            return
        for tok in cmd[1:]:
            if tok.startswith("-") or not re.search(r"\.(py|js|mjs|ts|tsx|sh)$", tok):
                continue
            self.executed_paths.add(tok[2:] if tok.startswith("./") else tok)

    def covering(self, test_path: str) -> str | None:
        """How verify.sh covers this test, or None."""
        if test_path in self.executed_paths:
            return "verify.sh runs it directly"
        for start, pattern in self.unittest_dirs:
            parent = test_path.rsplit("/", 1)[0] if "/" in test_path else "."
            if parent == start and fnmatch.fnmatch(test_path.rsplit("/", 1)[-1], pattern):
                return f"unittest discover -s {start}"
        for cfg in self.vitest_configs:
            cfg_path = self.root / cfg
            if not cfg_path.is_file():
                continue
            for pattern in vitest_includes(cfg_path.read_text()):
                if glob_match(test_path, pattern):
                    return f"vitest include in {cfg}"
        return None

    def empty_vitest_configs(self, tests: list[str]) -> list[str]:
        empties = []
        for cfg in sorted(self.vitest_configs):
            cfg_path = self.root / cfg
            patterns = vitest_includes(cfg_path.read_text()) if cfg_path.is_file() else []
            if not any(glob_match(t, p) for t in tests for p in patterns):
                empties.append(cfg)
        return empties


def audit(root: Path, manual: dict[str, str] | None = None) -> list[str]:
    manual = MANUAL if manual is None else manual
    problems: list[str] = []
    tests = discover_tests(root)
    if not tests:
        raise Unmeasured("no test files discovered; refusing to report an empty set as clean")
    battery = Battery(root)

    if not battery.set_e:
        problems.append("scripts/verify.sh has no `set -e`: a failing test would not stop the battery")
    for swallowed in battery.swallowed:
        problems.append(f"verify.sh line swallows failure (`||`/`; true`) and is not counted: {swallowed}")

    for rel in tests:
        how = battery.covering(rel)
        if how is None and rel not in manual:
            problems.append(
                f"ORPHANED TEST: {rel} is run by no gate. Add it to scripts/verify.sh "
                "(or MANUAL in scripts/check_gate_coverage.py with a reason). "
                "A test no gate runs proves nothing (5f15e80, 8a45aaa, 3169846)."
            )
        if how is not None and rel in manual:
            problems.append(f"STALE EXEMPTION: {rel} is in MANUAL but verify.sh already covers it ({how}); remove it")
    for rel, reason in sorted(manual.items()):
        if not reason.strip():
            problems.append(f"MANUAL entry {rel} has no reason")
        if rel not in tests:
            problems.append(f"STALE EXEMPTION: {rel} is in MANUAL but no such test file exists")
    for cfg in battery.empty_vitest_configs(tests):
        problems.append(f"EMPTY CHECK: {cfg} is run by verify.sh but its include matches no test file")

    for path, label in REQUIRED_VERIFY_STEPS:
        if path not in battery.executed_paths:
            problems.append(f"MISSING GATE: verify.sh does not run {label}")
    for name, label in REQUIRED_VERIFY_NPM:
        if name not in battery.npm_scripts_run:
            problems.append(f"MISSING GATE: verify.sh does not run {label}")

    for needle, label in REQUIRED_VERIFY_TEXT:
        if needle not in battery.command_text:
            problems.append(f"MISSING GATE: verify.sh does not run {label}")

    predeploy = battery.scripts.get("predeploy")
    if predeploy is None:
        problems.append("package.json has no `predeploy` script: the deploy path runs no gate")
    elif "scripts/verify.sh" not in predeploy and "npm run verify" not in predeploy:
        problems.append(
            "SPLIT BATTERY: `npm run predeploy` does not invoke scripts/verify.sh, so the deploy path "
            "can pass while verify is red (2946774, d930135)."
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        problems = audit(root)
    except Unmeasured as exc:
        print(f"GATE COVERAGE: UNMEASURED - {exc}", file=sys.stderr)
        return 2
    if problems:
        for p in problems:
            print(f"GATE COVERAGE: FAIL - {p}", file=sys.stderr)
        print(f"GATE COVERAGE: {len(problems)} problem(s) in {root}", file=sys.stderr)
        return 1
    n = len(discover_tests(root))
    print(f"GATE COVERAGE: PASS - all {n} test files are run by scripts/verify.sh; predeploy invokes it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
