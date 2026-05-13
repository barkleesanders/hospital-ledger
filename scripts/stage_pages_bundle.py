#!/usr/bin/env python3
"""Stage a Cloudflare Pages bundle without oversized data artifacts.

The live site serves heavy price JSON and the CPT index from R2 via Pages
Functions. Shipping those files inside the static bundle causes deploy failures
 because Pages rejects files larger than 25 MiB.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
DEFAULT_OUTPUT = ROOT / "data" / "_pages_bundle"
PAGES_FILE_LIMIT_BYTES = 25 * 1024 * 1024


def should_copy(rel: Path, size: int) -> tuple[bool, str | None]:
    if ".wrangler" in rel.parts:
        return False, "local wrangler state"
    if rel == Path("data/cpt-index.json"):
        return False, "served from R2"
    if rel.parts[:2] == ("data", "prices") and rel.name != "index.json":
        return False, "per-hospital previews served from R2"
    if size > PAGES_FILE_LIMIT_BYTES:
        return False, "exceeds Pages 25 MiB file limit"
    return True, None


def stage_bundle(output_dir: Path, *, verbose_skips: bool = False) -> tuple[int, int]:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0
    for src in SITE_DIR.rglob("*"):
        rel = src.relative_to(SITE_DIR)
        if ".wrangler" in rel.parts:
            continue
        dst = output_dir / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        keep, reason = should_copy(rel, src.stat().st_size)
        if not keep:
            skipped += 1
            if verbose_skips:
                print(f"skip {rel.as_posix()} ({reason})")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied, skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--verbose-skips", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output).resolve()
    copied, skipped = stage_bundle(output_dir, verbose_skips=args.verbose_skips)
    print(f"staged Pages bundle at {output_dir}")
    print(f"copied={copied} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
