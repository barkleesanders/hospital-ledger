#!/usr/bin/env python3
"""r2_put.py — upload files to Cloudflare R2 with nothing but the Python stdlib.

Exists because the muse.ai VM that runs the refresh has no `aws`/`rclone`
and a PEP-668 Python that refuses `pip install --user` (measured 2026-09-18).
Implements AWS SigV4 against the S3-compatible R2 endpoint.

    python3 scripts/r2_put.py <local> <key> [<local> <key> ...]
    python3 scripts/r2_put.py --check          # HEAD the bucket; prints 200/403
    python3 scripts/r2_put.py --get-sha256 <key>  # GET key, print sha256 (verify)
    python3 scripts/r2_put.py --get-file <key> <dest>  # GET key, save to dest
    python3 scripts/r2_put.py --list [prefix]  # paginated listing, retry on IncompleteRead
    python3 scripts/r2_put.py --if-none-match <local> <key> ...
        # conditional PUT: fails with 412 if the key already exists
    python3 scripts/r2_put.py --lock-acquire <owner> [--ttl 900] [--lock-key _pipeline/publish.lock]
    python3 scripts/r2_put.py --lock-refresh <owner> [--ttl 900] [--lock-key ...]
    python3 scripts/r2_put.py --lock-release <owner> [--lock-key ...]
    python3 scripts/r2_put.py --lock-status [--lock-key ...]  # print lock JSON or "none"

Uploads stream the body (peak RAM ~1 MiB + part buffer); objects larger than
MULTIPART_THRESHOLD (100 MiB) use S3 multipart upload with per-part retry and
a final streaming sha256 verify. Hashing is chunked (1 MiB) everywhere.

Env: AWS_ACCESS_KEY_ID  AWS_SECRET_ACCESS_KEY  R2_ENDPOINT  R2_BUCKET (default hl-mrf-parsed)
Exit 0 on success; 1 on any failed upload (prints the HTTP status + body).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import http.client
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REGION = "auto"
SERVICE = "s3"

CHUNK = 1 << 20                    # 1 MiB streaming chunk
MULTIPART_THRESHOLD = 100 * 1024 * 1024   # >100 MiB -> S3 multipart upload
PART_SIZE = 16 * 1024 * 1024       # 16 MiB per part (>= S3 5 MiB minimum)
MULTIPART_MAX_RETRIES = 5
LIST_MAX_RETRIES = 5               # retry-with-backoff on IncompleteRead
LOCK_KEY = "_pipeline/publish.lock"
LOCK_TTL = 900


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), date)
    k = _sign(k, REGION)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


def _auth_headers(method: str, path: str, query: str, payload_hash: str,
                  content_type: str | None,
                  extra_headers: dict | None) -> tuple[dict, str]:
    """SigV4 headers (incl. Authorization) for a request. Returns (headers, endpoint)."""
    ak = os.environ["AWS_ACCESS_KEY_ID"]
    sk = os.environ["AWS_SECRET_ACCESS_KEY"]
    endpoint = os.environ["R2_ENDPOINT"].rstrip("/")
    host = urllib.parse.urlparse(endpoint).netloc
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if content_type:
        headers["content-type"] = content_type
    if extra_headers:
        for k, v in extra_headers.items():
            headers[k.lower()] = v
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = "\n".join([method, path, query, canonical_headers, signed, payload_hash])
    scope = f"{date}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, date), to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
    )
    del headers["host"]
    return headers, endpoint


def _path(key: str) -> str:
    bucket = os.environ.get("R2_BUCKET", "hl-mrf-parsed")
    return "/" + bucket + ("/" + urllib.parse.quote(key, safe="/-_.~") if key else "")


def _open_signed(method: str, key: str, *, body=None, content_length: int | None = None,
                 payload_hash: str | None = None, content_type: str | None = None,
                 query: str = "", extra_headers: dict | None = None,
                 timeout: int = 300):
    """Open a signed HTTP request. body may be bytes or a binary file object
    (streamed by http.client when Content-Length is known). Raises
    urllib.error.HTTPError on 4xx/5xx."""
    if payload_hash is None:
        payload_hash = hashlib.sha256(body if isinstance(body, bytes)
                                      else b"").hexdigest()
    path = _path(key)
    headers, endpoint = _auth_headers(method, path, query, payload_hash,
                                      content_type, extra_headers)
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    url = endpoint + path + ("?" + query if query else "")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _request(method: str, key: str, body: bytes | None, content_type: str | None,
             if_none_match: bool = False) -> tuple[int, str]:
    extra = {"If-None-Match": "*"} if if_none_match else None
    try:
        with _open_signed(method, key, body=body, content_type=content_type,
                          extra_headers=extra) as r:
            return r.status, r.read(400).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode(errors="replace")


# ---------------------------------------------------------------- streaming primitives

def file_sha256(path: str, chunk: int = CHUNK) -> str:
    """Chunked sha256 of a local file. Peak RAM ~chunk bytes, never the file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def get_bytes(key: str) -> bytes | None:
    """Signed GET of a (small) key; return body bytes, or None on HTTP error."""
    try:
        with _open_signed("GET", key) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        print(f"GET {key} -> {e.code}", file=sys.stderr)
        return None


