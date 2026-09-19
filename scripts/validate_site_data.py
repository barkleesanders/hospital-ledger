#!/usr/bin/env python3
"""validate_site_data.py — the gate for hospitalledger.com data artifacts.

Validates a directory laid out exactly like the R2 key tree the Worker reads
(see ops/data-contract.md). Run it BEFORE uploading (producer side) and AFTER
downloading meta/* back from R2 (consumer side); a run that fails here must
not be published.

    python3 scripts/validate_site_data.py <dir> --tier counts   # meta/* only
    python3 scripts/validate_site_data.py <dir> --tier full     # everything

Exit codes (three outcomes, never two):
    0  PASS — every artifact present for the tier validated
    1  FAIL — a required artifact is missing or violates the contract
    2  UNMEASURED — the directory is unreadable / no artifacts found at all
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

CONTRACT_VERSION = "1"
CCN_RE = re.compile(r"^[0-9A-Z]{6}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
DISPLAY_TYPES = {"CPT", "HCPCS", "DRG", "MS-DRG", "REV", "CDM"}
# ceiling: cpt-index is streamed into the Worker's memory on every /api/cpt-index
# hit; refresh.sh has enforced <=15 MB / exactly 5000 codes since 2026-05
# (P1 regression class: a 650 MB index once shipped). Live 2026-09-18: 11.3 MB.
CPT_INDEX_CODES = 5000
CPT_INDEX_MAX_BYTES = 15 * 1024 * 1024
# ceiling: Workers static-asset limit is 25 MiB, which is why per-hospital files
# live in R2 at all; R2 single objects go to 5 TiB. Largest real file 2026-06:
# prices/020006.json = 85 MiB. Anything past 256 MiB is a parser blow-up.
PRICE_FILE_MAX_BYTES = 256 * 1024 * 1024
SUMMARY_MAX_AGE_DAYS = 400

REQUIRED_SUMMARY = {
    "generated_at": str,
    "total_facilities": int,
    "cms_required_total": int,
    "live_mrf_total": int,
    "compliant": int,
    "compliance_pct": (int, float),
    "missing": int,
    "enforcement_actions_total": int,
    "standardized_price_index_hospitals": int,
    "standardized_price_hospitals": int,
    "standardized_price_rows": int,
    "cpt_indexed_hospitals": int,
    "cpt_indexed_rows": int,
}


class Report:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.checked: list[str] = []

    def ok(self, what: str) -> None:
        self.checked.append(what)

    def fail(self, what: str) -> None:
        self.fails.append(what)

    def expect(self, cond: bool, what: str) -> bool:
        (self.ok if cond else self.fail)(what)
        return cond


def load_json(path: Path, rep: Report) -> object | None:
    try:
        with path.open("rb") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        rep.fail(f"{path}: unreadable JSON ({exc})")
        return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- meta/ ----
def check_manifest(root: Path, rep: Report) -> dict | None:
    p = root / "meta" / "manifest.json"
    if not rep.expect(p.is_file(), "meta/manifest.json present"):
        return None
    m = load_json(p, rep)
    if not isinstance(m, dict):
        rep.fail("meta/manifest.json: not an object")
        return None
    rep.expect(m.get("contract_version") == CONTRACT_VERSION,
               f"manifest.contract_version == {CONTRACT_VERSION!r} (got {m.get('contract_version')!r})")
    rep.expect(isinstance(m.get("generated_at"), str) and bool(ISO_RE.match(m["generated_at"])),
               "manifest.generated_at is ISO-8601")
    rep.expect(isinstance(m.get("producer"), str) and len(m["producer"]) > 0,
               "manifest.producer names who made this run")
    rep.expect(m.get("refresh_tier") in ("counts", "full"),
               f"manifest.refresh_tier in (counts, full) (got {m.get('refresh_tier')!r})")
    arts = m.get("artifacts")
    if rep.expect(isinstance(arts, dict) and len(arts) > 0, "manifest.artifacts is a non-empty map"):
        for key, meta in arts.items():
            fp = root / key
            if not rep.expect(fp.is_file(), f"manifest lists {key} and it exists"):
                continue
            if isinstance(meta, dict):
                if "bytes" in meta:
                    rep.expect(meta["bytes"] == fp.stat().st_size, f"{key}: bytes match manifest")
                if "sha256" in meta:
                    rep.expect(meta["sha256"] == sha256(fp), f"{key}: sha256 matches manifest")
    return m


def check_summary(root: Path, rep: Report, manifest: dict | None) -> dict | None:
    p = root / "meta" / "summary.json"
    if not rep.expect(p.is_file(), "meta/summary.json present"):
        return None
    s = load_json(p, rep)
    if not isinstance(s, dict):
        rep.fail("meta/summary.json: not an object")
        return None
    for k, t in REQUIRED_SUMMARY.items():
        v = s.get(k)
        rep.expect(isinstance(v, t) and not isinstance(v, bool), f"summary.{k} is {getattr(t, '__name__', 'number')}")
    if rep.fails:
        return s
    gen = s["generated_at"]
    rep.expect(bool(ISO_RE.match(gen)), "summary.generated_at is ISO-8601")
    try:
        when = dt.datetime.fromisoformat(gen.replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        rep.expect(when <= now + dt.timedelta(hours=1), "summary.generated_at is not in the future")
        rep.expect(now - when <= dt.timedelta(days=SUMMARY_MAX_AGE_DAYS),
                   f"summary.generated_at within {SUMMARY_MAX_AGE_DAYS} days")
    except ValueError:
        rep.fail("summary.generated_at unparsable")
    if manifest and isinstance(manifest.get("generated_at"), str):
        rep.expect(manifest["generated_at"] == gen, "manifest.generated_at == summary.generated_at")
    # Ordering invariants — the counts nest.
    rep.expect(0 < s["compliant"] <= s["cms_required_total"] <= s["total_facilities"],
               "compliant <= cms_required_total <= total_facilities")
    rep.expect(s["missing"] == s["cms_required_total"] - s["compliant"],
               "missing == cms_required_total - compliant")
    expect_pct = round(100 * s["compliant"] / s["cms_required_total"], 1)
    rep.expect(abs(s["compliance_pct"] - expect_pct) <= 0.15,
               f"compliance_pct {s['compliance_pct']} ≈ {expect_pct}")
    rep.expect(s["standardized_price_hospitals"] <= s["standardized_price_index_hospitals"] <= s["compliant"],
               "standardized_price_hospitals <= standardized_price_index_hospitals <= compliant")
    rep.expect(s["cpt_indexed_hospitals"] <= s["standardized_price_hospitals"],
               "cpt_indexed_hospitals <= standardized_price_hospitals")
    rep.expect(s["cpt_indexed_rows"] <= s["standardized_price_rows"],
               "cpt_indexed_rows <= standardized_price_rows")
    for k in ("states", "types", "worst_offenders"):
        rep.expect(isinstance(s.get(k), list), f"summary.{k} is a list")
    return s


def check_hospitals(root: Path, rep: Report, summary: dict | None) -> None:
    p = root / "meta" / "hospitals.json"
    if not rep.expect(p.is_file(), "meta/hospitals.json present"):
        return
    h = load_json(p, rep)
    if not isinstance(h, list) or not h:
        rep.fail("meta/hospitals.json: not a non-empty list")
        return
    bad = [x for x in h if not (isinstance(x, dict) and CCN_RE.match(str(x.get("ccn", "")))
                                  and isinstance(x.get("name"), str) and isinstance(x.get("state"), str)
                                  and isinstance(x.get("required"), bool) and isinstance(x.get("has_live_mrf"), bool))]
    rep.expect(not bad, f"hospitals.json rows carry ccn/name/state/required/has_live_mrf ({len(bad)} bad)")
    ccns = [x.get("ccn") for x in h]
    rep.expect(len(ccns) == len(set(ccns)), "hospitals.json CCNs are unique")
    if summary:
        rep.expect(len(h) == summary["total_facilities"], f"len(hospitals.json)={len(h)} == summary.total_facilities")
        live_req = sum(1 for x in h if x.get("required") and x.get("has_live_mrf"))
        rep.expect(live_req == summary["compliant"], f"required∧live={live_req} == summary.compliant")
        req = sum(1 for x in h if x.get("required"))
        rep.expect(req == summary["cms_required_total"], f"required={req} == summary.cms_required_total")


# --------------------------------------------------------------- prices/ ----
def check_prices_index(root: Path, rep: Report, summary: dict | None) -> list[str]:
    p = root / "prices" / "index.json"
    if not rep.expect(p.is_file(), "prices/index.json present"):
        return []
    d = load_json(p, rep)
    hs = d.get("hospitals") if isinstance(d, dict) else None
    if not isinstance(hs, list) or not hs:
        rep.fail("prices/index.json: {hospitals:[...]} non-empty")
        return []
    bad = 0
    for x in hs:
        if not (isinstance(x, dict) and CCN_RE.match(str(x.get("ccn", ""))) and isinstance(x.get("n"), int)
                and isinstance(x.get("name"), str) and isinstance(x.get("counts"), dict)
                and isinstance(x.get("cpt_indexed"), int) and isinstance(x.get("compliance"), dict)):
            bad += 1
    rep.expect(bad == 0, f"prices/index.json rows carry ccn/n/name/counts/cpt_indexed/compliance ({bad} bad)")
    if summary:
        rep.expect(len(hs) == summary["standardized_price_index_hospitals"],
                   f"len(prices/index)={len(hs)} == summary.standardized_price_index_hospitals")
        rep.expect(sum(x.get("n", 0) for x in hs) == summary["standardized_price_rows"],
                   "sum(n) == summary.standardized_price_rows")
        rep.expect(sum(1 for x in hs if x.get("n", 0) > 0) == summary["standardized_price_hospitals"],
                   "count(n>0) == summary.standardized_price_hospitals")
        rep.expect(sum(x.get("cpt_indexed", 0) for x in hs) == summary["cpt_indexed_rows"],
                   "sum(cpt_indexed) == summary.cpt_indexed_rows")
    return [str(x.get("ccn")) for x in hs]


def check_price_files(root: Path, rep: Report, index_ccns: list[str]) -> None:
    files = sorted((root / "prices").glob("*.json")) if (root / "prices").is_dir() else []
    files = [f for f in files if f.name != "index.json"]
    if not files:
        rep.ok("prices/{ccn}.json: none present (counts tier or index-only upload)")
        return
    idx = set(index_ccns)
    bad: list[str] = []
    for f in files:
        ccn = f.stem
        if not CCN_RE.match(ccn):
            bad.append(f"{f.name}: filename is not a CCN"); continue
        if f.stat().st_size > PRICE_FILE_MAX_BYTES:
            bad.append(f"{f.name}: {f.stat().st_size} bytes > {PRICE_FILE_MAX_BYTES}"); continue
        d = load_json(f, rep)
        if not isinstance(d, dict):
            bad.append(f"{f.name}: not an object"); continue
        if d.get("ccn") != ccn:
            bad.append(f"{f.name}: ccn field {d.get('ccn')!r} != filename"); continue
        if not isinstance(d.get("hospital_name"), str) or not isinstance(d.get("items"), list):
            bad.append(f"{f.name}: missing hospital_name/items"); continue
        for it in d["items"][:200]:
            if not (isinstance(it, dict) and isinstance(it.get("code"), str)
                    and it.get("type") in DISPLAY_TYPES
                    and all(k in it for k in ("gross", "cash", "min", "max"))):
                bad.append(f"{f.name}: item shape {it!r:.80}"); break
        if idx and ccn not in idx:
            bad.append(f"{f.name}: not listed in prices/index.json")
    rep.expect(not bad, f"prices/{{ccn}}.json ({len(files)} files) conform" + (": " + "; ".join(bad[:5]) if bad else ""))


# -------------------------------------------------------------- indexes/ ----
def check_cpt_index(root: Path, rep: Report) -> None:
    p = root / "indexes" / "cpt-index.json"
    if not rep.expect(p.is_file(), "indexes/cpt-index.json present"):
        return
    rep.expect(p.stat().st_size <= CPT_INDEX_MAX_BYTES,
               f"cpt-index.json {p.stat().st_size / 1048576:.1f} MB <= {CPT_INDEX_MAX_BYTES / 1048576:.0f} MB")
    d = load_json(p, rep)
    if not isinstance(d, dict):
        rep.fail("cpt-index.json: not an object keyed by code"); return
    rep.expect(len(d) == CPT_INDEX_CODES, f"cpt-index has exactly {CPT_INDEX_CODES} codes (got {len(d)})")
    # Real rows (live 2026-09-18, 125,000 rows): ccn+gross+type always; cash,
    # min, max, pc, payer_max only when that hospital's MRF supplied them.
    bad = 0
    for code, rows in list(d.items())[:500]:
        if not (isinstance(rows, list) and all(isinstance(r, dict) and CCN_RE.match(str(r.get("ccn", "")))
                                                and "gross" in r and isinstance(r.get("type"), str)
                                                for r in rows[:20])):
            bad += 1
    rep.expect(bad == 0, f"cpt-index rows are [{{ccn,gross,type,+optional cash/min/max/pc/payer_max}}] ({bad} bad of first 500)")


# ------------------------------------------------------------ aggregates/ ----
def check_aggregates(root: Path, rep: Report) -> None:
    a = root / "aggregates"
    pi = a / "payers-index.json"
    if rep.expect(pi.is_file(), "aggregates/payers-index.json present"):
        d = load_json(pi, rep)
        ok = isinstance(d, dict) and isinstance(d.get("featured"), list) and isinstance(d.get("total"), int)
        rep.expect(ok, "payers-index: {featured:[...], total:int}")
        if ok:
            rep.expect(all(isinstance(x, dict) and isinstance(x.get("slug"), str) and isinstance(x.get("display"), str)
                           for x in d["featured"]), "payers-index.featured rows carry slug/display")
    cr = a / "compliance-ranking.json"
    if rep.expect(cr.is_file(), "aggregates/compliance-ranking.json present"):
        d = load_json(cr, rep)
        rep.expect(isinstance(d, dict) and isinstance(d.get("hospitals"), list)
                   and isinstance(d.get("grade_distribution"), dict) and isinstance(d.get("total"), int),
                   "compliance-ranking: {hospitals:[...], grade_distribution:{}, total:int}")
    det = sorted((a / "cpt-detail").glob("*.json")) if (a / "cpt-detail").is_dir() else []
    if rep.expect(len(det) > 0, f"aggregates/cpt-detail/*.json present ({len(det)})"):
        bad = 0
        for f in det[:300]:
            d = load_json(f, rep)
            if not (isinstance(d, dict) and d.get("code") == f.stem and isinstance(d.get("stats"), dict)
                    and isinstance(d.get("hospitals"), list)):
                bad += 1
        rep.expect(bad == 0, f"cpt-detail files carry code==filename/stats/hospitals ({bad} bad of first 300)")
    pay = sorted((a / "payer").glob("*.json")) if (a / "payer").is_dir() else []
    if rep.expect(len(pay) > 0, f"aggregates/payer/*.json present ({len(pay)})"):
        bad = 0
        for f in pay[:300]:
            d = load_json(f, rep)
            if not (isinstance(d, dict) and isinstance(d.get("payer"), dict) and d["payer"].get("slug") == f.stem
                    and isinstance(d.get("hospitals"), list)):
                bad += 1
        rep.expect(bad == 0, f"payer files carry payer.slug==filename/hospitals ({bad} bad of first 300)")


# ---------------------------------------------------------------- main ----
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="directory laid out like the R2 key tree")
    ap.add_argument("--tier", choices=("counts", "full"), default=None,
                    help="counts = meta/* only; full = everything. Default: read manifest.refresh_tier")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"UNMEASURED: {root} is not a directory", file=sys.stderr)
        return 2
    if not any(root.rglob("*.json")):
        print(f"UNMEASURED: no JSON artifacts under {root}", file=sys.stderr)
        return 2
    rep = Report()
    manifest = check_manifest(root, rep)
    tier = args.tier or (manifest or {}).get("refresh_tier") or "counts"
    summary = check_summary(root, rep, manifest)
    check_hospitals(root, rep, summary)
    if tier == "full":
        ccns = check_prices_index(root, rep, summary)
        check_price_files(root, rep, ccns)
        check_cpt_index(root, rep)
        check_aggregates(root, rep)
    else:
        # A counts-tier run may still ship prices/index.json (cheap, keeps the
        # homepage registry in step with summary). Validate it if present.
        if (root / "prices" / "index.json").is_file():
            check_prices_index(root, rep, summary)
    if not args.quiet:
        for c in rep.checked:
            print(f"  ok   {c}")
        for f in rep.fails:
            print(f"  FAIL {f}")
    if rep.fails:
        print(f"FAIL: {len(rep.fails)} contract violation(s), {len(rep.checked)} checks passed (tier={tier})")
        return 1
    print(f"PASS: {len(rep.checked)} checks (tier={tier}, contract v{CONTRACT_VERSION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
