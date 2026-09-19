"""Dashboard: the workspace landing page and operational observability.

Two views (all-time KPIs + the job-run activity feed) plus a ``/health``
liveness probe. Every query here is scoped to the workspace via
:func:`require_org`.
"""
from __future__ import annotations

from flask import Blueprint, current_app, render_template, url_for
from sqlalchemy import func, select

from ..extensions import db
from ..models import Favorite, FundingDeal, JobRun, Report, ReportStatus
from ..tenancy import require_org

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def home():
    """Render the workspace overview: headline KPIs, sector mix, and data health.

    Computes, for the active org only: all-time deal/capital/report/sector
    counts; the top-8 sectors by capital (as percentage bars); data-quality
    flags that surface silent ingestion problems (uncategorised sectors,
    undisclosed amounts) the user would otherwise never notice; the five most
    recent reports; and the overall date span of the deal history.

    Each metric is a separate aggregate query rather than one wide join — the
    counts are independent and SQLite/Postgres both plan these trivially, so the
    clarity is worth more than shaving a round-trip.
    """
    org = require_org()

    total_deals = db.session.scalar(
        select(func.count(FundingDeal.id)).where(FundingDeal.organization_id == org.id)
    ) or 0
    total_capital = db.session.scalar(
        select(func.coalesce(func.sum(FundingDeal.amount_usd_mn), 0.0)).where(FundingDeal.organization_id == org.id)
    ) or 0.0
    report_count = db.session.scalar(
        select(func.count(Report.id)).where(Report.organization_id == org.id)
    ) or 0
    sector_total = db.session.scalar(
        select(func.count(func.distinct(FundingDeal.sector_canonical)))
        .where(FundingDeal.organization_id == org.id)
    ) or 0

    # Top sectors all-time (for a quick bar on the dashboard).
    sector_rows = db.session.execute(
        select(FundingDeal.sector_canonical, func.sum(FundingDeal.amount_usd_mn), func.count(FundingDeal.id))
        .where(FundingDeal.organization_id == org.id)
        .group_by(FundingDeal.sector_canonical)
        .order_by(func.sum(FundingDeal.amount_usd_mn).desc())
        .limit(8)
    ).all()
    max_sector = max((r[1] for r in sector_rows), default=1) or 1
    sectors = [{"name": r[0], "amt": round(r[1], 1), "deals": r[2],
                "pct": round(r[1] / max_sector * 100, 1)} for r in sector_rows]

    # Data-quality flags. Each one is actionable: it links straight to the
    # filtered ledger showing exactly the rows it is complaining about, so a
    # flag is one click from being fixed rather than just being a number.
    flags = []
    missing_sector = db.session.scalar(
        select(func.count(FundingDeal.id)).where(
            FundingDeal.organization_id == org.id, FundingDeal.sector_canonical == "Uncategorized")
    ) or 0
    if missing_sector:
        flags.append({
            "text": f"{missing_sector} deal{'' if missing_sector == 1 else 's'} uncategorized",
            "hint": "Select them in the ledger and use “Move to sector”.",
            "url": url_for("data.index", sector="Uncategorized"),
            "cta": "Review",
        })
    zero_amt = db.session.scalar(
        select(func.count(FundingDeal.id)).where(
            FundingDeal.organization_id == org.id, FundingDeal.amount_usd_mn == 0)
    ) or 0
    if zero_amt:
        flags.append({
            "text": f"{zero_amt} deal{'' if zero_amt == 1 else 's'} with no disclosed amount",
            "hint": "Usually genuine — Inc42 often reports undisclosed rounds.",
            "url": url_for("data.index", sort="amount", dir="asc"),
            "cta": "Review",
        })
    dupes = 0
    try:
        from ..ingestion.dedup_review import find_candidates
        dupes = len(find_candidates(org.id))
    except Exception:  # pragma: no cover - never let a scan break the dashboard
        current_app.logger.exception("Duplicate scan failed on the dashboard")
    if dupes:
        flags.append({
            "text": f"{dupes} possible duplicate pair{'' if dupes == 1 else 's'}",
            "hint": "Same company, near-identical date and amount.",
            "url": url_for("data.dedup_review"),
            "cta": "Resolve",
        })

    recent_reports = db.session.scalars(
        select(Report).where(Report.organization_id == org.id).order_by(Report.created_at.desc()).limit(5)
    ).all()

    span = db.session.execute(
        select(func.min(FundingDeal.deal_date), func.max(FundingDeal.deal_date))
        .where(FundingDeal.organization_id == org.id)
    ).first()

    favorites = db.session.scalars(
        select(Favorite).where(Favorite.organization_id == org.id).order_by(Favorite.created_at.desc())
    ).all()
    pinned = []
    for f in favorites:
        try:
            pinned.append(_favorite_link(f))
        except (TypeError, ValueError):
            continue

    return render_template(
        "dashboard/home.html", active="dashboard",
        total_deals=total_deals, total_capital=round(total_capital, 1),
        report_count=report_count, sector_total=sector_total, sectors=sectors, flags=flags,
        recent_reports=recent_reports, span=span, pinned=pinned,
    )


