#!/usr/bin/env python3
"""Self-training gate for the Hospital Ledger wave driver (2026-10-26,
Barklee-directed: "a wave literally cannot close without its lessons
landing in the skill").

Design contract (mirrors the skill's autonomy doctrine):
- The DATA plane is never blocked. note_run() is called from hl_wave's
  terminal record functions inside a try/except and swallows its own
  errors too; a broken gate can never stall a parse or a publish.
- CLOSURE is what is gated. Every completed wave, every chunk, and every
  transport-deferred wave registers a lessons DEBT here. A run counts as
  closed only when its debt is discharged, and discharge is mechanical:
  this module appends the run's SKILL-LESSON lines to the skill's
  Campaign lessons log itself, so "the lessons landed" is a filesystem
  fact, not a claim in a report.

Usage:
  hl_lessons_gate.py status
  hl_lessons_gate.py discharge <run_id> <lessons_file>
  hl_lessons_gate.py note <record_json_file>   (manual/testing)

Lessons file format: one or more lines starting with "SKILL-LESSON:".
A no-lessons run discharges with a single line
"SKILL-LESSON: none -- <reason of at least a few words>".
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEBT_PATH = Path(os.environ.get("HL_LESSONS_DEBT", HERE / "hl_lessons_debt.json"))
SKILL_PATH = Path(os.environ.get(
    "HL_SKILL_PATH",
    "/home/hatch/workspace/custom-skills/"
    "local-autoresearch-hospital-ledger/SKILL.md"))
SKILL_ANCHOR = "\n## Available connectors"


def _load_debt():
    try:
        d = json.loads(DEBT_PATH.read_text())
        if isinstance(d, dict) and isinstance(d.get("runs"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"runs": {}}


def _save_debt(d):
    tmp = DEBT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, DEBT_PATH)


def run_id_for(rec):
    ts = str(rec.get("ts", "?"))
    wave = rec.get("wave")
    if wave == "chunk":
        ccns = ",".join(rec.get("ccns") or [])
        return f"chunk:{ts}:{ccns}"
    if isinstance(wave, int):
        return f"wave-{wave}:{ts}"
    return f"{wave}:{ts}"


def note_run(rec):
    """Register a lessons debt for a terminal wave/chunk record.

    Returns the run_id, or None on any problem. NEVER raises.
    """
    try:
        rid = run_id_for(rec)
        d = _load_debt()
        if rid not in d["runs"]:
            d["runs"][rid] = {
                "run_id": rid,
                "ts": rec.get("ts"),
                "wave": rec.get("wave"),
                "ccns": rec.get("ccns") or rec.get("updated_ccns") or [],
                "status": "owed",
            }
            _save_debt(d)
        return rid
    except Exception:
        return None


def open_debts():
    d = _load_debt()
    return [r for r in d["runs"].values() if r.get("status") == "owed"]


def _read_lessons(path):
    lines = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if line.startswith("SKILL-LESSON:"):
            lines.append(line)
    if not lines:
        raise ValueError("no SKILL-LESSON: lines in lessons file")
    for line in lines:
        body = line[len("SKILL-LESSON:"):].strip()
        if body.lower().startswith("none") and len(body) < 16:
            raise ValueError(
                "a 'none' lesson needs a real reason: "
                "'SKILL-LESSON: none -- <why this run taught nothing>'")
        if len(body) < 8:
            raise ValueError(f"lesson too thin to be a lesson: {line!r}")
    return lines


def discharge(run_id, lessons_file):
    """Append the run's lessons to the skill and clear its debt.

    Idempotent: discharging an already-discharged run is a no-op that
    reports so (the skill is never appended twice for one run).
    """
    d = _load_debt()
    entry = d["runs"].get(run_id)
    if entry is None:
        raise KeyError(f"unknown run_id {run_id!r} (see: status)")
    if entry.get("status") == "discharged":
        return f"{run_id}: already discharged at {entry.get('discharged_at')}"
    lessons = _read_lessons(lessons_file)
    skill = SKILL_PATH.read_text()
    if SKILL_ANCHOR not in skill:
        raise ValueError("skill anchor '## Available connectors' not found; "
                         "refusing to append blindly")
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    block = (f"\n- {date}: Lessons for run {run_id} "
             f"(discharged via hl_lessons_gate).\n"
             + "".join(f"  {line}\n" for line in lessons))
    SKILL_PATH.write_text(skill.replace(SKILL_ANCHOR, block + SKILL_ANCHOR, 1))
    entry["status"] = "discharged"
    entry["discharged_at"] = datetime.now(timezone.utc).isoformat()
    entry["lessons"] = lessons
    _save_debt(d)
    return (f"{run_id}: discharged; {len(lessons)} lesson(s) appended to "
            f"{SKILL_PATH.name}")


def main(argv):
    if len(argv) >= 2 and argv[1] == "status":
        owed = open_debts()
        print(f"open lessons debts: {len(owed)}")
        for r in owed:
            print(f"  OWED {r['run_id']}")
        return 0
    if len(argv) == 4 and argv[1] == "discharge":
        try:
            print(discharge(argv[2], argv[3]))
        except (KeyError, ValueError, OSError) as e:
            print(f"discharge FAILED: {e}", file=sys.stderr)
            return 2
        return 0
    if len(argv) == 3 and argv[1] == "note":
        rec = json.loads(Path(argv[2]).read_text())
        print(note_run(rec))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
