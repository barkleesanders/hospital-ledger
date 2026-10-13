#!/usr/bin/env python3
"""Create and revoke short-lived, bucket-scoped R2 S3 credentials.

The bootstrap call is authenticated with a Cloudflare Global API Key. The
created account-owned API token is restricted to one R2 bucket, then converted to the
Access Key ID and Secret Access Key format expected by R2's S3 API.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx


API_BASE_URL = "https://api.cloudflare.com/client/v4"
PERMISSION_GROUP_NAME = "Workers R2 Storage Bucket Item Write"
DEFAULT_BUCKET = "hl-mrf-parsed"
DEFAULT_TTL_HOURS = 72
MAX_TTL_HOURS = 168
ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
TOKEN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class BootstrapError(RuntimeError):
    """An intentionally secret-free bootstrap failure."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BootstrapError(f"missing required environment variable {name}")
    return value


def utc_timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise BootstrapError("credential timestamps must be timezone-aware")
    return (
        value.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def validate_account_id(account_id: str) -> str:
    account_id = account_id.strip().lower()
    if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise BootstrapError("R2_ACCOUNT_ID must be a 32-character hexadecimal ID")
    return account_id


def validate_bucket(bucket: str) -> str:
    bucket = bucket.strip()
    if not BUCKET_PATTERN.fullmatch(bucket):
        raise BootstrapError("bucket name is not a valid R2 bucket name")
    return bucket


def validate_token_id(token_id: str) -> str:
    token_id = token_id.strip().lower()
    if not TOKEN_ID_PATTERN.fullmatch(token_id):
        raise BootstrapError("token ID must be a 32-character hexadecimal ID")
    return token_id


def safe_error_codes(payload: Any) -> str:
    """Return only numeric Cloudflare error codes, never response messages."""
    if not isinstance(payload, dict):
        return ""
    codes: list[str] = []
    errors = payload.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if not isinstance(error, dict):
                continue
            code = error.get("code")
            if isinstance(code, int) or (isinstance(code, str) and code.isdigit()):
                codes.append(str(code))
    return ",".join(codes[:5])


class CloudflareTokenClient:
    """Minimal Cloudflare user-token client with redacted errors."""

    def __init__(
        self,
        email: str,
        global_api_key: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not email.strip() or not global_api_key.strip():
            raise BootstrapError("Cloudflare Global API Key credentials are incomplete")
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Auth-Email": email.strip(),
            "X-Auth-Key": global_api_key.strip(),
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "CloudflareTokenClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                f"{API_BASE_URL}{path}",
                headers=self._headers,
                params=params,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise BootstrapError("Cloudflare API request failed before a response") from None

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            raise BootstrapError(
                f"Cloudflare API returned an invalid response (HTTP {response.status_code})"
            ) from None

        success = isinstance(body, dict) and body.get("success") is True
        if not response.is_success or not success:
            codes = safe_error_codes(body)
            code_suffix = f"; error codes {codes}" if codes else ""
            raise BootstrapError(
                f"Cloudflare API request failed for {method} {path} "
                f"(HTTP {response.status_code}{code_suffix})"
            )
        return body

    def permission_group_id(self) -> str:
        # The live endpoint returns the complete permission-group catalog and
        # rejects the otherwise tempting name/scope query parameters with
        # error 1001. Filter the catalog locally.
        body = self._request("GET", "/user/tokens/permission_groups")
        result = body.get("result")
        if not isinstance(result, list):
            raise BootstrapError("Cloudflare returned an invalid permission-group result")
        matches = []
        for item in result:
            if not isinstance(item, dict) or item.get("name") != PERMISSION_GROUP_NAME:
                continue
            scopes = item.get("scopes")
            if not isinstance(scopes, list) or "com.cloudflare.edge.r2.bucket" not in scopes:
                continue
            identifier = item.get("id")
            if isinstance(identifier, str) and TOKEN_ID_PATTERN.fullmatch(identifier):
                matches.append(identifier)
        if len(matches) != 1:
            raise BootstrapError(
                "expected exactly one bucket-item-write permission group"
            )
        return matches[0]

    def create_token(
        self,
        payload: dict[str, Any],
        *,
        account_id: str,
    ) -> tuple[str, str]:
        account_id = validate_account_id(account_id)
        body = self._request(
            "POST",
            f"/accounts/{account_id}/tokens",
            payload=payload,
        )
        result = body.get("result")
        if not isinstance(result, dict):
            raise BootstrapError("Cloudflare returned an invalid token result")
        token_id = result.get("id")
        token_value = result.get("value")
        if not isinstance(token_id, str) or not TOKEN_ID_PATTERN.fullmatch(token_id):
            raise BootstrapError("Cloudflare did not return a valid token ID")
        if not isinstance(token_value, str) or not token_value:
            raise BootstrapError("Cloudflare did not return a token value")

        # Minimize the lifetime of the plaintext value in the parsed response.
        result["value"] = "<redacted>"
        return token_id, token_value

    def revoke_token(self, token_id: str, *, account_id: str) -> None:
        account_id = validate_account_id(account_id)
        self._request(
            "DELETE",
            f"/accounts/{account_id}/tokens/{validate_token_id(token_id)}",
        )


def build_token_payload(
    *,
    account_id: str,
    bucket: str,
    permission_group_id: str,
    now: dt.datetime,
    ttl_hours: int,
) -> tuple[dict[str, Any], str]:
    account_id = validate_account_id(account_id)
    bucket = validate_bucket(bucket)
    permission_group_id = validate_token_id(permission_group_id)
    if ttl_hours < 1 or ttl_hours > MAX_TTL_HOURS:
        raise BootstrapError(
            f"credential lifetime must be between 1 and {MAX_TTL_HOURS} hours"
        )
    expires_on = utc_timestamp(now + dt.timedelta(hours=ttl_hours))
    resource = f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket}"
    token_name = f"hospital-ledger-{bucket}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    return (
        {
            "name": token_name[:120],
            "policies": [
                {
                    "effect": "allow",
                    "permission_groups": [{"id": permission_group_id, "meta": {}}],
                    "resources": {resource: "*"},
                }
            ],
            "expires_on": expires_on,
        },
        expires_on,
    )


