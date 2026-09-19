"""Sector normalization (ported from the original ``sector_normalizer.py``).

Maps messy, inconsistent Inc42 sector strings to clean canonical names via
exact alias lookup first, then a fuzzy token-sort fallback. Distinct sectors are
kept distinct (Supply Chain != Logistics, B2B SaaS != Enterprise Services, etc).

The original used rapidfuzz's ``token_sort_ratio``. To stay dependency-light we
reproduce the same idea with the stdlib ``difflib``: tokens are sorted before
comparison so word-order differences don't hurt the score. Threshold tuned to
match the original behaviour.
"""
from __future__ import annotations

from difflib import SequenceMatcher

# ── canonical sector → known aliases (lowercase) ──
CANONICAL_ALIASES: dict[str, list[str]] = {
    "Fintech": ["fintech", "fin-tech", "financial tech", "financial technology"],
    "Deeptech": ["deeptech", "deep tech", "deep-tech"],
    "Artificial Intelligence": [
        "artificial intelligence", "ai", "ai/ml", "machine learning",
        "artficial intelligence (ai)", "artificial intelligence (ai)",
        "artificial intelligence(ai)",
    ],
    "Healthtech": ["healthtech", "health tech", "health-tech", "health technology"],
    "Edtech": ["edtech", "ed-tech", "education technology", "education tech",
               "educational technology"],
    "Agritech": ["agritech", "agri-tech", "agriculture tech", "agriculture technology",
                 "agri tech"],
    "Cleantech": ["cleantech", "clean tech", "clean-tech", "clean energy",
                  "cleanenergy", "climate tech", "climatetech"],
    "Logistics": ["logistics", "logistic", "logistics tech"],
    "Supply Chain": ["supply chain", "supply chain management", "scm", "supply-chain"],
    "Ecommerce": ["ecommerce", "e-commerce", "e commerce", "ecommerce***"],
    "Enterprise Services": [
        "enterprise services", "enterprise tech", "enterprise technology",
        "enteprise tech", "enterprisetech", "enterprise service",
        "consumers services", "consumer services/ enteprise services",
    ],
    "B2B SaaS": ["b2b saas", "b2b software", "b2b-saas", "saas", "software as a service"],
    "D2C": ["d2c", "direct-to-consumer", "direct to consumer", "direct2consumer"],
    "Consumer Services": ["consumer services"],
    "Foodtech": ["foodtech", "food tech", "food-tech", "fooddtech"],
    "Media & Entertainment": ["media & entertainment", "media and entertainment",
                              "media", "entertainment tech", "entertainment technology"],
    "Proptech": ["proptech", "prop tech", "property tech", "property technology"],
    "Real Estate Tech": ["real estate tech", "real-estate tech", "real estate technology",
                         "real estate"],
    "Web3": ["web3", "web 3.0", "web 3", "web3.0"],
    "Blockchain": ["blockchain", "blockchain technology"],
    "Crypto": ["crypto", "cryptocurrency", "digital assets", "digital currency"],
    "Travel Tech": ["travel tech", "traveltech", "travel technology", "travel-tech"],
    "Retail Tech": ["retail tech", "retailtech", "retail technology", "retail-tech"],
    "Advanced Hardware & IoT": [
        "advanced hardware & iot", "advanced hardware & technology",
        "advanced hardware &technology", "advanced hardware and technology",
        "advanced technology & hardware", "hardware & iot", "iot",
        "advanced hardware", "hardware tech",
    ],
    "Alcoholic Beverages": ["alcoholic beverages", "alcoholic beverage"],
}

# ── email groupings (sector → top-level category) ──
IMPACTECH_SECTORS = frozenset({"Healthtech", "Edtech", "Agritech", "Cleantech", "Logistics"})
FIG_FINTECH_SECTORS = frozenset({"Fintech"})
DEEPTECH_AI_SECTORS = frozenset({"Deeptech", "Artificial Intelligence", "Advanced Hardware & IoT"})

MATCH_THRESHOLD = 0.78  # 0–1 (difflib ratio); original used 78/100

# ── flat lookup tables (built once) ──
_EXACT_MAP: dict[str, str] = {}
for _canon, _aliases in CANONICAL_ALIASES.items():
    for _a in _aliases:
        _EXACT_MAP[_a.lower().strip()] = _canon
    _EXACT_MAP[_canon.lower().strip()] = _canon
_ALIAS_POOL = list(_EXACT_MAP.keys())


def _token_sort_ratio(a: str, b: str) -> float:
    """difflib analogue of rapidfuzz token_sort_ratio (word-order insensitive)."""
    a2 = " ".join(sorted(a.split()))
    b2 = " ".join(sorted(b.split()))
    return SequenceMatcher(None, a2, b2).ratio()


def normalize_sector(raw: str) -> str:
    """Normalize a raw sector string to canonical form (unchanged if no match)."""
    if not raw or not isinstance(raw, str):
        return raw or ""
    clean = raw.strip()
    lower = clean.lower()
    if lower in _EXACT_MAP:               # exact / alias hit
        return _EXACT_MAP[lower]
    best_alias, best_score = None, 0.0    # fuzzy fallback
    for alias in _ALIAS_POOL:
        score = _token_sort_ratio(lower, alias)
        if score > best_score:
            best_alias, best_score = alias, score
    if best_alias and best_score >= MATCH_THRESHOLD:
        return _EXACT_MAP[best_alias]
    return clean


def get_email_group(canonical: str) -> str:
    """Map a canonical sector to its email category."""
    if canonical in IMPACTECH_SECTORS:
        return "ImpacTech"
    if canonical in FIG_FINTECH_SECTORS:
        return "FIG & Fintech"
    if canonical in DEEPTECH_AI_SECTORS:
        return "DeepTech & AI"
    return "Others"


def is_impact(canonical: str) -> bool:
    return canonical in IMPACTECH_SECTORS


# Accent colours for the seeded taxonomy (display only).
SECTOR_ACCENTS: dict[str, str] = {
    "Fintech": "#1f4e79", "Artificial Intelligence": "#3d8a67", "Ecommerce": "#1b4a36",
    "Cleantech": "#2d6b4f", "Healthtech": "#3d8a67", "Deeptech": "#0a2418",
    "Agritech": "#7ab896", "Edtech": "#d99a16", "Logistics": "#2d6b4f",
    "Foodtech": "#d99a16", "Travel Tech": "#3b82c4", "Advanced Hardware & IoT": "#0a2418",
}
