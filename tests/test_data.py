"""The data workspace: filtering, sorting, export, editing, and bulk actions."""
from __future__ import annotations

from datetime import date

import pytest

from intelligence.extensions import db
from intelligence.models import FundingDeal, Organization


def _add_deal(org_id, company, day, amount=10.0, sector="Fintech", **kw):
    d = date(2026, 4, day)
    deal = FundingDeal(
        organization_id=org_id, company_name=company, sector_canonical=sector,
        amount_usd_mn=amount, deal_date=d,
        dedup_hash=FundingDeal.make_hash(company, d, amount), **kw,
    )
    db.session.add(deal)
    return deal


@pytest.fixture
def seeded(app):
    """Three deals with distinct companies, sectors and amounts."""
    with app.app_context():
        org = db.session.query(Organization).first()
        _add_deal(org.id, "Alpha Corp", 1, 100.0, "Fintech", lead_investor="Sequoia")
        _add_deal(org.id, "Beta Labs", 2, 5.0, "Edtech", investors_raw="Accel, Nexus")
        _add_deal(org.id, "Gamma AI", 3, 50.0, "Artificial Intelligence")
        db.session.commit()
        return org.id


def test_search_filters_by_company(client, seeded):
    resp = client.get("/data/?q=Beta")
    body = resp.get_data(as_text=True)
    assert "Beta Labs" in body
    assert "Alpha Corp" not in body


def test_search_matches_investors(client, seeded):
    body = client.get("/data/?q=Accel").get_data(as_text=True)
    assert "Beta Labs" in body


def test_sector_filter(client, seeded):
    body = client.get("/data/?sector=Edtech").get_data(as_text=True)
    assert "Beta Labs" in body
    assert "Gamma AI" not in body


def test_no_match_shows_empty_state(client, seeded):
    body = client.get("/data/?q=nothingmatchesthis").get_data(as_text=True)
    assert "No deals match those filters" in body


def test_sort_by_amount_ascending(client, seeded):
    body = client.get("/data/?sort=amount&dir=asc").get_data(as_text=True)
    assert body.index("Beta Labs") < body.index("Alpha Corp")  # 5.0 before 100.0


def test_unknown_sort_key_falls_back_safely(client, seeded):
    """A hand-edited sort key must not 500 or reach the query."""
    assert client.get("/data/?sort=%27%3B+DROP+TABLE+funding_deals--").status_code == 200


def test_csv_export_respects_filters(client, seeded):
    resp = client.get("/data/export.csv?sector=Edtech")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    assert "attachment" in resp.headers["Content-Disposition"]
    body = resp.get_data(as_text=True)
    assert "Beta Labs" in body and "Alpha Corp" not in body


