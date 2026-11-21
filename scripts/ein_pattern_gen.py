#!/usr/bin/env python3
"""Stage 2.12: EIN-pattern MRF URL generator.

Generates candidate MRF URLs from known health-system URL patterns discovered
2026-10-03 (autoresearch-hospital-ledger wave 1). Hospital MRF filenames commonly
embed the EIN: <EIN>_<slug>_standardcharges.{csv,json,xlsx}

Patterns (verified live 2026-10-03):
- KPC Health:        kpchealth.com/csv/v2/<EIN>_<slug>_standardcharges.csv
- Banner Health:     bannerhealth.com/-/media/files/project/bh/patients-visitors/billing/price-transparency/hospitals/<EIN>_<slug>_standardcharges.ashx
- UHS (eSkyCity):    uhsfilecdn.eskycity.net/ac/<EIN>_<slug>_standardcharges.csv

Usage:
  python3 ein_pattern_gen.py --ccn 050517 --ein 123456789 --system kpc --slug victor-valley-global-medical-center
  python3 ein_pattern_gen.py --batch candidates.csv  (ccn,ein,system,slug per row)

Output: candidate URLs with HEAD-verification status. Verified URLs are printed
as JSON lines ready for mrf_url_map.json intake. Does NOT write to production
files; the caller stages verified URLs.
"""
import argparse, csv, json, re, sys, urllib.request, urllib.error

PATTERNS = {
    "kpc": "https://kpchealth.com/csv/v2/{ein}_{slug}_standardcharges.csv",
    "banner": "https://www.bannerhealth.com/-/media/files/project/bh/patients-visitors/billing/price-transparency/hospitals/{ein}_{slug}_standardcharges.ashx",
    "uhs": "https://uhsfilecdn.eskycity.net/ac/{ein}_{slug}_standardcharges.csv",
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; HospitalLedgerBot/1.0)"}


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return re.sub(r"-+", "-", s)


def head_verify(url, timeout=30):
    """HEAD request; fall back to 0-byte range probe. Returns (ok, status, content_type)."""
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code in (403, 405):  # HEAD blocked; try range probe
            req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return True, r.status, r.headers.get("Content-Type", "")
            except Exception:
                return False, e.code, "head-blocked"
        return False, e.code, ""
    except Exception as e:
        return False, 0, str(e)[:60]


def generate(system, ein, slug=None, name=None):
    slug = slug or slugify(name)
    if system not in PATTERNS:
        raise ValueError(f"unknown system: {system} (known: {list(PATTERNS)})")
    return PATTERNS[system].format(ein=ein, slug=slug)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccn")
    ap.add_argument("--ein")
    ap.add_argument("--system", choices=list(PATTERNS))
    ap.add_argument("--slug")
    ap.add_argument("--name")
    ap.add_argument("--batch", help="CSV with ccn,ein,system,slug columns")
    ap.add_argument("--verify", action="store_true", help="HEAD-verify each candidate")
    args = ap.parse_args()

    jobs = []
    if args.batch:
        with open(args.batch, newline="") as f:
            for row in csv.DictReader(f):
                jobs.append(row)
    else:
        if not (args.ccn and args.ein and args.system):
            ap.error("--ccn, --ein, --system required (or --batch)")
        jobs.append({"ccn": args.ccn, "ein": args.ein, "system": args.system,
                     "slug": args.slug or "", "name": args.name or ""})

    for j in jobs:
        try:
            url = generate(j["system"], j["ein"], j.get("slug") or None, j.get("name"))
        except ValueError as e:
            print(json.dumps({"ccn": j.get("ccn"), "error": str(e)}))
            continue
        rec = {"ccn": j.get("ccn"), "system": j["system"], "url": url,
               "host": urllib.request.urlparse(url).hostname}
        if args.verify:
            ok, status, ctype = head_verify(url)
            rec.update({"verified": ok, "http": status, "content_type": ctype})
        print(json.dumps(rec))


if __name__ == "__main__":
    sys.exit(main())
