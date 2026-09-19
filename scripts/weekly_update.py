"""Unattended weekly update — the entry point GitHub Actions runs on a cron.

This is the automation counterpart to the buttons on the Data page. One run:

1. **Scrapes** the most recent Inc42 "Funding Galore" editions and inserts any
   deals the database does not already have.
2. **Generates** the most recent *completed* weekly and monthly reports. Because
   ``PUBLISH_ON_GENERATE`` is on, each generation writes its page into the public
   site, repairs the neighbouring pager links, and refreshes the archive and home
   pages — exactly the same code path the app uses interactively, so the published
   markup is identical to what the app would produce.
3. **Mirrors** the database back into the canonical Excel master so the workbook
   never drifts from the data.

Nothing here renders HTML itself; it only drives the existing engine. Re-running
it is safe — generation is idempotent per (period, key), and a report that was
explicitly unpublished in the app is never silently republished.

Exit code is 0 only if every step succeeded. A non-zero exit makes the GitHub
Actions run go red, which is what triggers the failure email — so a scrape that
silently stops finding deals surfaces as a broken build instead of a site that
quietly stops updating.

Usage::

    python scripts/weekly_update.py                  # what the cron runs
    python scripts/weekly_update.py --skip-refresh   # rebuild reports, no scrape
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

# Allow `python scripts/weekly_update.py` from anywhere: the repository root
# (which holds config.py and the intelligence package) must be importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config import Config  # noqa: E402


class JobConfig(Config):
    """Config for a one-shot batch run."""

    # GitHub Actions' cron replaces the in-process scheduler; starting
    # APScheduler in a process that exits seconds later is pure overhead.
    ENABLE_SCHEDULER = False
    # Left on deliberately: generating a report is what publishes its page.
    PUBLISH_ON_GENERATE = True


def log(message: str) -> None:
    """Print immediately — Actions logs are line-buffered pipes, not a tty."""
    print(message, flush=True)


def completed_periods(period_type: str, count: int, today: date) -> list:
    """The ``count`` most recent *finished* periods, oldest first.

    The in-progress period is skipped: a report for a week that has not ended
    yet would be published and then immediately be wrong. Oldest-first ordering
    matches the full-rebuild path, so each publish sees its older neighbour
    already on disk when it repairs pager links.
    """
    from intelligence.engine.periods import resolve

    found = []
    anchor = today
    # Bounded so a bad clock or an unknown period type can never spin forever.
    for _ in range(count * 4 + 8):
        if len(found) >= count:
            break
        period = resolve(period_type, anchor)
        if period.headline_end < today:
            found.append(period)
        anchor = period.headline_start - timedelta(days=1)
    return list(reversed(found))


def period_has_deals(org_id: int, start: date, end: date) -> bool:
    """Whether any deal falls inside the period.

    Empty periods are skipped rather than generated: the publisher refuses to
    write a page with no deals, so generating one would only add a dead report
    row and a confusing log line.
    """
    from intelligence.extensions import db
    from intelligence.models import FundingDeal

    return bool(
        db.session.query(FundingDeal.id)
        .filter(
            FundingDeal.organization_id == org_id,
            FundingDeal.deal_date >= start,
            FundingDeal.deal_date <= end,
        )
        .first()
    )


def run_refresh(org_id: int, weeks: int) -> bool:
    """Scrape the latest Inc42 editions. Returns True on success."""
    from intelligence.ingestion.inc42 import refresh_latest
    from intelligence.models import RunStatus

    log(f"\n== Scraping the {weeks} most recent Inc42 editions ==")
    run = refresh_latest(org_id, weeks=weeks)
    for line in (run.log or "").splitlines():
        log(f"   {line}")

    if run.status == RunStatus.FAILED:
        log(f"   FAILED: {run.detail}")
        return False
    log(f"   OK: {run.detail}")
    return True


def generation_reason(org_id: int, period_type: str, period) -> str | None:
    """Why this period needs rebuilding, or ``None`` if it is already current.

    Every published page carries a render-time "last updated" stamp, so
    re-rendering a report whose data has not moved would restamp an otherwise
    unchanged page and show up as a spurious change every week. A period is
    rebuilt only when it has no usable report yet, or when a deal landed in it
    after that report was generated.
    """
    from intelligence.extensions import db
    from intelligence.models import FundingDeal, PeriodType, Report, ReportStatus

    report = db.session.query(Report).filter(
        Report.organization_id == org_id,
        Report.period_type == PeriodType(period_type),
        Report.period_key == period.period_key,
    ).first()

    if report is None or report.status != ReportStatus.READY or not report.payload:
        return "no report yet"
    if report.generated_at is None:
        return "report has no generation timestamp"

    newest = db.session.query(db.func.max(FundingDeal.created_at)).filter(
        FundingDeal.organization_id == org_id,
        FundingDeal.deal_date >= period.headline_start,
        FundingDeal.deal_date <= period.headline_end,
    ).scalar()
    if newest and newest > report.generated_at:
        return "new deal data since last build"
    return None


def run_generation(org_id: int, period_type: str, count: int, today: date,
                   force: bool = False) -> int:
    """Generate + publish the recent completed periods. Returns how many ran."""
    from intelligence.engine.service import generate_report

    log(f"\n== Generating the last {count} completed {period_type} report(s) ==")
    made = 0
    for period in completed_periods(period_type, count, today):
        if not period_has_deals(org_id, period.headline_start, period.headline_end):
            log(f"   skip {period.period_key} — no deals in period")
            continue
        reason = "forced" if force else generation_reason(org_id, period_type, period)
        if reason is None:
            log(f"   skip {period.period_key} — already up to date")
            continue
        report = generate_report(org_id, period_type, period_key=period.period_key)
        stats = (report.payload or {}).get("stats", {})
        log(f"   {period.period_key}: {stats.get('total_deals', 0)} deals, "
            f"{stats.get('total_capital_label', '—')} [{report.status.value}] ({reason})")
        made += 1
    if not made:
        log("   nothing to rebuild — every recent period is already current")
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly unattended data + report update.")
    parser.add_argument("--weeks", type=int, default=6,
                        help="How many recent Inc42 editions to scrape (default: 6). "
                             "More than one so a missed run self-heals next time.")
    parser.add_argument("--weekly-reports", type=int, default=4,
                        help="How many recent completed weeks to (re)generate (default: 4). "
                             "Covers deals Inc42 adds to an already-published week.")
    parser.add_argument("--monthly-reports", type=int, default=2,
                        help="How many recent completed months to (re)generate (default: 2).")
    parser.add_argument("--skip-refresh", action="store_true",
                        help="Do not scrape; only regenerate and publish from existing data.")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild the recent periods even if their data has not changed.")
    args = parser.parse_args(argv)

    from intelligence import create_app

    app = create_app(JobConfig)
    today = date.today()
    log(f"24IB weekly update — {today.isoformat()}")
    log(f"Publishing into: {app.config['PUBLISH_DIR']}")

    failures: list[str] = []

    with app.app_context():
        from intelligence.extensions import db
        from intelligence.models import FundingDeal, Organization

        org = db.session.query(Organization).first()
        if org is None:
            log("ERROR: no workspace exists and one could not be created.")
            return 1

        # A fresh clone may have an empty database; rehydrate from the Excel
        # master before doing anything that assumes history is present.
        from intelligence.ingestion.inc42 import ensure_org_has_data
        ensure_org_has_data(org.id)

        before = db.session.query(FundingDeal.id).filter_by(organization_id=org.id).count()
        log(f"Deals in database before this run: {before:,}")

        if args.skip_refresh:
            log("\n== Scrape skipped (--skip-refresh) ==")
        elif not run_refresh(org.id, args.weeks):
            # Keep going: the previous week's report may still be generatable
            # from data already stored, and a consistent site is better than a
            # half-updated one. The non-zero exit at the end still raises the alarm.
            failures.append("Inc42 scrape failed")

        after = db.session.query(FundingDeal.id).filter_by(organization_id=org.id).count()
        log(f"Deals in database after scrape:  {after:,}  (+{after - before})")

        for period_type, count in (("weekly", args.weekly_reports),
                                   ("monthly", args.monthly_reports)):
            try:
                run_generation(org.id, period_type, count, today, force=args.force)
            except Exception:
                traceback.print_exc()
                failures.append(f"{period_type} report generation failed")

        # Mirror the database into the workbook last, so it reflects everything
        # this run ingested. Best-effort by design — see sync_master_best_effort.
        log("\n== Syncing the Excel master from the database ==")
        try:
            from intelligence.ingestion.inc42 import rewrite_master_from_db
            rows = rewrite_master_from_db(org.id)
            log(f"   wrote {rows:,} rows to the master workbook")
        except Exception:
            traceback.print_exc()
            failures.append("Excel master sync failed")

    if failures:
        log("\nFINISHED WITH ERRORS:")
        for item in failures:
            log(f"  - {item}")
        return 1

    log("\nAll steps completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
