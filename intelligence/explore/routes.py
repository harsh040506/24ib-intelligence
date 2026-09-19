"""Explore: browse the dataset by company / investor / sector, and search.

Companies and investors aren't normalised entity tables (see the note in
``models.py``) — they're grouped straight off ``FundingDeal``'s own text
fields, which is where the real data lives. Every profile page also renders a
month-bucketed trend using the same ``.barchart`` CSS the report engine
already uses (``static/css/report.css``) — no new charting code.
"""
from __future__ import annotations

from collections import defaultdict

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request, url_for,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..engine.aggregate import _fmt_amt
from ..extensions import db
from ..ingestion import sector_normalizer
from ..models import Favorite, FundingDeal, Sector
from ..security import is_safe_redirect_path
from ..tenancy import require_org

bp = Blueprint("explore", __name__, url_prefix="/explore")

_SEARCH_LIMIT = 12
_PER_PAGE = 50


def _monthly_trend(deals: list[FundingDeal]) -> list[dict]:
    """Bucket deals by calendar month for the profile-page bar chart."""
    buckets: dict[str, list] = defaultdict(lambda: [0.0, 0])
    for d in deals:
        key = f"{d.deal_date.year}-{d.deal_date.month:02d}"
        buckets[key][0] += d.amount_usd_mn
        buckets[key][1] += 1
    out = [{"key": k, "label": k[2:], "amt": round(a, 1), "deals": n}
           for k, (a, n) in sorted(buckets.items())]
    return out[-18:]  # cap to the trailing ~1.5 years so the chart stays legible


def _profile_stats(deals: list[FundingDeal]) -> dict:
    total_amt = sum(d.amount_usd_mn for d in deals)
    dates = sorted(d.deal_date for d in deals)
    return {
        "count": len(deals),
        "total_amt": round(total_amt, 1),
        "total_label": _fmt_amt(total_amt),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
    }


