#!/usr/bin/env python3
"""The one admission function: may this file be listed under this CCN?

WHY THIS EXISTS (INVENTORY M2, step 2a, 2026-11-23)
---------------------------------------------------
On 2026-11-02 "batch 000" attached files to the wrong CCNs because the identity
test was a *name-similarity* test: a 0.5 token ratio (`grade_pair`) that two
different hospitals sharing "Memorial Hospital" or "Baptist Medical Center"
passed. Fourteen of those wrong pairs were later adjudicated by hand (W12/W13,
frozen in tests/fixtures/identity/known_wrong_pairs.json). The repo's own gate
(`process_submissions.identity_gate`) carried the same shape: a name that is a
token-superset of the registry name plus a *state* match, or the mere presence
of a license number, returned VERIFIED even when the CITY contradicted
(registry MEMORIAL HOSPITAL, Carthage IL vs file "Alton Memorial Hospital",
Alton IL: {memorial, hospital} <= {alton, memorial, hospital}; IL == IL).

THE RULE
--------
A name is a *necessary* condition and never a *sufficient* one. ACCEPT needs
positive, address-anchored evidence from the file's own header:

    name consistent  AND  state equal  AND  (city equal  OR  zip5 equal)

* a state contradiction REFUSEs even when the name matches;
* a city contradiction (city differs and the zip does not rescue it) REFUSEs
  even when the name matches;
* a license number is not evidence of identity (it only says the file has one);
* no usable address evidence is UNPROVEN, never ACCEPT.

Outcomes (three, never two):  ACCEPT / REFUSE / UNPROVEN.

`Verdict` cannot be constructed as ACCEPT with empty evidence, so no caller can
write an ACCEPT that carries no reason (INVENTORY M2, types rung).

Pure: stdlib only, no filesystem, no network, no clock. Callers pass the
registry row, the file's own header identity, and the holds/quarantine maps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

ACCEPT = "ACCEPT"
REFUSE = "REFUSE"
UNPROVEN = "UNPROVEN"
OUTCOMES = (ACCEPT, REFUSE, UNPROVEN)

# Reason codes are stable strings so callers/tests can branch without parsing prose.
R_HOLD = "hold"
R_QUARANTINED = "quarantined"
R_NO_REGISTRY = "no-registry-row"
R_NO_FILE_NAME = "no-file-name"
R_NAME_MISMATCH = "name-mismatch"
R_STATE_CONTRADICTION = "state-contradiction"
R_CITY_CONTRADICTION = "city-contradiction"
R_NO_ADDRESS = "no-address-evidence"
R_ANCHORED = "address-anchored"

# Words that spell the same place two ways; normalising them is what keeps a true
# match from REFUSING ("Saint Louis" vs "St. Louis"). Fail closed on anything else.
_ABBREV = {"st": "saint", "mt": "mount", "ft": "fort"}


class AdmissionError(ValueError):
    """An impossible Verdict (e.g. ACCEPT with no evidence)."""


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str
    evidence: tuple = field(default_factory=tuple)
    detail: str = ""

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise AdmissionError(f"unknown admission outcome {self.outcome!r}")
        if not self.reason:
            raise AdmissionError("a Verdict must carry a reason code")
        if self.outcome == ACCEPT and not self.evidence:
            raise AdmissionError(
                "ACCEPT requires non-empty evidence: name alone is not identity "
                "(batch-000, 2026-11-02)")


def accept_row(ccn: str, url: str, detail: str, evidence: Iterable[str]) -> str:
    """The ONLY place an ACCEPT verdict row (ccn<TAB>url<TAB>ACCEPT<TAB>detail) is
    rendered. It refuses to render one with no evidence, by constructing the
    Verdict first. scripts/check_identity_invariants.py fails any other source
    file that carries a bare "ACCEPT" literal."""
    ev = tuple(e for e in evidence if e and str(e).strip())
    verdict = Verdict(ACCEPT, R_ANCHORED, evidence=ev, detail=detail)
    return f"{ccn}\t{url}\t{verdict.outcome}\t{verdict.detail}\n"


def tokens(text: Optional[str]) -> list:
    """Lower-cased alphanumeric tokens with apostrophes dropped and
    saint/mount/fort abbreviations normalised. Case is normalised HERE, so no
    caller has to pre-lowercase (b3e2df9: uppercase registry names never matched
    because the helper only lowered the header, not the candidates)."""
    s = (text or "").lower().replace("'", "").replace("’", "")
    return [_singular(_ABBREV.get(t, t)) for t in re.findall(r"[a-z0-9]+", s)]


def _singular(tok: str) -> str:
    """Symmetric plural fold ('Hospitals' == 'Hospital'); applied to BOTH sides so
    it can only widen equivalence between spellings of one word."""
    if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def name_consistent(header_name: Optional[str], candidates: Iterable[Optional[str]]) -> bool:
    """True when one side's token set contains the other's (both >= 2 tokens).
    NECESSARY for admission, never sufficient: 'Memorial Hospital' is a subset of
    'Alton Memorial Hospital'. Case-insensitive on both sides."""
    ht = set(tokens(header_name))
    if len(ht) < 2:
        return False
    for cand in candidates:
        ct = set(tokens(cand))
        if len(ct) >= 2 and (ct <= ht or ht <= ct):
            return True
    return False


def _norm_city(text: Optional[str]) -> str:
    return " ".join(tokens(text))


def _zip5(text: Optional[str]) -> str:
    m = re.search(r"\d{5}", str(text or ""))
    return m.group(0) if m else ""


def _norm_state(text: Optional[str]) -> str:
    return (text or "").strip().upper()


def _addresses(source: Mapping) -> list:
    """The file's own address(es) as [{'city','state','zip'}...]. A multi-campus
    header may carry several; `addresses` wins over the flat city/state/zip."""
    out = []
    for a in source.get("addresses") or ():
        if isinstance(a, Mapping):
            out.append({"city": a.get("city", ""), "state": a.get("state", ""),
                        "zip": a.get("zip", "") or a.get("zip_code", "")})
    if not out and (source.get("city") or source.get("state") or source.get("zip")):
        out.append({"city": source.get("city", ""), "state": source.get("state", ""),
                    "zip": source.get("zip", "")})
    return out


def _judge_address(reg_state, reg_city, reg_zip, addr, reg_city_raw="") -> tuple:
    """-> (outcome, reason, evidence_str) for ONE file address vs the registry."""
    f_state, f_city, f_zip = _norm_state(addr["state"]), _norm_city(addr["city"]), _zip5(addr["zip"])
    if f_state and reg_state and f_state != reg_state:
        return (REFUSE, R_STATE_CONTRADICTION,
                f"file address state {f_state} != registry state {reg_state}")
    state_ok = bool(f_state) and f_state == reg_state
    city_eq = bool(f_city) and f_city == reg_city
    zip_eq = bool(f_zip) and f_zip == reg_zip
    if state_ok and (city_eq or zip_eq):
        how = "city" if city_eq else "zip5"
        return (ACCEPT, R_ANCHORED,
                f"state {f_state} equal and {how} equal "
                f"({addr['city'] or addr['zip']})")
    if f_city and reg_city and not city_eq and not zip_eq:
        return (REFUSE, R_CITY_CONTRADICTION,
                f"file address city '{addr['city']}' != registry city "
                f"'{reg_city_raw}' and zip does not corroborate")
    return (UNPROVEN, R_NO_ADDRESS, "state/city/zip in the file do not anchor the registry address")


def admit(ccn: str,
          registry: Optional[Mapping],
          source: Mapping,
          holds: Optional[Mapping] = None,
          quarantined: Optional[Mapping] = None) -> Verdict:
    """Decide whether the file whose own header is `source` may be listed under `ccn`.

    registry:    {'name','city','state','zip','aliases': [alt names]} or None
    source:      {'hospital_name','city','state','zip','addresses':[...]} — the
                 file's OWN header, not the filename or the URL
    holds:       {ccn: reason} adjudicated holds (a held CCN never admits)
    quarantined: {ccn: reason} quarantined CCNs
    """
    holds = holds or {}
    quarantined = quarantined or {}
    if ccn in holds:
        return Verdict(REFUSE, R_HOLD, detail=f"CCN {ccn} is on an adjudicated hold: {holds[ccn]}")
    if ccn in quarantined:
        return Verdict(REFUSE, R_QUARANTINED,
                       detail=f"CCN {ccn} is quarantined: {quarantined[ccn]}")
    if not registry:
        return Verdict(UNPROVEN, R_NO_REGISTRY,
                       detail=f"CCN {ccn} has no registry row to compare the file against")

    file_name = (source.get("hospital_name") or "").strip()
    if not file_name:
        return Verdict(UNPROVEN, R_NO_FILE_NAME,
                       detail="the file carries no in-file hospital name")
    names = [registry.get("name")] + list(registry.get("aliases") or ())
    if not name_consistent(file_name, names):
        return Verdict(REFUSE, R_NAME_MISMATCH,
                       detail=f"the file's own header names '{file_name}', which does not "
                              f"match the registry name '{registry.get('name')}' for CCN {ccn}")

    reg_state = _norm_state(registry.get("state"))
    reg_city = _norm_city(registry.get("city"))
    reg_zip = _zip5(registry.get("zip"))
    addrs = _addresses(source)
    if not addrs:
        return Verdict(UNPROVEN, R_NO_ADDRESS,
                       detail=f"file names '{file_name}' (matches CCN {ccn}) but carries no "
                              "state, city or zip to anchor it; a name or a license number "
                              "alone is not identity")
    judged = [_judge_address(reg_state, reg_city, reg_zip, a, registry.get("city") or "")
              for a in addrs]
    for outcome, reason, why in judged:
        if outcome == ACCEPT:
            return Verdict(ACCEPT, reason, evidence=(
                f"name '{file_name}' consistent with registry '{registry.get('name')}'", why),
                detail=f"file header names '{file_name}'; registry CCN {ccn} is "
                       f"{registry.get('name')} ({registry.get('city')}, {registry.get('state')}); "
                       f"corroborated by {why}")
    # No address anchored. It is a positive contradiction only when EVERY address
    # in the header contradicts; a mixed header (one campus unproven, one wrong) is
    # unproven, not refused.
    if all(outcome == REFUSE for outcome, _, _ in judged):
        _, reason, why = judged[0]
        return Verdict(REFUSE, reason, detail=f"{why}, though the name matches CCN {ccn}")
    unproven = next(j for j in judged if j[0] == UNPROVEN)
    return Verdict(UNPROVEN, R_NO_ADDRESS, detail=unproven[2])