def get_sha256(key: str) -> str | None:
    """Signed GET of key; return hex sha256, streaming in 1 MiB chunks."""
    try:
        h = hashlib.sha256()
        with _open_signed("GET", key, timeout=600) as r:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except urllib.error.HTTPError as e:
        print(f"GET {key} -> {e.code}", file=sys.stderr)
        return None


def get_file(key: str, dest: str) -> bool:
    """Signed GET of key; stream it to dest. Return True on success."""
    try:
        with _open_signed("GET", key, timeout=600) as r, open(dest, "wb") as fh:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
        return True
    except urllib.error.HTTPError as e:
        print(f"GET {key} -> {e.code}", file=sys.stderr)
        return False


# ---------------------------------------------------------------- listing (paginated, IncompleteRead-tolerant)

def _list_page(prefix: str, token: str | None) -> str:
    """One ListObjectsV2 page with retry-with-backoff on IncompleteRead.

    Regression guard for the 2026-09-23 archive failure: the VM-egress leg
    intermittently truncates list responses mid-body; a truncated page must be
    retried, never treated as the end of the listing."""
    pairs = [("list-type", "2"), ("max-keys", "1000"), ("prefix", prefix)]
    if token:
        pairs.append(("continuation-token", "\x00TOKEN\x00"))
    pairs.sort(key=lambda p: p[0])
    parts = []
    for name, val in pairs:
        if val == "\x00TOKEN\x00":
            parts.append("continuation-token=" + urllib.parse.quote(token, safe="%-_.~"))
        else:
            parts.append(urllib.parse.urlencode([(name, val)]))
    query = "&".join(parts)
    last: Exception | None = None
    for attempt in range(LIST_MAX_RETRIES):
        try:
            with _open_signed("GET", "", query=query, timeout=120) as r:
                return r.read().decode()
        except http.client.IncompleteRead as e:
            last = e
            wait = 2 ** attempt
            print(f"LIST {prefix!r}: IncompleteRead (attempt {attempt + 1}/"
                  f"{LIST_MAX_RETRIES}), retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
        except urllib.error.HTTPError:
            raise
    raise last  # exhausted retries


def list_keys_iter(prefix: str = ""):
    """Yield (key, size) for every object under prefix, following continuation
    tokens until IsTruncated=false. Raises on unrecoverable HTTP errors."""
    token: str | None = None
    while True:
        body = _list_page(prefix, token)
        for k, s in re.findall(r"<Key>(.*?)</Key>\s*<Size>(\d+)</Size>", body):
            yield k, int(s)
        m = re.search(r"<IsTruncated>(true|false)</IsTruncated>", body)
        if not m or m.group(1) != "true":
            return
        m2 = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", body)
        if not m2:
            return
        token = html.unescape(m2.group(1))


def list_keys(prefix: str = "") -> bool:
    """List object keys under prefix (paginated). Print key + size. Return True on success."""
    total = 0
    try:
        for k, s in list_keys_iter(prefix):
            print(f"{s}\t{k}")
            total += 1
    except urllib.error.HTTPError as e:
        print(f"LIST {prefix!r} -> {e.code}", file=sys.stderr)
        return False
    except http.client.IncompleteRead as e:
        print(f"LIST {prefix!r} -> IncompleteRead after {LIST_MAX_RETRIES} retries: {e}",
              file=sys.stderr)
        return False
    print(f"# total: {total}", file=sys.stderr)
    return True


# ---------------------------------------------------------------- multipart upload (>100 MiB)

def _mp_initiate(key: str, content_type: str | None) -> str:
    try:
        # Canonical query string needs the "name=" form for valueless params.
        with _open_signed("POST", key, body=b"", query="uploads=",
                          content_type=content_type, timeout=120) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"multipart initiate {key} -> {e.code}: "
                           f"{e.read(200).decode(errors='replace')}")
    m = re.search(r"<UploadId>(.*?)</UploadId>", body)
    if not m:
        raise RuntimeError(f"multipart initiate {key}: no UploadId in response")
    return m.group(1)


