#!/usr/bin/env python3
"""Payer canonicalization map.

Hospital MRFs publish payer names with wildly varying formatting:
  "Aetna PPO", "AETNA - HMO", "standard_charge|Aetna|HMO Plan", "Aetna Choice POS II"

Patients don't care about plan-level granularity — they care "does this hospital
take my insurance, and what did they negotiate." So we lump aggressively by parent
payer brand. Plan-level differences surface as a separate facet later if needed.

Usage:
    from payer_canonical import canonicalize, PATIENT_FACING_PAYERS

    slug, display = canonicalize("AETNA PPO HMO")  # -> ("aetna", "Aetna")
    slug, display = canonicalize("BCBS-AL")        # -> ("bcbs", "Blue Cross Blue Shield")
    slug, display = canonicalize("Cash")            # -> ("cash", "Cash / Self-Pay")

The map is hand-curated for the top ~40 payers patients actually have. Anything
that doesn't match falls through to slugified-raw-name (preserves the data, just
without lumping).
"""
from __future__ import annotations

import re
import unicodedata


# Strip common methodology prefixes that some MRFs prepend (e.g. CMS template).
PREFIX_RE = re.compile(
    r'^(standard_charge|median_amount|payer_specific_negotiated_charge|'
    r'negotiated_dollar|negotiated_pct)\s*[\|:_-]\s*',
    re.IGNORECASE,
)

# Strip common plan/product suffixes once we've identified the parent brand.
PLAN_SUFFIX_RE = re.compile(
    r'\s+(ppo|hmo|epo|pos|hdhp|exchange|marketplace|advantage|medicare\s*adv|'
    r'medadv|medsupp|gold|silver|bronze|platinum|choice|select|premier|elite|'
    r'plus|gold\s*card|preferred|network|plan|product|policy|individual|'
    r'commercial|group|family)\s*$',
    re.IGNORECASE,
)

