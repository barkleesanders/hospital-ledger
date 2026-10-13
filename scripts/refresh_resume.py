#!/usr/bin/env python3
"""Create and validate crash-resumable refresh staging manifests.

The immutable plan inputs and each completed parsed checkpoint live beneath a
unique R2 staging prefix.  This helper deliberately has no R2 dependency: the
shell runner transfers the files, while this module makes their content and
resume semantics deterministic and testable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
CCN_RE = re.compile(r"^[0-9]{6}$")
RESUME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INPUTS = {
    "probe.json": Path("data/cloud_refresh_probe.json"),
    "plan.json": Path("data/cloud_refresh_plan.json"),
    "worklist.json": Path("data/cloud_refresh_worklist.json"),
    "changed-ccns.txt": Path("data/cloud_refresh_changed_ccns.txt"),
    "hospital-ledger.db": Path("db/hospital_ledger.db"),
}
FALLBACK_REPOSITORY_PATHS = (
    "scripts",
    "seed",
    "src",
    ".github/workflows",
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "requirements-fast.txt",
    "data/hospital_ledger_scoreboard.csv",
    "data/coverage_terminal_exceptions.json",
    "tsconfig.json",
    "vite.config.ts",
    "wrangler.jsonc",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


def write_lines_atomic(path: Path, values: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(f"{value}\n" for value in values))
    temporary.replace(path)


def link_or_copy_atomic(source: Path, destination: Path) -> None:
    """Install a local checkpoint without duplicating multi-gigabyte staging."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        os.link(source, temporary)
    except OSError:
        shutil.copy2(source, temporary)
    temporary.replace(destination)


def read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON file must contain an object: {path}")
    return payload


def read_ccns(path: Path) -> list[str]:
    try:
        values = sorted({line.strip() for line in path.read_text().splitlines() if line.strip()})
    except OSError as exc:
        raise SystemExit(f"cannot read CCN list {path}: {exc}") from exc
    invalid = [value for value in values if not CCN_RE.fullmatch(value)]
    if invalid:
        raise SystemExit(f"invalid CCNs in {path}: {', '.join(invalid[:5])}")
    return values


def repository_digest(root: Path) -> str:
    override = os.environ.get("HOSPITAL_LEDGER_REPOSITORY_DIGEST", "").strip()
    if override:
        return override
    paths: list[Path] = []
    for relative in FALLBACK_REPOSITORY_PATHS:
        candidate = root / relative
        if candidate.is_file():
            paths.append(candidate)
        elif candidate.is_dir():
            paths.extend(
                path
                for path in candidate.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            )
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return f"content:{digest.hexdigest()}"


def plan_digest(inputs: dict[str, dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for name in sorted(inputs):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(inputs[name]["sha256"]).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_input_relationships(input_dir: Path, changed: list[str]) -> None:
    plan = read_json(input_dir / "plan.json")
    worklist = read_json(input_dir / "worklist.json")
    planned = plan.get("changed")
    hospitals = worklist.get("hospitals")
    if not isinstance(planned, list) or sorted(str(value) for value in planned) != changed:
        raise SystemExit("plan changed list does not match changed-ccns.txt")
    if not isinstance(hospitals, dict) or sorted(str(value) for value in hospitals) != changed:
        raise SystemExit("worklist hospital keys do not match changed-ccns.txt")
    probe = read_json(input_dir / "probe.json")
    if not isinstance(probe.get("hospitals"), dict):
        raise SystemExit("probe input has no hospitals object")


def initialize(root: Path, resume_dir: Path, resume_id: str) -> dict[str, object]:
    if not RESUME_ID_RE.fullmatch(resume_id):
        raise SystemExit(f"invalid resume ID: {resume_id}")
    if resume_dir.exists() and any(resume_dir.iterdir()):
        raise SystemExit(f"resume directory is not empty: {resume_dir}")
    input_dir = resume_dir / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, dict[str, object]] = {}
    for name, relative in INPUTS.items():
        source = root / relative
        if not source.is_file():
            raise SystemExit(f"missing refresh input: {source}")
        destination = input_dir / name
        shutil.copy2(source, destination)
        metadata[name] = {"sha256": sha256_file(destination), "size": destination.stat().st_size}
    changed = read_ccns(input_dir / "changed-ccns.txt")
    validate_input_relationships(input_dir, changed)
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "resume_id": resume_id,
        "repository_digest": repository_digest(root),
        "plan_digest": plan_digest(metadata),
        "total": len(changed),
        "inputs": metadata,
    }
    write_json_atomic(resume_dir / "resume.json", payload)
    write_json_atomic(
        resume_dir / "completed.json",
        {
            "schema_version": SCHEMA_VERSION,
            "resume_id": resume_id,
            "repository_digest": payload["repository_digest"],
            "plan_digest": payload["plan_digest"],
            "completed": [],
            "artifacts": {},
        },
    )
    write_json_atomic(
        resume_dir / "ready.json",
        {
            "schema_version": SCHEMA_VERSION,
            "resume_id": resume_id,
            "repository_digest": payload["repository_digest"],
            "plan_digest": payload["plan_digest"],
        },
    )
    return payload