def _mp_upload_part(key: str, upload_id: str, part_number: int, data: bytes) -> str:
    """Upload one part; return its ETag. Raises on HTTP error (caller retries)."""
    query = f"partNumber={part_number}&uploadId={urllib.parse.quote(upload_id, safe='')}"
    phash = hashlib.sha256(data).hexdigest()
    try:
        with _open_signed("PUT", key, body=data, content_length=len(data),
                          payload_hash=phash, query=query, timeout=600) as r:
            etag = r.headers.get("ETag", "")
            r.read()
            return etag
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"multipart part {part_number} {key} -> {e.code}: "
                           f"{e.read(200).decode(errors='replace')}")


def _mp_complete(key: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
    items = "".join(
        f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>"
        for n, e in parts)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
           f'<CompleteMultipartUpload>{items}</CompleteMultipartUpload>').encode()
    query = f"uploadId={urllib.parse.quote(upload_id, safe='')}"
    try:
        with _open_signed("POST", key, body=xml, content_length=len(xml),
                          payload_hash=hashlib.sha256(xml).hexdigest(),
                          content_type="application/xml", query=query,
                          timeout=600) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"multipart complete {key} -> {e.code}: "
                           f"{e.read(200).decode(errors='replace')}")


def _mp_abort(key: str, upload_id: str) -> None:
    query = f"uploadId={urllib.parse.quote(upload_id, safe='')}"
    try:
        with _open_signed("DELETE", key, query=query, timeout=120) as r:
            r.read()
    except urllib.error.HTTPError:
        pass


def put_multipart(local: str, key: str, content_type: str | None = None,
                  part_size: int | None = None,
                  max_retries: int | None = None) -> bool:
    """S3 multipart upload with per-part retry and a final streaming sha256
    verify against the local file. Returns True on success."""
    # Defaults resolved at call time (not def time) so tests/ops can tune the
    # module constants without re-importing.
    part_size = PART_SIZE if part_size is None else part_size
    max_retries = MULTIPART_MAX_RETRIES if max_retries is None else max_retries
    size = os.path.getsize(local)
    log = lambda *a: print("[multipart]", *a, flush=True)
    try:
        upload_id = _mp_initiate(key, content_type)
    except RuntimeError as e:
        print(f"FAIL multipart {key}: {e}", file=sys.stderr)
        return False
    parts: list[tuple[int, str]] = []
    try:
        with open(local, "rb") as fh:
            n = 1
            while True:
                data = fh.read(part_size)
                if not data:
                    break
                etag = None
                for attempt in range(max_retries):
                    try:
                        etag = _mp_upload_part(key, upload_id, n, data)
                        break
                    except (RuntimeError, urllib.error.URLError,
                            http.client.HTTPException, OSError) as e:
                        if attempt + 1 == max_retries:
                            raise
                        wait = 2 ** attempt
                        log(f"part {n} attempt {attempt + 1} failed ({e}); "
                            f"retrying in {wait}s")
                        time.sleep(wait)
                parts.append((n, etag))
                log(f"part {n}/{max(1, (size + part_size - 1) // part_size)} ok "
                    f"({len(data)} B)")
                n += 1
        _mp_complete(key, upload_id, parts)
    except Exception as e:
        print(f"FAIL multipart {key}: {e} — aborting upload {upload_id}",
              file=sys.stderr)
        _mp_abort(key, upload_id)
        return False
    want = file_sha256(local)
    got = get_sha256(key)
    if got != want:
        print(f"FAIL multipart {key}: final sha256 mismatch local={want} r2={got}",
              file=sys.stderr)
        return False
    print(f"ok   multipart PUT {key} ({size} B, {len(parts)} parts) verified {want[:12]}…")
    return True


