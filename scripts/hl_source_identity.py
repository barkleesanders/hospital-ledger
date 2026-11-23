#!/usr/bin/env python3
"""The source file's own stated identity, read from its header (remediation wave 4, lane D).

WHY THIS EXISTS
---------------
The publish gate (`hl_publish_gate`) decides identity from a canonical's `source_identity`:
what the source FILE says about itself, read from its header at build time. That read lives
on the Mini in `~/hl-fleet/remote_build.py::extract_source_identity`: untracked, untested from
here. Measured on the fleet's retained source files (2026-11-23), it returns NOTHING for 143 of
the hospitals whose files are retained, bound and live, and 108 of those are one cause:

* a UTF-8 byte-order mark in front of the header. `"﻿hospital_name"` is not
  `"hospital_name"`, so the CMS header row is not recognised (92 plain or zipped CSVs), and
  `"﻿{"` does not start with `{`, so a JSON file is read as a CSV (16 files).

Two smaller defects in the same function, both measured:

* the 2.0 CSV template names the facility column `hospital_location`; the reader knows only
  `location_name`, so those headers lose the one name the registry usually matches;
* a header row followed by a blank line (`\r\r\n` line ends: 224043 TaraVista) has no value
  row where the reader looks, and the header row itself is then read as a `key,value` meta
  row: the file's hospital_name is recorded as the literal text `last_updated_on`. An invented
  identity is worse than none. The mirror case is fixed with it: a `hospital_name,<value>`
  meta row in the first line was taken for a header row, with the same invented result.

This module is that function, in the repo, with those fixes and nothing else changed:
same keys, same 200-character cut, same read limits, same output shape. For every retained
file the fleet reader already reads, it returns the same identity (scripts/
prove_source_identity.sh measures that on the Mini). It is pure: bytes in, dict out; no
filesystem, network or clock.

An empty dict means the file's header carried no readable identity. It is never a guess: a web
page, a spreadsheet with no CMS header keys, or a custom layout whose first cell happens to be
a hospital's name yields {} and the gate treats that as no evidence.

Wiring it into `remote_build.py` on the Mini is a separate, owner-approved production change
(not done by the PR that added this file).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import zipfile

_ID_KEYS = ("hospital_name", "location_name", "hospital_address",
            "hospital_address_city", "hospital_address_state",
            "hospital_address_zip", "type_2_npi", "npi", "ein",
            "hospital_ein", "license_number")
# Header spellings of an identity key that are read and then stored under the canonical key.
# `hospital_location` is the CMS 2.0 CSV template's name for `location_name`.
_KEY_ALIASES = {"npi": "type_2_npi", "hospital_ein": "ein", "hospital_location": "location_name"}
_READ_KEYS = _ID_KEYS + ("hospital_location",)
_HEADER_TOKENS = frozenset(_READ_KEYS + ("last_updated_on", "version"))
_HEAD = 1 << 18
_BOM = "﻿"


def _text(raw_head: bytes) -> str:
    """Decoded header text with a leading byte-order mark removed."""
    return raw_head.decode("utf-8", "replace").lstrip(_BOM)


def _si_from_text(text, into):
    for k in _READ_KEYS:
        if k in into:
            continue
        m = re.search(r'"%s"\s*:\s*' % k, text)
        if m:
            try:
                val, _ = json.JSONDecoder().raw_decode(text, m.end())
                if isinstance(val, list):
                    val = " | ".join(str(x) for x in val if isinstance(x, str))
                if isinstance(val, str) and val.strip():
                    into[k] = val.strip()[:200]
            except Exception:
                pass


def _si_from_csv_head(head_text, into):
    lines = head_text.splitlines()[:16]
    if not lines:
        return
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines))))
    except Exception:
        return
    # a blank line is not a row: with `\r\r\n` line ends the value row is two lines below the header
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return
    r0 = [c.strip().lower() for c in rows[0]]
    # A header row lists several key NAMES. `hospital_name,Example General Hospital` is a meta
    # row (one key and its value), and must not send the reader to the row below for a value.
    header_row = "hospital_name" in r0 and \
        sum(1 for c in r0 if c.split("|")[0].strip() in _HEADER_TOKENS) >= 2
    if header_row and len(rows) > 1:
        for i, k in enumerate(r0):
            key = k.split("|")[0].strip()
            if key in _READ_KEYS and i < len(rows[1]) and rows[1][i].strip():
                into.setdefault(key, rows[1][i].strip()[:200])
    # `key,value` meta rows. The header row is a list of key NAMES, never a key and its value.
    for r in rows[1 if header_row else 0:14]:
        if len(r) >= 2 and r[1].strip():
            key = r[0].strip().lower().split("|")[0].strip()
            if key in _READ_KEYS:
                into.setdefault(key, r[1].strip()[:200])


def _si_from_head(head: str, into):
    if head.lstrip()[:1] in ("[", "{"):
        _si_from_text(head, into)
    else:
        _si_from_csv_head(head, into)


def extract_source_identity(raw: bytes) -> dict:
    """The identity a source file states in its own header: JSON header keys, the CSV header-row
    and meta-row forms, a ZIP's largest CSV member (or first JSON member), XLSX shared strings.
    Best-effort and fail-soft: any error yields what was read so far, usually {}."""
    into: dict = {}
    try:
        if raw[:2] == b"PK":
            zf = zipfile.ZipFile(io.BytesIO(raw))
            names = zf.namelist()
            if any(n.startswith("xl/") for n in names):
                if "xl/sharedStrings.xml" in names:
                    raw_ss = zf.read("xl/sharedStrings.xml")[:_HEAD]
                    strs = re.findall(r"<t[^>]*>([^<]{1,200})</t>", raw_ss.decode("utf-8", "replace"))
                    low = [s.strip().lower() for s in strs]
                    for i, s in enumerate(low):
                        if s in _READ_KEYS and i + 1 < len(strs) and strs[i + 1].strip():
                            into.setdefault(s, strs[i + 1].strip()[:200])
            else:
                csvs = [n for n in names if n.lower().endswith((".csv", ".txt"))]
                jsons = [n for n in names if n.lower().endswith(".json")]
                if csvs:
                    target = max(csvs, key=lambda n: zf.getinfo(n).file_size)
                    with zf.open(target) as mf:
                        _si_from_csv_head(_text(mf.read(_HEAD)), into)
                elif jsons:
                    with zf.open(jsons[0]) as mf:
                        _si_from_text(_text(mf.read(_HEAD)), into)
        elif raw[:2] == b"\x1f\x8b":
            with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8", errors="replace") as f:
                head = f.read(_HEAD).lstrip(_BOM)
            _si_from_head(head, into)
        else:
            _si_from_head(_text(raw[:_HEAD]), into)
    except Exception:
        pass
    out: dict = {}
    for k, v in into.items():
        if k != "hospital_location":
            out.setdefault(_KEY_ALIASES.get(k, k), v)
    # a header that carries both spellings keeps its own `location_name`
    if into.get("hospital_location"):
        out.setdefault("location_name", into["hospital_location"])
    return out
