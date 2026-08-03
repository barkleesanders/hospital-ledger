#!/usr/bin/env python3
"""Plan and checkpoint an incremental Hospital Ledger refresh.

The planner probes one preferred MRF URL per hospital, compares stable HTTP
validators with the last successful publication, and writes an explicit CCN
work list. Planning is read-only with respect to the parsed corpus. The
checkpoint advances only after publication succeeds.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "hospital_ledger.db"
STATE = ROOT / "data" / "cloud_refresh_state.json"
STAGED = ROOT / "data" / "cloud_refresh_probe.json"
PLAN = ROOT / "data" / "cloud_refresh_plan.json"
CHANGED = ROOT / "data" / "cloud_refresh_changed_ccns.txt"
PARSED = ROOT / "data" / "parsed"

USER_AGENT = "HospitalLedgerBot/1.0 (+https://hospitalledger.com)"
SAMPLE_BYTES = 64 * 1024
PROBE_TIMEOUT_SECONDS = max(5, int(os.environ.get("PROBE_TIMEOUT_SECONDS", "20")))
COMPARE_KEYS = (
    "url",
    "etag",
    "last_modified",
    "content_length",
    "sample_sha256",
)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temp.replace(path)


def write_lines_atomic(path: Path, values: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(f"{value}\n" for value in values))
    temp.replace(path)


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def candidates() -> dict[str, list[str]]:
    """Return ranked known MRF URLs for each CCN, preferred first."""
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    rows: list[tuple[str, str, int, str]] = []
    for row in connection.execute(
        """
        SELECT s.ccn, s.mrf_url AS url,
               CASE WHEN lower(COALESCE(s.mrf_url_status, '')) = 'up' THEN 100 ELSE 0 END AS score,
               MAX(COALESCE(p.probed_at, s.last_updated_date, '')) AS seen
        FROM mrf_seed s
        JOIN hospitals h ON h.ccn=s.ccn
        LEFT JOIN mrf_probe p ON p.ccn=s.ccn AND p.mrf_url=s.mrf_url
        WHERE s.mrf_url IS NOT NULL AND s.mrf_url != ''
          AND h.hospital_type IN
            ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
        GROUP BY s.ccn, s.mrf_url
        """
    ):
        rows.append((str(row["ccn"]), str(row["url"]), int(row["score"]), str(row["seen"])))

    if table_exists(connection, "mrf_rediscovered"):
        for row in connection.execute(
            """
            SELECT r.ccn, r.candidate_url AS url, COALESCE(r.score, 0) + 1000 AS score,
                   COALESCE(r.discovered_at, '') AS seen
            FROM mrf_rediscovered r
            JOIN hospitals h ON h.ccn=r.ccn
            WHERE r.alive=1 AND r.candidate_url IS NOT NULL AND r.candidate_url != ''
              AND h.hospital_type IN
                ('Acute Care Hospitals','Critical Access Hospitals','Childrens','Rural Emergency Hospital')
            """
        ):
            rows.append((str(row["ccn"]), str(row["url"]), int(row["score"]), str(row["seen"])))
    connection.close()

    grouped: dict[str, dict[str, tuple[int, str, str]]] = {}
    for ccn, url, score, seen in rows:
        rank = (score, seen, url)
        previous = grouped.setdefault(ccn, {}).get(url)
        if previous is None or rank > previous:
            grouped[ccn][url] = rank
    return {
        ccn: [url for url, _ in sorted(grouped[ccn].items(), key=lambda item: item[1], reverse=True)]
        for ccn in sorted(grouped)
    }


def content_length(headers: object) -> str | None:
    content_range = headers.get("Content-Range")  # type: ignore[attr-defined]
    if content_range:
        match = re.search(r"/(\d+)$", content_range)
        if match:
            return match.group(1)
    value = headers.get("Content-Length")  # type: ignore[attr-defined]
    return str(value) if value is not None else None


def range_sample(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Range": f"bytes=0-{SAMPLE_BYTES - 1}",
        },
    )
    with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
        body = response.read(SAMPLE_BYTES)
        return {
            "status": int(getattr(response, "status", 200) or 200),
            "final_url": response.geturl(),
            "content_length": content_length(response.headers),
            "sample_sha256": hashlib.sha256(body).hexdigest(),
        }


def probe_url(url: str) -> dict[str, object]:
    result: dict[str, object] = {"url": url, "checked_at": isoformat(now_utc())}
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            result.update(
                {
                    "status": int(getattr(response, "status", 200) or 200),
                    "final_url": response.geturl(),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "content_length": content_length(response.headers),
                }
            )
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            result.update({"status": exc.code, "error": f"HTTP {exc.code}"})
            return result
    except Exception:
        pass

    if not result.get("status"):
        try:
            result.update(range_sample(url))
        except urllib.error.HTTPError as exc:
            result.update({"status": exc.code, "error": f"HTTP {exc.code}"})
        except Exception as exc:
            result.update({"status": 0, "error": type(exc).__name__})
        return result

    if not result.get("etag") and not result.get("last_modified"):
        try:
            sampled = range_sample(str(result.get("final_url") or url))
            result["sample_sha256"] = sampled.get("sample_sha256")
            if not result.get("content_length"):
                result["content_length"] = sampled.get("content_length")
        except Exception as exc:
            result["sample_error"] = type(exc).__name__
    return result


def probe(item: tuple[str, str | list[str]]) -> tuple[str, dict[str, object]]:
    ccn, values = item
    urls = [values] if isinstance(values, str) else values
    failures: list[dict[str, object]] = []
    for url in urls:
        result = probe_url(url)
        if reachable(result):
            if failures:
                result["fallback_attempts"] = len(failures)
            return ccn, result
        failures.append(result)
    if failures:
        result = dict(failures[0])
        result["attempted_urls"] = [str(failure.get("url") or "") for failure in failures]
        return ccn, result
    return ccn, {"url": "", "checked_at": isoformat(now_utc()), "status": 0, "error": "no_url"}


def reachable(metadata: dict[str, object]) -> bool:
    status = int(metadata.get("status") or 0)
    return 200 <= status < 400


def next_force_at(ccn: str, current_time: dt.datetime, days: int, spread_days: int) -> str:
    digest = hashlib.sha256(ccn.encode()).digest()
    jitter = int.from_bytes(digest[:4], "big") % max(spread_days, 1)
    return isoformat(current_time + dt.timedelta(days=days + jitter))


def due_for_forced_refresh(previous: dict[str, object], current_time: dt.datetime) -> bool:
    due = parse_time(previous.get("next_force_at"))
    return due is None or due <= current_time


def update_probe_database(current: dict[str, dict[str, object]]) -> None:
    connection = sqlite3.connect(DB)
    rows = []
    for ccn, metadata in current.items():
        length = metadata.get("content_length")
        rows.append(
            (
                ccn,
                str(metadata.get("url") or ""),
                str(metadata.get("checked_at") or isoformat(now_utc())),
                int(metadata.get("status") or 0),
                "",
                int(length) if str(length or "").isdigit() else None,
                str(metadata.get("final_url") or ""),
                1 if reachable(metadata) else 0,
            )
        )
    connection.executemany(
        """
        INSERT OR REPLACE INTO mrf_probe
          (ccn, mrf_url, probed_at, http_status, content_type,
           content_length, final_url, alive)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    connection.commit()
    connection.close()


