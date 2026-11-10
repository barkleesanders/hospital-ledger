#!/usr/bin/env python3
"""process_submissions — fleet processor for hospital-submitted MRFs.

Claims submissions uploaded through hospitalledger.com/submit (records in
R2 bucket hl-mrf-raw under submissions/<id>/), and for each one:

  1. downloads the raw file, hashing it (SHA-256) as it lands;
  2. parses it with the fleet parser — the Mini payload build, whose
     SHA-256 is pinned below and verified before anything runs (a
     different parser is a different pipeline: fail loudly, touch
     nothing);
  3. reconciles the parsed item count against the source file's own
     entry count wherever the format lets us count it independently;
  4. extracts the identity INSIDE the file (JSON header fields / CSV
     metadata rows) and gates it against the claimed CCN's registry
     identity — VERIFIED / MISMATCH / UNVERIFIABLE, the wave-8
     trichotomy. The submitter's form answers are claims, never proof;
  5. validates against CMS v3.0.0 expectations — the official CMS
     validator CLI when it runs (its outcome is reported either way,
     never silently skipped), plus fleet checks over the parsed items;
  6. builds a normalized CMS-template (v3.0.0 JSON) file from the
     hospital's own data and uploads report + file back to R2 for the
     submitter to download;
  7. ONLY when the submitter opted in AND proved control of the
     hospital's website AND the verified domain is the domain on file
     for that CCN AND identity is VERIFIED AND completeness reconciles
     AND priced items exist AND the CCN is not already listed: writes a
     registry ACCEPT verdict (provenance class "hospital-submitted"),
     seeds the canonical, and publishes through the standard serial
     Mini publisher. Every other combination is processed for the
     report and refused for listing, with the reason recorded.

Doctrine carried over from the fleet skill: submissions never bypass
the gates; no R2 deletes; CCN 150042 is refused before anything else;
publishes are serial and lock-checked by publish_mini.py itself.

Usage:
  process_submissions.py [--id <submission-id>] [--dry-run-listing]
  --dry-run-listing: run a qualifying listing through
  publish_mini.py --dry-run instead of a live publish.

Env overrides (all optional): HL_PAYLOAD_PARSER, HL_PARSER_SHA256,
HL_FLEET_DIR, HL_SUBMISSIONS_WORK, HL_REGISTRY_DB, HL_STORE_DIR,
HL_VERDICTS_DIR, HL_ENV_FILE.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict

PROCESSOR_VERSION = "1.0.0"

HOME = os.path.expanduser("~")
FLEET = os.environ.get("HL_FLEET_DIR", os.path.join(HOME, "hl-fleet"))
ENV_FILE = os.environ.get("HL_ENV_FILE", os.path.join(HOME, "hospital-ledger.env"))
PAYLOAD_PARSER = os.environ.get(
    "HL_PAYLOAD_PARSER", os.path.join(FLEET, "payload", "mrf_parse.py"))
# The loud-cap parser build this lane was verified against (wave 6:
# SOURCE_ROW_CAP raises instead of silently truncating). A parser whose
# bytes differ is not this pipeline — refuse before touching records.
EXPECTED_PARSER_SHA256 = os.environ.get(
    "HL_PARSER_SHA256",
    "0bbace6d1f10e0c767ba3a14026422b3998a79c5134da2cfa152761ca4ca0ec8")
STORE_DIR = os.environ.get(
    "HL_STORE_DIR", os.path.realpath(os.path.join(FLEET, "results")))
REGISTRY_DB = os.environ.get(
    "HL_REGISTRY_DB",
    os.path.join(HOME, "hl-publish-tree", "db", "hospital_ledger.db"))
VERDICTS_DIR = os.environ.get("HL_VERDICTS_DIR", os.path.join(FLEET, "verdicts"))
VERDICTS_PENDING_DIR = os.path.join(FLEET, "verdicts-pending-vm")
WORK_ROOT = os.environ.get(
    "HL_SUBMISSIONS_WORK", "/Volumes/Share/hl-fleet/work-submissions")
PUBLISH_MINI = os.path.join(FLEET, "publish_mini.py")
# Submissions live in hl-mrf-parsed under submissions/: it is the
# bucket this credential set can reach (hl-mrf-raw answers it with 403,
# verified 2026-11-10) and the bucket the Worker writes through its
# HL_MRF_PARSED binding — the webmcp-beacon split, applied to uploads.
SUBMISSIONS_BUCKET = "hl-mrf-parsed"
PARSED_BUCKET = "hl-mrf-parsed"
QUARANTINED_CCN = "150042"

# In-memory parse ceiling. The fleet parses local bytes whole
# (build_canonical_mini.py precedent: an 870 MB inner CSV; the largest
# raw MRF handled fleet-wide is ~2.4 GB). 3 GiB keeps headroom over
# everything observed; the Mini has 16 GB RAM and the parser's working
# set runs several times the input. Above this, processing fails
# LOUDLY (a recorded process_failed with the reason) — never a silent
# truncation or a partial parse.
IN_MEMORY_PARSE_MAX = 3 * 1024**3
# The official CMS validator loads the whole file in Node; measured
# fleet MRFs make it swap-bound well before the parse ceiling. It runs
# at or below this size; above it the report says so, in words.
OFFICIAL_VALIDATOR_MAX = 200 * 1024**2
STALE_CLAIM_SECONDS = 2 * 3600

KNOWN_CODE_TYPES = {
    "CPT", "HCPCS", "ICD", "MS-DRG", "APR-DRG", "APC", "NDC", "RC",
    "CDT", "TRIS", "EAPG", "LOCAL", "OTHER",
}


def log(event, **kw):
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


def utcnow():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- R2 client
# Stdlib SigV4 against the R2 S3 endpoint, the r2_put.py pattern (the
# fleet has no boto3). Credentials come from ~/hospital-ledger.env.

def _load_env():
    env = {}
    for line in open(ENV_FILE):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"')
    for need in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "R2_ENDPOINT"):
        if need not in env:
            raise RuntimeError(f"{need} missing from {ENV_FILE}")
    return env


class R2:
    def __init__(self):
        self.env = _load_env()
        self.endpoint = self.env["R2_ENDPOINT"].rstrip("/")
        self.host = urllib.parse.urlparse(self.endpoint).netloc

    def _signed(self, method, bucket, key, query="", payload=b"", ctype=None):
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        path = "/" + bucket + "/" + urllib.parse.quote(key, safe="/~")
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if ctype:
            headers["content-type"] = ctype
        signed = ";".join(sorted(headers))
        canon = "\n".join([
            method, path, query,
            "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
            signed, payload_hash,
        ])
        scope = f"{date}/auto/s3/aws4_request"
        to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canon.encode()).hexdigest(),
        ])

        def _h(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k = _h(("AWS4" + self.env["AWS_SECRET_ACCESS_KEY"]).encode(), date)
        k = _h(k, "auto")
        k = _h(k, "s3")
        k = _h(k, "aws4_request")
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.env['AWS_ACCESS_KEY_ID']}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}")
        url = self.endpoint + path + (("?" + query) if query else "")
        return url, headers

    def _req(self, method, bucket, key, query="", payload=b"", ctype=None,
             stream_to=None):
        url, headers = self._signed(method, bucket, key, query, payload, ctype)
        req = urllib.request.Request(url, data=payload if method in ("PUT", "POST") else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                if stream_to is not None:
                    h = hashlib.sha256()
                    n = 0
                    while True:
                        chunk = resp.read(1 << 22)
                        if not chunk:
                            break
                        stream_to.write(chunk)
                        h.update(chunk)
                        n += len(chunk)
                    return {"size": n, "sha256": h.hexdigest()}
                return {"body": resp.read()}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def get_json(self, bucket, key):
        r = self._req("GET", bucket, key)
        if r is None:
            return None
        return json.loads(r["body"])

    def put_json(self, bucket, key, obj):
        payload = json.dumps(obj).encode()
        self._req("PUT", bucket, key, payload=payload, ctype="application/json")

    def put_file(self, bucket, key, path, ctype="application/octet-stream"):
        payload = open(path, "rb").read()
        self._req("PUT", bucket, key, payload=payload, ctype=ctype)

    def head(self, bucket, key):
        url, headers = self._signed("HEAD", bucket, key)
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return {"size": int(resp.headers.get("Content-Length", "0"))}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def list_keys(self, bucket, prefix):
        keys, token = [], ""
        while True:
            q = f"list-type=2&prefix={urllib.parse.quote(prefix, safe='')}"
            if token:
                q += f"&continuation-token={urllib.parse.quote(token, safe='')}"
            r = self._req("GET", bucket, "", query=q)
            body = r["body"].decode("utf-8", "replace")
            keys += re.findall(r"<Key>([^<]+)</Key>", body)
            m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
            if "<IsTruncated>true</IsTruncated>" in body and m:
                token = m.group(1)
            else:
                return sorted(keys)


# ---------------------------------------------------------------- helpers

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_payload_parser():
    actual = sha256_file(PAYLOAD_PARSER)
    if actual != EXPECTED_PARSER_SHA256:
        raise RuntimeError(
            f"payload parser sha256 {actual} != pinned {EXPECTED_PARSER_SHA256} "
            f"({PAYLOAD_PARSER}); refusing to process with an unverified parser")
    spec = importlib.util.spec_from_file_location("mrf_parse", PAYLOAD_PARSER)
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)
    return mp


def load_publish_helpers():
    spec = importlib.util.spec_from_file_location("publish_mini", PUBLISH_MINI)
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)
    return pm


def registry():
    db = sqlite3.connect(REGISTRY_DB)
    db.row_factory = sqlite3.Row
    return db


def tokens_of(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


# ------------------------------------------------------- identity from file

def extract_identity_json(data):
    """Identity fields from a parsed v2/v3 JSON document's header."""
    out = {}
    if not isinstance(data, dict):
        return out
    out["hospital_name"] = (
        data.get("hospital_name") or data.get("reporting_entity_name") or "")
    addr = data.get("hospital_address")
    if isinstance(addr, dict):
        out["address"] = addr
        out["city"] = addr.get("city", "")
        out["state"] = addr.get("state", "")
        out["zip"] = addr.get("zip_code") or addr.get("zip") or ""
    elif isinstance(addr, str):
        out["address_text"] = addr
    lic = data.get("license_number")
    if isinstance(lic, dict):
        out["license"] = lic.get("license_number", "")
        out["license_state"] = lic.get("state", "")
    elif lic:
        out["license"] = str(lic)
    out["version"] = str(data.get("version", "") or "")
    out["last_updated_on"] = str(data.get("last_updated_on", "") or "")
    out["reporting_entity_name"] = data.get("reporting_entity_name", "")
    return out


