"""Data workspace: the deal ledger, CSV import/export, and API-key management.

Single-user app, no auth wall — every route just operates on the one workspace
(``tenancy.require_org``). Two invariants this module is responsible for:

* **Defensive parsing.** Every user-supplied integer (pagination, page size,
  the "weeks" refresh window) and every sort/filter key is validated against a
  whitelist, so a hand-edited query string can never raise a 500 or reach the
  database as raw SQL.
* **The workbook mirrors the database.** ``instance/Inc42_Funding_Master_Data.xlsx``
  is what "Import 3-year history" reads back, so any in-app edit or deletion
  must be mirrored there — otherwise a deleted deal reappears on the next
  import. Mutating routes call :func:`sync_master_best_effort` for that.
"""
from __future__ import annotations

import csv
import io
from urllib.parse import urlparse

from flask import (
    Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..clock import utcnow
from ..extensions import db
from ..ingestion import sector_normalizer
from ..ingestion.connectors import ingest_csv
from ..ingestion.dedup_review import find_candidates
from ..ingestion.inc42 import (
    backfill_history, master_excel_path, refresh_latest, sync_master_best_effort,
)
from ..ingestion.normalize import normalize_sector, parse_amount, parse_date
from ..models import ApiKey, DedupIgnore, FundingDeal, JobRun, Sector
from ..security import is_safe_redirect_path
from ..tenancy import require_org

bp = Blueprint("data", __name__, url_prefix="/data")

# Accept only spreadsheet-style uploads. Defence-in-depth alongside the parser,
# which already tolerates arbitrary bytes — this gives the user a clear, early
# error instead of an empty import when they pick the wrong file.
_ALLOWED_UPLOAD_EXTS = (".csv", ".tsv", ".txt")

_PAGE_SIZES = (25, 40, 100, 250)
_DEFAULT_PAGE_SIZE = 40

# Sortable columns, whitelisted by name so the sort key can never be injected.
_SORTABLE = {
    "date": FundingDeal.deal_date,
    "company": FundingDeal.company_name,
    "sector": FundingDeal.sector_canonical,
    "amount": FundingDeal.amount_usd_mn,
    "round": FundingDeal.round_type,
    "lead": FundingDeal.lead_investor,
}

# Column length limits, mirroring the model definitions so an over-long paste is
# rejected with a clear message rather than a driver-level error on commit.
_MAX_LENGTHS = {
    "company_name": 200, "sector": 160, "subsector": 160, "business_model": 60,
    "funding_round_size": 80, "round_type": 80, "lead_investor": 200,
    "source_url": 500, "investors_raw": 4000,
}

_BULK_LIMIT = 1000  # cap a single bulk action, so one request can't lock the DB for minutes


def _clamp_int(raw, default: int, lo: int, hi: int) -> int:
    """Parse ``raw`` to an int clamped to [lo, hi], falling back to ``default``."""
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


def _filters_from_request():
    """Read the ledger's search/filter/sort state off the query string.

    Returns ``(filter_clauses, view_state)`` — the clauses go into the query,
    the state is echoed back into the template so pagination links and the
    export button preserve exactly what the user is looking at.
    """
    org = require_org()
    q = (request.args.get("q") or "").strip()[:200]
    sector = (request.args.get("sector") or "").strip()[:160]
    sort = request.args.get("sort") if request.args.get("sort") in _SORTABLE else "date"
    direction = "asc" if request.args.get("dir") == "asc" else "desc"

    clauses = [FundingDeal.organization_id == org.id]
    if q:
        like = f"%{q}%"
        clauses.append(or_(
            FundingDeal.company_name.ilike(like),
            FundingDeal.investors_raw.ilike(like),
            FundingDeal.lead_investor.ilike(like),
            FundingDeal.subsector.ilike(like),
        ))
    if sector:
        clauses.append(FundingDeal.sector_canonical == sector)

    state = {"q": q, "sector": sector, "sort": sort, "dir": direction}
    return clauses, state


def _ordering(state: dict):
    col = _SORTABLE[state["sort"]]
    primary = col.asc() if state["dir"] == "asc" else col.desc()
    # Stable secondary key so equal values keep a deterministic page order
    # (without it, rows can shuffle between pages and be silently skipped).
    return primary, FundingDeal.id.desc()


@bp.route("/")
def index():
    org = require_org()
    clauses, state = _filters_from_request()
    per = _clamp_int(request.args.get("per"), default=_DEFAULT_PAGE_SIZE, lo=10, hi=250)
    if per not in _PAGE_SIZES:
        per = _DEFAULT_PAGE_SIZE
    page = _clamp_int(request.args.get("page"), default=1, lo=1, hi=1_000_000)

    total = db.session.scalar(select(func.count(FundingDeal.id)).where(*clauses)) or 0
    pages = max(1, (total + per - 1) // per)
    page = min(page, pages)  # a stale ?page= past the end lands on the last page, not an empty one

    deals = db.session.scalars(
        select(FundingDeal).where(*clauses).order_by(*_ordering(state))
        .limit(per).offset((page - 1) * per)
    ).all()

    grand_total = db.session.scalar(
        select(func.count(FundingDeal.id)).where(FundingDeal.organization_id == org.id)
    ) or 0
    sector_count = db.session.scalar(
        select(func.count(Sector.id)).where(Sector.organization_id == org.id)
    ) or 0
    keys = db.session.scalars(
        select(ApiKey).where(ApiKey.organization_id == org.id, ApiKey.revoked == False)  # noqa: E712
    ).all()

    # Sectors actually present in the data — a filter that can only offer
    # choices which return results.
    used_sectors = [
        row[0] for row in db.session.execute(
            select(FundingDeal.sector_canonical)
            .where(FundingDeal.organization_id == org.id)
            .group_by(FundingDeal.sector_canonical)
            .order_by(FundingDeal.sector_canonical)
        )
    ]

    last_import = db.session.scalars(
        select(JobRun).where(
            JobRun.organization_id == org.id,
            JobRun.kind.in_(("ingest_csv", "ingest_inc42_backfill", "ingest_inc42_refresh")),
        ).order_by(JobRun.started_at.desc()).limit(1)
    ).first()

    return render_template(
        "data/index.html", deals=deals, total=total, grand_total=grand_total,
        page=page, pages=pages, per=per, page_sizes=_PAGE_SIZES,
        sector_count=sector_count, keys=keys,
        history_available=master_excel_path() is not None,
        last_import=last_import,
        canonical_sectors=sorted(sector_normalizer.CANONICAL_ALIASES.keys()),
        used_sectors=used_sectors, state=state, active="data",
    )


@bp.route("/export.csv")
def export_csv():
    """Download the currently-filtered ledger as CSV (streamed, not buffered)."""
    clauses, state = _filters_from_request()
    rows = db.session.scalars(
        select(FundingDeal).where(*clauses).order_by(*_ordering(state))
    ).all()

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Date", "Company", "Sector", "Subsector", "Business Model",
                         "Funding Round Size", "Amount (USD Mn)", "Round Type",
                         "Investors", "Lead Investor", "Source URL"])
        yield buf.getvalue()
        for d in rows:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow([
                d.deal_date.isoformat(), d.company_name, d.sector_canonical,
                d.subsector, d.business_model, d.funding_round_size,
                f"{d.amount_usd_mn:.2f}", d.round_type, d.investors_raw,
                d.lead_investor, d.source_url,
            ])
            yield buf.getvalue()

    stamp = utcnow().strftime("%Y%m%d")
    return Response(
        generate(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="deals-{stamp}.csv"'},
    )


@bp.route("/inc42/backfill", methods=["POST"])
def inc42_backfill():
    """Import the full ~3-year Inc42 history from the master workbook."""
    org = require_org()
    try:
        run = backfill_history(org.id)
    except Exception as exc:  # pragma: no cover
        current_app.logger.exception("Inc42 backfill failed for org %s", org.id)
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("data.index"))
    cat = "success" if run.status.value == "success" else "error"
    flash(f"Inc42 history import: {run.detail}", cat)
    return redirect(url_for("data.index"))


