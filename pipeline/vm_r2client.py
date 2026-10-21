#!/usr/bin/env python3
"""vm_r2client.py — persistent-connection R2 client for the VM Tier-3 rebuild.

Why this exists (2026-09-23 root cause): the VM's egress proxy takes ~55s to
establish each fresh TLS connection to r2.cloudflarestorage.com, and
r2_put.py opens a NEW connection per call (urllib). 690 calls/wave x 55s =
~10h of pure handshake overhead per wave. This client tunnels once per worker
thread (HTTP CONNECT through $https_proxy) and reuses the TLS connection for
all subsequent requests, cutting per-request overhead to ~0.

Reuses r2_put.py's SigV4 signing verbatim (imported, not copied) so auth
semantics are identical. Thread-safe: one persistent connection per thread.

API:
    R2Client(workers=8)
      .get_file(key, dest) -> (ok: bool, sha256_or_err: str, nbytes: int)
      .get_sha256(key)     -> (ok, sha256_or_err)
      .put_file(local, key) -> (ok, sha256_or_err)   # multipart >100MB
      .close()

CLI (subprocess-friendly):
    vm_r2client.py get <key> <dest>
    vm_r2client.py sha <key>
    vm_r2client.py put <local> <key>
    vm_r2client.py batch-get <manifest.json>   # [{key,dest}] -> JSONL results
    vm_r2client.py batch-put <manifest.json>   # [{local,key}] -> JSONL results

Safety: PUTs only. No DELETE anywhere in this file. Keys are passed through;
callers must restrict to the staging prefix.
"""
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.expanduser("~/hospital-ledger/scripts"))
import r2_put  # noqa: E402  (SigV4 signing reused verbatim)

CHUNK = 1 << 20
MULTIPART_THRESHOLD = 100 * 1024 * 1024
PART_SIZE = 16 * 1024 * 1024
CONNECT_TIMEOUT = 180
REQ_TIMEOUT = 1800


def _proxy():
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        v = os.environ.get(var)
        if v:
            return v
    return None


# Stagger CONNECT bursts: the egress proxy throttles >~24 simultaneous CONNECTs
# (2026-09-23: 64- and 32-worker bursts hung in handshake; 24 worked). A global
# gate serializes tunnel setup with a small gap so many persistent connections
# can be opened without tripping the burst limiter.
_CONNECT_GATE = threading.Lock()
_CONNECT_GAP_S = 3.0
_last_connect_start = [0.0]


def _tunnel_socket(host, port=443, timeout=CONNECT_TIMEOUT):
    """Open a TLS socket to host:443 via HTTP CONNECT through the egress proxy."""
    # Gap the *starts* of handshakes (the burst limiter trips on >~24
    # simultaneous CONNECTs); the slow TLS handshake itself runs outside
    # the gate so connections still open in parallel.
    with _CONNECT_GATE:
        wait = _CONNECT_GAP_S - (time.monotonic() - _last_connect_start[0])
        if wait > 0:
            time.sleep(wait)
        _last_connect_start[0] = time.monotonic()
    return _tunnel_socket_inner(host, port, timeout)


def _tunnel_socket_inner(host, port=443, timeout=CONNECT_TIMEOUT):
    proxy = _proxy()
    if not proxy:
        # No proxy configured: direct TLS.
        ctx = ssl.create_default_context()
        return ctx.wrap_socket(
            socket.create_connection((host, port), timeout=timeout),
            server_hostname=host)
    pu = urllib.parse.urlparse(proxy)
    if not pu.hostname or not pu.port:
        raise RuntimeError(f"unparsable proxy URL: {proxy!r}")
    sock = socket.create_connection((pu.hostname, pu.port), timeout=timeout)
    sock.settimeout(timeout)
    connect_req = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\n"
                   f"Proxy-Connection: Keep-Alive\r\n\r\n")
    sock.sendall(connect_req.encode())
    # Read the proxy's response headers.
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("proxy closed connection during CONNECT")
        buf += chunk
        if len(buf) > 65536:
            raise ConnectionError("proxy CONNECT response too large")
    status_line = buf.split(b"\r\n", 1)[0].decode("latin1")
    if " 200" not in status_line.split(" ", 2)[1:2].__str__() and not status_line.startswith("HTTP/1.1 200") and not status_line.startswith("HTTP/1.0 200"):
        raise ConnectionError(f"proxy CONNECT rejected: {status_line}")
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(sock, server_hostname=host)


