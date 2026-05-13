#!/usr/bin/env python3
"""Watch generated price files and validate source MRFs with CMS's official CLI."""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


ROOT = Path(__file__).resolve().parents[1]
PRICE_DIR = ROOT / "site" / "data" / "prices"
VALIDATION_DIR = ROOT / "data" / "cms_validation"
STATUS_FILE = ROOT / "data" / "cms_validation_monitor.status.json"
LOG_FILE = ROOT / "data" / "cms_validation_monitor.log"
DEFAULT_OPENCLAW_TARGET = "8335979324"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


class SourceTooLarge(RuntimeError):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def append_log(line: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as handle:
        handle.write(f"[{now_utc()}] {line}\n")


def write_status(**payload: object) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    status = {"updated_at": now_utc(), "host": socket.gethostname(), **payload}
    tmp = STATUS_FILE.with_suffix(STATUS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATUS_FILE)


def notify(message: str, *, target: str, dry_run: bool) -> None:
    candidates = ["/opt/homebrew/bin/openclaw", shutil.which("openclaw") or ""]
    openclaw = next((path for path in candidates if path and Path(path).exists()), "")
    if not openclaw:
        append_log(f"openclaw not available; notification skipped: {message}")
        return
    cmd = [
        openclaw,
        "message",
        "send",
        "--channel",
        "telegram",
        "--target",
        target,
        "--message",
        message[:3900],
    ]
    append_log("$ " + " ".join(cmd))
    if dry_run:
        return
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        append_log(f"openclaw notify failed: {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}")


def public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved)


def cms_format(format_hint: str, source_url: str) -> str:
    lower = f"{format_hint} {source_url}".lower()
    if "json" in lower:
        return "json"
    if any(token in lower for token in ("csv", "tsv", "txt")):
        return "csv"
    return ""


def extension_for(format_name: str, source_url: str) -> str:
    lower = source_url.lower()
    if lower.endswith(".gz") or ".gz?" in lower:
        return ".gz"
    if format_name == "json":
        return ".json"
    return ".csv"


def download_source(url: str, dest: Path, max_bytes: int) -> tuple[int, str]:
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=120.0, verify=False) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length") or 0)
            if content_length > max_bytes:
                raise SourceTooLarge(f"content-length {content_length} > {max_bytes}")
            total = 0
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceTooLarge(f"streamed {total} > {max_bytes}")
                    handle.write(chunk)
            return total, str(response.url)


def load_price(path: Path) -> dict[str, object] | None:
    try:
        with path.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def report_valid(report_path: Path) -> bool | None:
    try:
        with report_path.open() as handle:
            report = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    value = report.get("valid")
    return value if isinstance(value, bool) else None


def validate_price_file(price_path: Path, args: argparse.Namespace) -> str:
    data = load_price(price_path)
    if not data:
        return "skip:unreadable-price"
    ccn = str(data.get("ccn") or price_path.stem)
    source_url = str(data.get("source_url") or "")
    format_name = cms_format(str(data.get("format") or ""), source_url)
    report_path = VALIDATION_DIR / f"{ccn}.json"
    if report_path.exists() and not args.revalidate:
        return "skip:already-validated"
    if not source_url:
        return "skip:no-source-url"
    if not public_http_url(source_url):
        return "skip:unsafe-source-url"
    if format_name not in ("csv", "json"):
        return f"skip:unsupported-format:{data.get('format') or ''}"
    if args.dry_run:
        return "skip:dry-run-ready"

    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    suffix = extension_for(format_name, source_url)
    with tempfile.NamedTemporaryFile(prefix=f"cms-{ccn}-", suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            size, final_url = download_source(source_url, tmp_path, args.max_bytes)
        except SourceTooLarge as exc:
            append_log(f"skipped {ccn}: source too large for CMS validator sidecar: {exc}")
            return "skip:source-too-large"
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "cms_official_validate.py"),
            str(tmp_path),
            "--ccn",
            ccn,
            "--source-url",
            final_url,
            "--format",
            format_name,
            "--version",
            args.version,
            "--error-limit",
            str(args.error_limit),
            "--output-file",
            str(report_path),
        ]
        append_log("$ " + " ".join(cmd))
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            append_log(f"validator wrapper failed for {ccn}: {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:1000]}")
            return "error:validator"
        valid = report_valid(report_path)
        append_log(f"validated {ccn}: valid={valid} bytes={size} report={report_path}")
        return "valid" if valid is True else "invalid"
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def price_files() -> list[Path]:
    if not PRICE_DIR.exists():
        return []
    return sorted(
        [path for path in PRICE_DIR.glob("*.json") if path.name != "index.json"],
        key=lambda path: path.stat().st_mtime,
    )


def run_once(args: argparse.Namespace, totals: dict[str, int]) -> dict[str, int]:
    files = price_files()
    for price_path in files:
        ccn = price_path.stem
        if args.limit and totals["checked"] >= args.limit:
            break
        try:
            result = validate_price_file(price_path, args)
        except Exception as exc:
            result = f"error:{type(exc).__name__}"
            append_log(f"{ccn}: {result}: {exc}")
        totals["checked"] += 1
        if result == "valid":
            totals["valid"] += 1
        elif result == "invalid":
            totals["invalid"] += 1
            if args.notify_invalid:
                notify(f"CMS HPT validation found non-compliant file for CCN {ccn}. Report: {VALIDATION_DIR / (ccn + '.json')}", target=args.notify_target, dry_run=args.dry_run)
        elif result.startswith("error:"):
            totals["errors"] += 1
            if args.notify_errors:
                notify(f"CMS HPT validation monitor error for CCN {ccn}: {result}. Log: {LOG_FILE}", target=args.notify_target, dry_run=args.dry_run)
        else:
            totals["skipped"] += 1
        write_status(phase="watching", last_ccn=ccn, last_result=result, price_files=len(files), **totals)
    return totals


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=int(os.environ.get("CMS_VALIDATE_POLL_SECONDS", "60")))
    parser.add_argument("--max-bytes", type=int, default=int(os.environ.get("CMS_VALIDATE_MAX_BYTES", str(500 * 1024 * 1024))))
    parser.add_argument("--version", default=os.environ.get("CMS_HPT_VERSION", "v3.0"))
    parser.add_argument("--error-limit", type=int, default=int(os.environ.get("CMS_HPT_ERROR_LIMIT", "25")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("CMS_VALIDATE_LIMIT", "0")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--revalidate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notify-errors", action="store_true", default=os.environ.get("CMS_VALIDATE_NOTIFY_ERRORS", "1") != "0")
    parser.add_argument("--notify-invalid", action="store_true", default=os.environ.get("CMS_VALIDATE_NOTIFY_INVALID", "0") == "1")
    parser.add_argument("--notify-target", default=os.environ.get("OPENCLAW_NOTIFY_TARGET", DEFAULT_OPENCLAW_TARGET))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    totals = {"checked": 0, "valid": 0, "invalid": 0, "skipped": 0, "errors": 0}
    write_status(phase="starting", **totals)
    append_log("starting CMS validation monitor")
    while True:
        run_once(args, totals)
        if args.once:
            break
        time.sleep(max(5, args.poll_seconds))
    write_status(phase="done", **totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