def test_edit_updates_fields_and_hash(client, app, seeded):
    with app.app_context():
        deal = db.session.query(FundingDeal).filter_by(company_name="Alpha Corp").one()
        deal_id, old_hash = deal.id, deal.dedup_hash

    resp = client.post(f"/data/deals/{deal_id}/edit", data={
        "company_name": "Alpha Corporation", "deal_date": "2026-04-01", "amount": "120",
        "sector": "Fintech", "subsector": "", "business_model": "", "round_type": "Series B",
        "funding_round_size": "$120 Mn", "investors_raw": "", "lead_investor": "",
        "source_url": "",
    }, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        deal = db.session.get(FundingDeal, deal_id)
        assert deal.company_name == "Alpha Corporation"
        assert deal.amount_usd_mn == 120.0
        assert deal.round_type == "Series B"
        assert deal.dedup_hash != old_hash  # recomputed from the new values


def test_edit_rejects_missing_required_fields(client, app, seeded):
    with app.app_context():
        deal_id = db.session.query(FundingDeal.id).first()[0]
    resp = client.post(f"/data/deals/{deal_id}/edit", data={
        "company_name": "", "deal_date": "", "amount": "1", "sector": "Fintech",
    })
    assert resp.status_code == 400
    assert b"Company name is required" in resp.data


def test_edit_rejects_duplicate_identity(client, app, seeded):
    """Editing one deal onto another's (company, date, amount) must be refused."""
    with app.app_context():
        target = db.session.query(FundingDeal).filter_by(company_name="Beta Labs").one()
        target_id = target.id

    resp = client.post(f"/data/deals/{target_id}/edit", data={
        "company_name": "Alpha Corp", "deal_date": "2026-04-01", "amount": "100",
        "sector": "Fintech", "subsector": "", "business_model": "", "round_type": "",
        "funding_round_size": "", "investors_raw": "", "lead_investor": "", "source_url": "",
    })
    assert resp.status_code == 409
    with app.app_context():
        assert db.session.get(FundingDeal, target_id).company_name == "Beta Labs"


def test_edit_strips_dangerous_url_scheme(client, app, seeded):
    with app.app_context():
        deal_id = db.session.query(FundingDeal.id).first()[0]
    resp = client.post(f"/data/deals/{deal_id}/edit", data={
        "company_name": "Alpha Corp", "deal_date": "2026-04-01", "amount": "100",
        "sector": "Fintech", "subsector": "", "business_model": "", "round_type": "",
        "funding_round_size": "", "investors_raw": "", "lead_investor": "",
        "source_url": "javascript:alert(1)",
    })
    assert resp.status_code == 400
    assert b"http(s) link" in resp.data


def test_delete_removes_the_deal(client, app, seeded):
    with app.app_context():
        deal_id = db.session.query(FundingDeal).filter_by(company_name="Gamma AI").one().id
        before = db.session.query(FundingDeal).count()
    client.post(f"/data/deals/{deal_id}/delete", follow_redirects=True)
    with app.app_context():
        assert db.session.query(FundingDeal).count() == before - 1
        assert db.session.get(FundingDeal, deal_id) is None


def test_delete_rejects_offsite_next_redirect(client, app, seeded):
    """The ``next`` round-trip must never become an open redirect."""
    with app.app_context():
        deal_id = db.session.query(FundingDeal.id).first()[0]
    resp = client.post(f"/data/deals/{deal_id}/delete",
                       data={"next": "https://evil.example.com/pwn"})
    assert resp.status_code == 302
    assert "evil.example.com" not in resp.headers["Location"]


def test_bulk_delete(client, app, seeded):
    with app.app_context():
        ids = [row[0] for row in db.session.query(FundingDeal.id).limit(2)]
    client.post("/data/deals/bulk-delete", data={"ids": [str(i) for i in ids]},
                follow_redirects=True)
    with app.app_context():
        assert db.session.query(FundingDeal).count() == 1


def test_bulk_delete_with_no_selection_is_a_no_op(client, app, seeded):
    resp = client.post("/data/deals/bulk-delete", data={}, follow_redirects=True)
    assert b"Select at least one deal" in resp.data
    with app.app_context():
        assert db.session.query(FundingDeal).count() == 3


def test_bulk_sector_reassign(client, app, seeded):
    with app.app_context():
        ids = [row[0] for row in db.session.query(FundingDeal.id).all()]
    client.post("/data/deals/bulk-sector",
                data={"ids": [str(i) for i in ids], "sector": "Deeptech"},
                follow_redirects=True)
    with app.app_context():
        sectors = {d.sector_canonical for d in db.session.query(FundingDeal).all()}
        assert sectors == {"Deeptech"}


def test_bulk_sector_rejects_non_canonical_value(client, app, seeded):
    """An invented sector would fragment every rollup — refuse it."""
    with app.app_context():
        ids = [row[0] for row in db.session.query(FundingDeal.id).all()]
    resp = client.post("/data/deals/bulk-sector",
                       data={"ids": [str(i) for i in ids], "sector": "Not A Real Sector"},
                       follow_redirects=True)
    assert b"Choose a sector from the list" in resp.data
    with app.app_context():
        assert db.session.query(FundingDeal).filter_by(
            sector_canonical="Not A Real Sector").count() == 0
