#!/usr/bin/env python3
"""Integration proof: the REAL hl_wave.record_chunk fires the lessons gate.
WAVE_LOG is redirected to a temp file; gate paths via env overrides."""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="lgate-int-"))
os.environ["HL_LESSONS_DEBT"] = str(TMP / "debt.json")
os.environ["HL_SKILL_PATH"] = str(TMP / "SKILL.md")
(TMP / "SKILL.md").write_text("## Available connectors\n")
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location("hlw", str(HERE / "hl_wave.py"))
hlw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hlw)
hlw.WAVE_LOG = TMP / "waves.jsonl"

rec = hlw.record_chunk(["150089"], ["150089"], ["150089"], [],
                       {"predicates": {"no_progress_streak": 0}})
debt = json.loads((TMP / "debt.json").read_text())
runs = list(debt["runs"].values())
assert len(runs) == 1 and runs[0]["status"] == "owed", runs
assert runs[0]["run_id"].startswith("chunk:") \
    and "150089" in runs[0]["run_id"], runs
log_lines = (TMP / "waves.jsonl").read_text().strip().splitlines()
assert len(log_lines) == 1 and json.loads(log_lines[0])["wave"] == "chunk"
print("INTEGRATION PASS: record_chunk wrote its record AND registered "
      f"lessons debt {runs[0]['run_id']}")