def shell_env_contents(
    *,
    account_id: str,
    bucket: str,
    token_id: str,
    secret_access_key: str,
    expires_on: str,
) -> str:
    values = {
        "R2_ACCOUNT_ID": validate_account_id(account_id),
        "R2_BUCKET": validate_bucket(bucket),
        "R2_ACCESS_KEY_ID": validate_token_id(token_id),
        "R2_SECRET_ACCESS_KEY": secret_access_key,
        "R2_CREDENTIALS_EXPIRES_ON": expires_on,
    }
    if not re.fullmatch(r"[0-9a-f]{64}", secret_access_key):
        raise BootstrapError("derived R2 Secret Access Key is invalid")
    return "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in values.items()
    )


def write_private_env(path: Path, contents: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
    except OSError:
        raise BootstrapError("could not write the private credential file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def provision_credentials(
    client: CloudflareTokenClient,
    *,
    account_id: str,
    bucket: str,
    output: Path,
    ttl_hours: int,
    now: dt.datetime | None = None,
) -> str:
    account_id = validate_account_id(account_id)
    bucket = validate_bucket(bucket)
    now = now or dt.datetime.now(dt.timezone.utc)
    permission_group_id = client.permission_group_id()
    payload, expires_on = build_token_payload(
        account_id=account_id,
        bucket=bucket,
        permission_group_id=permission_group_id,
        now=now,
        ttl_hours=ttl_hours,
    )
    token_id, token_value = client.create_token(payload, account_id=account_id)
    secret_access_key = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    token_value = ""
    try:
        contents = shell_env_contents(
            account_id=account_id,
            bucket=bucket,
            token_id=token_id,
            secret_access_key=secret_access_key,
            expires_on=expires_on,
        )
        write_private_env(output, contents)
    except Exception:
        try:
            client.revoke_token(token_id, account_id=account_id)
        except Exception:
            raise BootstrapError(
                "credential-file creation failed and token rollback could not be verified"
            ) from None
        raise BootstrapError("credential-file creation failed; token was revoked") from None
    return expires_on


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create short-lived R2 credentials")
    create.add_argument("--bucket", default=DEFAULT_BUCKET)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--ttl-hours", type=int, default=DEFAULT_TTL_HOURS)

    revoke = subparsers.add_parser("revoke", help="revoke a bootstrap token")
    revoke.add_argument("token_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        email = required_env("CLOUDFLARE_EMAIL")
        global_api_key = required_env("CLOUDFLARE_API_KEY")
        account_id = required_env("R2_ACCOUNT_ID")
        with CloudflareTokenClient(email, global_api_key) as client:
            if args.command == "create":
                expires_on = provision_credentials(
                    client,
                    account_id=account_id,
                    bucket=args.bucket,
                    output=args.output,
                    ttl_hours=args.ttl_hours,
                )
                print(
                    f"Created bucket-scoped R2 credentials at {args.output} "
                    f"(expires {expires_on})."
                )
            else:
                client.revoke_token(args.token_id, account_id=account_id)
                print("Revoked the bucket-scoped R2 credential.")
        return 0
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Never serialize unexpected exception details because an HTTP client or
        # mocked response may retain credentials in its exception representation.
        print("error: R2 credential bootstrap failed safely", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