def _favorite_link(f: Favorite) -> dict:
    """Resolve a Favorite row to a display label + endpoint/kwargs for url_for."""
    common = {"label": f.label or f.ref, "kind": f.kind, "ref": f.ref}
    if f.kind == "report":
        return {**common, "icon": "file-text",
                "endpoint": "reports.view", "kwargs": {"report_id": int(f.ref)}}
    icons = {"company": "buildings", "sector": "squares-four", "investor": "handshake"}
    return {**common, "icon": icons.get(f.kind, "star"),
            "endpoint": f"explore.{f.kind}", "kwargs": {"name": f.ref}}


@bp.route("/activity")
def runs():
    """Show the org's recent job history (ingestion + report generation).

    The audit trail is the primary debugging surface when an import or a
    scheduled report misbehaves. Capped at the 100 most recent runs so the page
    stays bounded regardless of how busy the workspace is.
    """
    org = require_org()
    runs = db.session.scalars(
        select(JobRun).where(JobRun.organization_id == org.id).order_by(JobRun.started_at.desc()).limit(100)
    ).all()
    return render_template("dashboard/runs.html", runs=runs, active="runs")


@bp.route("/health")
def health():
    """Liveness/readiness probe for load balancers and uptime monitors.

    Intentionally unauthenticated so external probes can reach it, and
    deliberately issues a trivial query to assert the database is actually
    reachable (not just that the process is up). Returns HTTP 503 — not 500 — on
    failure so orchestrators interpret it as "temporarily unavailable" and route
    traffic elsewhere rather than treating the instance as crashed. The error
    detail is safe to expose here because the endpoint reveals no tenant data.
    """
    try:
        deals = db.session.scalar(select(func.count(FundingDeal.id))) or 0
        reports = db.session.scalar(select(func.count(Report.id))) or 0
        latest_deal = db.session.scalar(select(func.max(FundingDeal.deal_date)))
        last_run = db.session.scalars(
            select(JobRun).order_by(JobRun.started_at.desc()).limit(1)
        ).first()
        # Counts and freshness make this probe useful for more than liveness:
        # a monitor can alert on "data has gone stale" or "the last job failed"
        # without needing an API key.
        return {
            "status": "ok",
            "deals": deals,
            "reports": reports,
            "latest_deal_date": latest_deal.isoformat() if latest_deal else None,
            "last_job": {
                "kind": last_run.kind,
                "status": last_run.status.value,
                "at": last_run.started_at.isoformat(),
            } if last_run else None,
            "scheduler": bool(current_app.config.get("ENABLE_SCHEDULER")),
        }, 200
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "detail": str(exc)}, 503
