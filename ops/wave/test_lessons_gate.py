#!/usr/bin/env python3
"""Tests for hl_lessons_gate (temp files only; never touches live state)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="lgate-test-"))
SKILL = TMP / "SKILL.md"
DEBT = TMP / "debt.json"
SKILL.write_text("# Skill\n\n## Campaign lessons log\n\n- 2026-01-01: seed.\n"
                 "\n## Available connectors\n\nnone\n")
os.environ["HL_LESSONS_DEBT"] = str(DEBT)
os.environ["HL_SKILL_PATH"] = str(SKILL)
sys.path.insert(0, str(HERE))
import hl_lessons_gate as g  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


chunk_rec = {"ts": "2026-10-27T00:00:00Z", "wave": "chunk",
             "ccns": ["150089", "150051"]}
rid = g.note_run(chunk_rec)
check("chunk run_id", rid == "chunk:2026-10-27T00:00:00Z:150089,150051")
check("dedup on re-note", g.note_run(chunk_rec) == rid
      and len(g.open_debts()) == 1)
g.note_run({"ts": "2026-10-27T01:00:00Z", "wave": 49, "updated_ccns": ["1"]})
g.note_run({"ts": "2026-10-27T02:00:00Z", "wave": "deferred-3"})
check("three debts", len(g.open_debts()) == 3)

bad = TMP / "bad.md"
bad.write_text("## Skill lessons\nno tagged lines here\n")
try:
    g.discharge(rid, bad)
    check("untagged lessons rejected", False)
except ValueError:
    check("untagged lessons rejected", True)
check("debt survives bad discharge", len(g.open_debts()) == 3)

thin = TMP / "thin.md"
thin.write_text("SKILL-LESSON: none\n")
try:
    g.discharge(rid, thin)
    check("thin 'none' rejected", False)
except ValueError:
    check("thin 'none' rejected", True)

good = TMP / "good.md"
good.write_text("SKILL-LESSON: ZIP-member OOM route -- build canonicals "
                "on the Mini, never a VM chunk reparse.\n")
out = g.discharge(rid, good)
check("discharge ok", "discharged" in out)
text = SKILL.read_text()
check("lesson appended once", text.count("ZIP-member OOM route") == 1)
check("entry names the run", rid in text)
check("debt cleared", len(g.open_debts()) == 2)
out2 = g.discharge(rid, good)
check("re-discharge idempotent", "already discharged" in out2
      and SKILL.read_text().count("ZIP-member OOM route") == 1)

try:
    g.discharge("wave-999:nope", good)
    check("unknown run rejected", False)
except KeyError:
    check("unknown run rejected", True)

DEBT.write_text("{corrupt")
check("note_run survives corrupt debt",
      g.note_run({"ts": "2026-10-27T03:00:00Z", "wave": "chunk",
                  "ccns": ["1"]}) is not None)

r = subprocess.run([sys.executable, str(HERE / "hl_lessons_gate.py"),
                    "status"], capture_output=True, text=True,
                   env={**os.environ})
check("CLI status runs", r.returncode == 0 and "open lessons debts" in r.stdout)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
