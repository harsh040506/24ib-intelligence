"""Aggregation — turn raw deals + market snapshots into a report payload.

Pure functions over the ORM. Given a resolved period and an org id, produce a
single ``payload`` dict whose shape the Jinja report template consumes. Every
headline metric carries a prior-period comparison so the report can show deltas
(a table-stakes feature the original hand-built report lacked).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import and_, select

from ..extensions import db
from ..models import FundingDeal
from .periods import Bucket, ResolvedPeriod, parse_key, resolve

# Ticket-size bands (USD millions), darkest → lightest for the chart.
_TICKET_BANDS = [
    ("$50M+", 50.0, float("inf"), "#0a2418"),
    ("$20M–$49M", 20.0, 50.0, "#1b4a36"),
    ("$10M–$19M", 10.0, 20.0, "#2d6b4f"),
    ("$5M–$9.9M", 5.0, 10.0, "#3d8a67"),
    ("$1M–$4.9M", 1.0, 5.0, "#7ab896"),
    ("<$1M", 0.0, 1.0, "#b8ddc9"),
]


def _deals_between(org_id: int, start: date, end: date) -> list[FundingDeal]:
    """Load one org's deals within an inclusive ``[start, end]`` date window.

    The tenant filter here is the single point at which isolation is enforced:
    every section below derives exclusively from this function, so a report can
    never surface another organisation's deals.
    """
    stmt = select(FundingDeal).where(
        and_(
            FundingDeal.organization_id == org_id,
            FundingDeal.deal_date >= start,
            FundingDeal.deal_date <= end,
        )
    )
    return list(db.session.scalars(stmt))


def _fmt_amt(mn: float) -> str:
    """Format a USD-millions figure as a human label (``$1.50B`` / ``$15M`` / ``$200K``).

    Tiers at billions/millions/thousands so headline numbers read naturally. The
    whole-number branch avoids the noisy ``$15.0M`` in favour of ``$15M``.
    """
    if mn >= 1000:
        return f"${mn / 1000:.2f}B"
    if mn >= 1:
        return f"${mn:.0f}M" if mn == int(mn) else f"${mn:.1f}M"
    return f"${mn * 1000:.0f}K"


def _pct_delta(curr: float, prev: float) -> float | None:
    """Percentage change from ``prev`` to ``curr``, or ``None`` when incomparable.

    Returns ``None`` (rendered as an em-dash by the template) when the prior
    value is zero/falsy — this both avoids a division-by-zero and correctly
    conveys "no basis for comparison" rather than a misleading 0% or ∞%.
    """
    if not prev:
        return None
    return round((curr - prev) / prev * 100, 1)


# ───────────────────────── funding sections ─────────────────────────

def _headline_stats(deals: list[FundingDeal], prev_deals: list[FundingDeal]) -> dict:
    """Compute the top-of-report KPIs with prior-period deltas.

    ``deals``/``prev_deals`` are the headline and immediately-preceding period's
    deal lists. ``avg_ticket_mn`` is guarded against an empty period (no deals →
    0.0 rather than a ZeroDivisionError).
    """
    total_amt = sum(d.amount_usd_mn for d in deals)
    prev_amt = sum(d.amount_usd_mn for d in prev_deals)
    sectors = {d.sector_canonical for d in deals}
    return {
        "total_deals": len(deals),
        "total_deals_delta": _pct_delta(len(deals), len(prev_deals)),
        "total_capital_mn": round(total_amt, 1),
        "total_capital_label": _fmt_amt(total_amt),
        "total_capital_delta": _pct_delta(total_amt, prev_amt),
        "sector_count": len(sectors),
        "avg_ticket_mn": round(total_amt / len(deals), 1) if deals else 0.0,
    }


def _trend(org_id: int, buckets: list[Bucket]) -> list[dict]:
    """Build the per-bucket momentum series (deal count, capital, anchor deal).

    One query per bucket. This is technically N+1, but the trend window is small
    and bounded by design (≤ ~12 buckets — see ``periods.resolve``), so the
    simplicity is preferred over a single grouped query that would still need
    per-bucket top-deal lookups.
    """
    out = []
    for b in buckets:
        bd = _deals_between(org_id, b.start, b.end)
        top = max(bd, key=lambda d: d.amount_usd_mn, default=None)
        out.append({
            "label": b.label,
            "key": b.key,
            "deals": len(bd),
            "amt": round(sum(d.amount_usd_mn for d in bd), 1),
            "top": top.company_name if top else "—",
            "top_amt": _fmt_amt(top.amount_usd_mn) if top else "",
        })
    return out


def _sectors(deals: list[FundingDeal]) -> list[dict]:
    """Aggregate capital + deal count per canonical sector, sorted by capital desc.

    Aggregation runs in Python over the already-loaded headline deals to avoid a
    second database round-trip; the headline set is small enough that this is
    cheaper than re-querying with a GROUP BY.
    """
    agg: dict[str, list] = defaultdict(lambda: [0.0, 0])
    for d in deals:
        agg[d.sector_canonical][0] += d.amount_usd_mn
        agg[d.sector_canonical][1] += 1
    rows = [{"s": s, "amt": round(a, 1), "deals": n} for s, (a, n) in agg.items()]
    return sorted(rows, key=lambda r: r["amt"], reverse=True)


def _pivot(org_id: int, buckets: list[Bucket], top_sectors: list[str]) -> dict:
    """Build the sector × bucket deal-count matrix for the heatmap.

    Rows follow ``top_sectors`` order (the report's leading sectors); columns
    follow ``buckets``. ``max`` is the largest single cell, used by the template
    to scale heat intensity. Like :func:`_trend`, this is one query per bucket —
    acceptable for the bounded trend window.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for b in buckets:
        for d in _deals_between(org_id, b.start, b.end):
            counts[d.sector_canonical][b.key] += 1
    matrix = [[counts[s].get(b.key, 0) for b in buckets] for s in top_sectors]
    return {
        "sectors": top_sectors,
        "buckets": [b.label for b in buckets],
        "data": matrix,
        "max": max((c for row in matrix for c in row), default=0),
    }


def _tickets(deals: list[FundingDeal]) -> list[dict]:
    """Distribute deals across fixed cheque-size bands (count + capital share).

    The ``or 1.0`` / ``or 1`` fallbacks make an empty period yield 0% across all
    bands instead of raising on division by zero. Bands are half-open
    ``[lo, hi)`` so each deal lands in exactly one band with no double-counting.
    """
    total_cap = sum(d.amount_usd_mn for d in deals) or 1.0
    n = len(deals) or 1
    out = []
    for label, lo, hi, color in _TICKET_BANDS:
        band = [d for d in deals if lo <= d.amount_usd_mn < hi]
        cap = sum(d.amount_usd_mn for d in band)
        out.append({
            "label": label,
            "count": len(band),
            "pct_deals": round(len(band) / n * 100, 1),
            "capital": round(cap, 1),
            "pct_cap": round(cap / total_cap * 100, 1),
            "color": color,
        })
    return out


def _investors(deals: list[FundingDeal], limit: int = 10) -> list[dict]:
    """Rank the most active investors by disclosed deal participation.

    Counts are a *minimum*: an investor only appears for deals where it was
    named, so undisclosed participation is necessarily excluded (a known data
    limitation, surfaced as a footnote in the report). ``sectors`` is each
    investor's top-3 sectors by frequency, used as a focus summary.
    """
    counter: Counter[str] = Counter()
    sector_focus: dict[str, Counter] = defaultdict(Counter)
    for d in deals:
        for inv in d.investor_list:
            counter[inv] += 1
            sector_focus[inv][d.sector_canonical] += 1
    rows = []
    for name, n in counter.most_common(limit):
        focus = ", ".join(s for s, _ in sector_focus[name].most_common(3))
        rows.append({"name": name, "deals": n, "sectors": focus})
    return rows


def _ledger(deals: list[FundingDeal], limit: int = 12) -> list[dict]:
    """Return the top ``limit`` deals by capital, as compact ledger rows.

    Keys are deliberately terse (``d``, ``n``, ``s``, ``a``…): this payload is
    serialised to JSON and embedded inline in the standalone report document, so
    short keys measurably shrink the exported/shared file.
    """
    top = sorted(deals, key=lambda d: d.amount_usd_mn, reverse=True)[:limit]
    return [{
        "d": d.deal_date.strftime("%d-%b-%y"),
        "n": d.company_name,
        "s": d.sector_canonical,
        "a": round(d.amount_usd_mn, 1),
        "a_label": _fmt_amt(d.amount_usd_mn),
        "i": d.investors_raw,
        "round": d.round_type,
    } for d in top]


# ───────────────────────── orchestration ─────────────────────────

def build_payload(org_id: int, period: ResolvedPeriod) -> dict:
    """Assemble the full, template-ready report payload for one org and period.

    Returns the single ``dict`` consumed by ``reports/document.html`` — the
    integration contract between the engine and the view. Top-level keys
    (``meta``, ``stats``, ``trend``, ``sectors``, ``pivot``, ``tickets``,
    ``investors``, ``ledger``) map 1:1 to the report's sections.

    Prior-period resolution is wrapped in a broad ``except`` on purpose: a
    missing or malformed ``prev_key`` must degrade gracefully to "no deltas"
    rather than fail the entire report. The heatmap/pivot is limited to the top
    10 sectors to keep the matrix readable.
    """
    deals = _deals_between(org_id, period.headline_start, period.headline_end)

    prev_deals: list[FundingDeal] = []
    if period.prev_key:
        try:
            prev = resolve(period.period_type, parse_key(period.period_type, period.prev_key))
            prev_deals = _deals_between(org_id, prev.headline_start, prev.headline_end)
        except Exception:
            prev_deals = []

    sectors = _sectors(deals)
    top_sector_names = [r["s"] for r in sectors[:10]]

    return {
        "meta": {
            "period_type": period.period_type,
            "period_key": period.period_key,
            "title": period.title,
            "subtitle": period.subtitle,
            "edition_no": period.edition_no,
            "range_start": period.headline_start.isoformat(),
            "range_end": period.headline_end.isoformat(),
            "ledger_subtitle": _ledger_subtitle(period),
            "trend_axis": _trend_axis(period),
        },
        "stats": _headline_stats(deals, prev_deals),
        "trend": _trend(org_id, period.buckets),
        "sectors": sectors,
        "pivot": _pivot(org_id, period.buckets, top_sector_names),
        "tickets": _tickets(deals),
        "investors": _investors(deals),
        "ledger": _ledger(deals),
    }


def _ledger_subtitle(period: ResolvedPeriod) -> str:
    noun = {"weekly": "this week", "monthly": "this month",
            "quarterly": "this quarter", "annual": "this year"}[period.period_type]
    return f"Top transactions {noun}, ranked by capital deployed."


def _trend_axis(period: ResolvedPeriod) -> str:
    return {"weekly": "Weekly deal momentum", "monthly": "Monthly deal momentum",
            "quarterly": "Quarterly deal momentum", "annual": "Monthly momentum across the year"}[period.period_type]