# Canonical payer definitions. Each entry:
#   slug: short, URL-safe identifier
#   display: patient-friendly brand name
#   patterns: list of regex patterns (case-insensitive) that match raw payer strings
#   category: 'commercial' | 'medicare' | 'medicaid' | 'government' | 'cash' | 'specialty'
#
# Ordering matters within `_CANONICAL_PAYERS`: more specific patterns must come
# before more general ones (e.g. "BCBS Medicare Advantage" matches BCBS before
# generic Medicare).
_CANONICAL_PAYERS: list[dict] = [
    # ───── Cash / Self-pay (45 CFR § 180 element #3) ─────
    {
        'slug': 'cash',
        'display': 'Cash / Self-Pay',
        'category': 'cash',
        'patterns': [
            r'\bcash\b', r'\bself[\s\-]?pay\b', r'\bdiscount(ed)?\s*cash\b',
            r'\buninsured\b', r'\bprompt\s*pay\b', r'\bprivate\s*pay\b',
        ],
    },

    # ───── Government — Medicare ─────
    {
        'slug': 'medicare-advantage',
        'display': 'Medicare Advantage',
        'category': 'medicare',
        'patterns': [
            r'medicare\s*advantage', r'\bmed\s*adv\b', r'\bmcr\s*adv\b',
            r'medicare\s*part\s*c\b', r'medicare\s*replace',
            r'\bma\s*plan\b',
        ],
    },
    {
        'slug': 'medicare',
        'display': 'Medicare',
        'category': 'medicare',
        'patterns': [
            r'\bmedicare\b', r'\bmcr\b', r'\bcms\b(?!\s*\d)',
            r'medicare\s*part\s*[ab]\b',
        ],
    },

    # ───── Government — Medicaid (state-by-state, lump for patient view) ─────
    {
        'slug': 'medicaid',
        'display': 'Medicaid',
        'category': 'medicaid',
        'patterns': [
            r'\bmedicaid\b', r'\bmedi[\s\-]?cal\b',  # CA Medicaid
            r'\bmcd\b', r'\bmassh(ealth)?\b',  # MA Medicaid
            r'\bahcccs\b',  # AZ Medicaid
            r'\bsoonercare\b',  # OK Medicaid
            r'\bbadgercare\b',  # WI Medicaid
            r'\btenncare\b',  # TN Medicaid
            r'\bhealth\s*choice\b',
            r'\bchip\b',  # Children's Health Insurance Program
        ],
    },

    # ───── Government — Other ─────
    {
        'slug': 'tricare',
        'display': 'TRICARE (Military)',
        'category': 'government',
        'patterns': [r'\btricare\b', r'\bchampus\b', r'\bchampva\b'],
    },
    {
        'slug': 'va',
        'display': 'VA / Veterans',
        'category': 'government',
        'patterns': [
            r'\bva\s*community\s*care\b', r'\bvaccn\b', r'\bveterans\s*affairs\b',
            r'(?<![a-z])va(?:\s*choice|\s*ccn|\s*cc)\b',
        ],
    },
    {
        'slug': 'workers-comp',
        'display': 'Workers\' Compensation',
        'category': 'government',
        'patterns': [r'workers?[\s\-]?comp', r'\bwc\s*(insurance|board)?\b'],
    },

    # ───── BCBS family — lump all plans/states ─────
    {
        'slug': 'bcbs',
        'display': 'Blue Cross Blue Shield',
        'category': 'commercial',
        'patterns': [
            r'\bblue\s*cross\b', r'\bblue\s*shield\b', r'\bbcbs\b',
            r'\banthem\b', r'\belevance\b', r'\bcare\s*first\b',
            r'\bhighmark\b', r'\bregence\b', r'\bwellpoint\b', r'\bpremera\b',
            r'\bempire\s*blue\b',
        ],
    },

    # ───── Top commercial brands ─────
    {
        'slug': 'aetna',
        'display': 'Aetna',
        'category': 'commercial',
        'patterns': [r'\baetna\b', r'\bcoventry\b'],  # Coventry → Aetna 2013
    },
    {
        'slug': 'uhc',
        'display': 'UnitedHealthcare',
        'category': 'commercial',
        'patterns': [
            r'\bunited\s*health(care)?\b', r'\buhc\b', r'\bunitedhc\b',
            r'\boptum\b', r'\bumr\b', r'\bgolden\s*rule\b',
            r'\boxford\s*health\b', r'\bnavigate\b',
        ],
    },
    {
        'slug': 'cigna',
        'display': 'Cigna',
        'category': 'commercial',
        'patterns': [
            r'\bcigna\b', r'\bevernorth\b', r'\bgreat[\s\-]?west\b',
            r'\bloomis\b',
        ],
    },
    {
        'slug': 'humana',
        'display': 'Humana',
        'category': 'commercial',
        'patterns': [r'\bhumana\b', r'\bchoicecare\b'],
    },
    {
        'slug': 'kaiser',
        'display': 'Kaiser Permanente',
        'category': 'commercial',
        'patterns': [r'\bkaiser\b', r'\bkp\b(?!\s*\d)'],
    },
    {
        'slug': 'centene',
        'display': 'Centene / Ambetter',
        'category': 'commercial',
        'patterns': [
            r'\bcentene\b', r'\bambetter\b', r'\ballwell\b',
            r'\bsuperior\s*health\b', r'\bpeach\s*state\b',
            r'\bmagellan\s*health\b', r'\bhealth\s*net\b',
            r'\bfidelis\b',
        ],
    },
    {
        'slug': 'molina',
        'display': 'Molina Healthcare',
        'category': 'commercial',
        'patterns': [r'\bmolina\b'],
    },
    {
        'slug': 'wellcare',
        'display': 'WellCare',
        'category': 'commercial',
        'patterns': [r'\bwellcare\b', r'\bwell\s*care\b'],
    },
    {
        'slug': 'caresource',
        'display': 'CareSource',
        'category': 'commercial',
        'patterns': [r'\bcaresource\b', r'\bcare\s*source\b'],
    },
    {
        'slug': 'oscar',
        'display': 'Oscar Health',
        'category': 'commercial',
        'patterns': [r'\boscar\s*(health|insurance)?\b'],
    },
    {
        'slug': 'bright',
        'display': 'Bright HealthCare',
        'category': 'commercial',
        'patterns': [r'\bbright\s*health(care)?\b'],
    },

    # ───── Regional / specialty payers ─────
    {
        'slug': 'kp-northern-ca',
        'display': 'Kaiser Northern California',
        'category': 'commercial',
        'patterns': [r'kaiser.{0,8}northern.{0,8}ca', r'kp.{0,4}nor.?cal'],
    },
    {
        'slug': 'tufts',
        'display': 'Tufts Health Plan',
        'category': 'commercial',
        'patterns': [r'\btufts\b'],
    },
    {
        'slug': 'health-partners',
        'display': 'HealthPartners',
        'category': 'commercial',
        'patterns': [r'\bhealth\s*partners\b'],
    },
    {
        'slug': 'priority',
        'display': 'Priority Health',
        'category': 'commercial',
        'patterns': [r'\bpriority\s*health\b'],
    },
    {
        'slug': 'mvp',
        'display': 'MVP Health Care',
        'category': 'commercial',
        'patterns': [r'\bmvp\s*health\b', r'\bmvp\s*care\b'],
    },
    {
        'slug': 'emblem',
        'display': 'EmblemHealth',
        'category': 'commercial',
        'patterns': [r'\bemblem(health)?\b', r'\bgha?i\b', r'\bhip\s*health\b'],
    },
    {
        'slug': 'scan',
        'display': 'SCAN Health Plan',
        'category': 'commercial',
        'patterns': [r'\bscan\s*health\b'],
    },
    {
        'slug': 'devoted',
        'display': 'Devoted Health',
        'category': 'commercial',
        'patterns': [r'\bdevoted\s*health\b'],
    },
    {
        'slug': 'clover',
        'display': 'Clover Health',
        'category': 'commercial',
        'patterns': [r'\bclover\s*health\b'],
    },
    {
        'slug': 'naphcare',
        'display': 'NaphCare',
        'category': 'specialty',
        'patterns': [r'\bnaphcare\b'],
    },
]