# ---------------------------------------------------------------- PUT (streaming; multipart for large objects)

def _content_type_for(key: str) -> str:
    ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
    if key.endswith(".json"):
        ctype = "application/json"
    return ctype


def put_streaming(local: str, key: str, content_type: str | None = None,
                  if_none_match: bool = False) -> bool:
    """Single-PUT with a streamed body: the file is hashed in 1 MiB chunks
    first (for the SigV4 payload hash), then sent as a file object so
    http.client streams it in 8 KiB blocks. Peak RAM ~1 MiB."""
    size = os.path.getsize(local)
    ctype = content_type or _content_type_for(key)
    payload_hash = file_sha256(local)
    extra = {"If-None-Match": "*"} if if_none_match else None
    try:
        with open(local, "rb") as fh, \
                _open_signed("PUT", key, body=fh, content_length=size,
                             payload_hash=payload_hash, content_type=ctype,
                             extra_headers=extra, timeout=1800) as r:
            r.read(400)
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
        text = e.read(400).decode(errors="replace")
        print(f"FAIL PUT {key} ({size} B) -> {status} {text}")
        return False
    ok = 200 <= status < 300
    print(f"{'ok  ' if ok else 'FAIL'} PUT {key} ({size} B) -> {status}"
          f"{' [If-None-Match: *]' if if_none_match else ''}")
    return ok


def put(local: str, key: str, if_none_match: bool = False) -> bool:
    """Upload local -> key. Objects > MULTIPART_THRESHOLD go via multipart
    (per-part retry + final sha256 verify); everything else streams."""
    size = os.path.getsize(local)
    if size > MULTIPART_THRESHOLD:
        return put_multipart(local, key)
    return put_streaming(local, key, if_none_match=if_none_match)


# ---------------------------------------------------------------- publish lock (_pipeline/publish.lock)

def _lock_body(owner: str, ttl: int) -> bytes:
    return json.dumps({"owner": owner, "ts": int(time.time()),
                       "ttl": int(ttl)}).encode()


def lock_status(lock_key: str = LOCK_KEY) -> dict | None:
    """Current lock document, or None if no lock exists / unreadable."""
    raw = get_bytes(lock_key)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode())
        return d if isinstance(d, dict) and d.get("owner") else None
    except (ValueError, UnicodeDecodeError):
        return None


def lock_acquire(owner: str, ttl: int = LOCK_TTL,
                 lock_key: str = LOCK_KEY) -> tuple[bool, str]:
    """Acquire the publish lock via conditional PUT (If-None-Match: *).
    A stale lock (ts + ttl in the past) may be broken by the acquirer with a
    logged warning. Returns (acquired, message)."""
    body = _lock_body(owner, ttl)
    status, text = _request("PUT", lock_key, body, "application/json",
                            if_none_match=True)
    if 200 <= status < 300:
        return True, "acquired"
    if status == 412:
        cur = lock_status(lock_key)
        holder = (cur or {}).get("owner", "?")
        age = time.time() - (cur or {}).get("ts", 0)
        if cur and age > cur.get("ttl", LOCK_TTL):
            print(f"WARNING: breaking stale publish lock held by {holder} "
                  f"(age {age:.0f}s > ttl {cur.get('ttl')}s)", file=sys.stderr)
            _request("DELETE", lock_key, None, None)
            status2, text2 = _request("PUT", lock_key, body, "application/json",
                                      if_none_match=True)
            if 200 <= status2 < 300:
                return True, f"acquired after breaking stale lock of {holder}"
            return False, f"stale-break raced: PUT -> {status2} {text2}"
        return False, f"lock held by {holder}"
    return False, f"unexpected status {status}: {text}"


