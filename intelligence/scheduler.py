"""In-process scheduler (APScheduler).

Drives automated report generation. Each active ``ReportSchedule`` becomes a
cron job that generates the *current* period's report for its org. Kept simple
and in-process so the platform runs from a single ``python run.py`` with no
external broker; swap for Celery/RQ when horizontal scale is needed.
"""
from __future__ import annotations

from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler: BackgroundScheduler | None = None


def start_scheduler(app) -> None:
    global _scheduler
    # Avoid double-start under the Flask reloader's parent (watcher) process —
    # only relevant when the reloader is actually active; a plain (non-reload)
    # run has no parent/child split, so it must not be caught by this guard.
    import os
    if app.config.get("USE_RELOADER") and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        lambda: _run_due_schedules(app),
        CronTrigger.from_crontab("*/30 * * * *"),  # re-evaluate every 30 min
        id="dispatch",
        replace_existing=True,
    )
    _scheduler.start()
    app.logger.info("Scheduler started (dispatch every 30 min).")


def _run_due_schedules(app) -> None:  # pragma: no cover - time-driven
    """Generate the current-period report for every active schedule.

    APScheduler fires this dispatcher; per-schedule cron matching is delegated to
    croniter-style evaluation kept intentionally coarse. In practice
    each schedule would register its own cron job at creation time.
    """
    try:
        from croniter import croniter
    except Exception:
        app.logger.info("croniter not installed — schedule dispatch skipped. "
                        "`pip install croniter` to enable automated runs.")
        return
    with app.app_context():
        from datetime import datetime

        from .clock import utcnow
        from .engine import generate_report
        from .extensions import db
        from .models import ReportSchedule
        from sqlalchemy import select

        now = utcnow()
        for sch in db.session.scalars(select(ReportSchedule).where(ReportSchedule.is_active == True)):  # noqa: E712
            try:
                base = sch.last_run_at or sch.created_at
                if croniter(sch.cron, base).get_next(datetime) <= now:
                    generate_report(sch.organization_id, sch.period_type.value, anchor=date.today())
                    sch.last_run_at = now
                    db.session.commit()
                    app.logger.info("Scheduled report generated for org %s (%s).",
                                    sch.organization_id, sch.period_type.value)
            except Exception as exc:
                app.logger.warning("Schedule %s failed: %s", sch.id, exc)
