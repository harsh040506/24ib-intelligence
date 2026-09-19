"""Report library, generation, single-report view, PDF export and share links."""
from __future__ import annotations

from datetime import date

from flask import (
    Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for,
)
from sqlalchemy import func, or_, select

from ..engine import generate_report, render_report_html
from ..engine.compare import diff_payloads
from ..engine.periods import parse_key, resolve
from ..extensions import db
from ..models import PeriodType, Report, ReportStatus
from ..publish import rebuild_site, remove_report_page
from ..tenancy import require_org

bp = Blueprint("reports", __name__, url_prefix="/reports")

_PER_PAGE = 20


def _clamp_int(raw, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


@bp.route("/")
def index():
    org = require_org()
    q = (request.args.get("q") or "").strip()
    type_filter = request.args.get("type") or ""
    page = _clamp_int(request.args.get("page"), default=1, lo=1, hi=1_000_000)

    filters = [Report.organization_id == org.id]
    if type_filter in {pt.value for pt in PeriodType}:
        filters.append(Report.period_type == PeriodType(type_filter))
    if q:
        like = f"%{q}%"
        filters.append(or_(Report.title.ilike(like), Report.period_key.ilike(like)))

    total = db.session.scalar(select(func.count(Report.id)).where(*filters)) or 0
    pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    page = min(page, pages)
    reports = db.session.scalars(
        select(Report).where(*filters).order_by(Report.created_at.desc())
        .limit(_PER_PAGE).offset((page - 1) * _PER_PAGE)
    ).all()

    all_total = db.session.scalar(
        select(func.count(Report.id)).where(Report.organization_id == org.id)
    ) or 0

    return render_template(
        "reports/index.html", reports=reports, total=total, all_total=all_total,
        page=page, pages=pages, q=q, type_filter=type_filter, active="reports",
    )


@bp.route("/new", methods=["GET", "POST"])
def new():
    org = require_org()
    if request.method == "POST":
        period_type = request.form.get("period_type", "weekly")
        key = request.form.get("period_key", "").strip() or None
        if period_type not in {pt.value for pt in PeriodType}:
            flash("Choose a valid report cadence.", "error")
            return redirect(url_for("reports.new"))
        try:
            report = generate_report(org.id, period_type, period_key=key)
        except Exception as exc:
            current_app.logger.exception("Report generation failed (%s %s)", period_type, key)
            flash(f"Generation failed: {exc}", "error")
            return redirect(url_for("reports.new"))
        flash(f"Generated “{report.title}”.", "success")
        return redirect(url_for("reports.view", report_id=report.id))

    # Suggest sensible default keys for each period from today.
    today = date.today()
    suggestions = {pt.value: resolve(pt.value, today).period_key for pt in PeriodType}
    return render_template("reports/new.html", suggestions=suggestions, active="reports")


@bp.route("/compare")
def compare():
    org = require_org()
    a_id = request.args.get("a", type=int)
    b_id = request.args.get("b", type=int)

    by_type: dict[str, list[Report]] = {pt.value: [] for pt in PeriodType}
    for r in db.session.scalars(
        select(Report).where(Report.organization_id == org.id, Report.status == ReportStatus.READY)
        .order_by(Report.period_type, Report.period_key)
    ):
        by_type[r.period_type.value].append(r)

    if not a_id or not b_id:
        return render_template("reports/compare.html", by_type=by_type,
                               a=None, b=None, diff=None, active="reports")

    a, b = _get_report(a_id), _get_report(b_id)
    diff = diff_payloads(a.payload, b.payload)
    return render_template("reports/compare.html", by_type=by_type,
                           a=a, b=b, diff=diff, active="reports")


@bp.route("/<int:report_id>")
def view(report_id: int):
    report = _get_report(report_id)
    prev_report = None
    try:
        anchor = parse_key(report.period_type.value, report.period_key)
        prev_key = resolve(report.period_type.value, anchor).prev_key
        prev_report = db.session.scalars(
            select(Report).where(
                Report.organization_id == report.organization_id,
                Report.period_type == report.period_type,
                Report.period_key == prev_key,
                Report.status == ReportStatus.READY,
            )
        ).first()
    except Exception:
        pass
    from ..models import Favorite
    is_favorite = db.session.scalar(
        select(Favorite.id).where(Favorite.organization_id == report.organization_id,
                                  Favorite.kind == "report", Favorite.ref == str(report.id))
    ) is not None
    return render_template("reports/view.html", report=report, p=report.payload,
                           standalone=False, prev_report=prev_report,
                           is_favorite=is_favorite, active="reports")


@bp.route("/<int:report_id>/regenerate", methods=["POST"])
def regenerate(report_id: int):
    report = _get_report(report_id)
    try:
        generate_report(report.organization_id, report.period_type.value, period_key=report.period_key)
        flash("Report regenerated with the latest data.", "success")
    except Exception as exc:
        current_app.logger.exception("Regeneration failed for report %s", report_id)
        flash(f"Regeneration failed: {exc}", "error")
    return redirect(url_for("reports.view", report_id=report.id))


@bp.route("/<int:report_id>/publish", methods=["POST"])
def publish(report_id: int):
    report = _get_report(report_id)
    report.is_published = True
    db.session.commit()
    _try_rebuild(report.organization_id)
    flash("Published to the public site.", "success")
    return redirect(url_for("reports.view", report_id=report.id))


@bp.route("/<int:report_id>/unpublish", methods=["POST"])
def unpublish(report_id: int):
    report = _get_report(report_id)
    report.is_published = False
    db.session.commit()
    try:
        remove_report_page(report)
    except Exception as exc:  # pragma: no cover
        current_app.logger.warning("Removing published page failed: %s", exc)
    _try_rebuild(report.organization_id)
    flash("Removed from the public site.", "info")
    return redirect(url_for("reports.view", report_id=report.id))


@bp.route("/<int:report_id>/notes", methods=["POST"])
def save_notes(report_id: int):
    report = _get_report(report_id)
    report.notes = (request.form.get("notes") or "").strip()[:20_000]
    db.session.commit()
    flash("Notes saved.", "success")
    return redirect(url_for("reports.view", report_id=report.id))


@bp.route("/<int:report_id>/pdf")
def pdf(report_id: int):
    report = _get_report(report_id)
    html = render_report_html(report, standalone=True)
    from ..engine.pdf import is_available, html_to_pdf
    if not is_available():
        # Graceful fallback: serve the print-ready standalone HTML.
        flash("Server-side PDF backend not installed — opened the print-ready page instead "
              "(use your browser's Print → Save as PDF).", "info")
        return Response(html, mimetype="text/html")
    out = current_app.config["EXPORT_DIR"] / f"{report.period_key}-{report.share_token[:8]}.pdf"
    try:
        html_to_pdf(html, out)
        _prune_exports(current_app.config["EXPORT_DIR"])
    except Exception as exc:
        # Any rendering failure (missing Chromium, launch error, timeout) must
        # degrade to the print-ready page rather than 500 — the user still gets
        # a usable document via the browser's print dialog.
        current_app.logger.warning("PDF render failed for report %s: %s", report_id, exc)
        flash("Couldn't render a server-side PDF — opened the print-ready page instead "
              "(use your browser's Print → Save as PDF).", "info")
        return Response(html, mimetype="text/html")
    return Response(out.read_bytes(), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{out.name}"'})


@bp.route("/<int:report_id>/html")
def html(report_id: int):
    """Export the report as a self-contained, downloadable HTML document.

    The document inlines the design-system CSS (see ``render_report_html``), so
    the saved file renders identically offline — no server, no external assets.
    """
    report = _get_report(report_id)
    doc = render_report_html(report, standalone=True)
    filename = f"{report.period_key}-{report.share_token[:8]}.html"
    return Response(
        doc,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/share/<token>")
def share(token: str):
    """Public, read-only report view. No auth required — the token is the secret."""
    report = db.session.scalars(select(Report).where(Report.share_token == token)).first()
    if not report or report.status != ReportStatus.READY:
        abort(404)
    return render_report_html(report, standalone=True)


# ── helpers ──

def _get_report(report_id: int) -> Report:
    org = require_org()
    report = db.session.get(Report, report_id)
    if not report or report.organization_id != org.id:
        abort(404)
    return report


def _prune_exports(export_dir, keep: int = 40) -> None:
    """Keep only the ``keep`` most recent generated PDFs.

    Every PDF export writes a file into the instance folder and nothing ever
    removed them, so a long-lived install grows without bound. Pruning here
    (rather than on a timer) keeps the cleanup tied to the only thing that
    creates the files. Best-effort: a locked or vanished file is skipped.
    """
    try:
        pdfs = sorted(export_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in pdfs[keep:]:
            try:
                stale.unlink()
            except OSError:
                continue
    except OSError:  # pragma: no cover - defensive
        pass


def _try_rebuild(org_id: int) -> None:
    """Best-effort static-site rebuild after a publish/unpublish toggle.

    Isolated the same way ``engine.service._maybe_publish`` is: a failure here
    must never surface as a 500 on the toggle itself.
    """
    try:
        rebuild_site(org_id)
    except Exception as exc:  # pragma: no cover
        current_app.logger.warning("Site rebuild after publish toggle failed: %s", exc)