# Pre-compile patterns for speed.
for entry in _CANONICAL_PAYERS:
    entry['_compiled'] = [re.compile(p, re.IGNORECASE) for p in entry['patterns']]


PATIENT_FACING_PAYERS: list[dict] = [
    {'slug': e['slug'], 'display': e['display'], 'category': e['category']}
    for e in _CANONICAL_PAYERS
]


def _strip_methodology_prefix(raw: str) -> str:
    """Remove CMS-template prefixes like 'standard_charge|payer|plan'."""
    cleaned = raw
    for _ in range(3):  # sometimes nested
        m = PREFIX_RE.match(cleaned)
        if not m:
            break
        cleaned = cleaned[m.end():]
    # Also handle bare pipe-delimited triples without keyword prefix
    parts = [p.strip() for p in cleaned.split('|')]
    if len(parts) >= 2 and parts[0].lower() in {
        'standard_charge', 'median_amount', 'payer_specific_negotiated_charge',
        'negotiated_dollar', 'negotiated_pct',
    }:
        cleaned = '|'.join(parts[1:])
    return cleaned.strip()


def _slugify(text: str) -> str:
    """Make a URL-safe slug. Falls back to 'unknown' if empty."""
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text or 'unknown'


def canonicalize(raw_payer: str | None) -> tuple[str, str]:
    """Return (slug, display) for a raw payer string.

    Falls back to slugified raw name if no canonical match.
    """
    if not raw_payer:
        return ('unknown', 'Unknown')

    cleaned = _strip_methodology_prefix(str(raw_payer)).strip()
    if not cleaned:
        return ('unknown', 'Unknown')

    # Try canonical patterns in order. First match wins.
    for entry in _CANONICAL_PAYERS:
        for pat in entry['_compiled']:
            if pat.search(cleaned):
                return (entry['slug'], entry['display'])

    # Long-tail: strip plan suffix, slugify what remains.
    base = PLAN_SUFFIX_RE.sub('', cleaned).strip()
    if not base:
        base = cleaned
    # Truncate before slugifying so we don't get 200-char slugs
    base = base[:60]
    return (_slugify(base), base.title())


def category_for(slug: str) -> str:
    for entry in _CANONICAL_PAYERS:
        if entry['slug'] == slug:
            return entry['category']
    return 'other'


if __name__ == '__main__':
    # Smoke test
    samples = [
        'Aetna', 'AETNA PPO', 'standard_charge|Aetna|HMO Plan',
        'Blue Cross Blue Shield of AL', 'BCBS-AL', 'Anthem BCBS', 'Highmark BCBS',
        'United Healthcare', 'UHC Medicare Advantage', 'OPTUM',
        'Cigna', 'Humana Gold Choice', 'Kaiser Permanente Northern California',
        'Medicare A AL JJ', 'Medicare Part B', 'Medicare Advantage',
        'Medicaid Alabama', 'Medi-Cal',
        'TRICARE Prime', 'VA Community Care Network VACCN Region 1',
        'Cash', 'Self-Pay', 'Discounted Cash Price',
        'Workers Comp', 'NaphCare',
        'Some Random Local Plan LLC',
    ]
    for s in samples:
        slug, display = canonicalize(s)
        print(f"  {s!r:60} -> ({slug!r}, {display!r})")