def validate_resume(
    root: Path,
    resume_dir: Path,
) -> tuple[dict[str, object], dict[str, object], list[str], list[str]]:
    resume = read_json(resume_dir / "resume.json")
    completed_payload = read_json(resume_dir / "completed.json")
    ready_payload = read_json(resume_dir / "ready.json")
    if resume.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit("unsupported refresh resume schema")
    resume_id = str(resume.get("resume_id") or "")
    if not RESUME_ID_RE.fullmatch(resume_id):
        raise SystemExit("refresh resume has an invalid resume ID")
    expected_repository = str(resume.get("repository_digest") or "")
    current_repository = repository_digest(root)
    if expected_repository != current_repository:
        raise SystemExit(
            "refresh resume repository digest mismatch: "
            f"staged={expected_repository} current={current_repository}"
        )
    input_metadata = resume.get("inputs")
    if not isinstance(input_metadata, dict) or sorted(input_metadata) != sorted(INPUTS):
        raise SystemExit("refresh resume input manifest is invalid")
    normalized_metadata: dict[str, dict[str, object]] = {}
    for name, raw in input_metadata.items():
        if not isinstance(raw, dict):
            raise SystemExit(f"refresh input metadata is invalid: {name}")
        path = resume_dir / "inputs" / name
        expected_sha = str(raw.get("sha256") or "")
        expected_size = int(raw.get("size") or -1)
        if not path.is_file() or path.stat().st_size != expected_size or sha256_file(path) != expected_sha:
            raise SystemExit(f"refresh input failed digest validation: {name}")
        normalized_metadata[name] = {"sha256": expected_sha, "size": expected_size}
    expected_plan = plan_digest(normalized_metadata)
    if str(resume.get("plan_digest") or "") != expected_plan:
        raise SystemExit("refresh resume plan digest mismatch")
    for field in ("schema_version", "resume_id", "repository_digest", "plan_digest"):
        if ready_payload.get(field) != resume.get(field):
            raise SystemExit(f"refresh ready marker {field} does not match refresh resume")
    changed = read_ccns(resume_dir / "inputs" / "changed-ccns.txt")
    validate_input_relationships(resume_dir / "inputs", changed)
    if int(resume.get("total") or 0) != len(changed):
        raise SystemExit("refresh resume total does not match its changed list")

    for field in ("resume_id", "repository_digest", "plan_digest"):
        if completed_payload.get(field) != resume.get(field):
            raise SystemExit(f"completed manifest {field} does not match refresh resume")
    completed_raw = completed_payload.get("completed")
    artifacts = completed_payload.get("artifacts")
    if not isinstance(completed_raw, list) or not isinstance(artifacts, dict):
        raise SystemExit("completed manifest is invalid")
    completed = sorted({str(value) for value in completed_raw})
    if len(completed) != len(completed_raw) or not set(completed).issubset(changed):
        raise SystemExit("completed manifest contains invalid or duplicate CCNs")
    if sorted(str(value) for value in artifacts) != completed:
        raise SystemExit("completed manifest artifact keys do not match completed CCNs")
    for ccn in completed:
        raw = artifacts.get(ccn)
        if not isinstance(raw, dict):
            raise SystemExit(f"completed artifact metadata is invalid: {ccn}")
        path = resume_dir / "parsed" / f"{ccn}.json.gz"
        if (
            not path.is_file()
            or path.stat().st_size != int(raw.get("size") or -1)
            or sha256_file(path) != str(raw.get("sha256") or "")
        ):
            raise SystemExit(f"completed parsed checkpoint failed validation: {ccn}")
    remaining = sorted(set(changed) - set(completed))
    return resume, completed_payload, completed, remaining