@bp.route("/inc42/refresh", methods=["POST"])
def inc42_refresh():
    """Live-scrape the latest weeks from Inc42 and add only new records."""
    org = require_org()
    weeks = _clamp_int(request.form.get("weeks"), default=10, lo=1, hi=26)
    try:
        run = refresh_latest(org.id, weeks=weeks)
    except Exception as exc:  # pragma: no cover
        current_app.logger.exception("Inc42 refresh failed for org %s", org.id)
        flash(f"Refresh failed: {exc}", "error")
        return redirect(url_for("data.index"))
    cat = "success" if run.status.value == "success" else "error"
    flash(f"Inc42 refresh ({weeks} weeks): {run.detail}", cat)
    return redirect(url_for("data.index"))


@bp.route("/import", methods=["POST"])
def import_csv():
    org = require_org()
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("data.index"))
    if not file.filename.lower().endswith(_ALLOWED_UPLOAD_EXTS):
        flash("Unsupported file type — upload a .csv, .tsv or .txt export.", "error")
        return redirect(url_for("data.index"))
    try:
        run = ingest_csv(org.id, file)
    except Exception as exc:
        # ingest_csv already rolls back its own transaction on failure; surface a
        # bounded message to the user and log the full detail server-side.
        current_app.logger.exception("CSV import failed for org %s", org.id)
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("data.index"))
    cat = "success" if run.status.value == "success" else "error"
    flash(f"Import complete — {run.detail} See Activity for the row-by-row log.", cat)
    return redirect(url_for("data.index"))


