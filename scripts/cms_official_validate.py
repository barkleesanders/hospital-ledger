#!/usr/bin/env python3
"""Run the official CMS HPT validator and persist a normalized JSON report.

This is intentionally a compliance sidecar. Its result is never used for
extraction decisions in the Hospital Ledger parser.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "data" / "cms_validation"


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned[:120] or "cms_validation"


def validator_command() -> list[str]:
    local = shutil.which("cms-hpt-validator")
    if local:
        return [local]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "@cmsgov/hpt-validator-cli"]
    raise RuntimeError("cms-hpt-validator or npx is required for official CMS validation")


def parse_validator_json(stdout: str) -> dict[str, object]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {"raw_stdout": stdout}
    if isinstance(parsed, dict):
        return parsed
    return {"raw_stdout": stdout, "parsed_stdout": parsed}


def run_validation(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    cmd = validator_command()
    cmd.extend([str(args.file), args.version, "--output", "json", "--error-limit", str(args.error_limit)])
    if args.format:
        cmd.extend(["--format", args.format])

    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    result = parse_validator_json(proc.stdout.strip())
    result.update(
        {
            "ccn": args.ccn,
            "source_url": args.source_url,
            "local_file": str(Path(args.file).resolve()),
            "format": args.format,
            "cms_dictionary_version": args.version,
            "checked_at": now_utc(),
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[:4000],
            "official_validator": "CMSgov/hpt-validator-cli",
            "official_dictionary_repo": "CMSgov/hospital-price-transparency",
            "used_for_extraction": False,
        }
    )
    structured = "valid" in result or "errorCount" in result or "alertCount" in result
    execution_error = proc.returncode != 0 and not structured
    if execution_error:
        result["validator_execution_error"] = True
    return result, execution_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--ccn", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--format", choices=("csv", "json"), default="")
    parser.add_argument("--version", default=os.environ.get("CMS_HPT_VERSION", "v3.0"))
    parser.add_argument("--error-limit", type=int, default=int(os.environ.get("CMS_HPT_ERROR_LIMIT", "25")))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report, execution_error = run_validation(args)
    out_path = args.output_file or args.output_dir / f"{safe_name(args.ccn or args.file.stem)}.json"
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(out_path)
    print(out_path)
    return 1 if execution_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
