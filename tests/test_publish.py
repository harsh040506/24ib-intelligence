"""Static-site publishing: auto-publish on generation, neighbour repair, scope."""
from __future__ import annotations

from pathlib import Path

import pytest

from intelligence.extensions import db
from intelligence.ingestion.connectors import ingest_deals
from intelligence.models import Organization
from intelligence.seeds import bootstrap_org


@pytest.fixture
def org_with_deals(app):
    """An org holding deals in two adjacent, completed ISO weeks (W02 & W03 2025)."""
    with app.app_context():
        org = Organization(name="Pub Org", slug="pub-org")
        db.session.add(org)
        db.session.flush()
        bootstrap_org(org.id)
        rows = [
            {"company": "Alpha", "sector": "Fintech", "amount": "$10 Mn",
             "date": "2025-01-08", "investors": "Acme Capital"},
            {"company": "Beta", "sector": "Fintech", "amount": "$5 Mn",
             "date": "2025-01-15", "investors": "Beta Partners"},
        ]
        ingest_deals(org.id, rows, source="test", sync_excel=False)
        db.session.commit()
        return org.id


def _site(app) -> Path:
    return Path(app.config["PUBLISH_DIR"])


def test_generation_publishes_and_repairs_previous_pager(app, org_with_deals):
    """Generating the later week writes it *and* re-points the prior week's pager."""
    app.config["PUBLISH_ON_GENERATE"] = True
    from intelligence.engine.service import generate_report

    with app.app_context():
        generate_report(org_with_deals, "weekly", period_key="2025-W02")
        generate_report(org_with_deals, "weekly", period_key="2025-W03")

    site = _site(app)
    w02 = (site / "weekly" / "2025-W02.html").read_text(encoding="utf-8")
    w03 = (site / "weekly" / "2025-W03.html").read_text(encoding="utf-8")

    # The earlier week now links forward to the newer edition…
    assert '<a class="pg next" href="2025-W03.html">' in w02
    # …and the latest week's "Next" is disabled.
    assert '<span class="pg next disabled">' in w03
    assert '<a class="pg prev" href="2025-W02.html">' in w03
    # Archive index lists both editions.
    archive = (site / "weekly" / "index.html").read_text(encoding="utf-8")
    assert "2025-W03.html" in archive and "2025-W02.html" in archive


def test_quarterly_and_annual_are_never_published(app, org_with_deals):
    """The public site is weekly/monthly only — other cadences write nothing."""
    app.config["PUBLISH_ON_GENERATE"] = True
    from intelligence.engine.service import generate_report

    site = _site(app)
    with app.app_context():
        generate_report(org_with_deals, "quarterly", period_key="2025-Q1")
        generate_report(org_with_deals, "annual", period_key="2025")

    assert not (site / "quarterly").exists()
    assert not (site / "annual").exists()


def test_publish_disabled_writes_nothing(app, org_with_deals):
    """With the flag off, generation never touches the site directory."""
    app.config["PUBLISH_ON_GENERATE"] = False
    from intelligence.engine.service import generate_report

    with app.app_context():
        generate_report(org_with_deals, "weekly", period_key="2025-W02")

    assert not (_site(app) / "weekly" / "2025-W02.html").exists()


def test_publish_failure_never_breaks_generation(app, org_with_deals, monkeypatch):
    """A publishing error is swallowed; the report is still generated and READY."""
    app.config["PUBLISH_ON_GENERATE"] = True
    monkeypatch.setattr("intelligence.publish.publish_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    from intelligence.engine.service import generate_report
    from intelligence.models import ReportStatus

    with app.app_context():
        report = generate_report(org_with_deals, "weekly", period_key="2025-W02")
        assert report.status == ReportStatus.READY
