#!/usr/bin/env python3
"""CSP audit — every external subresource host must be allow-listed in the CSP.

WHY THIS EXISTS
---------------
2026-07-06 (fc386c6) added a Content-Security-Policy whose comment read
"self + Google Fonts (the only external loads in rendered HTML)". That
enumeration was built from the <link> tags and MISSED
`<script src="https://cdn.tailwindcss.com">` in Layout.tsx. The browser then
blocked Tailwind outright, ~484 utility classes rendered inert, and
hospitalledger.com served a visually unstyled page for ~4 weeks. Nothing failed
loudly: the header was present, the HTML was valid, the build was green, and
`curl` looked perfect — the breakage existed only in a browser that enforces CSP.

A CSP that is merely PRESENT is not a CSP that is CORRECT. This script closes
that gap by deriving the required hosts from the source instead of trusting a
hand-maintained comment.

WHAT IT CHECKS
--------------
Scans every .tsx/.ts source file and public/*.js island for absolute-URL
subresources, maps each to the CSP directive the browser will enforce it
against, and fails if the host is not permitted by that directive (or by
default-src as fallback).

  <script src>              -> script-src
  <link rel=stylesheet href>-> style-src
  <img src> / url()         -> img-src
  fetch()/XHR literals      -> connect-src
  @font-face / fonts host   -> font-src

Deliberately NOT checked: plain <a href> targets (navigation is not a
subresource and needs no CSP entry) — a false positive there would train people
to ignore this gate.

Exit 0 = every external subresource is allow-listed. Exit 1 = at least one is
blocked, printed with the file:line that loads it and the directive that blocks
it. Run standalone or via `npm run audit:csp`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

# The CSP is set in one place; read it from source rather than duplicating it.
CSP_SOURCE = ROOT / "src" / "index.tsx"

# (regex, directive) — regex must expose the URL as group 1.
SUBRESOURCE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<script[^>]*\bsrc=[\"'`]?(https?://[^\"'`\s>]+)"), "script-src"),
    (re.compile(r"<link[^>]*\bhref=[\"'`]?(https?://[^\"'`\s>]+)[^>]*rel=[\"']?stylesheet"), "style-src"),
    (re.compile(r"<link[^>]*rel=[\"']?stylesheet[^>]*\bhref=[\"'`]?(https?://[^\"'`\s>]+)"), "style-src"),
    (re.compile(r"<img[^>]*\bsrc=[\"'`]?(https?://[^\"'`\s>]+)"), "img-src"),
    (re.compile(r"url\(\s*[\"']?(https?://[^\"')\s]+)"), "img-src"),
    (re.compile(r"\bfetch\(\s*[\"'`](https?://[^\"'`]+)"), "connect-src"),
]

# rel="preconnect"/"dns-prefetch" are hints, not loads — the actual load that
# follows is matched by its own tag, so skip them to avoid double-reporting.
HINT_REL = re.compile(r"rel=[\"']?(preconnect|dns-prefetch|preload)")


def parse_csp(text: str) -> dict[str, list[str]]:
    """Pull the CSP string literal out of the source and parse it.

    NOTE: the literal contains single quotes ('self', 'unsafe-inline'), so a
    naive [^"']* capture terminates at the first 'self' and silently yields a
    CSP with no script-src — which then reports EVERY host as blocked. Match to
    the closing DOUBLE quote only. (This bug existed in the first version of
    this script and was caught by running it against the known-good CSP.)
    """
    m = re.search(r'"(default-src[^"]*)"', text)
    if not m:
        print("FAIL: could not locate the CSP string literal in", CSP_SOURCE)
        sys.exit(2)
    directives: dict[str, list[str]] = {}
    for part in m.group(1).split(";"):
        part = part.strip()
        if not part:
            continue
        name, *sources = part.split()
        directives[name] = sources
    return directives


def permitted(directives: dict[str, list[str]], directive: str, url: str) -> bool:
    """Does the CSP allow this URL under this directive (falling back to default-src)?"""
    sources = directives.get(directive)
    if sources is None:
        sources = directives.get("default-src", [])
    host = urlparse(url).netloc
    origin = f"{urlparse(url).scheme}://{host}"
    for s in sources:
        if s in ("*", "https:"):
            return True
        if s.startswith("'"):  # 'self', 'unsafe-inline', nonces, hashes
            continue
        allowed = s.rstrip("/")
        if allowed in (origin, host):
            return True
        # wildcard subdomain form: https://*.example.com
        if "*." in allowed:
            suffix = allowed.split("*.", 1)[1]
            if host == suffix or host.endswith("." + suffix):
                return True
    return False


def main() -> int:
    csp_text = CSP_SOURCE.read_text(encoding="utf-8")
    directives = parse_csp(csp_text)

    files = [
        p
        for pat in ("src/**/*.tsx", "src/**/*.ts", "public/**/*.js")
        for p in ROOT.glob(pat)
        if "node_modules" not in p.parts
    ]

    violations: list[str] = []
    checked = 0
    for path in sorted(set(files)):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        in_block_comment = False
        for lineno, line in enumerate(lines, 1):
            # Skip comments. Prose that *describes* a blocked URL (like this
            # script's own header, or the incident note in index.tsx) is not a
            # subresource load — counting it produced a false positive on the
            # very first run.
            stripped = line.lstrip()
            if in_block_comment:
                if "*/" in line:
                    in_block_comment = False
                continue
            if stripped.startswith("/*"):
                if "*/" not in line:
                    in_block_comment = True
                continue
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if HINT_REL.search(line):
                continue
            for pattern, directive in SUBRESOURCE_PATTERNS:
                for m in pattern.finditer(line):
                    url = m.group(1)
                    checked += 1
                    if not permitted(directives, directive, url):
                        rel = path.relative_to(ROOT)
                        allowed = directives.get(directive, directives.get("default-src", []))
                        violations.append(
                            f"  {rel}:{lineno}\n"
                            f"    loads : {url}\n"
                            f"    needs : {directive} to permit {urlparse(url).netloc}\n"
                            f"    has   : {directive} {' '.join(allowed) or '(unset -> default-src)'}"
                        )

    if violations:
        print("CSP AUDIT: FAIL — external subresource(s) blocked by the deployed CSP\n")
        print("\n\n".join(violations))
        print(
            "\nThe browser will refuse these loads. Add the host to the directive in\n"
            f"{CSP_SOURCE.relative_to(ROOT)}, then verify in a real browser that the\n"
            "console shows zero 'violates the following Content Security Policy' errors."
        )
        return 1

    print(f"CSP AUDIT: PASS — {checked} external subresource reference(s), all allow-listed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