def lock_refresh(owner: str, ttl: int = LOCK_TTL,
                 lock_key: str = LOCK_KEY) -> bool:
    """Heartbeat: refresh the lock timestamp. Only the owner may refresh."""
    cur = lock_status(lock_key)
    if not cur:
        print("WARNING: lock_refresh: no lock exists", file=sys.stderr)
        return False
    if cur.get("owner") != owner:
        print(f"WARNING: lock_refresh: lock owned by {cur.get('owner')}, not {owner}",
              file=sys.stderr)
        return False
    status, _ = _request("PUT", lock_key, _lock_body(owner, ttl), "application/json")
    return 200 <= status < 300


def lock_release(owner: str, lock_key: str = LOCK_KEY) -> bool:
    """Release the lock (DELETE). Only the owner may release; never releases
    another publisher's lock."""
    cur = lock_status(lock_key)
    if not cur:
        return True
    if cur.get("owner") != owner:
        print(f"WARNING: lock_release: lock owned by {cur.get('owner')}, not {owner}; "
              f"not releasing", file=sys.stderr)
        return False
    status, _ = _request("DELETE", lock_key, None, None)
    ok = 200 <= status < 300 or status == 404
    if not ok:
        print(f"WARNING: lock_release DELETE -> {status}", file=sys.stderr)
    return ok


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "R2_ENDPOINT"):
        if not os.environ.get(v):
            print(f"missing env {v}", file=sys.stderr)
            return 2
    if argv == ["--check"]:
        status, text = _request("HEAD", "", None, None)
        print(f"HEAD bucket -> {status}")
        return 0 if status == 200 else 1
    if len(argv) == 2 and argv[0] == "--get-sha256":
        digest = get_sha256(argv[1])
        if digest is None:
            return 1
        print(digest)
        return 0
    if len(argv) == 3 and argv[0] == "--get-file":
        return 0 if get_file(argv[1], argv[2]) else 1
    if argv[:1] == ["--list"]:
        prefix = argv[1] if len(argv) > 1 else ""
        return 0 if list_keys(prefix) else 1
    if argv[:1] == ["--lock-status"]:
        lock_key = argv[2] if len(argv) > 2 and argv[1] == "--lock-key" else LOCK_KEY
        cur = lock_status(lock_key)
        print(json.dumps(cur) if cur else "none")
        return 0
    if argv[:1] in (["--lock-acquire"], ["--lock-refresh"], ["--lock-release"]):
        cmd, rest = argv[0], argv[1:]
        owner = rest[0] if rest else None
        if not owner:
            print(f"{cmd} <owner> [--ttl N] [--lock-key K]", file=sys.stderr)
            return 2
        ttl, lock_key = LOCK_TTL, LOCK_KEY
        i = 1
        while i < len(rest):
            if rest[i] == "--ttl" and i + 1 < len(rest):
                ttl = int(rest[i + 1]); i += 2
            elif rest[i] == "--lock-key" and i + 1 < len(rest):
                lock_key = rest[i + 1]; i += 2
            else:
                i += 1
        if cmd == "--lock-acquire":
            ok, msg = lock_acquire(owner, ttl, lock_key)
        elif cmd == "--lock-refresh":
            ok, msg = lock_refresh(owner, ttl, lock_key), "refreshed"
        else:
            ok, msg = lock_release(owner, lock_key), "released"
        print(f"{cmd[7:]}: {msg} (owner={owner})")
        return 0 if ok else 1
    args = list(argv)
    if_none_match = False
    if args[:1] == ["--if-none-match"]:
        if_none_match, args = True, args[1:]
    if len(args) < 2 or len(args) % 2:
        print(__doc__, file=sys.stderr)
        return 2
    ok = True
    for local, key in zip(args[0::2], args[1::2]):
        ok = put(local, key, if_none_match=if_none_match) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