def restore(root: Path, resume_dir: Path, completed_file: Path, remaining_file: Path) -> dict[str, int]:
    resume, _, completed, remaining = validate_resume(root, resume_dir)
    for name, relative in INPUTS.items():
        source = resume_dir / "inputs" / name
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    parsed = root / "data" / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    for ccn in completed:
        source = resume_dir / "parsed" / f"{ccn}.json.gz"
        destination = parsed / source.name
        link_or_copy_atomic(source, destination)
    write_lines_atomic(completed_file, completed)
    write_lines_atomic(remaining_file, remaining)
    return {"total": int(resume["total"]), "completed": len(completed), "remaining": len(remaining)}


def complete(root: Path, resume_dir: Path, additions_file: Path) -> dict[str, int]:
    resume, completed_payload, completed, _ = validate_resume(root, resume_dir)
    changed = read_ccns(resume_dir / "inputs" / "changed-ccns.txt")
    additions = read_ccns(additions_file)
    if not set(additions).issubset(changed):
        raise SystemExit("completed additions are not a subset of the immutable changed list")
    parsed_dir = resume_dir / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    artifacts = dict(completed_payload.get("artifacts") or {})
    for ccn in additions:
        source = root / "data" / "parsed" / f"{ccn}.json.gz"
        if not source.is_file() or source.stat().st_size <= 0:
            raise SystemExit(f"missing parsed checkpoint for completed CCN: {ccn}")
        destination = parsed_dir / source.name
        link_or_copy_atomic(source, destination)
        artifacts[ccn] = {"sha256": sha256_file(destination), "size": destination.stat().st_size}
    merged = sorted(set(completed) | set(additions))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "resume_id": resume["resume_id"],
        "repository_digest": resume["repository_digest"],
        "plan_digest": resume["plan_digest"],
        "completed": merged,
        "artifacts": {ccn: artifacts[ccn] for ccn in merged},
    }
    write_json_atomic(resume_dir / "completed.json", payload)
    validate_resume(root, resume_dir)
    return {"total": int(resume["total"]), "completed": len(merged)}


def resumable_status(status_file: Path) -> str:
    try:
        payload = read_json(status_file)
    except SystemExit:
        return ""
    resume_id = str(payload.get("resume_id") or "")
    status = str(payload.get("status") or "")
    phase = str(payload.get("phase") or "")
    final = {"published", "committed", "no_changes", "recovered"}
    if RESUME_ID_RE.fullmatch(resume_id) and status not in final and phase not in {"committed", "complete"}:
        return resume_id
    return ""


def completed_staging_status(status_file: Path) -> str:
    try:
        payload = read_json(status_file)
    except SystemExit:
        return ""
    resume_id = str(payload.get("resume_id") or "")
    status = str(payload.get("status") or "")
    phase = str(payload.get("phase") or "")
    if RESUME_ID_RE.fullmatch(resume_id) and (
        status in {"committed", "published"} or phase in {"committed", "complete"}
    ):
        return resume_id
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("resume_dir", type=Path)
    init_parser.add_argument("resume_id")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("resume_dir", type=Path)
    restore_parser.add_argument("completed_file", type=Path)
    restore_parser.add_argument("remaining_file", type=Path)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("resume_dir", type=Path)
    complete_parser.add_argument("additions_file", type=Path)

    status_parser = subparsers.add_parser("resume-id")
    status_parser.add_argument("status_file", type=Path)

    cleanup_parser = subparsers.add_parser("cleanup-id")
    cleanup_parser.add_argument("status_file", type=Path)

    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "init":
        result = initialize(root, args.resume_dir, args.resume_id)
        print(json.dumps({"resume_id": result["resume_id"], "total": result["total"]}, sort_keys=True))
    elif args.command == "restore":
        print(json.dumps(restore(root, args.resume_dir, args.completed_file, args.remaining_file), sort_keys=True))
    elif args.command == "complete":
        print(json.dumps(complete(root, args.resume_dir, args.additions_file), sort_keys=True))
    elif args.command == "resume-id":
        print(resumable_status(args.status_file))
    elif args.command == "cleanup-id":
        print(completed_staging_status(args.status_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
