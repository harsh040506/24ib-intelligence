"""Public REST API (v1).

Authenticated with an org API key via the ``Authorization: Bearer ib_...`` header
(or ``X-API-Key``). Returns JSON. A minimal OpenAPI document + an interactive
docs page are served so the API is self-describing and SDK-generatable.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, Response, current_app, g, jsonify, render_template, request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from ..clock import utcnow
from ..engine import generate_report, render_report_html
from ..engine.periods import parse_key
from ..extensions import db
from ..ingestion.normalize import parse_date
from ..models import ApiKey, FundingDeal, PeriodType, Report
from ..security import RateLimiter

bp = Blueprint("api", __name__, url_prefix="/api/v1")

# Per-key throttle: 120 requests/minute. Report generation aggregates the full
# deal history, so an unbounded caller could exhaust CPU; this caps a single
# key's burst while leaving ample headroom for normal SDK/dashboard usage.
_api_limiter = RateLimiter(max_hits=120, window_seconds=60)

_VALID_PERIODS = {pt.value for pt in PeriodType}


def _extract_key() -> str | None:
    """Read the API key from ``Authorization: Bearer`` or the ``X-API-Key`` header."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key")


def require_api_key(fn):
    """Decorator enforcing a valid, non-revoked API key plus per-key rate limiting.

    Keys are looked up by their public prefix (indexed) and then verified against
    a stored hash — the raw key is never persisted, so a database leak cannot be
    replayed against the API.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        raw = _extract_key()
        if not raw:
            return jsonify(error="Missing API key"), 401
        rec = db.session.scalars(
            select(ApiKey).where(ApiKey.prefix == raw[:10], ApiKey.revoked == False)  # noqa: E712
        ).first()
        if not rec or not rec.verify(raw):
            return jsonify(error="Invalid API key"), 401
        if not _api_limiter.hit(f"key:{rec.id}"):
            return jsonify(error="Rate limit exceeded — retry shortly."), 429
        # last_used_at is telemetry, not correctness; never let a write failure
        # here block an otherwise-valid request.
        try:
            rec.last_used_at = utcnow()
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
        g.api_org_id = rec.organization_id
        return fn(*args, **kwargs)
    return wrapper


def _generate_or_error(period_type: str, period_key: str):
    """Resolve+generate a report, returning ``(report, None)`` or ``(None, json_resp)``.

    Centralises validation so every report endpoint rejects an unknown period
    type/key with a 400 and never lets a generation exception surface as a 500.
    """
    if period_type not in _VALID_PERIODS:
        return None, (jsonify(error=f"Unknown period_type: {period_type}"), 400)
    try:
        parse_key(period_type, period_key)  # validate the key shape up front
    except Exception as exc:
        return None, (jsonify(error=f"Bad period key: {exc}"), 400)
    try:
        report = generate_report(g.api_org_id, period_type, period_key=period_key)
    except Exception:
        current_app.logger.exception("API report generation failed (%s %s)", period_type, period_key)
        return None, (jsonify(error="Report generation failed."), 500)
    return report, None


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(request.args.get(name, default)), hi))
    except (TypeError, ValueError):
        return default


def _deal_json(d: FundingDeal) -> dict:
    return {
        "id": d.id, "company": d.company_name, "sector": d.sector_canonical,
        "subsector": d.subsector, "business_model": d.business_model,
        "funding_round_size": d.funding_round_size,
        "amount_usd_mn": d.amount_usd_mn, "round": d.round_type,
        "date": d.deal_date.isoformat(), "investors": d.investor_list,
        "lead": d.lead_investor, "source": d.source_url,
    }


@bp.get("/deals")
@require_api_key
def list_deals():
    """Page through the deal ledger, with optional sector/date/text filters.

    Returns ``total`` alongside the page so a client can drive its own
    pagination without guessing when it has reached the end.
    """
    limit = _int_arg("limit", 100, 1, 500)
    offset = _int_arg("offset", 0, 0, 10_000_000)

    clauses = [FundingDeal.organization_id == g.api_org_id]
    sector = (request.args.get("sector") or "").strip()
    if sector:
        clauses.append(FundingDeal.sector_canonical == sector)
    q = (request.args.get("q") or "").strip()
    if q:
        like = f"%{q[:200]}%"
        clauses.append(or_(FundingDeal.company_name.ilike(like),
                           FundingDeal.investors_raw.ilike(like)))
    for arg, op in (("from", "ge"), ("to", "le")):
        raw = request.args.get(arg)
        if not raw:
            continue
        parsed = parse_date(raw)
        if parsed is None:
            return jsonify(error=f"Bad {arg!r} date: use YYYY-MM-DD"), 400
        clauses.append(FundingDeal.deal_date >= parsed if op == "ge"
                       else FundingDeal.deal_date <= parsed)

    total = db.session.scalar(select(func.count(FundingDeal.id)).where(*clauses)) or 0
    rows = db.session.scalars(
        select(FundingDeal).where(*clauses)
        .order_by(FundingDeal.deal_date.desc(), FundingDeal.id.desc())
        .limit(limit).offset(offset)
    ).all()
    return jsonify(count=len(rows), total=total, limit=limit, offset=offset,
                   deals=[_deal_json(d) for d in rows])


@bp.get("/reports")
@require_api_key
def list_reports():
    """List stored reports (newest first), optionally filtered by cadence."""
    limit = _int_arg("limit", 50, 1, 200)
    offset = _int_arg("offset", 0, 0, 1_000_000)

    clauses = [Report.organization_id == g.api_org_id]
    period_type = (request.args.get("period_type") or "").strip()
    if period_type:
        if period_type not in _VALID_PERIODS:
            return jsonify(error=f"Unknown period_type: {period_type}"), 400
        clauses.append(Report.period_type == PeriodType(period_type))

    total = db.session.scalar(select(func.count(Report.id)).where(*clauses)) or 0
    rows = db.session.scalars(
        select(Report).where(*clauses).order_by(Report.period_key.desc())
        .limit(limit).offset(offset)
    ).all()
    return jsonify(count=len(rows), total=total, limit=limit, offset=offset, reports=[{
        "period_type": r.period_type.value, "period_key": r.period_key,
        "title": r.title, "status": r.status.value,
        "is_published": r.is_published,
        "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        "stats": (r.payload or {}).get("stats"),
        "share_url": request.host_url.rstrip("/") + f"/reports/share/{r.share_token}",
    } for r in rows])


@bp.get("/reports/<period_type>/<period_key>")
@require_api_key
def get_report(period_type: str, period_key: str):
    """Return a report summary, generating it on demand if missing."""
    report, err = _generate_or_error(period_type, period_key)
    if err:
        return err
    return jsonify(meta=report.payload.get("meta"), stats=report.payload.get("stats"),
                   sectors=report.payload.get("sectors"), share_url=request.host_url.rstrip("/")
                   + f"/reports/share/{report.share_token}")


@bp.get("/reports/<period_type>/<period_key>/full")
@require_api_key
def get_report_full(period_type: str, period_key: str):
    report, err = _generate_or_error(period_type, period_key)
    if err:
        return err
    return jsonify(report.payload)


@bp.get("/reports/<period_type>/<period_key>/html")
@require_api_key
def get_report_html(period_type: str, period_key: str):
    """Return the report as a self-contained, downloadable HTML document."""
    report, err = _generate_or_error(period_type, period_key)
    if err:
        return err
    doc = render_report_html(report, standalone=True)
    filename = f"{report.period_key}-{report.share_token[:8]}.html"
    return Response(doc, mimetype="text/html",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.get("/openapi.json")
def openapi():
    return jsonify(_OPENAPI)


@bp.get("/docs")
def docs():
    return render_template("api/docs.html", spec=_OPENAPI)


_OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "24IB Intelligence API", "version": "1.0.0",
             "description": "Programmatic access to funding deals and generated intelligence reports."},
    "servers": [{"url": "/api/v1"}],
    "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    "security": [{"bearerAuth": []}],
    "paths": {
        "/deals": {"get": {
            "summary": "List funding deals (paged + filterable)",
            "parameters": [
                {"name": "limit", "in": "query", "description": "Page size (1–500).",
                 "schema": {"type": "integer", "default": 100, "maximum": 500}},
                {"name": "offset", "in": "query", "description": "Rows to skip.",
                 "schema": {"type": "integer", "default": 0}},
                {"name": "sector", "in": "query", "description": "Exact canonical sector name.",
                 "schema": {"type": "string", "example": "Fintech"}},
                {"name": "q", "in": "query", "description": "Substring match on company or investors.",
                 "schema": {"type": "string"}},
                {"name": "from", "in": "query", "description": "Earliest deal date (inclusive).",
                 "schema": {"type": "string", "format": "date", "example": "2026-01-01"}},
                {"name": "to", "in": "query", "description": "Latest deal date (inclusive).",
                 "schema": {"type": "string", "format": "date"}}],
            "responses": {"200": {"description": "A page of deals plus the matching total"},
                          "400": {"description": "Unparseable date filter"}}}},
        "/reports": {"get": {
            "summary": "List stored reports",
            "parameters": [
                {"name": "period_type", "in": "query",
                 "schema": {"type": "string", "enum": ["weekly", "monthly", "quarterly", "annual"]}},
                {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50, "maximum": 200}},
                {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0}}],
            "responses": {"200": {"description": "A page of report summaries"}}}},
        "/reports/{period_type}/{period_key}": {"get": {
            "summary": "Get (and generate) a report summary",
            "parameters": [
                {"name": "period_type", "in": "path", "required": True,
                 "schema": {"type": "string", "enum": ["weekly", "monthly", "quarterly", "annual"]}},
                {"name": "period_key", "in": "path", "required": True,
                 "schema": {"type": "string", "example": "2026-W14"}}],
            "responses": {"200": {"description": "Report summary + share URL"}}}},
        "/reports/{period_type}/{period_key}/full": {"get": {
            "summary": "Get the full report payload",
            "parameters": [
                {"name": "period_type", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "period_key", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Full payload"}}}},
        "/reports/{period_type}/{period_key}/html": {"get": {
            "summary": "Export the report as a self-contained HTML document",
            "parameters": [
                {"name": "period_type", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "period_key", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "A standalone HTML document (text/html)"}}}},
    },
}