# ───────────────────────── single-deal editing ─────────────────────────

def _get_deal(deal_id: int) -> FundingDeal:
    org = require_org()
    deal = db.session.get(FundingDeal, deal_id)
    if not deal or deal.organization_id != org.id:
        abort(404)
    return deal


def _safe_next(fallback_endpoint: str = "data.index") -> str:
    """Return the submitted ``next`` target if it is a local path, else a default.

    Lets edit/delete return the user to the exact filtered page they came from
    without becoming an open redirect.
    """
    target = request.values.get("next") or ""
    return target if is_safe_redirect_path(target) else url_for(fallback_endpoint)


def _clean_url(raw: str) -> str:
    """Keep only http(s) URLs. Anything else (``javascript:``, ``data:``) is dropped.

    Deal source URLs are rendered as links, so an attacker-controlled scheme
    would be a stored-XSS vector on click.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw
    if not parsed.scheme and parsed.path:  # bare "inc42.com/..." → assume https
        return f"https://{raw}"
    return ""


def _deal_form_values(deal: FundingDeal | None = None) -> dict:
    """Form state: the submitted values on POST, otherwise the stored deal."""
    if request.method == "POST":
        return {k: (request.form.get(k) or "").strip() for k in (
            "company_name", "deal_date", "sector", "subsector", "business_model",
            "funding_round_size", "amount", "round_type", "investors_raw",
            "lead_investor", "source_url",
        )}
    return {
        "company_name": deal.company_name, "deal_date": deal.deal_date.isoformat(),
        "sector": deal.sector_canonical, "subsector": deal.subsector,
        "business_model": deal.business_model,
        "funding_round_size": deal.funding_round_size,
        "amount": f"{deal.amount_usd_mn:g}", "round_type": deal.round_type,
        "investors_raw": deal.investors_raw, "lead_investor": deal.lead_investor,
        "source_url": deal.source_url,
    }


def _validate_deal(values: dict) -> dict:
    """Return ``{field: message}`` for anything the form got wrong."""
    errors: dict[str, str] = {}
    if not values["company_name"]:
        errors["company_name"] = "Company name is required."
    if not values["deal_date"]:
        errors["deal_date"] = "Date is required."
    elif parse_date(values["deal_date"]) is None:
        errors["deal_date"] = "Use a date like 2026-04-15 or 15-Apr-26."
    for field, limit in _MAX_LENGTHS.items():
        if len(values.get(field, "")) > limit:
            errors[field] = f"Keep this under {limit} characters."
    if values["source_url"] and not _clean_url(values["source_url"]):
        errors["source_url"] = "Enter an http(s) link, or leave this blank."
    return errors


@bp.route("/deals/<int:deal_id>/edit", methods=["GET", "POST"])
def edit_deal(deal_id: int):
    org = require_org()
    deal = _get_deal(deal_id)
    values = _deal_form_values(deal)
    next_url = _safe_next()

    if request.method == "POST":
        errors = _validate_deal(values)
        if errors:
            return render_template("data/edit_deal.html", deal=deal, values=values,
                                   errors=errors, next_url=next_url, active="data"), 400

        d = parse_date(values["deal_date"])
        amount = parse_amount(values["amount"] or values["funding_round_size"])
        new_hash = FundingDeal.make_hash(values["company_name"], d, amount)
        if new_hash != deal.dedup_hash:
            clash = db.session.scalar(
                select(FundingDeal.id).where(
                    FundingDeal.organization_id == org.id,
                    FundingDeal.dedup_hash == new_hash, FundingDeal.id != deal.id,
                )
            )
            if clash:
                errors["company_name"] = (
                    "Another deal already has this company, date and amount.")
                return render_template("data/edit_deal.html", deal=deal, values=values,
                                       errors=errors, next_url=next_url, active="data"), 409

        deal.company_name = values["company_name"]
        deal.deal_date = d
        deal.amount_usd_mn = amount
        deal.sector_raw = values["sector"]
        deal.sector_canonical = normalize_sector(org.id, values["sector"])
        deal.subsector = values["subsector"]
        deal.business_model = values["business_model"]
        deal.funding_round_size = values["funding_round_size"]
        deal.round_type = values["round_type"]
        deal.investors_raw = values["investors_raw"]
        deal.lead_investor = values["lead_investor"]
        deal.source_url = _clean_url(values["source_url"])
        deal.dedup_hash = new_hash
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            errors["company_name"] = "Another deal already has this company, date and amount."
            return render_template("data/edit_deal.html", deal=deal, values=values,
                                   errors=errors, next_url=next_url, active="data"), 409
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Deal %s update failed", deal_id)
            flash("Could not save the deal. Please try again.", "error")
            return render_template("data/edit_deal.html", deal=deal, values=values,
                                   errors={}, next_url=next_url, active="data"), 500

        current_app.logger.info("Deal %s edited (%s)", deal_id, deal.company_name)
        sync_master_best_effort(org.id)
        flash(f"Updated {deal.company_name}.", "success")
        return redirect(next_url)

    return render_template("data/edit_deal.html", deal=deal, values=values,
                           errors={}, next_url=next_url, active="data")


@bp.route("/deals/<int:deal_id>/delete", methods=["POST"])
def delete_deal(deal_id: int):
    org = require_org()
    deal = _get_deal(deal_id)
    name = deal.company_name
    next_url = _safe_next()
    try:
        db.session.delete(deal)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Deal %s delete failed", deal_id)
        flash("Could not delete that deal. Please try again.", "error")
        return redirect(next_url)
    current_app.logger.info("Deal %s deleted (%s)", deal_id, name)
    sync_master_best_effort(org.id)
    flash(f"Deleted {name}.", "info")
    return redirect(next_url)


def _selected_ids() -> list[int]:
    """Parse the checkbox selection, ignoring anything non-numeric."""
    out = []
    for raw in request.form.getlist("ids"):
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out[:_BULK_LIMIT]


@bp.route("/deals/bulk-delete", methods=["POST"])
def bulk_delete_deals():
    org = require_org()
    ids = _selected_ids()
    next_url = _safe_next()
    if not ids:
        flash("Select at least one deal first.", "error")
        return redirect(next_url)
    try:
        n = db.session.query(FundingDeal).filter(
            FundingDeal.organization_id == org.id, FundingDeal.id.in_(ids)
        ).delete(synchronize_session=False)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Bulk delete failed for org %s", org.id)
        flash("Could not delete those deals. Please try again.", "error")
        return redirect(next_url)
    current_app.logger.info("Bulk-deleted %d deal(s) for org %s", n, org.id)
    sync_master_best_effort(org.id)
    flash(f"Deleted {n} deal{'' if n == 1 else 's'}.", "info")
    return redirect(next_url)


@bp.route("/deals/bulk-sector", methods=["POST"])
def bulk_reassign_sector():
    org = require_org()
    ids = _selected_ids()
    target = (request.form.get("sector") or "").strip()
    next_url = _safe_next()
    if not ids:
        flash("Select at least one deal first.", "error")
        return redirect(next_url)
    # Only ever write a name from the canonical taxonomy — a hand-crafted POST
    # must not be able to invent a sector and fragment the reporting rollups.
    if target not in sector_normalizer.CANONICAL_ALIASES:
        flash("Choose a sector from the list.", "error")
        return redirect(next_url)
    try:
        n = db.session.query(FundingDeal).filter(
            FundingDeal.organization_id == org.id, FundingDeal.id.in_(ids)
        ).update({"sector_canonical": target}, synchronize_session=False)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Bulk sector reassign failed for org %s", org.id)
        flash("Could not reassign those deals. Please try again.", "error")
        return redirect(next_url)
    current_app.logger.info("Reassigned %d deal(s) to %s for org %s", n, target, org.id)
    sync_master_best_effort(org.id)
    flash(f"Moved {n} deal{'' if n == 1 else 's'} to {target}.", "success")
    return redirect(next_url)


# ───────────────────────── API keys ─────────────────────────

@bp.route("/api-keys", methods=["POST"])
def create_api_key():
    org = require_org()
    name = (request.form.get("name") or "default").strip()[:120] or "default"
    rec, raw = ApiKey.issue(org.id, name=name)
    try:
        db.session.add(rec)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("API key creation failed for org %s", org.id)
        flash("Could not create the API key. Please try again.", "error")
        return redirect(url_for("data.index"))
    # The raw key is shown exactly once — only its hash is stored, so it cannot
    # be recovered later. This is the standard "copy it now" secret-issuance UX.
    flash(f"API key created — copy it now, it won't be shown again: {raw}", "success")
    return redirect(url_for("data.index"))


@bp.route("/api-keys/<int:key_id>/revoke", methods=["POST"])
def revoke_api_key(key_id: int):
    org = require_org()
    key = db.session.get(ApiKey, key_id)
    if key and key.organization_id == org.id:
        try:
            key.revoked = True
            db.session.commit()
            flash("API key revoked.", "info")
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("API key revoke failed for org %s", org.id)
            flash("Could not revoke the API key. Please try again.", "error")
    else:
        flash("API key not found.", "error")
    return redirect(url_for("data.index"))


# ───────────────────────── duplicate review ─────────────────────────

@bp.route("/dedup-review")
def dedup_review():
    org = require_org()
    try:
        candidates = find_candidates(org.id)
    except Exception:  # pragma: no cover - defensive
        current_app.logger.exception("Duplicate scan failed for org %s", org.id)
        flash("Could not scan for duplicates just now.", "error")
        candidates = []
    return render_template("data/dedup_review.html", candidates=candidates, active="data")


@bp.route("/dedup-review/dismiss", methods=["POST"])
def dedup_dismiss():
    org = require_org()
    hash_a = (request.form.get("hash_a") or "").strip()
    hash_b = (request.form.get("hash_b") or "").strip()
    if not (hash_a and hash_b) or len(hash_a) > 40 or len(hash_b) > 40 or hash_a == hash_b:
        flash("Invalid selection.", "error")
        return redirect(url_for("data.dedup_review"))
    db.session.add(DedupIgnore(organization_id=org.id, hash_a=hash_a, hash_b=hash_b))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # already dismissed — idempotent, nothing to do
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Dedup dismiss failed for org %s", org.id)
        flash("Could not save that. Please try again.", "error")
        return redirect(url_for("data.dedup_review"))
    flash("Marked as separate deals — this pair won't come back.", "info")
    return redirect(url_for("data.dedup_review"))


@bp.route("/dedup-review/merge", methods=["POST"])
def dedup_merge():
    """Delete one half of a near-duplicate pair, keeping the other."""
    org = require_org()
    keep_id, drop_id = request.form.get("keep_id", ""), request.form.get("drop_id", "")
    if not (keep_id.isdigit() and drop_id.isdigit()) or keep_id == drop_id:
        flash("Invalid selection.", "error")
        return redirect(url_for("data.dedup_review"))

    drop = db.session.get(FundingDeal, int(drop_id))
    keep = db.session.get(FundingDeal, int(keep_id))
    if not drop or not keep or drop.organization_id != org.id or keep.organization_id != org.id:
        flash("That pair no longer exists — it may already have been resolved.", "info")
        return redirect(url_for("data.dedup_review"))

    name = drop.company_name
    try:
        db.session.delete(drop)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Dedup merge failed for org %s", org.id)
        flash("Could not remove that duplicate. Please try again.", "error")
        return redirect(url_for("data.dedup_review"))
    current_app.logger.info("Dedup: removed deal %s (%s), kept %s", drop_id, name, keep_id)
    sync_master_best_effort(org.id)
    flash(f"Removed the duplicate {name} entry.", "info")
    return redirect(url_for("data.dedup_review"))
