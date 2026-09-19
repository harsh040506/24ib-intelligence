"""Normalisation helpers shared by every ingestion connector.

Ported faithfully from the original ``config.parse_amount`` /
``config.sanitize_date_string`` and ``sector_normalizer`` so the new platform
reproduces the exact behaviour the reference pipeline relied on.

* ``parse_amount`` turns money strings ("$15 Mn", "1.5 Bn", "150K") into USD millions.
* ``parse_date`` accepts the Inc42 / spreadsheet formats (with typo sanitisation).
* ``normalize_sector`` delegates to the ported canonical sector taxonomy.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime

from . import sector_normalizer


def _clean_amount(value: float) -> float:
    """Coerce a parsed amount to a safe, non-negative, finite USD-millions float.

    ``NaN``/``inf`` are poison in aggregation: a single one turns every sum,
    average and percentage delta in a report into ``NaN``. Negative funding
    amounts are nonsensical and almost always a parse artefact. Both are
    normalised to ``0.0`` (treated as "undisclosed") so bad input degrades a
    single row instead of an entire report.
    """
    if value is None or not math.isfinite(value) or value < 0:
        return 0.0
    return float(value)

# Inc42 date formats (from config.DATE_FORMATS) + common spreadsheet ones.
_DATE_FORMATS = ("%d %b %Y", "%d-%b-%y", "%d %b, %Y", "%Y-%m-%d", "%d-%b-%Y",
                 "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%Y/%m/%d")


def sanitize_date_string(s: str) -> str:
    """Fix known Inc42 date typos (trailing *, 5-digit years like 20205→2025)."""
    if not s:
        return ""
    out = str(s).strip()
    out = re.sub(r"\*+", "", out)
    out = re.sub(r"20205", "2025", out)
    out = re.sub(r"20206", "2026", out)
    out = re.sub(r"(\d{4})\d+$", r"\1", out)
    return out.strip()


def parse_amount(raw) -> float:
    """Return amount in USD millions. Undisclosed / unparseable → 0.0.

    Mirrors ``config.parse_amount``: handles Mn / K / Bn suffixes and bare numbers.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, bool):  # bool is an int subclass; never a money amount
        return 0.0
    if isinstance(raw, (int, float)):
        return _clean_amount(float(raw))
    s = str(raw).strip()
    if s in ("–", "-", ""):
        return 0.0
    # A leading minus is dropped by the alphanumeric clean below, which would
    # silently turn "-50 Mn" into a positive 50. Detect it first and treat a
    # negative figure as undisclosed (0) rather than fabricating capital.
    if re.match(r"^\s*[-–]\s*\d", s):
        return 0.0
    cleaned = re.sub(r"[^\d\.a-zA-Z]", "", s).lower()
    try:
        if "mn" in cleaned:
            return _clean_amount(float(cleaned.replace("mn", "")))
        if "bn" in cleaned:
            return _clean_amount(float(cleaned.replace("bn", "")) * 1000.0)
        if "k" in cleaned:
            return _clean_amount(float(cleaned.replace("k", "")) / 1000.0)
        return _clean_amount(float(cleaned))
    except (ValueError, TypeError):
        return 0.0


def parse_date(raw) -> date | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = sanitize_date_string(str(raw))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def normalize_sector(org_id: int, raw: str, **_kw) -> str:
    """Normalize a raw sector string to canonical form.

    ``org_id`` is accepted for call-site compatibility but the canonical taxonomy
    is global (matches the reference implementation), so it is unused.
    """
    canonical = sector_normalizer.normalize_sector(raw or "")
    return canonical or "Uncategorized"