def plan_refresh(*, workers: int, force_all: bool = False) -> dict[str, object]:
    previous_payload = read_json(STATE, {"hospitals": {}})
    previous = previous_payload.get("hospitals", {}) if isinstance(previous_payload, dict) else {}
    if not isinstance(previous, dict):
        previous = {}

    candidate_items = list(candidates().items())
    current: dict[str, dict[str, object]] = {}
    print(
        f"probing {len(candidate_items)} candidate hospitals with {workers} workers",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, item): item for item in candidate_items}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            item = futures[future]
            try:
                ccn, metadata = future.result()
            except Exception as exc:
                ccn, url = item
                metadata = {
                    "url": url,
                    "checked_at": isoformat(now_utc()),
                    "status": 0,
                    "error": type(exc).__name__,
                }
            current[ccn] = metadata
            if index % 100 == 0 or index == len(candidate_items):
                up = sum(1 for value in current.values() if reachable(value))
                print(
                    f"probe progress {index}/{len(candidate_items)} reachable={up}",
                    flush=True,
                )
    update_probe_database(current)

    timestamp = now_utc()
    changed: list[str] = []
    reasons: dict[str, str] = {}
    unavailable = 0
    for ccn in sorted(current):
        metadata = current[ccn]
        old = previous.get(ccn)
        if not reachable(metadata):
            unavailable += 1
            continue
        if not isinstance(old, dict):
            changed.append(ccn)
            reasons[ccn] = "new"
            continue
        differing = [key for key in COMPARE_KEYS if metadata.get(key) != old.get(key)]
        if differing:
            changed.append(ccn)
            reasons[ccn] = "validator:" + ",".join(differing)
        elif force_all or due_for_forced_refresh(old, timestamp):
            changed.append(ccn)
            reasons[ccn] = "forced"

    staged = {
        "generated_at": isoformat(timestamp),
        "hospitals": current,
    }
    plan = {
        "generated_at": staged["generated_at"],
        "candidate_count": len(candidate_items),
        "probed": len(current),
        "reachable": len(current) - unavailable,
        "unavailable": unavailable,
        "changed": changed,
        "reasons": reasons,
    }
    write_json_atomic(STAGED, staged)
    write_json_atomic(PLAN, plan)
    write_lines_atomic(CHANGED, changed)
    return plan


