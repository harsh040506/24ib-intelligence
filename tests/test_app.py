"""Integration tests: HTTP surface, headers, workspace bootstrap, and the XSS fix."""
from __future__ import annotations

from datetime import date


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "Content-Security-Policy" in resp.headers


def test_dashboard_loads_with_no_login(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_workspace_auto_created_on_boot(app):
    from intelligence.models import Organization
    with app.app_context():
        assert Organization.query.count() == 1


def test_api_requires_key(client):
    resp = client.get("/api/v1/deals")
    assert resp.status_code == 401
    assert resp.get_json()["error"]


def test_api_unknown_period_is_400(client, app):
    # Mint a key directly so we can exercise the API.
    from intelligence.extensions import db
    from intelligence.models import ApiKey, Organization
    with app.app_context():
        org = db.session.query(Organization).first()
        rec, raw = ApiKey.issue(org.id)
        db.session.add(rec)
        db.session.commit()
    resp = client.get("/api/v1/reports/daily/2026-W14",
                      headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 400


def test_report_share_escapes_untrusted_data(client, app):
    """A malicious company name must not break out into executable markup."""
    from intelligence.engine import generate_report
    from intelligence.extensions import db
    from intelligence.models import FundingDeal, Organization, Report

    payload_attack = "<script>alert(1)</script>"
    with app.app_context():
        org = db.session.query(Organization).first()
        db.session.add(FundingDeal(
            organization_id=org.id, company_name=payload_attack,
            sector_canonical="Fintech", amount_usd_mn=10.0,
            deal_date=date(2026, 4, 1),
            dedup_hash=FundingDeal.make_hash(payload_attack, date(2026, 4, 1), 10.0),
        ))
        db.session.commit()
        report = generate_report(org.id, "weekly", period_key="2026-W14")
        token = db.session.get(Report, report.id).share_token

    resp = client.get(f"/reports/share/{token}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The raw <script> must never appear verbatim in the document payload — it is
    # JSON-unicode-escaped at render time, and the client-side esc() helper ships.
    assert "<script>alert(1)</script>" not in body
    assert "function esc(" in body
