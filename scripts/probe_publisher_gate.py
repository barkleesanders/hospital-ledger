#!/usr/bin/env python3
"""Probe the REAL publisher's identity gate (INVENTORY M2, step 2c).

One instrument for the before and the after of the cut-over: it imports the actual
`publish_mini.py` the Mini runs and calls its real `identity_gate(ccn, ...)`, so what it
reports is what `publish_mini.py CCN` would do at its gate. It writes nothing to the
store, the publish tree, R2 or the holds/verdict files (read-only; fixture canonicals go to
a temp dir).

Modes
-----
fixture   the frozen known pairs (tests/fixtures/identity/known_wrong_pairs.json), each as a
          production-shaped canonical built from the pair's VERBATIM header quote, with
          EMPTY holds and verdicts so only the identity logic is measured:
            * the 14 known-wrong pairs     (a PASSED row is a wrong file that would publish)
            * the 7 true owners            (a REFUSED row is a real owner that would not)
            * the rebrand 372004 Curahealth, under (a) the fixture's registry row and (b) the
              registry row the publisher really reads
            * the 9 unproven W12 reassignments
census    every canonical in the live store, through the real gate with the real holds,
          verdicts and registry; reports pass/refuse counts and, when the repo gate is
          importable, what the repo gate (under the shipped policy) would do instead.

Exit 0 when the probe ran, whatever it found. Findings are in the output, not the exit code;
a probe that cannot measure exits 2 and says so (never a silent pass).

Usage:
  probe_publisher_gate.py fixture [--publisher PATH] [--json OUT]
  probe_publisher_gate.py census  [--publisher PATH] [--json OUT] [--limit N]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "tests", "fixtures", "identity", "known_wrong_pairs.json")
DEFAULT_PUBLISHER = os.path.expanduser("~/hl-fleet/publish_mini.py")


def die(msg: str) -> "None":
    print(f"PROBE UNMEASURED: {msg}", file=sys.stderr)
    sys.exit(2)


def load_publisher(path: str):
    if not os.path.isfile(path):
        die(f"publisher not found: {path}")
    spec = importlib.util.spec_from_file_location("publish_mini_probe", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # the publisher failing to import is itself the finding
        die(f"publisher {path} failed to import: {type(exc).__name__}: {exc}")
    for needed in ("identity_gate", "_registry_identity", "_REGISTRY_CACHE"):
        if not hasattr(mod, needed):
            die(f"publisher {path} has no `{needed}`: not the shape this probe measures")
    return mod


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def quoted(q: str, key: str):
    m = re.search(key + r"""\s+(?:'([^']*)'|"([^"]*)")""", q)
    return (m.group(1) or m.group(2)) if m else None


def head_of(pair: dict) -> dict:
    q = pair["header_quote"]
    si = {"hospital_name": quoted(q, "hospital_name"), "hospital_address": quoted(q, "hospital_address")}
    if quoted(q, "location_name"):
        si["location_name"] = quoted(q, "location_name")
    return {"source_url": f"https://example.invalid/{pair['ccn']}_file.json", "source_identity": si}


