#!/usr/bin/env python3
"""Concurrent Cloudflare R2 state transfer for ephemeral refresh workers."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # Pure transfer helpers remain testable without R2 dependencies.
    boto3 = None
    Config = None

    class ClientError(Exception):
        def __init__(self, error_response: dict[str, object], operation_name: str):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(f"{operation_name}: {error_response}")


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing {name}")
    return value


def make_client(workers: int):
    if boto3 is None or Config is None:
        raise SystemExit("missing boto3; install requirements.txt before using R2")
    account_id = required_env("R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            max_pool_connections=max(workers * 2, 16),
            retries={"mode": "adaptive", "max_attempts": 10},
        ),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_objects(client, bucket: str, prefix: str) -> list[dict[str, object]]:
    paginator = client.get_paginator("list_objects_v2")
    objects: list[dict[str, object]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    return [item for item in objects if not str(item.get("Key", "")).endswith("/")]


def list_keys(client, *, bucket: str, prefixes: list[str]) -> list[str]:
    keys: set[str] = set()
    for prefix in prefixes:
        keys.update(str(item["Key"]) for item in list_objects(client, bucket, prefix))
    return sorted(keys)


def download_one(
    client,
    bucket: str,
    key: str,
    destination: Path,
    expected_size: int,
    expected_sha256: str | None = None,
) -> str:
    if (
        destination.exists()
        and destination.stat().st_size == expected_size
        and expected_sha256
        and sha256_file(destination) == expected_sha256
    ):
        return "unchanged"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    client.download_file(bucket, key, str(temporary))
    temporary.replace(destination)
    return "downloaded"


def download_prefix(
    client,
    *,
    bucket: str,
    prefix: str,
    destination: Path,
    workers: int,
    optional: bool,
) -> dict[str, int]:
    objects = list_objects(client, bucket, prefix)
    if not objects:
        if optional:
            print(f"optional prefix is empty: r2://{bucket}/{prefix}")
            return {"objects": 0, "downloaded": 0, "unchanged": 0}
        raise SystemExit(f"required prefix is empty: r2://{bucket}/{prefix}")

    counts = {"objects": len(objects), "downloaded": 0, "unchanged": 0}

    def transfer(item: dict[str, object]) -> str:
        key = str(item["Key"])
        relative = key[len(prefix):].lstrip("/")
        head = client.head_object(Bucket=bucket, Key=key)
        metadata = head.get("Metadata") or {}
        return download_one(
            client,
            bucket,
            key,
            destination / relative,
            int(item.get("Size") or 0),
            str(metadata.get("sha256") or "") or None,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for index, result in enumerate(pool.map(transfer, objects), start=1):
            counts[result] += 1
            if index % 100 == 0 or index == len(objects):
                print(
                    f"download {index}/{len(objects)} "
                    f"new={counts['downloaded']} unchanged={counts['unchanged']}",
                    flush=True,
                )
    return counts


def download_file(
    client,
    *,
    bucket: str,
    key: str,
    destination: Path,
    optional: bool,
) -> str:
    try:
        metadata = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if optional and code in {"404", "NoSuchKey", "NotFound"}:
            print(f"optional object is absent: r2://{bucket}/{key}")
            return "absent"
        raise
    result = download_one(
        client,
        bucket,
        key,
        destination,
        int(metadata.get("ContentLength") or 0),
        str((metadata.get("Metadata") or {}).get("sha256") or "") or None,
    )
    print(f"{result}: r2://{bucket}/{key} -> {destination}")
    return result


def remote_matches(client, bucket: str, key: str, size: int, digest: str) -> bool:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    metadata = head.get("Metadata") or {}
    return int(head.get("ContentLength") or -1) == size and metadata.get("sha256") == digest


def upload_one(client, bucket: str, key: str, source: Path) -> str:
    size = source.stat().st_size
    digest = sha256_file(source)
    if remote_matches(client, bucket, key, size, digest):
        return "unchanged"
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    client.upload_file(
        str(source),
        bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "Metadata": {"sha256": digest},
        },
    )
    return "uploaded"


def upload_paths(
    client,
    *,
    bucket: str,
    prefix: str,
    root: Path,
    paths: list[Path],
    workers: int,
) -> dict[str, int]:
    existing = [path for path in paths if path.is_file()]
    counts = {
        "requested": len(paths),
        "missing": len(paths) - len(existing),
        "uploaded": 0,
        "unchanged": 0,
    }

    def transfer(path: Path) -> str:
        relative = path.relative_to(root).as_posix()
        key = f"{prefix.rstrip('/')}/{relative}" if prefix else relative
        return upload_one(client, bucket, key, path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(transfer, path): path for path in existing}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            counts[result] += 1
            if index % 50 == 0 or index == len(existing):
                print(
                    f"upload {index}/{len(existing)} "
                    f"new={counts['uploaded']} unchanged={counts['unchanged']}",
                    flush=True,
                )
    if counts["missing"]:
        raise SystemExit(f"{counts['missing']} requested upload files are missing")
    return counts


def paths_from_ccns(root: Path, ccns_file: Path, extension: str) -> list[Path]:
    ccns = [line.strip() for line in ccns_file.read_text().splitlines() if line.strip()]
    return [root / f"{ccn}{extension}" for ccn in ccns]


def is_missing(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def read_keys(path: Path) -> list[str]:
    return sorted({line.strip().lstrip("/") for line in path.read_text().splitlines() if line.strip()})


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


def snapshot_keys(
    client,
    *,
    bucket: str,
    snapshot_prefix: str,
    keys: list[str],
    manifest_path: Path,
    workers: int,
) -> dict[str, int]:
    prefix = snapshot_prefix.strip("/")

    def snapshot(key: str) -> dict[str, object]:
        snapshot_key = f"{prefix}/{key}"
        try:
            client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if is_missing(exc):
                return {"key": key, "existed": False}
            raise
        client.copy_object(
            Bucket=bucket,
            Key=snapshot_key,
            CopySource={"Bucket": bucket, "Key": key},
            MetadataDirective="COPY",
        )
        return {"key": key, "existed": True, "snapshot_key": snapshot_key}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        entries = list(pool.map(snapshot, keys))
    write_json_atomic(
        manifest_path,
        {"bucket": bucket, "snapshot_prefix": prefix, "entries": entries},
    )
    existing = sum(1 for entry in entries if entry.get("existed"))
    return {"keys": len(entries), "copied": existing, "absent": len(entries) - existing}


def restore_snapshot(client, *, manifest_path: Path, workers: int) -> dict[str, int]:
    payload = json.loads(manifest_path.read_text())
    bucket = str(payload["bucket"])
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit("invalid rollback manifest")

    def restore(entry: object) -> str:
        if not isinstance(entry, dict) or not entry.get("key"):
            raise SystemExit("invalid rollback manifest entry")
        key = str(entry["key"])
        if entry.get("existed"):
            snapshot_key = str(entry.get("snapshot_key") or "")
            if not snapshot_key:
                raise SystemExit(f"rollback entry has no snapshot key: {key}")
            client.copy_object(
                Bucket=bucket,
                Key=key,
                CopySource={"Bucket": bucket, "Key": snapshot_key},
                MetadataDirective="COPY",
            )
            return "restored"
        client.delete_object(Bucket=bucket, Key=key)
        return "deleted"

    counts = {"restored": 0, "deleted": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(restore, entries):
            counts[result] += 1
    return counts


def delete_keys(client, *, bucket: str, keys: list[str], workers: int) -> dict[str, int]:
    if not keys:
        return {"deleted": 0}

    def remove(key: str) -> None:
        client.delete_object(Bucket=bucket, Key=key)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(remove, keys))
    return {"deleted": len(keys)}


def prune_snapshots(
    client,
    *,
    bucket: str,
    prefix: str,
    retain: int,
    workers: int,
) -> dict[str, int]:
    normalized = prefix.strip("/") + "/"
    keys = list_keys(client, bucket=bucket, prefixes=[normalized])
    run_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
    by_run: dict[str, list[str]] = {}
    for key in keys:
        relative = key[len(normalized):]
        run_id = relative.split("/", 1)[0]
        if run_pattern.fullmatch(run_id):
            by_run.setdefault(run_id, []).append(key)
    keep = set(sorted(by_run, reverse=True)[: max(retain, 0)])
    stale = [key for run_id, run_keys in by_run.items() if run_id not in keep for key in run_keys]
    result = delete_keys(client, bucket=bucket, keys=stale, workers=workers)
    return {"runs": len(by_run), "retained": len(keep), "deleted": result["deleted"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=16)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check")
    check.add_argument("--bucket", default="hl-mrf-parsed")

    get_prefix = subparsers.add_parser("download-prefix")
    get_prefix.add_argument("bucket")
    get_prefix.add_argument("prefix")
    get_prefix.add_argument("destination", type=Path)
    get_prefix.add_argument("--optional", action="store_true")

    get_file = subparsers.add_parser("download-file")
    get_file.add_argument("bucket")
    get_file.add_argument("key")
    get_file.add_argument("destination", type=Path)
    get_file.add_argument("--optional", action="store_true")

    put_prefix = subparsers.add_parser("upload-prefix")
    put_prefix.add_argument("bucket")
    put_prefix.add_argument("prefix")
    put_prefix.add_argument("source", type=Path)
    put_prefix.add_argument("--suffix", action="append", default=[])

    put_ccns = subparsers.add_parser("upload-ccns")
    put_ccns.add_argument("bucket")
    put_ccns.add_argument("prefix")
    put_ccns.add_argument("source", type=Path)
    put_ccns.add_argument("ccns_file", type=Path)
    put_ccns.add_argument("--extension", default=".json")

    put_file = subparsers.add_parser("upload-file")
    put_file.add_argument("bucket")
    put_file.add_argument("key")
    put_file.add_argument("source", type=Path)

    snapshot = subparsers.add_parser("snapshot-keys")
    snapshot.add_argument("bucket")
    snapshot.add_argument("snapshot_prefix")
    snapshot.add_argument("keys_file", type=Path)
    snapshot.add_argument("manifest_file", type=Path)

    restore = subparsers.add_parser("restore-snapshot")
    restore.add_argument("manifest_file", type=Path)

    list_remote = subparsers.add_parser("list-keys")
    list_remote.add_argument("bucket")
    list_remote.add_argument("output_file", type=Path)
    list_remote.add_argument("prefix", nargs="+")

    delete_remote = subparsers.add_parser("delete-keys")
    delete_remote.add_argument("bucket")
    delete_remote.add_argument("keys_file", type=Path)

    prune = subparsers.add_parser("prune-snapshots")
    prune.add_argument("bucket")
    prune.add_argument("prefix")
    prune.add_argument("--retain", type=int, default=2)

    args = parser.parse_args()
    client = make_client(args.workers)

    if args.command == "check":
        client.head_bucket(Bucket=args.bucket)
        print(f"R2 access verified: {args.bucket}")
        return 0
    if args.command == "download-prefix":
        download_prefix(
            client,
            bucket=args.bucket,
            prefix=args.prefix,
            destination=args.destination,
            workers=args.workers,
            optional=args.optional,
        )
        return 0
    if args.command == "download-file":
        download_file(
            client,
            bucket=args.bucket,
            key=args.key,
            destination=args.destination,
            optional=args.optional,
        )
        return 0
    if args.command == "upload-prefix":
        suffixes = tuple(args.suffix)
        paths = sorted(
            path
            for path in args.source.rglob("*")
            if path.is_file() and (not suffixes or path.name.endswith(suffixes))
        )
        upload_paths(
            client,
            bucket=args.bucket,
            prefix=args.prefix,
            root=args.source,
            paths=paths,
            workers=args.workers,
        )
        return 0
    if args.command == "upload-ccns":
        paths = paths_from_ccns(args.source, args.ccns_file, args.extension)
        upload_paths(
            client,
            bucket=args.bucket,
            prefix=args.prefix,
            root=args.source,
            paths=paths,
            workers=args.workers,
        )
        return 0
    if args.command == "upload-file":
        result = upload_one(client, args.bucket, args.key, args.source)
        print(f"{result}: {args.source} -> r2://{args.bucket}/{args.key}")
        return 0
    if args.command == "snapshot-keys":
        result = snapshot_keys(
            client,
            bucket=args.bucket,
            snapshot_prefix=args.snapshot_prefix,
            keys=read_keys(args.keys_file),
            manifest_path=args.manifest_file,
            workers=args.workers,
        )
        print(
            f"snapshot keys={result['keys']} copied={result['copied']} "
            f"absent={result['absent']}"
        )
        return 0
    if args.command == "restore-snapshot":
        result = restore_snapshot(client, manifest_path=args.manifest_file, workers=args.workers)
        print(f"rollback restored={result['restored']} deleted={result['deleted']}")
        return 0
    if args.command == "list-keys":
        keys = list_keys(client, bucket=args.bucket, prefixes=args.prefix)
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        args.output_file.write_text("".join(f"{key}\n" for key in keys))
        print(f"listed={len(keys)} -> {args.output_file}")
        return 0
    if args.command == "delete-keys":
        result = delete_keys(
            client,
            bucket=args.bucket,
            keys=read_keys(args.keys_file),
            workers=args.workers,
        )
        print(f"deleted={result['deleted']}")
        return 0
    if args.command == "prune-snapshots":
        result = prune_snapshots(
            client,
            bucket=args.bucket,
            prefix=args.prefix,
            retain=args.retain,
            workers=args.workers,
        )
        print(
            f"snapshot_runs={result['runs']} retained={result['retained']} "
            f"deleted_objects={result['deleted']}"
        )
        return 0
    raise SystemExit("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
