"""Report library: search/filter, publish toggle, comparison, and notes."""
from __future__ import annotations

from datetime import date

import pytest

from intelligence.engine import generate_report
from intelligence.extensions import db
from intelligence.models import FundingDeal, Organization, Report


@pytest.fixture
def two_weeks(app):
    """Two consecutive weekly reports built from real deals."""
    with app.app_context():
        org = db.session.query(Organization).first()
        for day, company, amt in ((1, "Alpha Corp", 100.0), (8, "Beta Labs", 40.0)):
            d = date(2026, 4, day)
            db.session.add(FundingDeal(
                organization_id=org.id, company_name=company, sector_canonical="Fintech",
                amount_usd_mn=amt, deal_date=d,
                dedup_hash=FundingDeal.make_hash(company, d, amt),
            ))
        db.session.commit()
        a = generate_report(org.id, "weekly", period_key="2026-W14")
        b = generate_report(org.id, "weekly", period_key="2026-W15")
        return org.id, a.id, b.id


def test_index_search_matches_period_key(client, two_weeks):
    body = client.get("/reports/?q=2026-W14").get_data(as_text=True)
    assert "2026-W14" in body
    assert "2026-W15" not in body


def test_index_type_filter(client, two_weeks):
    assert client.get("/reports/?type=monthly").status_code == 200
    assert b"No reports match" in client.get("/reports/?type=annual").data


def test_weekly_reports_auto_publish_on_first_generation(app, two_weeks):
    _, a_id, _ = two_weeks
    with app.app_context():
        assert db.session.get(Report, a_id).is_published is True


def test_publish_toggle_round_trip(client, app, two_weeks):
    _, a_id, _ = two_weeks
    client.post(f"/reports/{a_id}/unpublish", follow_redirects=True)
    with app.app_context():
        assert db.session.get(Report, a_id).is_published is False
    client.post(f"/reports/{a_id}/publish", follow_redirects=True)
    with app.app_context():
        assert db.session.get(Report, a_id).is_published is True


def test_regenerate_does_not_resurrect_an_unpublished_report(client, app, two_weeks):
    """An explicit unpublish must survive a later regenerate."""
    org_id, a_id, _ = two_weeks
    client.post(f"/reports/{a_id}/unpublish", follow_redirects=True)
    client.post(f"/reports/{a_id}/regenerate", follow_redirects=True)
    with app.app_context():
        assert db.session.get(Report, a_id).is_published is False


def test_unpublished_reports_are_excluded_from_the_site(app, two_weeks, tmp_path):
    from intelligence.publish import rebuild_site

    org_id, a_id, _ = two_weeks
    with app.app_context():
        before = rebuild_site(org_id, site_dir=tmp_path / "site")["weekly"]
        db.session.get(Report, a_id).is_published = False
        db.session.commit()
        after = rebuild_site(org_id, site_dir=tmp_path / "site2")["weekly"]
    assert after == before - 1


def test_compare_view_renders_deltas(client, two_weeks):
    _, a_id, b_id = two_weeks
    resp = client.get(f"/reports/compare?a={a_id}&b={b_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Capital deployed" in body and "2026-W14" in body and "2026-W15" in body


def test_compare_picker_without_arguments(client, two_weeks):
    assert client.get("/reports/compare").status_code == 200


def test_compare_rejects_a_foreign_report_id(client, two_weeks):
    assert client.get("/reports/compare?a=999999&b=999998").status_code == 404


def test_notes_round_trip(client, app, two_weeks):
    _, a_id, _ = two_weeks
    client.post(f"/reports/{a_id}/notes", data={"notes": "Watch the fintech spike."},
                follow_redirects=True)
    with app.app_context():
        assert db.session.get(Report, a_id).notes == "Watch the fintech spike."


def test_notes_are_length_capped(client, app, two_weeks):
    _, a_id, _ = two_weeks
    client.post(f"/reports/{a_id}/notes", data={"notes": "x" * 50_000}, follow_redirects=True)
    with app.app_context():
        assert len(db.session.get(Report, a_id).notes) <= 20_000
