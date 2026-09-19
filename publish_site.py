"""Rebuild the public static site (``24IB-Private-Market-Research/``) from scratch.

This is the batch counterpart to the automatic per-report publishing that the
running app performs (see :mod:`intelligence.publish`). It boots the app, makes
sure every completed weekly/monthly period with deals has a stored report, then
writes the whole site — report pages, archive indexes, and the home page — in one
pass. The site is **weekly + monthly only** by design.

Usage::

    python publish_site.py                 # rebuild the live site in place
    python publish_site.py ./_preview      # rebuild into another directory (dry run)
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from config import Config


class _PubConfig(Config):
    # No scheduler for a batch job; and suppress the per-report auto-publish so we
    # generate everything first and write the site exactly once at the end.
    ENABLE_SCHEDULER = False
    PUBLISH_ON_GENERATE = False


from intelligence import create_app  # noqa: E402
from intelligence.engine.periods import resolve  # noqa: E402
from intelligence.engine.service import generate_report  # noqa: E402
from intelligence.extensions import db  # noqa: E402
from intelligence.models import FundingDeal, Organization  # noqa: E402
from intelligence.publish import rebuild_site  # noqa: E402
from intelligence.seeds import bootstrap_org  # noqa: E402


def _ensure_org_with_data(app) -> int:
    with app.app_context():
        org = (
            db.session.query(Organization)
            .join(FundingDeal, FundingDeal.organization_id == Organization.id)
            .first()
        )
        if org:
            return org.id
        from intelligence.ingestion.inc42 import backfill_history

        org = Organization(name="24IB Research", slug="24ib-research")
        db.session.add(org)
        db.session.flush()
        bootstrap_org(org.id)
        db.session.commit()
        backfill_history(org.id)
        if not db.session.query(FundingDeal).filter_by(organization_id=org.id).count():
            raise SystemExit("No deals available — is the master workbook present?")
        return org.id


def _has_deals(org_id: int, start: date, end: date) -> bool:
    return bool(
        db.session.query(FundingDeal.id)
        .filter(
            FundingDeal.organization_id == org_id,
            FundingDeal.deal_date >= start,
            FundingDeal.deal_date <= end,
        )
        .first()
    )


def _generate_completed(app, org_id: int, period_type: str, today: date) -> int:
    """Generate every completed period with deals; return how many were stored."""
    with app.app_context():
        bounds = db.session.query(
            db.func.min(FundingDeal.deal_date)
        ).filter(FundingDeal.organization_id == org_id).scalar()
        if not bounds:
            return 0
        first = date.fromisoformat(bounds) if isinstance(bounds, str) else bounds
        n = 0
        anchor = first
        while True:
            p = resolve(period_type, anchor)
            if p.headline_end >= today:  # only fully-completed periods
                break
            if _has_deals(org_id, p.headline_start, p.headline_end):
                generate_report(org_id, period_type, period_key=p.period_key)
                n += 1
            anchor = p.headline_end + timedelta(days=1)
        return n


def main(out_dir: Path) -> None:
    app = create_app(_PubConfig)
    org_id = _ensure_org_with_data(app)
    today = date.today()

    for period_type in ("weekly", "monthly"):
        made = _generate_completed(app, org_id, period_type, today)
        print(f"Ensured {made} {period_type} reports.")

    with app.app_context():
        counts = rebuild_site(org_id, site_dir=out_dir)

        # Only reports flagged is_published reach the site. If nothing is
        # publishable but READY reports exist, the run would silently produce an
        # empty site — say so, and say how to fix it, rather than reporting "0".
        if not sum(counts.values()):
            from intelligence.models import Report, ReportStatus

            ready = (
                db.session.query(Report)
                .filter(
                    Report.organization_id == org_id,
                    Report.status == ReportStatus.READY,
                    Report.period_type.in_(("weekly", "monthly")),
                )
                .count()
            )
            if ready:
                print(
                    f"\n  WARNING: {ready} ready weekly/monthly report(s) exist but none are\n"
                    "  marked published, so nothing was written. Publish them from the\n"
                    "  report page, or run:\n"
                    "    UPDATE reports SET is_published=1\n"
                    "     WHERE status='READY' AND period_type IN ('WEEKLY','MONTHLY');"
                )

    print(f"Published site to {out_dir}")
    for section, n in counts.items():
        print(f"  {section}: {n} reports")


if __name__ == "__main__":
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    app_default = Path(__file__).resolve().parent / "24IB-Private-Market-Research"
    main(target or app_default)