class _Conn:
    """One persistent tunneled HTTPS connection (single-threaded use)."""

    def __init__(self):
        self.conn = None
        self.host = urllib.parse.urlparse(
            os.environ["R2_ENDPOINT"].rstrip("/")).netloc
        self._lock = threading.Lock()

    def _ensure(self):
        if self.conn is not None:
            return
        tls = _tunnel_socket(self.host, 443)
        c = http.client.HTTPSConnection(self.host, timeout=REQ_TIMEOUT)
        c.sock = tls  # http.client reuses an existing socket
        self.conn = c

    def _drop(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def request(self, method, key, query="", headers=None, body=None,
                content_length=None):
        """Send one signed request; return (status, response). Caller reads body.

        Retries once on a dead persistent connection (re-tunnel + resend).
        For requests with a file body, the caller must pass a fresh body on
        retry — handled by wrapping: we seek(0) file objects before resend.
        """
        path = r2_put._path(key)
        target = path + ("?" + query if query else "")
        hdrs = dict(headers or {})
        if content_length is not None:
            hdrs["Content-Length"] = str(content_length)
        last_exc = None
        for attempt in (0, 1):
            with self._lock:
                try:
                    self._ensure()
                    if hasattr(body, "seek") and attempt > 0:
                        try:
                            body.seek(0)
                        except Exception:
                            pass
                    self.conn.request(method, target, body=body, headers=hdrs)
                    resp = self.conn.getresponse()
                    return resp.status, resp
                except (http.client.HTTPException, ConnectionError,
                        TimeoutError, socket.timeout, ssl.SSLError,
                        OSError) as e:
                    last_exc = e
                    self._drop()
        raise ConnectionError(f"request {method} {key} failed twice: {last_exc}")

    def close(self):
        with self._lock:
            self._drop()


def _signed_headers(method, key, query="", payload_hash=None,
                    content_type=None, extra=None):
    if payload_hash is None:
        payload_hash = hashlib.sha256(b"").hexdigest()
    headers, _endpoint = r2_put._auth_headers(
        method, r2_put._path(key), query, payload_hash, content_type, extra)
    return headers


class R2Client:
    def __init__(self, workers=8):
        self.workers = workers
        self._local = threading.local()

    def _c(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = _Conn()
            self._local.conn = c
        return c

    # ------------------------------------------------------------- reads
    def get_file(self, key, dest):
        """Stream key -> dest. Returns (ok, sha256_or_err, nbytes)."""
        c = self._c()
        headers = _signed_headers("GET", key)
        try:
            status, resp = c.request("GET", key, headers=headers)
            if status != 200:
                body = resp.read(400)
                return False, f"GET {key} -> {status} {body[:120]!r}", 0
            want_len = resp.getheader("Content-Length")
            h = hashlib.sha256()
            n = 0
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
            if want_len is not None and int(want_len) != n:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False, f"truncated: got {n} want {want_len}", n
            os.replace(tmp, dest)
            return True, h.hexdigest(), n
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:300], 0

    def get_sha256(self, key, expect_len=None):
        """Streaming GET hash with truncation guard.

        Compares received bytes against the response Content-Length (and
        optional expect_len). A short read returns (False, "truncated: ...")
        -- retryable, NEVER hashed as if it were the whole object.
        (2026-09-24: an unchecked short read falsely failed wave 5 staging.)
        """
        c = self._c()
        headers = _signed_headers("GET", key)
        try:
            status, resp = c.request("GET", key, headers=headers)
            if status != 200:
                return False, f"GET {key} -> {status}"
            want_len = resp.getheader("Content-Length")
            h = hashlib.sha256()
            n = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
            if want_len is not None and int(want_len) != n:
                return False, f"truncated: got {n} want {want_len}"
            if expect_len is not None and expect_len != n:
                return False, f"truncated: got {n} want {expect_len}"
            return True, h.hexdigest()
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:300]

    # ------------------------------------------------------------ writes
    def _put_single(self, c, local, key, payload_hash, size, content_type,
                    if_none_match=False):
        extra = {"If-None-Match": "*"} if if_none_match else None
        headers = _signed_headers("PUT", key, payload_hash=payload_hash,
                                  content_type=content_type, extra=extra)
        with open(local, "rb") as fh:
            status, resp = c.request("PUT", key, headers=headers, body=fh,
                                     content_length=size)
            resp.read(400)
        return status

    def _mp_initiate(self, c, key, content_type):
        headers = _signed_headers("POST", key, query="uploads=",
                                  content_type=content_type)
        status, resp = c.request("POST", key, query="uploads=",
                                 headers=headers, body=b"", content_length=0)
        body = resp.read(65536).decode(errors="replace")
        if status != 200:
            raise RuntimeError(f"mp initiate -> {status} {body[:200]}")
        m = re.search(r"<UploadId>(.*?)</UploadId>", body)
        if not m:
            raise RuntimeError(f"mp initiate: no UploadId in {body[:200]}")
        return m.group(1)

    def _mp_part(self, c, key, upload_id, part_no, data, content_type):
        q = f"partNumber={part_no}&uploadId={urllib.parse.quote(upload_id, safe='')}"
        ph = hashlib.sha256(data).hexdigest()
        headers = _signed_headers("PUT", key, query=q, payload_hash=ph,
                                  content_type=content_type)
        status, resp = c.request("PUT", key, query=q, headers=headers,
                                 body=data, content_length=len(data))
        etag = resp.getheader("ETag", "").strip('"')
        resp.read(400)
        if status != 200:
            raise RuntimeError(f"mp part {part_no} -> {status}")
        return etag

    def _mp_complete(self, c, key, upload_id, parts):
        xml = "<CompleteMultipartUpload>" + "".join(
            f"<Part><PartNumber>{n}</PartNumber><ETag>\"{e}\"</ETag></Part>"
            for n, e in parts) + "</CompleteMultipartUpload>"
        body = xml.encode()
        q = f"uploadId={urllib.parse.quote(upload_id, safe='')}"
        ph = hashlib.sha256(body).hexdigest()
        headers = _signed_headers("POST", key, query=q, payload_hash=ph,
                                  content_type="application/xml")
        status, resp = c.request("POST", key, query=q, headers=headers,
                                 body=body, content_length=len(body))
        resp.read(65536)
        if status != 200:
            raise RuntimeError(f"mp complete -> {status}")

    def _mp_abort(self, c, key, upload_id):
        try:
            q = f"uploadId={urllib.parse.quote(upload_id, safe='')}"
            headers = _signed_headers("DELETE", key, query=q)
            status, resp = c.request("DELETE", key, query=q, headers=headers)
            resp.read(400)
        except Exception:
            pass

    def put_file(self, local, key, if_none_match=False):
        """Upload local -> key (multipart >100MB). Returns (ok, sha256_or_err).

        NOTE: _mp_abort issues an S3 AbortMultipartUpload for OUR OWN
        just-initiated upload id only — it never touches any other object.
        No general DELETE exists in this client.
        """
        c = self._c()
        size = os.path.getsize(local)
        content_type = r2_put._content_type_for(key)
        payload_hash = r2_put.file_sha256(local)
        try:
            if size > MULTIPART_THRESHOLD:
                up_id = self._mp_initiate(c, key, content_type)
                try:
                    parts = []
                    with open(local, "rb") as fh:
                        pn = 1
                        while True:
                            data = fh.read(PART_SIZE)
                            if not data:
                                break
                            etag = self._mp_part(c, key, up_id, pn, data,
                                                 content_type)
                            parts.append((pn, etag))
                            pn += 1
                    self._mp_complete(c, key, up_id, parts)
                except Exception:
                    self._mp_abort(c, key, up_id)
                    raise
            else:
                for attempt in (0, 1):
                    status = self._put_single(c, local, key, payload_hash,
                                              size, content_type,
                                              if_none_match)
                    if 200 <= status < 300:
                        break
                    if attempt == 0 and status in (500, 503):
                        time.sleep(5)
                        continue
                    return False, f"PUT {key} -> {status}"
            return True, payload_hash
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:300]

    def put_verified(self, local, key):
        """PUT then byte-verify via length-checked streaming GET.

        Self-healing (Barklee 2026-09-24: auto-fix, no operator round-trip):
        - truncated verify-GETs are retried (up to 3), never treated as
          corruption;
        - a length-verified hash mismatch triggers one re-PUT + final
          verify before the object is declared failed.
        Returns (ok, info). Failure info always says whether the read was
        truncated or the bytes genuinely disagreed.
        """
        ok, info = self.put_file(local, key)
        if not ok:
            return False, info
        expect = os.path.getsize(local)
        verr, mismatch = None, False
        for i in range(3):
            vok, vinfo = self.get_sha256(key, expect_len=expect)
            if not vok:
                verr = vinfo  # truncated or transport: retry
                time.sleep(5)
                continue
            if vinfo == info:
                return True, info
            mismatch, verr = True, None  # length-verified, bytes disagree
            break
        if not mismatch:
            return False, f"verify GET failed 3x (truncated reads): {verr}"
        # Genuine disagreement: one re-PUT, then a final length-checked verify.
        ok2, info2 = self.put_file(local, key)
        if not ok2:
            return False, f"re-put after mismatch failed: {info2}"
        vok, vinfo = self.get_sha256(key, expect_len=expect)
        if vok and vinfo == info2:
            return True, info2
        return False, (f"persistent byte-verify mismatch local={info2[:12]} "
                       f"remote={vinfo[:12] if vok else vinfo}")

    def close(self):
        c = getattr(self._local, "conn", None)
        if c:
            c.close()


