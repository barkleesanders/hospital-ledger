#!/usr/bin/env python3
"""Skip-and-report orchestration for the Mini publisher (INVENTORY M2, step 2d).

WHY THIS EXISTS: the cut-over publisher (step 2c) aborted the WHOLE run on the first refused
item, so under the strict gate (19 of the 1,857 store canonicals pass) a real run could never
complete. This module is what lets a run process the full set: every item goes through the
ordered guards, the ones that refuse are SKIPPED (never handed to the upload path) and collected
into a refusal report, and the accepted ones publish as before.

WHAT IT DOES NOT DO: it decides nothing about identity. The guards are injected callables
(quarantine, key guard, `hl_publish_gate` under its policy); this module only runs them for every
item and records what they said. The gate policy (unproven = refuse, the named override list,
what counts as proof) is not read, interpreted or changed here. Stdlib only, so it installs
beside the gate in `~/hl-fleet/gate/` with no dependencies.

THE TWO PROPERTIES THAT MATTER (each has a test and a mutation proof, scripts/prove_skip_report.sh):
  * a refusal never stops the loop: a guard that refuses, raises, or returns something malformed
    REFUSES THAT ITEM (fail closed) and the next item is still decided;
  * the refusal report is built only from refused decisions: an accepted item can never appear in
    it, and a refused item can never reach `accepted_ccns`.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

# exit codes the publisher returns (see publish_mini.main):
RC_OK = 0                  # everything requested was accepted and the run succeeded
RC_NOTHING_ACCEPTED = 2    # every item refused (or usage / fatal): unchanged for one refused CCN
RC_COMPLETE_WITH_REFUSALS = 10  # the run completed; accepted items processed; some items refused

OVERRIDE_NONE = "none"
OVERRIDE_APPLIED = "applied"
OVERRIDE_NOT_APPLIED = "named-override-not-applied"

GUARD_IDENTITY = "identity-gate"

Guard = Callable[[str], tuple]


@dataclass(frozen=True)
class Decision:
    ccn: str
    accepted: bool
    guard: str = ""            # the guard that refused ("" when accepted)
    reason: str = ""           # the guard's own reason ("" when accepted)
    warnings: tuple = ()
    override: str = OVERRIDE_NONE


def _override_status(ccn: str, accepted: bool, warnings: Sequence[str], override_ids: Iterable[str]) -> str:
    if accepted and any(str(w).startswith(f"override applied: {ccn} ") for w in warnings):
        return OVERRIDE_APPLIED
    if ccn in set(override_ids):
        return OVERRIDE_NOT_APPLIED if not accepted else OVERRIDE_NONE
    return OVERRIDE_NONE


def decide(ccn: str, guards: Sequence[tuple], override_ids: Iterable[str] = ()) -> Decision:
    """Run the ordered `(name, fn)` guards for one CCN. `fn(ccn) -> (refusal_or_None, warnings)`.
    The first refusal wins. A guard that raises, or returns something that is not a 2-tuple,
    refuses this item: it never propagates, because one bad item must not take the set down."""
    warnings: list = []
    for name, fn in guards:
        try:
            reason, warns = fn(ccn)
            warns = [warns] if isinstance(warns, str) else list(warns or [])
        except Exception as exc:  # noqa: BLE001 - fail closed per item, never abort the run
            reason, warns = f"{name} raised {type(exc).__name__}: {exc} - failing closed", []
        warnings += warns
        if reason is not None:
            return Decision(ccn, False, name, str(reason), tuple(warnings),
                            _override_status(ccn, False, warnings, override_ids))
    return Decision(ccn, True, "", "", tuple(warnings), _override_status(ccn, True, warnings, override_ids))


def decide_all(ccns: Iterable[str], guards: Sequence[tuple], override_ids: Iterable[str] = ()) -> list:
    """Decide EVERY item, in order, each exactly once (a repeated CCN is decided once)."""
    ids = set(override_ids)
    seen, out = set(), []
    for ccn in ccns:
        if ccn in seen:
            continue
        seen.add(ccn)
        out.append(decide(ccn, guards, ids))
    return out


def accepted_ccns(decisions: Iterable[Decision]) -> list:
    return [d.ccn for d in decisions if d.accepted]


# Same buckets, in the same order, as scripts/probe_publisher_gate.bucket, so the counts in a
# publisher run are directly comparable to the PR2C census (no-file-name 1,422, no-registry-row 369,
# name-mismatch 20, ein-contradiction 14, name-but-no-address 11, hold 1, rejected-url 1).
_NEEDLES = (("hold list", "hold"), ("failing closed", "fail-closed"),
            ("exact file a registry reject", "rejected-url"), ("ein ", "ein-contradiction"),
            ("no registry row", "unproven:no-registry-row"),
            ("no store canonical head", "unproven:no-canonical-head"),
            ("carries no state, city or zip", "unproven:name-but-no-address"),
            ("do not anchor", "unproven:address-does-not-anchor"),
            ("no in-file hospital name", "unproven:no-file-name"),
            ("does not match the registry name", "refused:name-mismatch"),
            ("city", "refused:city-contradiction"), ("state", "refused:state-contradiction"),
            ("identity mismatch", "refused:identity-mismatch"))


def category(guard: str, reason: str) -> str:
    if guard != GUARD_IDENTITY:
        return guard or "refused:other"
    text = (reason or "").lower()
    for needle, name in _NEEDLES:
        if needle in text:
            return name
    return "refused:other"


def build_report(decisions: Sequence[Decision], dry_run: bool, scope: str, store: str = "",
                 generated_utc: str = "") -> dict:
    refused = [d for d in decisions if not d.accepted]
    accepted = [d for d in decisions if d.accepted]
    by_cat: dict = {}
    for d in refused:
        c = category(d.guard, d.reason)
        by_cat[c] = by_cat.get(c, 0) + 1
    return {
        "generated_utc": generated_utc or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "dry-run" if dry_run else "live",
        "scope": scope,
        "store": store,
        "counts": {"requested": len(decisions), "accepted": len(accepted), "refused": len(refused),
                   "accepted_with_override": sum(1 for d in accepted if d.override == OVERRIDE_APPLIED)},
        "refused_by_category": dict(sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))),
        "accepted": [{"hospital_id": d.ccn, "override": d.override, "warnings": list(d.warnings)}
                     for d in accepted],
        "refused": [{"hospital_id": d.ccn, "guard": d.guard, "category": category(d.guard, d.reason),
                     "reason": d.reason, "override": d.override} for d in refused],
    }


def log_lines(report: Mapping) -> list:
    """The report as publisher log lines: one per item, then the counts. Refusals are listed in
    full (the owner asked for the refusal report in the log), accepted items keep the legacy
    `identity gate PASS` / WARNING lines."""
    c = report["counts"]
    lines = [f"RUN START {report['mode']} scope={report['scope']} requested={c['requested']}"]
    for a in report["accepted"]:
        for w in a["warnings"]:
            lines.append(f"identity gate {a['hospital_id']}: WARNING: {w}")
        lines.append(f"identity gate PASS: {a['hospital_id']} override={a['override']}")
    for r in report["refused"]:
        lines.append(f"REFUSED {r['hospital_id']} guard={r['guard']} category={r['category']} "
                     f"override={r['override']} :: {r['reason']}")
    for cat, n in report["refused_by_category"].items():
        lines.append(f"REFUSAL REPORT category {cat}: {n}")
    lines.append(f"GATE SUMMARY requested={c['requested']} accepted={c['accepted']} "
                 f"(with override {c['accepted_with_override']}) refused={c['refused']} - "
                 f"refused items are skipped and never uploaded")
    return lines


def write_report(report: Mapping, report_dir: str):
    """Write the JSON report atomically. Returns the path, or None on failure: a failed file
    write is reported by the caller and never aborts the run (the log lines are the primary
    report), but it is never silent."""
    try:
        os.makedirs(report_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = f"refusals-{stamp}-{report['mode']}"
        path, n = os.path.join(report_dir, base + ".json"), 0
        while os.path.exists(path):
            n += 1
            path = os.path.join(report_dir, f"{base}-{n}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def enumerate_store(store: str) -> list:
    """Every top-level `<key>.json.gz` canonical in the store, sorted. Nothing is filtered out:
    a malformed key is the key guard's to refuse (and report), not this function's to drop.
    An unreadable store returns [] and the caller must treat that as FATAL, never as clean."""
    try:
        return sorted(f[:-len(".json.gz")] for f in os.listdir(store)
                      if f.endswith(".json.gz") and os.path.isfile(os.path.join(store, f)))
    except OSError:
        return []


def exit_code(accepted: int, refused: int, publish_rc: int) -> int:
    """The publisher's exit code. A failed publish keeps its own code; nothing accepted is 2
    (so a single refused CCN still exits non-zero, which process_submissions.py relies on);
    a completed run with refusals is 10, never 0."""
    if accepted == 0:
        return RC_NOTHING_ACCEPTED
    if publish_rc != 0:
        return publish_rc
    return RC_COMPLETE_WITH_REFUSALS if refused else RC_OK
