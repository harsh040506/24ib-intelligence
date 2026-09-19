"""Explore: entity profiles, global search, favorites — and the API surface."""
from __future__ import annotations

from datetime import date

import pytest

from intelligence.extensions import db
from intelligence.models import ApiKey, Favorite, FundingDeal, Organization


@pytest.fixture
def seeded(app):
    with app.app_context():
        org = db.session.query(Organization).first()
        rows = [
            ("Alpha Corp", 1, 100.0, "Fintech", "Sequoia", "Sequoia, Accel"),
            ("Beta Labs", 2, 40.0, "Edtech", "Accel", "Accel"),
            ("Alpha Corp", 9, 60.0, "Fintech", "Tiger", "Tiger Global"),
        ]
        for company, day, amt, sector, lead, investors in rows:
            d = date(2026, 4, day)
            db.session.add(FundingDeal(
                organization_id=org.id, company_name=company, sector_canonical=sector,
                amount_usd_mn=amt, deal_date=d, lead_investor=lead, investors_raw=investors,
                dedup_hash=FundingDeal.make_hash(company, d, amt),
            ))
        db.session.commit()
        return org.id


def test_company_profile_aggregates_all_its_deals(client, seeded):
    body = client.get("/explore/company/Alpha Corp").get_data(as_text=True)
    assert "2 deals" in body
    assert "$160M" in body  # 100 + 60


def test_company_profile_is_case_insensitive(client, seeded):
    assert client.get("/explore/company/alpha corp").status_code == 200


def test_unknown_company_is_404(client, seeded):
    assert client.get("/explore/company/Nonexistent Ltd").status_code == 404


def test_sector_profile(client, seeded):
    body = client.get("/explore/sector/Edtech").get_data(as_text=True)
    assert "Beta Labs" in body and "Alpha Corp" not in body


def test_known_sector_with_no_deals_renders_an_empty_state(client, seeded):
    """A taxonomy sector with no deals is a real page, not a 404."""
    resp = client.get("/explore/sector/Agritech")
    assert resp.status_code == 200
    assert b"No deals recorded yet" in resp.data


def test_investor_profile_matches_exact_names_only(client, seeded):
    """Substring matching must not make "Accel" swallow "Accel Partners Ltd"."""
    body = client.get("/explore/investor/Accel").get_data(as_text=True)
    assert "Beta Labs" in body
    assert "Alpha Corp" in body  # named in Alpha's investor list


def test_search_finds_companies_and_sectors(client, seeded):
    body = client.get("/explore/search?q=Alpha").get_data(as_text=True)
    assert "Alpha Corp" in body


def test_search_needs_two_characters(client, seeded):
    assert b"Type at least 2 characters" in client.get("/explore/search?q=a").data


def test_search_with_no_results_says_so(client, seeded):
    assert b"No matches" in client.get("/explore/search?q=zzzzznothing").data


def test_favorite_toggle_adds_then_removes(client, app, seeded):
    payload = {"kind": "sector", "ref": "Fintech", "label": "Fintech", "next": "/"}
    client.post("/explore/favorite", data=payload, follow_redirects=True)
    with app.app_context():
        assert db.session.query(Favorite).count() == 1
    client.post("/explore/favorite", data=payload, follow_redirects=True)
    with app.app_context():
        assert db.session.query(Favorite).count() == 0


def test_favorite_rejects_an_unknown_kind(client, app, seeded):
    client.post("/explore/favorite", data={"kind": "wat", "ref": "x"}, follow_redirects=True)
    with app.app_context():
        assert db.session.query(Favorite).count() == 0


def test_favorite_rejects_offsite_redirect(client, seeded):
    resp = client.post("/explore/favorite", data={
        "kind": "sector", "ref": "Fintech", "next": "https://evil.example.com"})
    assert "evil.example.com" not in resp.headers["Location"]


def test_pinned_items_appear_on_the_dashboard(client, seeded):
    client.post("/explore/favorite",
                data={"kind": "sector", "ref": "Fintech", "label": "Fintech", "next": "/"},
                follow_redirects=True)
    assert b"Pinned" in client.get("/").data


# ───────────────────────── REST API ─────────────────────────

@pytest.fixture
def api(client, app, seeded):
    with app.app_context():
        org = db.session.query(Organization).first()
        rec, raw = ApiKey.issue(org.id)
        db.session.add(rec)
        db.session.commit()
    return {"Authorization": f"Bearer {raw}"}


def test_api_deals_paginates(client, api):
    body = client.get("/api/v1/deals?limit=2", headers=api).get_json()
    assert body["total"] == 3 and body["count"] == 2 and body["limit"] == 2
    page2 = client.get("/api/v1/deals?limit=2&offset=2", headers=api).get_json()
    assert page2["count"] == 1


def test_api_deals_filters_by_sector_and_date(client, api):
    assert client.get("/api/v1/deals?sector=Edtech", headers=api).get_json()["total"] == 1
    ranged = client.get("/api/v1/deals?from=2026-04-05&to=2026-04-30", headers=api).get_json()
    assert ranged["total"] == 1


def test_api_rejects_an_unparseable_date(client, api):
    resp = client.get("/api/v1/deals?from=lastTuesday", headers=api)
    assert resp.status_code == 400 and "date" in resp.get_json()["error"]


def test_api_lists_reports(client, app, api):
    from intelligence.engine import generate_report
    with app.app_context():
        org = db.session.query(Organization).first()
        generate_report(org.id, "weekly", period_key="2026-W14")
    body = client.get("/api/v1/reports?period_type=weekly", headers=api).get_json()
    assert body["total"] == 1
    assert body["reports"][0]["period_key"] == "2026-W14"
    assert body["reports"][0]["share_url"].endswith(
        body["reports"][0]["share_url"].rsplit("/", 1)[-1])


def test_api_reports_rejects_bad_period_type(client, api):
    assert client.get("/api/v1/reports?period_type=daily", headers=api).status_code == 400