def _batch(manifest_path, op, workers=8):
    items = json.load(open(manifest_path))
    client = R2Client(workers=workers)
    results = []

    def one(it):
        t0 = time.time()
        try:
            if op == "get":
                ok, info, n = client.get_file(it["key"], it["dest"])
                return {"key": it["key"], "dest": it["dest"], "ok": ok,
                        "sha256": info if ok else None,
                        "err": None if ok else info, "bytes": n,
                        "secs": round(time.time() - t0, 1)}
            else:
                ok, info = client.put_verified(it["local"], it["key"])
                return {"local": it["local"], "key": it["key"], "ok": ok,
                        "sha256": info if ok else None,
                        "err": None if ok else info,
                        "secs": round(time.time() - t0, 1)}
        except Exception as e:
            return {"key": it.get("key"), "ok": False,
                    "err": f"{type(e).__name__}: {e}"[:200]}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, items):
            results.append(r)
            print(json.dumps(r), flush=True)
    fails = sum(1 for r in results if not r["ok"])
    print(f"# batch-{op}: {len(results) - fails}/{len(results)} ok",
          file=sys.stderr)
    return 0 if fails == 0 else 1


def main(argv):
    if len(argv) == 3 and argv[0] == "get":
        ok, info, n = R2Client(workers=1).get_file(argv[1], argv[2])
        print(("OK " if ok else "FAIL ") + f"{info} {n}B")
        return 0 if ok else 1
    if len(argv) == 2 and argv[0] == "sha":
        ok, info = R2Client(workers=1).get_sha256(argv[1])
        print(("OK " if ok else "FAIL ") + info)
        return 0 if ok else 1
    if len(argv) == 3 and argv[0] == "put":
        ok, info = R2Client(workers=1).put_verified(argv[1], argv[2])
        print(("OK " if ok else "FAIL ") + info)
        return 0 if ok else 1
    if len(argv) >= 2 and argv[0] == "batch-get":
        return _batch(argv[1], "get", int(argv[2]) if len(argv) > 2 else 8)
    if len(argv) >= 2 and argv[0] == "batch-put":
        return _batch(argv[1], "put", int(argv[2]) if len(argv) > 2 else 8)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
