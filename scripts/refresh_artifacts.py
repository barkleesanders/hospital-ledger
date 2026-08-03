#!/usr/bin/env python3
"""Manage refresh backups, success lists, and explicit publication keys."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PARSED = ROOT / "data" / "parsed"
PRICES = ROOT / "public" / "data" / "prices"
PUBLIC_DATA = ROOT / "public" / "data"
ROW_COUNT_RE = re.compile(rb'"row_count"\s*:\s*(\d+)')


def read_ccns(path: Path) -> list[str]:
    return sorted({line.strip() for line in path.read_text().splitlines() if line.strip()})


def write_lines(path: Path, values: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(f"{value}\n" for value in values))
    temporary.replace(path)


def link_or_copy(source: str | Path, destination: str | Path) -> str:
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source_path, destination_path)
    except OSError:
        shutil.copy2(source_path, destination_path)
    return str(destination_path)


def copy_if_present(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def link_if_present(source: Path, destination: Path) -> None:
    if source.is_file():
        link_or_copy(source, destination)


def backup(ccns_file: Path, backup_dir: Path) -> None:
    for ccn in read_ccns(ccns_file):
        # batch_ingest unlinks parsed records before writing replacements, so a
        # hard link safely preserves the old inode without duplicating gigabytes.
        link_if_present(PARSED / f"{ccn}.json", backup_dir / "parsed" / f"{ccn}.json")
        link_if_present(PARSED / f"{ccn}.json.gz", backup_dir / "parsed" / f"{ccn}.json.gz")
        # slim_parsed overwrites compact JSON in place, so these must be copies.
        copy_if_present(PRICES / f"{ccn}.json", backup_dir / "prices" / f"{ccn}.json")
    copy_if_present(PRICES / "index.json", backup_dir / "prices" / "index.json")


def parsed_row_count(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            match = ROW_COUNT_RE.search(handle.read(4096))
    except OSError:
        return 0
    return int(match.group(1)) if match else 0


def classify_parsed(ccns_file: Path, output_file: Path) -> list[str]:
    success = [ccn for ccn in read_ccns(ccns_file) if parsed_row_count(PARSED / f"{ccn}.json") > 0]
    write_lines(output_file, success)
    return success


def classify_display(ccns_file: Path, output_file: Path) -> list[str]:
    success: list[str] = []
    for ccn in read_ccns(ccns_file):
        try:
            payload = json.loads((PRICES / f"{ccn}.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and int(payload.get("n_slim") or 0) > 0:
            success.append(ccn)
    write_lines(output_file, success)
    return success


def restore_except(planned_file: Path, keep_file: Path, backup_dir: Path) -> list[str]:
    keep = set(read_ccns(keep_file))
    restored: list[str] = []
    for ccn in read_ccns(planned_file):
        if ccn in keep:
            continue
        for current in (PARSED / f"{ccn}.json", PARSED / f"{ccn}.json.gz", PRICES / f"{ccn}.json"):
            current.unlink(missing_ok=True)
        for relative in (
            Path("parsed") / f"{ccn}.json",
            Path("parsed") / f"{ccn}.json.gz",
            Path("prices") / f"{ccn}.json",
        ):
            destination = ROOT / "data" / relative if relative.parts[0] == "parsed" else PUBLIC_DATA / relative
            if relative.parts[0] == "parsed":
                link_if_present(backup_dir / relative, destination)
            else:
                copy_if_present(backup_dir / relative, destination)
        restored.append(ccn)
    index_backup = backup_dir / "prices" / "index.json"
    copy_if_present(index_backup, PRICES / "index.json")
    return restored


def backup_public(backup_dir: Path) -> None:
    for name in (
        "summary.json",
        "hospitals.json",
        "cpt-index.json",
        "compliance-ranking.json",
        "payers-index.json",
    ):
        copy_if_present(PUBLIC_DATA / name, backup_dir / name)
    for directory in ("payer", "cpt-detail"):
        source = PUBLIC_DATA / directory
        if source.is_dir():
            # reset_aggregate_dirs unlinks originals before rebuilding. Hard
            # links therefore preserve a cheap baseline for file-count checks.
            shutil.copytree(
                source,
                backup_dir / directory,
                dirs_exist_ok=True,
                copy_function=link_or_copy,
            )


def reset_aggregate_dirs() -> None:
    for directory in (PUBLIC_DATA / "payer", PUBLIC_DATA / "cpt-detail"):
        directory.mkdir(parents=True, exist_ok=True)
        for path in directory.glob("*.json"):
            path.unlink()


def publication_keys(success_file: Path, output_file: Path) -> list[str]:
    keys = {
        "prices/index.json",
        "indexes/cpt-index.json",
        "aggregates/compliance-ranking.json",
        "aggregates/payers-index.json",
    }
    keys.update(f"prices/{ccn}.json" for ccn in read_ccns(success_file))
    keys.update(f"aggregates/payer/{path.name}" for path in (PUBLIC_DATA / "payer").glob("*.json"))
    keys.update(
        f"aggregates/cpt-detail/{path.name}" for path in (PUBLIC_DATA / "cpt-detail").glob("*.json")
    )
    values = sorted(keys)
    write_lines(output_file, values)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("ccns_file", type=Path)
    backup_parser.add_argument("backup_dir", type=Path)

    parsed_parser = subparsers.add_parser("classify-parsed")
    parsed_parser.add_argument("ccns_file", type=Path)
    parsed_parser.add_argument("output_file", type=Path)

    display_parser = subparsers.add_parser("classify-display")
    display_parser.add_argument("ccns_file", type=Path)
    display_parser.add_argument("output_file", type=Path)

    restore_parser = subparsers.add_parser("restore-except")
    restore_parser.add_argument("planned_file", type=Path)
    restore_parser.add_argument("keep_file", type=Path)
    restore_parser.add_argument("backup_dir", type=Path)

    public_parser = subparsers.add_parser("backup-public")
    public_parser.add_argument("backup_dir", type=Path)

    subparsers.add_parser("reset-aggregate-dirs")

    keys_parser = subparsers.add_parser("publication-keys")
    keys_parser.add_argument("success_file", type=Path)
    keys_parser.add_argument("output_file", type=Path)

    args = parser.parse_args()
    if args.command == "backup":
        backup(args.ccns_file, args.backup_dir)
    elif args.command == "classify-parsed":
        values = classify_parsed(args.ccns_file, args.output_file)
        print(f"parsed_success={len(values)}")
    elif args.command == "classify-display":
        values = classify_display(args.ccns_file, args.output_file)
        print(f"display_success={len(values)}")
    elif args.command == "restore-except":
        values = restore_except(args.planned_file, args.keep_file, args.backup_dir)
        print(f"restored={len(values)}")
    elif args.command == "backup-public":
        backup_public(args.backup_dir)
    elif args.command == "reset-aggregate-dirs":
        reset_aggregate_dirs()
    elif args.command == "publication-keys":
        values = publication_keys(args.success_file, args.output_file)
        print(f"publication_keys={len(values)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
