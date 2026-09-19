"""Fuzzy near-duplicate detection.

Exact-hash dedup (``FundingDeal.dedup_hash``) only catches identical
company/date/amount triples. The same deal reported twice with a slightly
different company spelling, a date off by a day or two, or a rounded amount
slips through — this module finds those candidates for human review.

Deals are already indexed on ``deal_date``, so candidates are found with a
sliding window over the date-sorted list rather than an O(n²) all-pairs scan:
for each deal, only the deals within ``_DATE_WINDOW_DAYS`` ahead of it (in
date order) are compared.
"""
from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher

from sqlalchemy import select

from ..extensions import db
from ..models import DedupIgnore, FundingDeal

_DATE_WINDOW_DAYS = 5
_NAME_SIMILARITY = 0.85
_AMOUNT_TOLERANCE = 0.10  # ±10%
_MAX_CANDIDATES = 100


def _name_similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio() >= _NAME_SIMILARITY


def _amount_close(a: float, b: float) -> bool:
    if a == 0 and b == 0:
        return True
    hi, lo = max(a, b), min(a, b)
    if hi == 0:
        return False
    return (hi - lo) / hi <= _AMOUNT_TOLERANCE


def find_candidates(org_id: int) -> list[dict]:
    """Return up to ``_MAX_CANDIDATES`` near-duplicate deal pairs, newest first."""
    deals = db.session.scalars(
        select(FundingDeal).where(FundingDeal.organization_id == org_id)
        .order_by(FundingDeal.deal_date)
    ).all()
    if len(deals) < 2:
        return []

    ignored = {
        frozenset((row.hash_a, row.hash_b))
        for row in db.session.scalars(
            select(DedupIgnore).where(DedupIgnore.organization_id == org_id)
        )
    }

    out: list[dict] = []
    for i, a in enumerate(deals):
        for b in deals[i + 1:]:
            if (b.deal_date - a.deal_date) > timedelta(days=_DATE_WINDOW_DAYS):
                break  # date-sorted: nothing further out can be in-window either
            if a.dedup_hash == b.dedup_hash:
                continue  # already an exact duplicate (shouldn't happen — unique constraint)
            if frozenset((a.dedup_hash, b.dedup_hash)) in ignored:
                continue
            if not _amount_close(a.amount_usd_mn, b.amount_usd_mn):
                continue
            if not _name_similar(a.company_name, b.company_name):
                continue
            out.append({"a": a, "b": b})
            if len(out) >= _MAX_CANDIDATES:
                return out
    return out
