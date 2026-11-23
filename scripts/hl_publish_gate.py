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

POLICY (the owner's decision, not mine)
---------------------------------------
`admit` has three outcomes. REFUSE always refuses. UNPROVEN (the file carries no usable
identity: legacy canonicals, 513 of them in wave 8) is a *warning* by default, which is
production's behaviour, because refusing them would halt the campaign. `unproven="refuse"`
is the strict policy and is tested, so flipping it is a one-word change when the owner
decides (INVENTORY M2: "publish refuses a canonical with no recorded evidence").
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hl_admission  # noqa: E402  (the one admission function)

UNPROVEN_WARN = "warn"
UNPROVEN_REFUSE = "refuse"

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


def identity_gate(ccn: str,
                  head: Optional[Mapping],
                  registry: Optional[Mapping],
                  holds: Optional[Iterable[str]],
                  verdict_rows: Sequence[tuple] = (),
                  unproven: str = UNPROVEN_WARN) -> tuple:
    """Gate 1 for one CCN. Returns (refusal_reason_or_None, warnings).

    head:         the store canonical's header fields: {'source_url', 'source_identity'}
    registry:     {'name','city','state','zip','aliases'} or None
    holds:        the adjudicated hold set, or None when the holds file was unreadable
                  (fails CLOSED: a publisher that cannot see the hold list must not publish)
    verdict_rows: [(bn, url, 'ACCEPT'|'REJECT', detail)] for this CCN
    unproven:     "warn" (production parity) or "refuse" (strict)
    """
    if unproven not in (UNPROVEN_WARN, UNPROVEN_REFUSE):
        raise ValueError(f"unproven must be 'warn' or 'refuse', not {unproven!r}")
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
    verdict = hl_admission.admit(ccn, registry, source_from_identity(si))
    if verdict.outcome == hl_admission.REFUSE:
        return f"identity mismatch: {verdict.detail}", warnings
    if verdict.outcome == hl_admission.UNPROVEN:
        if unproven == UNPROVEN_REFUSE:
            return f"identity unproven: {verdict.detail}", warnings
        warnings.append(f"identity unproven ({verdict.reason}): {verdict.detail}")
    return None, warnings
