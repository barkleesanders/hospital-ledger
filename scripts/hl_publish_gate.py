#!/usr/bin/env python3
"""The publisher-facing identity gate, in the repo (INVENTORY M2, step 2b, 2026-11-23).

WHY THIS EXISTS
---------------
Step 2a put the admission decision in the repo (`hl_admission.admit`) but the gate that
actually guards *publishing* is still `~/hl-fleet/publish_mini.py::identity_gate` on the
Mini: untracked, untested from here, and (read from its source, measured by
scripts/prove_publish_gate.sh) it refuses only on a *positive contradiction* it can parse out
of an address string. Two of its properties matter:

* a pair whose state AND city equal the registry's but whose hospital differs (W13 050663
  Los Angeles Community Hospital vs Kindred Hospital Los Angeles; 184004 Eastern State vs UK
  Hospital) passes by construction: name only counts when no location matches;
* its city/state extraction (`_address_cities_states`) needs a comma *before* the city, so a
  street-less `Alton, IL 62002` yields no location at all, and a spelled-out state
  (`Aurora, Illinois 60506`) is not a state to it.

This module is the same gate with the same return contract, `(refusal_reason | None,
warnings)`, but pure (no filesystem, network or clock: the caller loads the canonical head,
registry row, holds and verdict rows) and decided by `hl_admission.admit`, so there is one
identity decision in the repo, not two. The Mini can `import` it; wiring it in is a separate,
owner-approved production change (not done by the PR that added this file).

POLICY (the owner's decision, 2026-11-23, step 2c)
--------------------------------------------------
`admit` has three outcomes. REFUSE always refuses. UNPROVEN (the file carries no usable
identity, e.g. legacy canonicals with no recorded evidence) now REFUSES BY DEFAULT: unproven
data does not publish (INVENTORY M2: "publish refuses a canonical with no recorded
evidence"). `unproven="warn"` still exists as an explicit, tested value for tooling that wants
the old parity behaviour, but it is no longer the default anywhere.

The only way a rebranded facility publishes is the named OVERRIDE LIST in the gate
configuration (scripts/hl_publish_gate_policy.json). Each entry carries the hospital id, the
accepted name(s), a reason, and (because the live data forced it, see below) the exact source
file it covers and the attested location. An override is deliberately narrow:

* it applies only when the canonical's `source_url` IS the pinned file. A different file under
  the same CCN gets no help from it, so a later wrong file cannot ride an old adjudication;
* it is consulted only after plain `admit` did not ACCEPT, and only for the reasons an override
  can legitimately repair (name mismatch, no registry row, no in-file name, no anchoring
  address). A state or city CONTRADICTION is never overridden;
* it never decides by itself: it hands `admit` a registry row (accepted names as aliases, blank
  location fields filled from the attested anchor, never replacing a non-blank value) and, only
  where the file's own header carries no name/address, the attested name/anchor. The same single
  `hl_admission.admit` decides. Anything the file's own header does say is judged as written.

WHY THE FILE PIN AND THE ATTESTED ANCHOR (measured 2026-11-23, on the Mini): for 372004 the
publisher's registry has NO row (publish tree db and hospitals.json both empty for it) and the
store canonical carries NO `source_identity`; the only evidence is the W12/b83 ACCEPT verdict
that quotes the file's header. A name alias alone can therefore never publish it. The anchor is
the owner's attestation of the location, not independent evidence; the warning an applied
override emits says which parts were attested.

The policy file is validated hard and fails CLOSED (`PolicyError`): `gate_from_policy` turns a
bad policy into "refuse everything", never into "no overrides".
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hl_admission  # noqa: E402  (the one admission function)

UNPROVEN_WARN = "warn"
UNPROVEN_REFUSE = "refuse"
DEFAULT_UNPROVEN = UNPROVEN_REFUSE  # the owner's strict policy; flipping it needs a new decision
DEFAULT_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "hl_publish_gate_policy.json")

_HOSPITAL_ID = re.compile(r"^[0-9A-Z]{6}$")  # the widest grammar in use (src/lib/hospital-id.ts)
_POLICY_KEYS = {"unproven", "overrides"}
_OVERRIDE_KEYS = {"hospital_id", "accepted_names", "reason", "source_url", "anchor"}
_ANCHOR_KEYS = {"city", "state", "zip"}
# What an override may repair. A state/city contradiction (REFUSE) is never in this set.
_OVERRIDABLE = frozenset({hl_admission.R_NAME_MISMATCH, hl_admission.R_NO_REGISTRY,
                          hl_admission.R_NO_FILE_NAME, hl_admission.R_NO_ADDRESS})


class PolicyError(ValueError):
    """The gate policy is unreadable or invalid. The publisher must refuse everything."""


@dataclass(frozen=True)
class Override:
    hospital_id: str
    accepted_names: tuple
    reason: str
    source_url: str = ""
    anchor: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    unproven: str = DEFAULT_UNPROVEN
    overrides: Mapping = field(default_factory=dict)  # hospital_id -> Override


def parse_policy(doc) -> Policy:
    """Validate a decoded policy document. Strict on purpose: a typo'd key, a dead override
    (one that could never match) or a duplicate is an error, not a silently ignored line."""
    if not isinstance(doc, dict):
        raise PolicyError("policy must be a JSON object")
    unknown = set(doc) - _POLICY_KEYS
    if unknown:
        raise PolicyError(f"unknown policy key(s): {sorted(unknown)}")
    unproven = doc.get("unproven", DEFAULT_UNPROVEN)
    if unproven not in (UNPROVEN_WARN, UNPROVEN_REFUSE):
        raise PolicyError(f"unproven must be 'warn' or 'refuse', not {unproven!r}")
    entries = doc.get("overrides", [])
    if not isinstance(entries, list):
        raise PolicyError("overrides must be a list")
    out: dict = {}
    for i, e in enumerate(entries):
        where = f"overrides[{i}]"
        if not isinstance(e, dict):
            raise PolicyError(f"{where} must be an object")
        bad = set(e) - _OVERRIDE_KEYS
        if bad:
            raise PolicyError(f"{where}: unknown key(s) {sorted(bad)}")
        hid = e.get("hospital_id")
        if not isinstance(hid, str) or not _HOSPITAL_ID.match(hid):
            raise PolicyError(f"{where}: hospital_id must be a 6-character CCN, not {hid!r}")
        if hid in out:
            raise PolicyError(f"{where}: duplicate override for hospital_id {hid}")
        names = e.get("accepted_names")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise PolicyError(f"{where} ({hid}): accepted_names must be a non-empty list of names")
        for n in names:
            # name_consistent needs >= 2 tokens, so a shorter name could never match: reject it
            # at load time rather than ship an override that silently does nothing.
            if len(set(hl_admission.tokens(n))) < 2:
                raise PolicyError(f"{where} ({hid}): accepted name {n!r} has fewer than 2 "
                                  "distinguishing words and could never match")
        reason = e.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise PolicyError(f"{where} ({hid}): a non-empty reason is required")
        url = e.get("source_url")
        if not isinstance(url, str) or not re.match(r"^https?://\S+$", url.strip()):
            raise PolicyError(f"{where} ({hid}): source_url must be the exact http(s) file the "
                              "override covers; an override not bound to a file would let any "
                              "later file under this CCN ride an old adjudication")
        anchor = e.get("anchor")
        if not isinstance(anchor, dict) or set(anchor) - _ANCHOR_KEYS or \
                not all(isinstance(v, str) for v in anchor.values()):
            raise PolicyError(f"{where} ({hid}): anchor must be an object of string city/state/zip")
        if not anchor.get("state", "").strip() or not (anchor.get("city", "").strip()
                                                       or anchor.get("zip", "").strip()):
            raise PolicyError(f"{where} ({hid}): anchor needs a state and a city or zip, or the "
                              "address test could never be satisfied")
        out[hid] = Override(hid, tuple(n.strip() for n in names), reason.strip(), url.strip(),
                            {k: v.strip() for k, v in anchor.items()})
    return Policy(unproven=unproven, overrides=out)


def load_policy(path: Optional[str] = None) -> Policy:
    """Read and validate the policy file. Raises PolicyError on ANY problem."""
    path = path or DEFAULT_POLICY_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PolicyError(f"cannot read gate policy {path}: {exc}") from exc
    return parse_policy(doc)

_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
    "guam": "GU", "virgin islands": "VI",
}
_STATE_NAME_RE = "|".join(sorted((re.escape(n) for n in _STATES), key=len, reverse=True))
# one campus: [street,] City, ST [ZIP]   (street optional; ST may be spelled out)
_CAMPUS = re.compile(
    r"(?:^|,)\s*([^,|]+?)\s*,\s*([A-Za-z]{2}|" + _STATE_NAME_RE + r")\.?"
    r"(?:\s+(\d{5})(?:-\d{4})?)?\s*$", re.IGNORECASE)


def _state_code(text: str) -> str:
    t = (text or "").strip().rstrip(".")
    return _STATES.get(t.lower(), t.upper())


def parse_addresses(address: Optional[str]) -> list:
    """`hospital_address` -> [{'city','state','zip'}, ...]; campuses are `|`-separated.
    A street is optional, a state may be spelled out. A part that does not parse is
    dropped, never guessed: an empty list means UNPROVEN downstream, not a match."""
    out = []
    for part in str(address or "").split("|"):
        m = _CAMPUS.search(part.strip())
        if m:
            out.append({"city": m.group(1).strip(), "state": _state_code(m.group(2)),
                        "zip": m.group(3) or ""})
    return out


def source_from_identity(si: Optional[Mapping]) -> dict:
    """The canonical's `source_identity` (the source file's own header, stamped at build
    time) as the `source` argument of `hl_admission.admit`."""
    si = si if isinstance(si, Mapping) else {}
    name = (si.get("hospital_name") or "").strip() or (si.get("location_name") or "").strip()
    addrs = parse_addresses(si.get("hospital_address"))
    if si.get("hospital_address_city") or si.get("hospital_address_state"):
        addrs.append({"city": si.get("hospital_address_city") or "",
                      "state": _state_code(si.get("hospital_address_state") or ""),
                      "zip": si.get("hospital_address_zip") or ""})
    return {"hospital_name": name, "addresses": addrs}


def _norm_url(url: Optional[str]) -> str:
    try:
        p = urlsplit((url or "").strip())
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), unquote(p.path), p.query, ""))
    except Exception:
        return (url or "").strip()


def _ein_of_url(url: Optional[str]) -> Optional[str]:
    """EIN slug in a source filename (<9 digits> or <2>-<7> at a token boundary)."""
    base = os.path.basename((url or "").split("?")[0])
    m = re.search(r"(?:^|[_\-.])(\d{9}|\d{2}-\d{7})(?:[_\-.]|$)", base)
    return m.group(1).replace("-", "") if m else None


def winning_verdict(rows: Sequence[tuple]) -> Optional[tuple]:
    """(bn, url, verdict, detail) with the highest bN; the later row wins a tie.
    Rows are (bn, url, verdict, detail) in verdict-file order."""
    win = None
    for bn, url, verdict, detail in rows:
        if win is None or bn >= win[0]:
            win = (bn, url, verdict, detail)
    return win


def _with_override(registry: Optional[Mapping], ov: Override) -> dict:
    """The registry row as the override sees it: the accepted names join the aliases (and name
    the row if it has none), and the anchor fills only BLANK location fields. A non-blank
    registry value is never replaced."""
    row = dict(registry or {})
    row["aliases"] = list(row.get("aliases") or ()) + list(ov.accepted_names)
    if not str(row.get("name") or "").strip():
        row["name"] = ov.accepted_names[0]
    for key, value in ov.anchor.items():
        if not str(row.get(key) or "").strip():
            row[key] = value
    return row


def _source_with_override(source: Mapping, ov: Override) -> tuple:
    """The file's identity as the override sees it. Only a MISSING name or MISSING address is
    attested from the override; anything the file's own header says is judged as written.
    Returns (source, [which parts were attested])."""
    src = dict(source)
    attested = []
    if not str(src.get("hospital_name") or "").strip():
        src["hospital_name"] = ov.accepted_names[0]
        attested.append("name")
    if not src.get("addresses"):
        src["addresses"] = [dict(ov.anchor)]
        attested.append("address")
    return src, attested


def identity_gate(ccn: str,
                  head: Optional[Mapping],
                  registry: Optional[Mapping],
                  holds: Optional[Iterable[str]],
                  verdict_rows: Sequence[tuple] = (),
                  unproven: str = DEFAULT_UNPROVEN,
                  overrides: Optional[Mapping] = None) -> tuple:
    """Gate 1 for one CCN. Returns (refusal_reason_or_None, warnings).

    head:         the store canonical's header fields: {'source_url', 'source_identity'}
    registry:     {'name','city','state','zip','aliases'} or None
    holds:        the adjudicated hold set, or None when the holds file was unreadable
                  (fails CLOSED: a publisher that cannot see the hold list must not publish)
    verdict_rows: [(bn, url, 'ACCEPT'|'REJECT', detail)] for this CCN
    unproven:     "refuse" (the default: unproven data does not publish) or "warn"
    overrides:    {hospital_id: Override}, the named rebrand list from the gate policy
    """
    if unproven not in (UNPROVEN_WARN, UNPROVEN_REFUSE):
        raise ValueError(f"unproven must be 'warn' or 'refuse', not {unproven!r}")
    overrides = overrides or {}
    warnings: list = []
    if holds is None:
        return "adjudicated holds file unreadable, failing closed", warnings
    if ccn in set(holds):
        return "CCN is on the adjudicated hold list", warnings
    if head is None:
        if unproven == UNPROVEN_REFUSE:
            return "no store canonical head readable to verify identity against", warnings
        warnings.append("no store canonical head readable; the seed step's canonical check "
                        "remains the guard")
        return None, warnings

    canon_url = head.get("source_url") or ""
    rows = sorted(verdict_rows, key=lambda r: r[0])
    # R0: the exact file a registry REJECT rejected for this CCN.
    if canon_url:
        cu = _norm_url(canon_url)
        for bn, url, verdict, _detail in rows:
            if verdict == "REJECT" and url and _norm_url(url) == cu:
                later_accept = any(v2 == hl_admission.ACCEPT and u2 and _norm_url(u2) == cu and b2 >= bn
                                   for b2, u2, v2, _d2 in rows)
                if not later_accept:
                    return (f"identity mismatch: canonical source_url is the exact file a "
                            f"registry REJECT (b{bn}) rejected for this CCN"), warnings
    # R2: EIN contradiction between the canonical's file and the winning verdict's file.
    si = head.get("source_identity")
    si = si if isinstance(si, Mapping) else {}
    win = winning_verdict(rows)
    file_ein = None
    if si.get("ein"):
        digits = re.sub(r"\D", "", str(si["ein"]))
        file_ein = digits if len(digits) == 9 else None
    file_ein = file_ein or _ein_of_url(canon_url)
    win_ein = _ein_of_url(win[1]) if win else None
    if file_ein and win_ein and file_ein != win_ein:
        return (f"identity mismatch: canonical file EIN {file_ein} contradicts the "
                f"registry-ACCEPTED file's EIN {win_ein} (winning verdict b{win[0]})"), warnings

    # The identity decision is hl_admission.admit, nothing else.
    source = source_from_identity(si)
    verdict = hl_admission.admit(ccn, registry, source)
    ov = overrides.get(ccn)
    if (ov is not None and verdict.outcome != hl_admission.ACCEPT
            and verdict.reason in _OVERRIDABLE
            and canon_url and _norm_url(canon_url) == _norm_url(ov.source_url)):
        # A named rebrand, for exactly the pinned file: the same admit(), on the registry row and
        # file identity as the override completes them (see the module docstring).
        o_source, attested = _source_with_override(source, ov)
        verdict = hl_admission.admit(ccn, _with_override(registry, ov), o_source)
        if verdict.outcome == hl_admission.ACCEPT:
            warnings.append(
                f"override applied: {ccn} file {ov.source_url} accepted under named rebrand "
                f"{list(ov.accepted_names)}; attested by the override: "
                f"{', '.join(attested + ['accepted names']) if attested else 'accepted names'} "
                f"({ov.reason})")
    if verdict.outcome == hl_admission.REFUSE:
        return f"identity mismatch: {verdict.detail}", warnings
    if verdict.outcome == hl_admission.UNPROVEN:
        if unproven == UNPROVEN_REFUSE:
            return f"identity unproven: {verdict.detail}", warnings
        warnings.append(f"identity unproven ({verdict.reason}): {verdict.detail}")
    return None, warnings


def gate_from_policy(ccn: str,
                     head: Optional[Mapping],
                     registry: Optional[Mapping],
                     holds: Optional[Iterable[str]],
                     verdict_rows: Sequence[tuple] = (),
                     policy: Optional[Policy] = None,
                     policy_path: Optional[str] = None) -> tuple:
    """`identity_gate` under the gate policy file. This is what the publisher calls.

    A policy that cannot be read or validated REFUSES (fails closed), it never degrades to
    "no overrides" or to the lenient setting. Pass a loaded `policy` to avoid re-reading."""
    if policy is None:
        try:
            policy = load_policy(policy_path)
        except PolicyError as exc:
            return f"gate policy invalid, failing closed: {exc}", []
    return identity_gate(ccn, head, registry, holds, verdict_rows,
                         unproven=policy.unproven, overrides=policy.overrides)