def extract_identity_head(raw_head):
    """Best-effort header identity from the first bytes of a large JSON
    file (v3 template files carry every header field before
    standard_charge_information). Basis is recorded by the caller."""
    text = raw_head.decode("utf-8", "replace")
    out = {}
    for key in ("hospital_name", "reporting_entity_name", "version",
                "last_updated_on"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]{{0,200}})"', text)
        if m:
            out[key] = m.group(1)
    m = re.search(r'"license_number"\s*:\s*\{[^}]*"license_number"\s*:\s*"([^"]+)"', text)
    if m:
        out["license"] = m.group(1)
    m = re.search(r'"hospital_address"\s*:\s*\{([^}]*)\}', text)
    if m:
        blk = m.group(1)
        addr = {}
        for k in ("street_address", "city", "state", "zip_code"):
            mm = re.search(rf'"{k}"\s*:\s*"([^"]*)"', blk)
            if mm:
                addr[k] = mm.group(1)
        out["address"] = addr
        out["city"] = addr.get("city", "")
        out["state"] = addr.get("state", "")
        out["zip"] = addr.get("zip_code", "")
    return out


def extract_identity_csv(raw_head, mp):
    """CMS CSVs prepend metadata (hospital name / address / license as
    leading rows in the v3 tall template, or carry them in wide-template
    columns). We scan the leading rows' cells for what is actually
    there; absence is reported, never papered over."""
    text = raw_head.decode("utf-8", "replace")
    out = {"meta_cells": []}
    try:
        _, rows = mp.delimited_rows(text)
    except Exception:
        return out
    header_idx = mp.find_header_row(rows)
    meta = rows[:header_idx] if header_idx else rows[:6]
    cells = [c.strip() for row in meta for c in row if c and c.strip()]
    out["meta_cells"] = cells[:24]
    joined = " | ".join(cells)
    m = re.search(r"license[^0-9a-z]*[:#]?\s*([A-Za-z0-9-]{4,})", joined, re.I)
    if m:
        out["license"] = m.group(1)
    m = re.search(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b", joined)
    if m:
        out["state"] = m.group(1)
        out["zip"] = m.group(2)
    return out


def identity_gate(extracted, claimed_name, db, ccn, pm):
    """VERIFIED / MISMATCH / UNVERIFIABLE for the file's own identity
    against the claimed CCN's registry row."""
    row = db.execute("SELECT * FROM hospitals WHERE ccn = ?", (ccn,)).fetchone()
    if row is None:
        return "UNVERIFIABLE", f"CCN {ccn} is not in the CMS registry the fleet carries"
    seeds = db.execute(
        "SELECT entity_name_legal, entity_name_common FROM mrf_seed WHERE ccn = ?",
        (ccn,)).fetchall()
    candidates = [row["name"]] + [
        s[k] for s in seeds for k in ("entity_name_legal", "entity_name_common") if s[k]]
    name_in_file = (extracted.get("hospital_name") or "").strip()
    if not name_in_file and extracted.get("meta_cells"):
        # CSV: any registry-name candidate token-consistent with the
        # metadata cells counts as the file naming the facility.
        joined = " ".join(extracted["meta_cells"])
        for cand in candidates:
            if pm._name_consistent(joined, [cand]):
                name_in_file = cand
                break
    if not name_in_file:
        return ("UNVERIFIABLE",
                "the file carries no in-file hospital identity (no JSON header "
                "name, no CSV metadata naming the facility)")
    if not any(pm._name_consistent(name_in_file, [c]) for c in candidates):
        # Name a different facility? Check whether it matches ANOTHER
        # registry row — that is the mis-seed signature.
        others = db.execute("SELECT ccn, name FROM hospitals").fetchall()
        for other in others:
            if other["ccn"] != ccn and pm._name_consistent(name_in_file, [other["name"]]):
                return ("MISMATCH",
                        f"the file's own header names '{name_in_file}', which is "
                        f"{other['name']} (CCN {other['ccn']}), not the claimed "
                        f"{row['name']} (CCN {ccn})")
        return ("MISMATCH",
                f"the file's own header names '{name_in_file}', which does not "
                f"match the claimed hospital {row['name']} (CCN {ccn})")
    # Name agrees. Require one corroborating signal: state, city, or
    # license presence in the file.
    state_f = (extracted.get("state") or "").upper()
    city_f = (extracted.get("city") or "").lower()
    if state_f and row["state"] and state_f != row["state"].upper():
        return ("MISMATCH",
                f"file header state {state_f} contradicts the registry state "
                f"{row['state']} for CCN {ccn}, though the name matches")
    corroborated = (
        (state_f and state_f == (row["state"] or "").upper()) or
        (city_f and city_f == (row["city"] or "").lower()) or
        bool(extracted.get("license")))
    if not corroborated:
        return ("UNVERIFIABLE",
                f"file header names '{name_in_file}' (matches CCN {ccn}) but "
                "carries no state, city, or license to corroborate it")
    return ("VERIFIED",
            f"file header names '{name_in_file}'; registry CCN {ccn} is "
            f"{row['name']} ({row['city']}, {row['state']}); corroborated by "
            + ("state" if state_f else "city" if city_f else "license"))


# ------------------------------------------------------------- source count

def count_source_entries(fmt, raw_path, data_bytes, mp):
    """Independent entry count where the format allows one, else None
    (basis recorded by the caller). CSV counts are line-based and skip
    blank lines; quoted multi-line fields can skew them, which is why a
    mismatch surfaces as a finding instead of being silently absorbed."""
    try:
        if fmt == "json" and data_bytes is not None:
            data = mp.load_json_bytes(data_bytes)
            if isinstance(data, dict):
                sci = data.get("standard_charge_information")
                if isinstance(sci, list):
                    return len(sci)
                return None
            if isinstance(data, list):
                return len(data)
            return None
        if fmt in ("csv", "tsv", "txt"):
            with open(raw_path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
            _, rows = mp.delimited_rows(text)
            if not rows:
                return None
            hdr = mp.find_header_row(rows)
            body = rows[hdr + 1:] if hdr is not None else rows[1:]
            return sum(1 for r in body if any((c or "").strip() for c in r))
        if fmt == "zip":
            zf = zipfile.ZipFile(raw_path)
            members = [m for m in zf.namelist()
                       if m.lower().endswith((".csv", ".json", ".tsv", ".txt"))]
            if not members:
                return None
            biggest = max(members, key=lambda m: zf.getinfo(m).file_size)
            inner = zf.read(biggest)
            inner_fmt, _ = mp.detect_format(inner, biggest)
            return count_source_entries(inner_fmt, raw_path, inner, mp)
    except Exception:
        return None
    return None


# --------------------------------------------------------------- validation

def fleet_findings(header, items, fmt, source_count, completeness_note):
    """Plain-language findings vs CMS v3.0.0 MRF expectations, computed
    from the file's own header and the parsed items. Each finding names
    what CMS requires and what this file does — specifically."""
    F = []

    def add(sev, rule, msg, count=None, examples=None):
        f = {"severity": sev, "rule": rule, "message": msg}
        if count is not None:
            f["count"] = count
        if examples:
            f["examples"] = examples[:5]
        F.append(f)

    version = (header.get("version") or "").strip()
    if fmt == "json":
        if not version:
            add("error", "version-missing",
                "The file does not declare a template version. CMS requires "
                "the version field (current template: 3.0.0) in the file header.")
        elif version != "3.0.0":
            add("error", "version-not-current",
                f"The file declares version {version}; the current CMS "
                "machine-readable file template is 3.0.0.")
        if not (header.get("hospital_name") or "").strip():
            add("error", "hospital-name-missing",
                "The file header does not name the hospital. CMS requires "
                "hospital_name in the file header.")
        addr = header.get("address")
        if not (isinstance(addr, dict) and addr.get("city") and addr.get("state")) \
                and not header.get("address_text"):
            add("error", "hospital-address-missing",
                "The file header has no usable hospital_address (city/state). "
                "CMS requires the hospital's address in the file header.")
        if not (header.get("license") or "").strip():
            add("error", "license-missing",
                "The file header carries no hospital license number. CMS "
                "requires the license number (or an explanation in its place).")
    updated = (header.get("last_updated_on") or "").strip()
    if fmt == "json":
        if not updated:
            add("error", "last-updated-missing",
                "The file does not state last_updated_on. CMS requires the "
                "date the file was last updated.")
        else:
            try:
                d = dt.date.fromisoformat(updated[:10])
                if d > dt.date.today():
                    add("error", "last-updated-future",
                        f"last_updated_on ({updated}) is in the future.")
            except ValueError:
                add("error", "last-updated-invalid",
                    f"last_updated_on ('{updated}') is not a valid date.")
    if source_count is not None and source_count != len(items):
        add("error", "completeness-mismatch",
            f"Completeness does not reconcile: the source file holds "
            f"{source_count} entries but parsing produced {len(items)} items. "
            f"{completeness_note}")
    if not items:
        add("error", "no-items",
            "No charge items could be parsed from this file at all.")
        return F

    def has_price(it):
        return bool(it.get("gross_charge") or it.get("cash_discount")
                    or it.get("payer_rates") or it.get("min_negotiated")
                    or it.get("max_negotiated"))

    priceless = [it for it in items if not has_price(it)]
    if priceless:
        add("error", "items-without-prices",
            "Some parsed items carry no price of any kind (no gross charge, "
            "no discounted cash price, no negotiated rate, no min/max). CMS "
            "requires a standard charge for every item listed.",
            count=len(priceless),
            examples=[str(it.get("description") or it.get("code") or "?")
                      for it in priceless])
    no_gross = sum(1 for it in items if not it.get("gross_charge"))
    if no_gross:
        add("warning", "gross-charge-missing",
            "Items with no gross charge. CMS expects a gross charge for "
            "each item or an explanation of why none applies.",
            count=no_gross)
    no_cash = sum(1 for it in items if not it.get("cash_discount"))
    if no_cash:
        add("warning", "discounted-cash-missing",
            "Items with no discounted cash price. CMS expects the "
            "discounted cash price for each item.",
            count=no_cash)
    no_payer = sum(1 for it in items if not it.get("payer_rates"))
    if no_payer:
        add("warning", "payer-rates-missing",
            "Items with no payer-specific negotiated rate. Where a "
            "negotiated rate exists for an item, CMS requires it to be "
            "listed by payer and plan.",
            count=no_payer)
    no_method = sum(
        1 for it in items for pr in (it.get("payer_rates") or [])
        if not (pr.get("methodology") or "").strip())
    if no_method:
        add("warning", "methodology-missing",
            "Negotiated rates with no methodology stated. CMS requires "
            "the methodology (fee schedule, case rate, per diem, percent "
            "of billed charges, …) behind each negotiated rate.",
            count=no_method)
    dupes = Counter(
        (it.get("code"), it.get("code_type"), it.get("setting"),
         (it.get("description") or "")[:60]) for it in items)
    dup_groups = sum(1 for v in dupes.values() if v > 1)
    if dup_groups:
        add("warning", "duplicate-items",
            "Groups of items that repeat the same code, setting, and "
            "description. Duplicates make the file ambiguous about which "
            "charge applies.",
            count=dup_groups)
    unknown_types = Counter(
        (it.get("code_type") or "").upper() for it in items
        if (it.get("code_type") or "").upper() not in KNOWN_CODE_TYPES)
    for t, n in unknown_types.most_common(5):
        add("warning", "code-type-unrecognized",
            f"Items use code type '{t or '(blank)'}', which is not one of "
            "the code types the CMS template enumerates (CPT, HCPCS, ICD, "
            "MS-DRG, APR-DRG, APC, NDC, …).",
            count=n)
    payers = {pr.get("payer") for it in items for pr in (it.get("payer_rates") or [])
              if pr.get("payer")}
    add("info", "coverage",
        f"Parsed {len(items)} items in {fmt} format; "
        f"{len(items) - len(priceless)} carry at least one price; "
        f"{len(payers)} distinct payers named.")
    return F


def official_validator(raw_path, fmt):
    """Run the official CMS validator when it is runnable. Returns
    (status, findings) where status says, in words, what happened —
    'ran', 'unavailable: …', or 'skipped: …'. Never raises."""
    size = os.path.getsize(raw_path)
    if size > OFFICIAL_VALIDATOR_MAX:
        return (f"skipped: file is {size} bytes, above the "
                f"{OFFICIAL_VALIDATOR_MAX}-byte ceiling for the official "
                "validator in this lane", [])
    exe = shutil.which("cms-hpt-validator")
    if exe:
        cmd = [exe]
    elif shutil.which("npx"):
        cmd = ["npx", "-y", "@cmsgov/hpt-validator-cli"]
    else:
        return "unavailable: neither cms-hpt-validator nor npx is installed", []
    cmd += [raw_path, "3.0.0", "--output", "json", "--error-limit", "50"]
    if fmt in ("csv", "json"):
        cmd += ["--format", fmt]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as e:
        return f"unavailable: validator execution failed ({e})", []
    try:
        result = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return (f"unavailable: validator exited {proc.returncode} without "
                f"structured output ({proc.stderr.strip()[:200]})", [])
    if not isinstance(result, dict) or not (
            "valid" in result or "errorCount" in result or "alertCount" in result):
        return (f"unavailable: validator exited {proc.returncode} without a "
                "recognizable verdict", [])
    findings = []
    errors = result.get("errors") or result.get("errorList") or []
    for e in errors[:50]:
        msg = e.get("message") or e.get("error") or str(e) if isinstance(e, dict) else str(e)
        findings.append({"severity": "error", "rule": "cms-official-validator",
                         "message": f"CMS official validator: {msg}"})
    alerts = result.get("alerts") or result.get("alertList") or []
    for a in alerts[:50]:
        msg = a.get("message") or a.get("alert") or str(a) if isinstance(a, dict) else str(a)
        findings.append({"severity": "warning", "rule": "cms-official-validator",
                         "message": f"CMS official validator: {msg}"})
    valid = result.get("valid")
    return (f"ran: official CMS validator verdict valid={valid} "
            f"errors={result.get('errorCount')} alerts={result.get('alertCount')}",
            findings)


# -------------------------------------------------------------- normalized

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "hospital"


def build_normalized(header, items, registry_name, fallback_updated):
    """A CMS v3.0.0-shaped JSON document built ONLY from the submitted
    file's own data (header fields from the file; charges from the
    parsed canonical items). Nothing is invented: a field the file
    does not carry is omitted, and the validation report says so."""
    sci_by_key = {}
    order = []
    for it in items:
        key = (it.get("description") or "", it.get("code") or "",
               it.get("code_type") or "", it.get("setting") or "",
               it.get("billing_class") or "")
        if key not in sci_by_key:
            sci_by_key[key] = {"item": it, "charges": {}}
            order.append(key)
        grp = sci_by_key[key]
        ck = (it.get("gross_charge"), it.get("cash_discount"),
              it.get("min_negotiated"), it.get("max_negotiated"))
        charge = grp["charges"].setdefault(ck, {"payers": {}})
        for pr in (it.get("payer_rates") or []):
            pk = (pr.get("payer") or "", pr.get("plan") or "")
            entry = {"payer_name": pr.get("payer") or ""}
            if pr.get("plan"):
                entry["plan_name"] = pr["plan"]
            if pr.get("rate_dollar") is not None:
                entry["standard_charge_dollar"] = pr["rate_dollar"]
            if pr.get("methodology"):
                entry["methodology"] = pr["methodology"]
            charge["payers"][pk] = entry
    sci = []
    for key in order:
        it = sci_by_key[key]["item"]
        entry = {"description": it.get("description") or ""}
        if it.get("code"):
            entry["code_information"] = [{
                "code": it["code"],
                "type": it.get("code_type") or "LOCAL",
            }]
        if it.get("setting") in ("inpatient", "outpatient", "both"):
            entry["setting"] = it["setting"]
        if it.get("billing_class"):
            entry["billing_class"] = it["billing_class"]
        charges = []
        for (gross, cash, mn, mx), grp in sci_by_key[key]["charges"].items():
            ch = {}
            if gross is not None:
                ch["gross_charge"] = gross
            if cash is not None:
                ch["discounted_cash"] = cash
            if mn is not None:
                ch["minimum"] = mn
            if mx is not None:
                ch["maximum"] = mx
            if grp["payers"]:
                ch["payers_information"] = list(grp["payers"].values())
            charges.append(ch)
        entry["standard_charges"] = charges
        sci.append(entry)
    updated = (header.get("last_updated_on") or "").strip()
    try:
        dt.date.fromisoformat(updated[:10])
    except ValueError:
        updated = fallback_updated
    doc = {
        "reporting_entity_name": header.get("reporting_entity_name")
        or header.get("hospital_name") or registry_name,
        "reporting_entity_type": "hospital",
        "version": "3.0.0",
        "last_updated_on": updated[:10],
        "hospital_name": header.get("hospital_name") or registry_name,
        "standard_charge_information": sci,
    }
    addr = header.get("address")
    if isinstance(addr, dict) and any(addr.values()):
        doc["hospital_address"] = {
            k: v for k, v in addr.items() if v}
    if header.get("license"):
        lic = {"license_number": header["license"]}
        if header.get("license_state"):
            lic["state"] = header["license_state"]
        doc["license_number"] = lic
    return doc


def render_report_md(report):
    lines = [
        f"# Hospital Ledger validation report",
        "",
        f"Submission: {report['submission_id']}",
        f"Hospital (claimed): {report['hospital_claimed']} — CCN {report['ccn_claimed']}",
        f"Processed: {report['generated_at']} by fleet processor v{PROCESSOR_VERSION}",
        f"File: {report['filename']} ({report['size_bytes']} bytes, SHA-256 {report['sha256']})",
        f"Format detected: {report['format_detected']}; items parsed: {report['items_parsed']} "
        f"({report['priced_items']} priced); source entries: {report['source_entry_count']}",
        "",
        "Submitting a file to Hospital Ledger does not make a hospital "
        "CMS-compliant. Compliance means posting the machine-readable file "
        "on the hospital's own website. The normalized file provided with "
        "this report is built for exactly that.",
        "",
        f"Identity gate: {report['identity']['verdict']} — {report['identity']['detail']}",
        f"Official CMS validator: {report['engines']['official_validator']}",
        f"Listing: {report['listing']['status']}"
        + (f" — {report['listing']['reason']}" if report['listing'].get('reason') else ""),
        "",
        "## Findings",
        "",
    ]
    if not report["findings"]:
        lines.append("No findings.")
    for f in report["findings"]:
        lines.append(f"- **{f['severity'].upper()}** ({f['rule']}): {f['message']}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- listing

def known_domains(db, ccn):
    hosts = set()
    for row in db.execute(
            "SELECT mrf_url, mrf_page FROM mrf_seed WHERE ccn = ?", (ccn,)):
        for v in (row["mrf_url"], row["mrf_page"]):
            if v:
                try:
                    h = urllib.parse.urlparse(
                        v if "://" in v else "https://" + v).hostname
                    if h:
                        hosts.add(h.lower())
                except ValueError:
                    pass
    return hosts


def next_batch_number():
    best = 0
    for d in (VERDICTS_DIR, os.path.expanduser("~/workspace/hl-gap-research")):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            m = re.search(r"_b(\d+)\.tsv$", n)
            if m:
                best = max(best, int(m.group(1)))
    return best + 1


def write_verdict(rec, identity_detail, verified_domain, file_header_name):
    bn = next_batch_number()
    fname = f"d1_verdicts_b{bn}.tsv"
    detail = (
        f"hospital-submitted via submission {rec['id']}: submitter proved "
        f"control of {verified_domain} with the site-token check; identity "
        f"evidenced in-file as '{file_header_name}' and the identity gate "
        f"returned VERIFIED against the claimed CCN {rec['ccn_claimed']} "
        f"({identity_detail}); completeness reconciled against the source "
        f"file's own entry count; provenance class: hospital-submitted.")
    row = f"{rec['ccn_claimed']}\thttps://hospitalledger.com/submit/status/{rec['id']}" \
          f"\tACCEPT\t{detail}\n"
    for d in (VERDICTS_DIR, VERDICTS_PENDING_DIR):
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, fname)
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write("ccn\turl\tverdict\tdetail\n")
        with open(path, "a") as f:
            # The verdict row is the fleet's evidence record: the
            # identity detail is quoted so the publish guard can check
            # it — intentional clear-text evidence storage.
            # codeql[py/clear-text-storage-sensitive-data]
            f.write(row)
    return fname


def attempt_listing(rec, header, items, priced, source_count, db, pm,
                    workdir, dry_run, mp):
    """Returns the listing dict for the record. Every refusal carries
    its reason; nothing here can list a hospital on a partial pass."""
    ccn = rec["ccn_claimed"]
    L = {"status": "refused", "reason": "", "verdict_batch": "",
         "published_n": None}
    if ccn == QUARANTINED_CCN:
        L["reason"] = "CCN 150042 is quarantined fleet-wide and is never listed."
        return L
    if not rec.get("opt_in_listing"):
        L["status"] = "not_opted_in"
        return L
    if rec["verify"]["status"] != "verified":
        L["status"] = "awaiting_verification"
        L["reason"] = ("the submitter has not yet proved control of the "
                       "hospital's website (site-token check)")
        return L
    verdict = rec["_identity_verdict"]
    if verdict != "VERIFIED":
        L["reason"] = (f"identity gate returned {verdict}: "
                       f"{rec['_identity_detail']}")
        return L
    if source_count is not None and source_count != len(items):
        L["reason"] = (f"completeness does not reconcile (source "
                       f"{source_count} entries vs {len(items)} parsed items)")
        return L
    if priced == 0:
        L["reason"] = "the file parses but contains no priced items"
        return L
    canonical_path = os.path.join(STORE_DIR, f"{ccn}.json.gz")
    if os.path.exists(canonical_path):
        L["reason"] = ("this hospital is already listed from its own "
                       "published file; a submission does not replace a "
                       "harvested listing")
        return L
    hosts = known_domains(db, ccn)
    domain = (rec["verify"].get("domain") or "").lower()
    if not hosts:
        L["reason"] = ("the fleet has no transparency page on file for this "
                       "CCN, so the verified domain cannot be bound to the "
                       "hospital — listing refused (fail closed)")
        return L
    if domain not in hosts:
        L["reason"] = (f"the verified domain {domain} is not the domain on "
                       f"file for CCN {ccn} ({', '.join(sorted(hosts))}) — "
                       "listing refused (fail closed)")
        return L

    # All gates passed. Record provenance, seed the canonical in the
    # fleet's exact shape, and publish through the standard path.
    reg = db.execute("SELECT name FROM hospitals WHERE ccn = ?",
                     (ccn,)).fetchone()
    canon_name = header.get("hospital_name") or (reg["name"] if reg else None) \
        or rec["hospital_name_claimed"]
    batch = write_verdict(rec, rec["_identity_detail"], domain, canon_name)
    L["verdict_batch"] = batch
    record = {
        "ccn": ccn,
        "hospital_name": canon_name,
        "source_url": f"https://hospitalledger.com/submit/status/{rec['id']}",
        "fetched_at": utcnow(),
        "format_detected": rec["_format_detected"],
        "row_count": len(items),
        "column_notes": list(getattr(mp, "COLUMN_MAP_NOTES", []) or []),
        "items": items,
    }
    if rec.get("_source_updated"):
        record["source_updated"] = rec["_source_updated"]
    tmp = canonical_path + ".tmp-submission"
    with gzip.open(tmp, "wt") as f:
        json.dump(record, f, separators=(",", ":"))
    os.replace(tmp, canonical_path)
    log("submission-canonical-seeded", ccn=ccn, items=len(items), batch=batch)

    # The publisher's own preflight on what was just seeded: its
    # canonical acceptance check and its key guard (the ^\d{6}$ /
    # ACCEPT-evidenced suffixed-key rule). Failing here refuses the
    # listing with the publisher's own reason instead of discovering it
    # inside the publish subprocess.
    from pathlib import Path as _Path
    pre = {}
    _fn = getattr(pm, "canonical_ok", None)
    if callable(_fn):
        try:
            pre["canonical_ok"] = bool(_fn(canonical_path))
        except Exception as e:  # noqa: BLE001
            pre["canonical_ok_error"] = repr(e)
    _fn = getattr(pm, "key_refusal", None)
    if callable(_fn):
        try:
            pre["key_refusal"] = _fn(ccn, verdicts_dir=_Path(VERDICTS_DIR),
                                     store_dir=_Path(STORE_DIR))
        except Exception as e:  # noqa: BLE001
            pre["key_refusal_error"] = repr(e)
    L["preflight"] = pre
    if pre.get("canonical_ok") is False:
        L["reason"] = ("the seeded canonical failed publish_mini's "
                       "canonical_ok acceptance check — listing refused")
        return L
    if pre.get("key_refusal"):
        L["reason"] = ("publish_mini's key guard refused this CCN: "
                       + str(pre["key_refusal"]))
        return L

    cmd = [sys.executable, PUBLISH_MINI]
    if dry_run:
        cmd.append("--dry-run")
    cmd.append(ccn)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                          cwd=FLEET)
    tail = (proc.stdout + proc.stderr)[-2000:]
    with open(os.path.join(workdir, "publish.log"), "w") as f:
        f.write(tail)
    if proc.returncode == 9:
        L["status"] = "publish_deferred"
        L["reason"] = ("the publish lock is held by another fleet run; the "
                       "canonical is seeded and the verdict recorded, and "
                       "the publish can be re-run")
        return L
    if proc.returncode != 0 or "SUCCESS" not in proc.stdout and not dry_run:
        if dry_run and proc.returncode == 0:
            L["status"] = "dry_run_ok"
            L["reason"] = "publish_mini.py --dry-run completed (seed + slim + patch + validate)"
            return L
        L["status"] = "publish_deferred"
        L["reason"] = f"publish_mini.py exited {proc.returncode}; see publish.log"
        return L
    if dry_run:
        L["status"] = "dry_run_ok"
        L["reason"] = "publish_mini.py --dry-run completed (seed + slim + patch + validate)"
        return L
    L["status"] = "published"
    return L


# -------------------------------------------------------------------- main

def process_one(r2, rec, mp, pm, dry_run_listing):
    sid = rec["id"]
    workdir = os.path.join(WORK_ROOT, sid)
    os.makedirs(workdir, exist_ok=True)
    rec["status"] = "processing"
    rec["processing"] = rec.get("processing") or {}
    rec["processing"]["claimed_at"] = utcnow()
    rec["processing"]["processor_version"] = PROCESSOR_VERSION
    r2.put_json(SUBMISSIONS_BUCKET, f"submissions/{sid}/submission.json", rec)

    try:
        raw_path = os.path.join(workdir, "raw" + os.path.splitext(rec["filename"])[1])
        free = shutil.disk_usage(WORK_ROOT).free
        need = rec["size_bytes_declared"] * 4 + 2 * 1024**3
        if free < need:
            raise RuntimeError(
                f"insufficient space on the work volume: {free} free, "
                f"{need} required (4x the declared file size + 2 GiB)")
        with open(raw_path, "wb") as f:
            got = r2._req("GET", SUBMISSIONS_BUCKET, rec["raw_key"], stream_to=f)
        if got is None:
            raise RuntimeError("raw object missing from R2 after a completed upload")
        sha = got["sha256"]
        size = got["size"]
        if size != rec["size_bytes_declared"]:
            rec["status"] = "size_mismatch"
            rec["processing"]["sha256"] = sha
            rec["processing"]["size_bytes_actual"] = size
            r2.put_json(SUBMISSIONS_BUCKET, f"submissions/{sid}/submission.json", rec)
            log("submission-size-mismatch", id=sid, declared=rec["size_bytes_declared"],
                actual=size)
            return
        if size > IN_MEMORY_PARSE_MAX:
            raise RuntimeError(
                f"file is {size} bytes, above this lane's in-memory parse "
                f"ceiling of {IN_MEMORY_PARSE_MAX} bytes (derived from the "
                "fleet's largest handled MRF and the Mini's 16 GB RAM); "
                "post the file on the hospital website and the harvester "
                "will process it there")

        data = open(raw_path, "rb").read()
        fmt, items = mp.parse(data, rec["filename"],
                              hospital_name=rec["hospital_name_claimed"],
                              ccn=rec["ccn_claimed"])
        items = items or []
        priced = sum(1 for it in items
                     if it.get("gross_charge") or it.get("cash_discount")
                     or it.get("payer_rates"))
        declared = mp.extract_declared_updated(data)

        # Identity: full header when the doc parses in memory, else the
        # head-scan (basis noted in the detail).
        header, header_basis = {}, "none"
        if fmt == "json":
            try:
                doc = mp.load_json_bytes(data)
                if isinstance(doc, dict):
                    header = extract_identity_json(doc)
                    header_basis = "full document header"
            except Exception:
                pass
            if not header:
                header = extract_identity_head(data[:1 << 20])
                header_basis = "first-1MiB head scan"
        elif fmt in ("csv", "tsv", "txt"):
            header = extract_identity_csv(data[:1 << 20], mp)
            header_basis = "leading metadata rows"
        elif fmt == "zip":
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                members = [m for m in zf.namelist()
                           if m.lower().endswith((".json", ".csv", ".tsv", ".txt"))]
                if members:
                    biggest = max(members, key=lambda m: zf.getinfo(m).file_size)
                    inner = zf.read(biggest)
                    inner_fmt, _ = mp.detect_format(inner, biggest)
                    if inner_fmt == "json":
                        header = extract_identity_head(inner[:1 << 20])
                    elif inner_fmt in ("csv", "tsv", "txt"):
                        header = extract_identity_csv(inner[:1 << 20], mp)
                    header_basis = f"largest member {biggest}"
            except Exception:
                pass

        source_count = count_source_entries(fmt, raw_path, data, mp)
        completeness_note = (
            f"source entries counted independently ({fmt}); parse is by the "
            "pinned fleet parser, which raises rather than truncating at "
            "its source cap.")

        db = registry()
        try:
            verdict, detail = identity_gate(header, rec["hospital_name_claimed"],
                                            db, rec["ccn_claimed"], pm)
        finally:
            db.close()
        detail = f"{detail} [identity basis: {header_basis}]"

        off_status, off_findings = official_validator(raw_path, fmt)
        findings = off_findings + fleet_findings(
            header, items, fmt, source_count, completeness_note)

        db = registry()
        try:
            reg_row = db.execute("SELECT name FROM hospitals WHERE ccn = ?",
                                 (rec["ccn_claimed"],)).fetchone()
        finally:
            db.close()
        registry_name = reg_row["name"] if reg_row else rec["hospital_name_claimed"]
        normalized = build_normalized(
            header, items, registry_name,
            dt.date.today().isoformat())
        normalized_filename = (
            f"{rec['ccn_claimed']}_{slugify(registry_name)}_standardcharges.json")
        normalized_path = os.path.join(workdir, normalized_filename)
        with open(normalized_path, "w") as f:
            json.dump(normalized, f, separators=(",", ":"))
        normalized_key = f"submissions/{sid}/normalized/{normalized_filename}"
        r2.put_file(SUBMISSIONS_BUCKET, normalized_key, normalized_path,
                    ctype="application/json")

        # A site verification can land while we work; the Worker's
        # write is the newer truth for the verify block. Re-read and
        # merge it before the listing attempt reads rec["verify"].
        fresh = r2.get_json(SUBMISSIONS_BUCKET,
                            f"submissions/{sid}/submission.json")
        if fresh and (fresh.get("verify") or {}).get("status") == "verified" \
                and rec["verify"].get("status") != "verified":
            rec["verify"] = fresh["verify"]
            log("submission-verify-merged", id=sid)
        rec["_identity_verdict"] = verdict
        rec["_identity_detail"] = detail
        rec["_format_detected"] = fmt
        if declared:
            rec["_source_updated"] = declared
        db = registry()
        try:
            listing = attempt_listing(rec, header, items, priced, source_count,
                                      db, pm, workdir, dry_run_listing, mp)
        finally:
            db.close()
        if listing["status"] == "published":
            try:
                idx = r2.get_json(PARSED_BUCKET, "prices/index.json")
                if idx:
                    for h in idx.get("hospitals", []):
                        if h.get("ccn") == rec["ccn_claimed"]:
                            listing["published_n"] = h.get("n")
                            break
            except Exception as e:
                log("submission-index-verify-failed", id=sid, error=str(e)[:200])

        report = {
            "submission_id": sid,
            "generated_at": utcnow(),
            "hospital_claimed": rec["hospital_name_claimed"],
            "ccn_claimed": rec["ccn_claimed"],
            "filename": rec["filename"],
            "size_bytes": size,
            "sha256": sha,
            "format_detected": fmt,
            "items_parsed": len(items),
            "priced_items": priced,
            "source_entry_count": source_count,
            "engines": {
                "fleet_parser": f"mrf_parse.py sha256 {EXPECTED_PARSER_SHA256[:12]}… "
                                f"(pinned loud-cap build), format={fmt}",
                "official_validator": off_status,
                "fleet_checks": "ran",
            },
            "identity": {"verdict": verdict, "detail": detail},
            "findings": findings,
            "normalized": {
                "filename": normalized_filename,
                "entries": len(normalized["standard_charge_information"]),
                "note": "built from the submitted file's own data only; "
                        "fields the file does not carry are omitted, and "
                        "the findings above say which",
            },
            "listing": listing,
        }
        report_key = f"submissions/{sid}/report.json"
        r2.put_json(SUBMISSIONS_BUCKET, report_key, report)
        md_key = f"submissions/{sid}/report.md"
        md_path = os.path.join(workdir, "report.md")
        with open(md_path, "w") as f:
            # The submitter's own report in the private, token-gated
            # submissions bucket — it necessarily names the hospital.
            # codeql[py/clear-text-storage-sensitive-data]
            f.write(render_report_md(report))
        r2.put_file(SUBMISSIONS_BUCKET, md_key, md_path, ctype="text/markdown")

        rec["processing"].update({
            "sha256": sha,
            "size_bytes_actual": size,
            "format_detected": fmt,
            "source_entry_count": source_count,
            "items_parsed": len(items),
            "priced_items": priced,
            "identity_verdict": verdict,
            "identity_detail": detail,
            "findings": findings,
            "report_key": report_key,
            "normalized_key": normalized_key,
            "normalized_filename": normalized_filename,
        })
        rec["listing"] = listing
        rec["status"] = "listed" if listing["status"] == "published" else "processed"
        for k in ("_identity_verdict", "_identity_detail", "_format_detected",
                  "_source_updated"):
            rec.pop(k, None)
        r2.put_json(SUBMISSIONS_BUCKET, f"submissions/{sid}/submission.json", rec)
        log("submission-processed", id=sid, format=fmt, items=len(items),
            priced=priced, identity=verdict, listing=listing["status"],
            findings=len(findings))
    except Exception as e:
        rec["status"] = "process_failed"
        rec["processing"] = rec.get("processing") or {}
        rec["processing"]["identity_detail"] = f"processing failed: {e}"
        try:
            r2.put_json(SUBMISSIONS_BUCKET, f"submissions/{sid}/submission.json", rec)
        except Exception:
            pass
        log("submission-process-failed", id=sid, error=str(e)[:400])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="process exactly this submission id")
    ap.add_argument("--dry-run-listing", action="store_true",
                    help="qualifying listings run publish_mini.py --dry-run")
    args = ap.parse_args()

    mp = load_payload_parser()  # raises (loudly) on a sha mismatch
    pm = load_publish_helpers()
    r2 = R2()
    os.makedirs(WORK_ROOT, exist_ok=True)

    if args.id:
        ids = [args.id]
    else:
        keys = r2.list_keys(SUBMISSIONS_BUCKET, "submissions/")
        ids = sorted({k.split("/")[1] for k in keys
                      if k.endswith("/submission.json")})
    claimed = 0
    for sid in ids:
        rec = r2.get_json(SUBMISSIONS_BUCKET, f"submissions/{sid}/submission.json")
        if rec is None:
            log("submission-record-missing", id=sid)
            continue
        status = rec.get("status")
        claimable = status == "uploaded"
        if (status == "processed"
                and (rec.get("listing") or {}).get("status") == "awaiting_processing"
                and (rec.get("verify") or {}).get("status") == "verified"):
            # The submitter verified their site after processing ran
            # (the Worker re-arms the listing on a successful check).
            # Re-running is idempotent: report and normalized file are
            # rewritten identically; the listing attempt starts fresh.
            claimable = True
        if status == "processing":
            claimed_at = (rec.get("processing") or {}).get("claimed_at") or ""
            try:
                t = dt.datetime.strptime(claimed_at, "%Y-%m-%dT%H:%M:%SZ")
                age = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - t).total_seconds()
                claimable = age > STALE_CLAIM_SECONDS
            except ValueError:
                claimable = True
        if not claimable:
            continue
        if rec.get("ccn_claimed") == QUARANTINED_CCN:
            rec["status"] = "process_failed"
            rec["processing"] = rec.get("processing") or {}
            rec["processing"]["identity_detail"] = (
                "CCN 150042 is quarantined fleet-wide; submission refused")
            r2.put_json(SUBMISSIONS_BUCKET, f"submissions/{sid}/submission.json", rec)
            log("submission-quarantined", id=sid)
            continue
        claimed += 1
        process_one(r2, rec, mp, pm, args.dry_run_listing)
    log("processor-done", claimed=claimed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
