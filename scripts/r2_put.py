#!/usr/bin/env python3
"""r2_put.py — upload files to Cloudflare R2 with nothing but the Python stdlib.

Exists because the muse.ai VM that runs the refresh has no `aws`/`rclone`
and a PEP-668 Python that refuses `pip install --user` (measured 2026-09-18).
Implements AWS SigV4 (single PUT, unsigned payload hash is NOT used — the
body is hashed) against the S3-compatible R2 endpoint.

    python3 scripts/r2_put.py <local> <key> [<local> <key> ...]
    python3 scripts/r2_put.py --check          # HEAD the bucket; prints 200/403

Env: AWS_ACCESS_KEY_ID  AWS_SECRET_ACCESS_KEY  R2_ENDPOINT  R2_BUCKET (default hl-mrf-parsed)
Exit 0 on success; 1 on any failed upload (prints the HTTP status + body).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REGION = "auto"
SERVICE = "s3"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), date)
    k = _sign(k, REGION)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


def _request(method: str, key: str, body: bytes | None, content_type: str | None) -> tuple[int, str]:
    ak = os.environ["AWS_ACCESS_KEY_ID"]
    sk = os.environ["AWS_SECRET_ACCESS_KEY"]
    endpoint = os.environ["R2_ENDPOINT"].rstrip("/")
    bucket = os.environ.get("R2_BUCKET", "hl-mrf-parsed")
    host = urllib.parse.urlparse(endpoint).netloc
    path = "/" + bucket + ("/" + urllib.parse.quote(key, safe="/-_.~") if key else "")
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if content_type:
        headers["content-type"] = content_type
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = "\n".join([method, path, "", canonical_headers, signed, payload_hash])
    scope = f"{date}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, date), to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
    )
    del headers["host"]
    req = urllib.request.Request(endpoint + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read(400).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode(errors="replace")


def put(local: str, key: str) -> bool:
    with open(local, "rb") as fh:
        body = fh.read()
    ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
    if key.endswith(".json"):
        ctype = "application/json"
    status, text = _request("PUT", key, body, ctype)
    ok = 200 <= status < 300
    print(f"{'ok  ' if ok else 'FAIL'} PUT {key} ({len(body)} B) -> {status}{'' if ok else ' ' + text}")
    return ok


def main(argv: list[str]) -> int:
    for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "R2_ENDPOINT"):
        if not os.environ.get(v):
            print(f"missing env {v}", file=sys.stderr)
            return 2
    if argv == ["--check"]:
        status, text = _request("HEAD", "", None, None)
        print(f"HEAD bucket -> {status}")
        return 0 if status == 200 else 1
    if len(argv) < 2 or len(argv) % 2:
        print(__doc__, file=sys.stderr)
        return 2
    ok = True
    for local, key in zip(argv[0::2], argv[1::2]):
        ok = put(local, key) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