def _paginate(deals: list[FundingDeal]) -> tuple[list[FundingDeal], dict]:
    """Slice a profile's deal list for display.

    Stats and the trend chart are always computed over the *whole* history —
    only the ledger table is paged, so a sector with 400 deals renders a fast
    page without misreporting its totals.
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (len(deals) + _PER_PAGE - 1) // _PER_PAGE)
    page = min(page, pages)
    start = (page - 1) * _PER_PAGE
    return deals[start:start + _PER_PAGE], {"page": page, "pages": pages, "total": len(deals)}


def _render_profile(kind: str, name: str, deals: list[FundingDeal], org_id: int):
    rows, pagination = _paginate(deals)
    return render_template(
        "explore/profile.html", kind=kind, name=name, deals=rows,
        stats=_profile_stats(deals), trend=_monthly_trend(deals),
        pagination=pagination, is_favorite=_is_favorite(org_id, kind, name),
        active="explore",
    )


@bp.route("/company/<path:name>")
def company(name: str):
    org = require_org()
    deals = db.session.scalars(
        select(FundingDeal).where(
            FundingDeal.organization_id == org.id,
            func.lower(FundingDeal.company_name) == name.lower(),
        ).order_by(FundingDeal.deal_date.desc())
    ).all()
    if not deals:
        abort(404)
    return _render_profile("company", deals[0].company_name, deals, org.id)


@bp.route("/sector/<path:name>")
def sector(name: str):
    org = require_org()
    deals = db.session.scalars(
        select(FundingDeal).where(
            FundingDeal.organization_id == org.id,
            FundingDeal.sector_canonical == name,
        ).order_by(FundingDeal.deal_date.desc())
    ).all()
    # A sector that exists in the taxonomy but has no deals yet is a real,
    # meaningful page (an empty state) — not a 404.
    if not deals and name not in sector_normalizer.CANONICAL_ALIASES:
        known = db.session.scalar(
            select(Sector.id).where(Sector.organization_id == org.id, Sector.canonical == name)
        )
        if not known:
            abort(404)
    return _render_profile("sector", name, deals, org.id)


@bp.route("/investor/<path:name>")
def investor(name: str):
    org = require_org()
    candidates = db.session.scalars(
        select(FundingDeal).where(
            FundingDeal.organization_id == org.id,
            or_(FundingDeal.lead_investor.ilike(f"%{name}%"),
                FundingDeal.investors_raw.ilike(f"%{name}%")),
        ).order_by(FundingDeal.deal_date.desc())
    ).all()
    needle = name.strip().lower()
    deals = [d for d in candidates
             if d.lead_investor.strip().lower() == needle
             or any(i.strip().lower() == needle for i in d.investor_list)]
    if not deals:
        abort(404)
    return _render_profile("investor", name, deals, org.id)


@bp.route("/search")
def search():
    org = require_org()
    q = (request.args.get("q") or "").strip()
    results = {"companies": [], "investors": [], "sectors": [], "deals": []}
    if len(q) >= 2:
        like = f"%{q}%"

        results["companies"] = [row[0] for row in db.session.execute(
            select(FundingDeal.company_name).where(
                FundingDeal.organization_id == org.id, FundingDeal.company_name.ilike(like))
            .group_by(FundingDeal.company_name).limit(_SEARCH_LIMIT)
        )]
        results["sectors"] = [row[0] for row in db.session.execute(
            select(Sector.canonical).where(
                Sector.organization_id == org.id, Sector.canonical.ilike(like))
            .limit(_SEARCH_LIMIT)
        )]
        # Investors live only as free text on deals — pull matching rows, then
        # dedupe the actual investor names in Python (a deal can name several).
        inv_rows = db.session.scalars(
            select(FundingDeal).where(
                FundingDeal.organization_id == org.id,
                or_(FundingDeal.lead_investor.ilike(like), FundingDeal.investors_raw.ilike(like)),
            ).limit(200)
        ).all()
        seen = set()
        for d in inv_rows:
            for inv in ([d.lead_investor] if d.lead_investor else []) + d.investor_list:
                if q.lower() in inv.lower() and inv.lower() not in seen:
                    seen.add(inv.lower())
                    results["investors"].append(inv)
            if len(results["investors"]) >= _SEARCH_LIMIT:
                break
        results["investors"] = results["investors"][:_SEARCH_LIMIT]

        results["deals"] = db.session.scalars(
            select(FundingDeal).where(
                FundingDeal.organization_id == org.id, FundingDeal.company_name.ilike(like))
            .order_by(FundingDeal.deal_date.desc()).limit(_SEARCH_LIMIT)
        ).all()

    return render_template("explore/search.html", q=q, results=results, active="explore")


@bp.route("/favorite", methods=["POST"])
def toggle_favorite():
    org = require_org()
    kind = request.form.get("kind", "")
    ref = (request.form.get("ref") or "").strip()[:240]
    label = (request.form.get("label") or ref).strip()[:240]

    submitted_next = request.form.get("next") or ""
    next_url = submitted_next if is_safe_redirect_path(submitted_next) else url_for("dashboard.home")

    if kind not in {"report", "company", "sector", "investor"} or not ref:
        flash("Could not pin that.", "error")
        return redirect(next_url)

    existing = db.session.scalars(
        select(Favorite).where(Favorite.organization_id == org.id,
                               Favorite.kind == kind, Favorite.ref == ref)
    ).first()
    try:
        if existing:
            db.session.delete(existing)
            db.session.commit()
            flash(f"Unpinned {label}.", "info")
        else:
            db.session.add(Favorite(organization_id=org.id, kind=kind, ref=ref, label=label))
            db.session.commit()
            flash(f"Pinned {label} to your dashboard.", "success")
    except IntegrityError:
        # Double-submit raced us to the same pin — the desired end state already
        # holds, so this is a success, not an error.
        db.session.rollback()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Favorite toggle failed (%s %s)", kind, ref)
        flash("Could not update your pins. Please try again.", "error")
    return redirect(next_url)


def _is_favorite(org_id: int, kind: str, ref: str) -> bool:
    return db.session.scalar(
        select(Favorite.id).where(Favorite.organization_id == org_id,
                                  Favorite.kind == kind, Favorite.ref == ref)
    ) is not None
