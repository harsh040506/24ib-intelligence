"""Report generation service.

Ties the engine to persistence and observability: resolve the period, build the
payload, store it on a ``Report`` row, and record the work in a ``JobRun`` so
every generation is auditable. Idempotent per (org, period_type, period_key):
re-generating updates the existing report in place.
"""
from __future__ import annotations

from datetime import date

from flask import render_template
from sqlalchemy import and_, select

from ..clock import utcnow
from ..extensions import db
from ..models import JobRun, PeriodType, Report, ReportStatus, RunStatus
from .aggregate import build_payload
from .periods import parse_key, resolve


def _find_or_create_report(org_id: int, period) -> Report:
    stmt = select(Report).where(
        and_(
            Report.organization_id == org_id,
            Report.period_type == PeriodType(period.period_type),
            Report.period_key == period.period_key,
        )
    )
    report = db.session.scalars(stmt).first()
    if report is None:
        report = Report(
            organization_id=org_id,
            period_type=PeriodType(period.period_type),
            period_key=period.period_key,
        )
        db.session.add(report)
    return report


def generate_report(org_id: int, period_type: str, anchor: date | None = None,
                    period_key: str | None = None) -> Report:
    """Generate (or regenerate) a report for an org + period. Records a JobRun."""
    if period_key:
        anchor = parse_key(period_type, period_key)
    period = resolve(period_type, anchor)

    run = JobRun(organization_id=org_id, kind="generate_report",
                 status=RunStatus.RUNNING,
                 detail=f"{period_type} {period.period_key}")
    db.session.add(run)
    run.append_log(f"Resolving period {period.period_key} ({period.subtitle})")

    report = _find_or_create_report(org_id, period)
    is_new = report.id is None
    report.status = ReportStatus.BUILDING
    report.title = period.title
    report.edition_no = period.edition_no
    if is_new and period_type in ("weekly", "monthly"):
        # Auto-publish weekly/monthly on first generation (matches the
        # long-standing "publish on generate" default); a later explicit
        # unpublish is never overridden by a regenerate.
        report.is_published = True
    db.session.flush()

    try:
        run.append_log("Aggregating deals, sectors, investors and market data…")
        payload = build_payload(org_id, period)
        report.payload = payload
        report.status = ReportStatus.READY
        report.generated_at = utcnow()
        report.error = None
        run.append_log(
            f"Done: {payload['stats']['total_deals']} deals, "
            f"{payload['stats']['total_capital_label']} capital across "
            f"{payload['stats']['sector_count']} sectors."
        )
        run.status = RunStatus.SUCCESS
    except Exception as exc:  # pragma: no cover - defensive
        report.status = ReportStatus.FAILED
        report.error = str(exc)
        run.status = RunStatus.FAILED
        run.append_log(f"ERROR: {exc}")
        run.finished_at = utcnow()
        db.session.commit()
        raise
    finally:
        run.finished_at = utcnow()
        db.session.commit()

    _maybe_publish(report)
    return report


def _maybe_publish(report: Report) -> None:
    """Mirror a freshly generated weekly/monthly report onto the public site.

    Best-effort and fully isolated: publishing the static site must never fail or
    roll back report generation, so every error is swallowed and logged. The
    feature is config-gated (``PUBLISH_ON_GENERATE``) and a no-op outside an app
    context (e.g. unit tests that call the engine directly)."""
    try:
        from flask import current_app, has_app_context

        if not has_app_context() or not current_app.config.get("PUBLISH_ON_GENERATE"):
            return
        if report.status != ReportStatus.READY:
            return
        from ..publish import publish_report

        publish_report(report)
    except Exception:  # pragma: no cover - defensive; never break generation
        try:
            current_app.logger.exception(
                "Auto-publish failed for report %s", getattr(report, "period_key", "?")
            )
        except Exception:
            pass


def _inline_css() -> str:
    """Concatenate the design-system stylesheets for a portable document."""
    from pathlib import Path
    css_dir = Path(__file__).resolve().parent.parent / "static" / "css"
    parts = []
    for name in ("tokens.css", "app.css", "report.css"):
        f = css_dir / name
        if f.exists():
            parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


def render_report_html(report: Report, *, standalone: bool = False) -> str:
    """Render a report to a fully self-contained HTML document (PDF/share/export)."""
    return render_template(
        "reports/document.html",
        report=report,
        p=report.payload,
        standalone=standalone,
        inline_css=_inline_css(),
    )