def load_ccns(path: Path) -> set[str]:
    try:
        return {line.strip() for line in path.read_text().splitlines() if line.strip()}
    except OSError:
        return set()


def commit_state(
    *,
    processed_file: Path | None,
    force_after_days: int,
    force_spread_days: int,
) -> dict[str, object]:
    staged_payload = read_json(STAGED, None)
    plan_payload = read_json(PLAN, None)
    if not isinstance(staged_payload, dict) or not isinstance(plan_payload, dict):
        raise SystemExit("missing staged probe or refresh plan")
    current = staged_payload.get("hospitals")
    changed_list = plan_payload.get("changed")
    if not isinstance(current, dict) or not isinstance(changed_list, list):
        raise SystemExit("invalid staged probe or refresh plan")

    previous_payload = read_json(STATE, {"hospitals": {}})
    previous = previous_payload.get("hospitals", {}) if isinstance(previous_payload, dict) else {}
    if not isinstance(previous, dict):
        previous = {}
    changed = {str(ccn) for ccn in changed_list}
    processed = load_ccns(processed_file) if processed_file else changed
    timestamp = now_utc()
    committed: dict[str, dict[str, object]] = {}

    for ccn, metadata_obj in current.items():
        if not isinstance(metadata_obj, dict):
            continue
        metadata = dict(metadata_obj)
        old = previous.get(ccn)
        old = dict(old) if isinstance(old, dict) else {}

        if ccn in changed and ccn not in processed:
            if old:
                old["last_attempt_at"] = isoformat(timestamp)
                old["last_attempt_error"] = "ingest_not_published"
                committed[ccn] = old
            continue

        if not reachable(metadata) and old:
            old["last_checked_at"] = isoformat(timestamp)
            old["last_probe_status"] = metadata.get("status")
            committed[ccn] = old
            continue

        merged = old | metadata
        merged["last_checked_at"] = isoformat(timestamp)
        if ccn in processed:
            merged["last_processed_at"] = isoformat(timestamp)
            merged["next_force_at"] = next_force_at(
                ccn,
                timestamp,
                force_after_days,
                force_spread_days,
            )
            merged.pop("last_attempt_error", None)
        committed[ccn] = merged

    # Keep a previously published hospital checkpoint when it temporarily
    # disappears from the current seed/candidate set. Dropping it here would
    # turn a discovery regression into a false "new hospital" on the next run.
    for ccn, old in previous.items():
        if ccn not in committed and isinstance(old, dict):
            committed[ccn] = dict(old)

    payload = {
        "generated_at": isoformat(timestamp),
        "hospitals": committed,
    }
    write_json_atomic(STATE, payload)
    return {
        "saved": len(committed),
        "processed": len(processed & changed),
        "deferred": len(changed - processed),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=min(32, (os.cpu_count() or 4) * 4))
    parser.add_argument("--force-all", action="store_true")
    parser.add_argument("--commit", action="store_true", help="advance state after successful publication")
    parser.add_argument("--processed-file", type=Path, help="newline-delimited successfully published CCNs")
    parser.add_argument("--force-after-days", type=int, default=35)
    parser.add_argument("--force-spread-days", type=int, default=28)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    if args.commit:
        result = commit_state(
            processed_file=args.processed_file,
            force_after_days=args.force_after_days,
            force_spread_days=args.force_spread_days,
        )
        print(
            f"saved={result['saved']} processed={result['processed']} "
            f"deferred={result['deferred']}"
        )
        return 0

    plan = plan_refresh(workers=args.workers, force_all=args.force_all)
    print(
        f"probed={plan['probed']} reachable={plan['reachable']} "
        f"unavailable={plan['unavailable']} changed={len(plan['changed'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
