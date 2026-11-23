#!/usr/bin/env python3
"""Cut the Mini publisher over to `hl_publish_gate` (INVENTORY M2, step 2c): backup, install,
status, rollback. Stdlib only.

WHY A TOOL, NOT A HAND EDIT: `~/hl-fleet/publish_mini.py` is untracked (INVENTORY M7), so the
only record of its prior state is the backup this tool makes, and a hand edit to the file that
guards what gets published is exactly the kind of change that needs a proven undo. The tool
refuses to install unless the backup re-hashes clean, and `rollback` restores byte-identical
files and removes what the install added.

  backup    copy the publisher's scripts/config (publish_mini.py, process_submissions.py,
            adjudicated_holds.txt, publish-state.json, every runner script that calls
            publish_mini, the verdicts it reads, and any existing gate/ dir) to
            <backups>/pr2c-<UTC stamp>/ with MANIFEST.json (sha256/size/mode) and an
            independent RESTORE.sh. Prints the backup path.
  verify    re-hash a backup against its manifest.
  install   (needs --backup) verify the backup, refuse if a publish is running, install
            hl_publish_gate.py / hl_admission.py / hl_publish_gate_policy.json into <fleet>/gate/,
            and patch publish_mini.py so `identity_gate` delegates to them (the legacy gate is
            renamed `_legacy_identity_gate` and never called). A missing/invalid gate or policy
            REFUSES; it never falls back to the legacy gate. Idempotent.
  status    is the publisher patched; shas of publish_mini.py and the installed gate files.
  rollback  (needs --backup) restore every backed-up file byte-for-byte, delete the files the
            install added, and re-verify against the manifest.

Never touches R2, the store, the publish tree, the verdict/holds contents, or a running publish.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

GATE_FILES = ("hl_publish_gate.py", "hl_admission.py", "hl_publish_gate_policy.json")
MARKER = "PR2C-GATE-CUTOVER"
LEGACY_DEF = ("def identity_gate(ccn, store_dir=None, verdicts_dir=None,\n"
              "                  holds_path=None):\n")
LEGACY_RENAMED = ("def _legacy_identity_gate(ccn, store_dir=None, verdicts_dir=None,\n"
                  "                         holds_path=None):\n")
MAIN_ANCHOR = "\n\ndef main():\n"
FIXED_FILES = ("publish_mini.py", "process_submissions.py", "adjudicated_holds.txt", "publish-state.json")

NEW_GATE_BLOCK = f'''

# ---------------------------------------------------------------------------
# {MARKER} (INVENTORY M2, step 2c; owner decision 2026-11-23)
# The identity decision is the repo's hl_publish_gate under its gate policy: an
# unproven hospital/owner pair is REFUSED, and a rebrand publishes only through a
# named override in hl_publish_gate_policy.json. The legacy gate above is kept for
# reference and rollback comparison only and is NOT called. A missing or invalid
# gate module/policy REFUSES (it never falls back to the legacy gate).
# ---------------------------------------------------------------------------
GATE_DIR = os.environ.get("HL_PUBLISH_GATE_DIR") or os.path.join(FLEET, "gate")
GATE_POLICY = (os.environ.get("HL_PUBLISH_GATE_POLICY")
               or os.path.join(GATE_DIR, "hl_publish_gate_policy.json"))


def _gate_module():
    """The INSTALLED gate, pinned to GATE_DIR: a same-named module elsewhere on sys.path must
    never stand in for the audited copy."""
    path = os.path.join(GATE_DIR, "hl_publish_gate.py")
    if not os.path.isfile(path):
        raise ImportError(f"{{path}} not found")
    if GATE_DIR not in sys.path:
        sys.path.insert(0, GATE_DIR)
    import hl_publish_gate
    if os.path.dirname(os.path.realpath(hl_publish_gate.__file__)) != os.path.realpath(GATE_DIR):
        raise ImportError(f"hl_publish_gate resolved to {{hl_publish_gate.__file__}}, not {{GATE_DIR}}")
    return hl_publish_gate


def identity_gate(ccn, store_dir=None, verdicts_dir=None, holds_path=None):
    """Gate 1 for one CCN. Returns (refusal_reason_or_None, warnings).
    Decided by hl_publish_gate.gate_from_policy; same contract as the legacy gate."""
    store_dir = store_dir or STORE
    verdicts_dir = verdicts_dir or VERDICTS_DIR
    try:
        gate = _gate_module()
    except Exception as exc:  # never degrade to the legacy gate
        return (f"identity gate module unavailable, failing closed "
                f"({{type(exc).__name__}}: {{exc}})"), []
    return gate.gate_from_policy(
        ccn, _canonical_record_head(ccn, store_dir), _registry_identity(ccn),
        load_holds(holds_path), _all_verdict_rows(ccn, verdicts_dir),
        policy_path=GATE_POLICY)
'''


class CutoverError(Exception):
    pass


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def atomic_write(path: str, data: bytes, mode: int) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cutover-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fleet_inventory(fleet: str) -> list:
    """Relative paths the backup must cover (those that exist)."""
    rels = [f for f in FIXED_FILES if os.path.isfile(os.path.join(fleet, f))]
    for name in sorted(os.listdir(fleet)):
        p = os.path.join(fleet, name)
        if name.endswith(".sh") and os.path.isfile(p):
            with open(p, errors="replace") as fh:
                if "publish_mini" in fh.read():
                    rels.append(name)
    vdir = os.path.join(fleet, "verdicts")
    if os.path.isdir(vdir):
        rels += [f"verdicts/{n}" for n in sorted(os.listdir(vdir))
                 if os.path.isfile(os.path.join(vdir, n))]
    gdir = os.path.join(fleet, "gate")
    if os.path.isdir(gdir):
        rels += [f"gate/{n}" for n in sorted(os.listdir(gdir)) if os.path.isfile(os.path.join(gdir, n))]
    return rels


def cmd_backup(fleet: str, backups: str, stamp=None) -> str:
    if not os.path.isfile(os.path.join(fleet, "publish_mini.py")):
        raise CutoverError(f"{fleet}/publish_mini.py not found: nothing to back up")
    dest = os.path.join(backups, f"pr2c-{stamp or utc_stamp()}")
    if os.path.exists(dest):
        raise CutoverError(f"backup path already exists, refusing to overwrite: {dest}")
    os.makedirs(dest)
    files = []
    for rel in fleet_inventory(fleet):
        src, dst = os.path.join(fleet, rel), os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        if sha256(src) != sha256(dst):
            raise CutoverError(f"backup copy of {rel} does not match its source")
        files.append({"rel": rel, "sha256": sha256(dst), "size": os.path.getsize(dst),
                      "mode": os.stat(src).st_mode & 0o7777})
    manifest = {"created_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "fleet": fleet,
                "files": files, "installed_new": [], "note": "step 2c publisher gate cut-over"}
    with open(os.path.join(dest, "MANIFEST.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    write_restore_script(dest, fleet, files)
    return dest


def write_restore_script(dest: str, fleet: str, files: list) -> None:
    """RESTORE.sh depends on nothing but cp/shasum, so it still works if this tool breaks."""
    lines = ["#!/bin/bash", "# Restores the pre-cut-over publisher files byte-for-byte.",
             "set -euo pipefail", f'BK="$(cd "$(dirname "$0")" && pwd)"', f'FLEET="{fleet}"']
    for f in files:
        if f["rel"].startswith("verdicts/"):
            continue  # inputs this cut-over never writes; restored by manifest verification only
        lines.append(f'cp -p "$BK/{f["rel"]}" "$FLEET/{f["rel"]}"')
        lines.append(f'[ "$(shasum -a 256 "$FLEET/{f["rel"]}" | cut -d" " -f1)" = "{f["sha256"]}" ] '
                     f'|| {{ echo "MISMATCH {f["rel"]}"; exit 1; }}')
    lines.append('echo "restored; remove any gate/ files the install added: see MANIFEST.json installed_new"')
    path = os.path.join(dest, "RESTORE.sh")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)


def load_manifest(backup: str) -> dict:
    try:
        with open(os.path.join(backup, "MANIFEST.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise CutoverError(f"cannot read manifest in {backup}: {exc}") from exc


def cmd_verify(backup: str) -> int:
    m = load_manifest(backup)
    if not m.get("files"):
        raise CutoverError("manifest lists no files: an empty backup is never a verified one")
    bad = []
    for f in m["files"]:
        p = os.path.join(backup, f["rel"])
        if not os.path.isfile(p) or sha256(p) != f["sha256"]:
            bad.append(f["rel"])
    if bad:
        raise CutoverError(f"backup does not match its manifest: {bad}")
    return len(m["files"])


def publish_running(fleet: str) -> str:
    if os.path.exists(os.path.join(fleet, "publish.lock")):
        return f"{fleet}/publish.lock exists"
    try:
        out = subprocess.run(["pgrep", "-f", "publish_mini.py"], capture_output=True, text=True)
    except OSError:
        return ""
    own = str(os.getpid())
    pids = [p for p in out.stdout.split() if p != own]
    return f"publish_mini.py running (pid {','.join(pids)})" if pids else ""


def patched_source(src: str) -> str:
    if MARKER in src:
        raise CutoverError("already patched")
    if src.count(LEGACY_DEF) != 1 or src.count(MAIN_ANCHOR) != 1:
        raise CutoverError("publish_mini.py is not the shape this cut-over patches: expected exactly one "
                           "legacy `def identity_gate(...)` header and one `def main():`")
    out = src.replace(LEGACY_DEF, LEGACY_RENAMED).replace(MAIN_ANCHOR, NEW_GATE_BLOCK + MAIN_ANCHOR)
    compile(out, "publish_mini.py", "exec")  # a syntax error must never reach the publisher
    return out


def cmd_install(fleet: str, repo_scripts: str, backup: str, force_running: bool = False) -> list:
    n = cmd_verify(backup)
    running = publish_running(fleet)
    if running and not force_running:
        raise CutoverError(f"refusing to install under a running publish: {running}")
    for g in GATE_FILES:
        if not os.path.isfile(os.path.join(repo_scripts, g)):
            raise CutoverError(f"{g} not found in {repo_scripts}")
    pm_path = os.path.join(fleet, "publish_mini.py")
    with open(pm_path) as fh:
        src = fh.read()
    new_src = patched_source(src)  # raises before anything is written if it cannot patch
    gdir = os.path.join(fleet, "gate")
    manifest = load_manifest(backup)
    existed = {f["rel"] for f in manifest["files"]}
    os.makedirs(gdir, exist_ok=True)
    added = []
    for g in GATE_FILES:
        dst = os.path.join(gdir, g)
        with open(os.path.join(repo_scripts, g), "rb") as fh:
            atomic_write(dst, fh.read(), 0o644)
        if f"gate/{g}" not in existed:
            added.append(f"gate/{g}")
    atomic_write(pm_path, new_src.encode(), os.stat(pm_path).st_mode & 0o7777)
    manifest["installed_new"] = added
    manifest["installed_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["installed_sha256"] = {g: sha256(os.path.join(gdir, g)) for g in GATE_FILES}
    manifest["installed_sha256"]["publish_mini.py"] = sha256(pm_path)
    manifest["repo_commit"] = repo_commit(repo_scripts)
    with open(os.path.join(backup, "MANIFEST.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(f"installed {len(GATE_FILES)} gate files into {gdir}; patched {pm_path}; backup verified ({n} files)")
    return added


def repo_commit(repo_scripts: str) -> str:
    try:
        return subprocess.run(["git", "-C", repo_scripts, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def cmd_status(fleet: str) -> dict:
    pm = os.path.join(fleet, "publish_mini.py")
    with open(pm) as fh:
        src = fh.read()
    out = {"fleet": fleet, "patched": MARKER in src, "publish_mini_sha256": sha256(pm), "gate": {}}
    for g in GATE_FILES:
        p = os.path.join(fleet, "gate", g)
        out["gate"][g] = sha256(p) if os.path.isfile(p) else None
    return out


def cmd_rollback(fleet: str, backup: str) -> None:
    cmd_verify(backup)  # never restore from a backup that does not match its manifest
    m = load_manifest(backup)
    for f in m["files"]:
        if f["rel"].startswith("verdicts/"):
            continue
        dst = os.path.join(fleet, f["rel"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(backup, f["rel"]), dst)
    for rel in m.get("installed_new", []):
        p = os.path.join(fleet, rel)
        if os.path.exists(p):
            os.unlink(p)
    gdir = os.path.join(fleet, "gate")
    if os.path.isdir(gdir) and not os.listdir(gdir):
        os.rmdir(gdir)
    bad = [f["rel"] for f in m["files"] if not f["rel"].startswith("verdicts/")
           and sha256(os.path.join(fleet, f["rel"])) != f["sha256"]]
    if bad:
        raise CutoverError(f"rollback finished but these files still differ from the backup: {bad}")
    print(f"rolled back: {len(m['files'])} files match the backup; removed {m.get('installed_new', [])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("backup", "verify", "install", "status", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--fleet", default=os.path.expanduser("~/hl-fleet"))
        if name == "backup":
            p.add_argument("--backups", default=os.path.expanduser("~/hl-fleet-backups"))
            p.add_argument("--stamp")
        if name in ("verify", "install", "rollback"):
            p.add_argument("--backup", required=True)
        if name == "install":
            p.add_argument("--repo-scripts", default=os.path.dirname(os.path.abspath(__file__)))
            p.add_argument("--force-running", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "backup":
            print(cmd_backup(a.fleet, a.backups, a.stamp))
        elif a.cmd == "verify":
            print(f"backup verified: {cmd_verify(a.backup)} files match the manifest")
        elif a.cmd == "install":
            cmd_install(a.fleet, a.repo_scripts, a.backup, a.force_running)
        elif a.cmd == "status":
            print(json.dumps(cmd_status(a.fleet), indent=1))
        else:
            cmd_rollback(a.fleet, a.backup)
    except CutoverError as exc:
        print(f"CUTOVER REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