class Scratch:
    """Fixture canonicals in a temp store, plus empty holds and an empty verdicts dir."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="probe-")
        self.store = os.path.join(self.tmp, "store")
        self.verdicts = os.path.join(self.tmp, "verdicts")
        os.makedirs(self.store)
        os.makedirs(self.verdicts)
        self.holds = os.path.join(self.tmp, "holds.txt")
        open(self.holds, "w").close()

    def write_canonical(self, ccn: str, head: dict):
        body = json.dumps({"ccn": ccn, "hospital_name": head["source_identity"].get("hospital_name"),
                           **head})[:-1] + ', "items": []}'
        with gzip.open(os.path.join(self.store, f"{ccn}.json.gz"), "wt") as fh:
            fh.write(body)

    def gate(self, pm, ccn: str):
        return pm.identity_gate(ccn, store_dir=self.store, verdicts_dir=self.verdicts,
                                holds_path=self.holds)


def run_fixture(pm, publisher: str) -> dict:
    fx = json.load(open(FIXTURE))
    sc = Scratch()
    rows, out = [], {"mode": "fixture", "publisher": publisher, "publisher_sha256": sha256(publisher)}

    def one(group, ccn, head, registry, note=""):
        sc.write_canonical(ccn, head)
        pm._REGISTRY_CACHE[ccn] = registry
        reason, warns = sc.gate(pm, ccn)
        rows.append({"group": group, "ccn": ccn, "published": reason is None, "reason": reason,
                     "warnings": warns, "note": note})

    def reg(r):
        return {"name": r["name"], "address": "", "city": r.get("city", ""),
                "state": r.get("state", ""), "zip": r.get("zip", "")}

    for p in fx["wrong_pairs"]:
        one("wrong", p["ccn"], head_of(p), reg(p["registry"]))
    for c in fx["true_owner_controls"]:
        one("owner", c["ccn"], head_of(c), reg(c["registry"]))

    def flat_head(ccn, s):
        return {"source_url": f"https://example.invalid/{ccn}_file.json", "source_identity": {
            "hospital_name": s["hospital_name"], "hospital_address_city": s["city"],
            "hospital_address_state": s["state"], "hospital_address_zip": s["zip"]}}

    for p in fx["unproven_controls"]["pairs"]:
        one("unproven", p["ccn"], flat_head(p["ccn"], p["source"]), reg(p["registry"]))
    shipped = {o["hospital_id"]: o for o in
               json.load(open(os.path.join(REPO, "scripts", "hl_publish_gate_policy.json")))["overrides"]}
    for p in fx["unproven_controls"]["name_differs_known_false_refusal"]:
        ccn = p["ccn"]
        pinned = shipped.get(ccn, {}).get("source_url") or f"https://example.invalid/{ccn}_file.json"
        full = flat_head(ccn, p["source"])
        full["source_url"] = pinned
        # (a) the fixture's registry row (name + state only), the file's full header, the pinned file
        one("rebrand-fixture-registry", ccn, full, reg(p["registry"]))
        # (b) the registry row the publisher REALLY reads for this CCN, same full header
        pm._REGISTRY_CACHE.pop(ccn, None)
        live = pm._registry_identity(ccn)
        sc.write_canonical(ccn, full)
        reason, warns = sc.gate(pm, ccn)
        rows.append({"group": "rebrand-live-registry", "ccn": ccn, "published": reason is None,
                     "reason": reason, "warnings": warns, "note": f"live registry row: {live!r}"})
        # (c) the LIVE SHAPE: no source_identity in the canonical at all (what the store holds)
        pm._REGISTRY_CACHE.pop(ccn, None)
        bare = {"source_url": pinned, "source_identity": {}}
        sc.write_canonical(ccn, bare)
        reason, warns = sc.gate(pm, ccn)
        rows.append({"group": "rebrand-live-shape", "ccn": ccn, "published": reason is None,
                     "reason": reason, "warnings": warns, "note": "canonical with no source_identity"})
        # (d) control: same CCN, a DIFFERENT file than the one the override is pinned to
        pm._REGISTRY_CACHE.pop(ccn, None)
        other = {"source_url": "https://example.invalid/not-the-adjudicated-file.csv",
                 "source_identity": {}}
        sc.write_canonical(ccn, other)
        reason, warns = sc.gate(pm, ccn)
        rows.append({"group": "rebrand-wrong-file", "ccn": ccn, "published": reason is None,
                     "reason": reason, "warnings": warns, "note": "different file under the same CCN"})
    out["rows"] = rows

    def tally(group):
        g = [r for r in rows if r["group"] == group]
        return g, [r["ccn"] for r in g if r["published"]], [r["ccn"] for r in g if not r["published"]]

    for r in rows:
        print(f"{r['group']:<26} {r['ccn']} {'PUBLISHED' if r['published'] else 'REFUSED  '} "
              f"{(r['reason'] or '; '.join(r['warnings']) or '')[:130]}"
              + (f"  [{r['note']}]" if r["group"].startswith("rebrand-") and r["note"] else ""))
    w, wp, _ = tally("wrong")
    o, _, orf = tally("owner")
    u, up, _ = tally("unproven")
    print(f"TALLY wrong_pairs_PUBLISHED={len(wp)}/{len(w)} {wp}")
    print(f"TALLY true_owners_REFUSED={len(orf)}/{len(o)} {orf}")
    print(f"TALLY unproven_reassignments_PUBLISHED={len(up)}/{len(u)} {up}")
    for g in ("rebrand-fixture-registry", "rebrand-live-registry", "rebrand-live-shape",
              "rebrand-wrong-file"):
        gg, gp, _ = tally(g)
        print(f"TALLY {g}_PUBLISHED={len(gp)}/{len(gg)} {gp}")
    out["tally"] = {"wrong_published": wp, "owners_refused": orf, "unproven_published": up}
    return out


def bucket(reason, warns) -> str:
    if reason is None:
        return "publishes-with-warning" if warns else "publishes-clean"
    text = reason.lower()
    for needle, name in (("hold list", "hold"), ("failing closed", "fail-closed"),
                         ("exact file a registry reject", "rejected-url"), ("ein ", "ein-contradiction"),
                         ("no registry row", "unproven:no-registry-row"),
                         ("no store canonical head", "unproven:no-canonical-head"),
                         ("carries no state, city or zip", "unproven:name-but-no-address"),
                         ("do not anchor", "unproven:address-does-not-anchor"),
                         ("no in-file hospital name", "unproven:no-file-name"),
                         ("does not match the registry name", "refused:name-mismatch"),
                         ("city", "refused:city-contradiction"), ("state", "refused:state-contradiction"),
                         ("identity mismatch", "refused:identity-mismatch")):
        if needle in text:
            return name
    return "refused:other"


def run_census(pm, publisher: str, limit) -> dict:
    if hasattr(pm, "_gate_module"):
        # a cut-over publisher: use the INSTALLED gate and policy, the code that really runs, and
        # say where they came from (a repo copy leaking in via sys.modules would be caught here)
        pg = pm._gate_module()
        policy = pg.load_policy(pm.GATE_POLICY)
        print(f"GATE installed module {pg.__file__} sha256={sha256(pg.__file__)}")
        print(f"GATE policy {pm.GATE_POLICY} sha256={sha256(pm.GATE_POLICY)}")
    else:
        sys.path.insert(0, os.path.join(REPO, "scripts"))
        import hl_publish_gate as pg  # the repo gate, evaluated on the same real inputs
        policy = pg.load_policy()
    store = pm.STORE
    ccns = sorted(f[:-len(".json.gz")] for f in os.listdir(store) if f.endswith(".json.gz"))
    if not ccns:
        die(f"no canonicals under {store}: refusing to report an empty census as clean")
    if limit:
        ccns = ccns[:limit]
    holds = pm.load_holds()
    t0 = time.time()
    rows = []
    for ccn in ccns:
        old_reason, old_warns = pm.identity_gate(ccn)
        head = pm._canonical_record_head(ccn, store)
        verdict_rows = pm._all_verdict_rows(ccn, pm.VERDICTS_DIR)
        new_reason, new_warns = pg.gate_from_policy(ccn, head, pm._registry_identity(ccn), holds,
                                                    verdict_rows, policy=policy)
        url = pg._norm_url((head or {}).get("source_url"))
        win = pg.winning_verdict(verdict_rows)
        # a hand-adjudicated registry ACCEPT for the exact file this canonical was built from
        adjudicated = bool(win and url and win[2] == pg.hl_admission.ACCEPT
                           and pg._norm_url(win[1]) == url)
        rows.append({"ccn": ccn, "publisher": bucket(old_reason, old_warns),
                     "adjudicated_accept_for_this_file": adjudicated,
                     "repo_strict": bucket(new_reason, new_warns),
                     "publisher_reason": old_reason, "repo_reason": new_reason,
                     "publisher_warnings": old_warns, "repo_warnings": new_warns})
    elapsed = time.time() - t0
    from collections import Counter
    pub, repo = Counter(r["publisher"] for r in rows), Counter(r["repo_strict"] for r in rows)
    pub_ok = lambda b: b.startswith("publishes")
    newly_refused = [r["ccn"] for r in rows if pub_ok(r["publisher"]) and not pub_ok(r["repo_strict"])]
    newly_allowed = [r["ccn"] for r in rows if not pub_ok(r["publisher"]) and pub_ok(r["repo_strict"])]
    agree = sum(1 for r in rows if pub_ok(r["publisher"]) == pub_ok(r["repo_strict"]))
    print(f"CENSUS canonicals={len(rows)} elapsed={elapsed:.1f}s store={store}")
    print("CENSUS publisher gate :", dict(sorted(pub.items())))
    print("CENSUS repo gate strict:", dict(sorted(repo.items())))
    print(f"CENSUS publisher and repo-strict agree on publish/refuse for {agree}/{len(rows)}")
    print(f"CENSUS publishes today, refused by repo-strict: {len(newly_refused)}")
    by = Counter(r["repo_strict"] for r in rows if r["ccn"] in set(newly_refused))
    print("CENSUS   ...by reason:", dict(sorted(by.items())))
    adj = [r["ccn"] for r in rows if r["ccn"] in set(newly_refused) and r["adjudicated_accept_for_this_file"]]
    print(f"CENSUS   ...of which carry a hand-adjudicated registry ACCEPT for the exact file: {len(adj)}"
          " (candidates for a named override; the policy does not count a verdict as in-file evidence)")
    print(f"CENSUS refused today, published by repo-strict: {len(newly_allowed)} {newly_allowed[:20]}")
    return {"mode": "census", "publisher": publisher, "publisher_sha256": sha256(publisher),
            "elapsed_s": round(elapsed, 1), "n": len(rows), "publisher_buckets": dict(pub),
            "repo_strict_buckets": dict(repo), "newly_refused": newly_refused,
            "newly_allowed": newly_allowed, "newly_refused_with_adjudicated_accept": adj,
            "agree": agree, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("fixture", "census"))
    ap.add_argument("--publisher", default=DEFAULT_PUBLISHER)
    ap.add_argument("--json")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    pm = load_publisher(args.publisher)
    print(f"PUBLISHER {args.publisher} sha256={sha256(args.publisher)}")
    out = run_fixture(pm, args.publisher) if args.mode == "fixture" else run_census(pm, args.publisher, args.limit)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"WROTE {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
