"""Data integrity: workbook/database mirroring and near-duplicate review.

The Excel master is what "Import 3-year history" reads back, so if an in-app
deletion does not reach the workbook the deal is resurrected on the next
import. These tests pin that round-trip down.
"""
from __future__ import annotations

from datetime import date

import pytest

from intelligence.extensions import db
from intelligence.ingestion.dedup_review import find_candidates
from intelligence.ingestion.inc42 import rewrite_master_from_db
from intelligence.models import DedupIgnore, FundingDeal, Organization


def _add(org_id, company, day, amount, **kw):
    d = date(2026, 4, day)
    deal = FundingDeal(
        organization_id=org_id, company_name=company, sector_canonical="Fintech",
        amount_usd_mn=amount, deal_date=d,
        dedup_hash=FundingDeal.make_hash(company, d, amount), **kw,
    )
    db.session.add(deal)
    return deal


@pytest.fixture
def master_app(app, tmp_path, monkeypatch):
    """An app whose canonical workbook lives in a throwaway directory."""
    target = tmp_path / "Inc42_Funding_Master_Data.xlsx"
    monkeypatch.setattr(
        "intelligence.ingestion.inc42.canonical_master_path", lambda: target)
    return app, target


def _workbook_companies(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    names = {r[3] for r in rows if r[3]}  # "Name" is the 4th master column
    wb.close()
    return names


def test_master_rewrite_mirrors_the_database(master_app):
    app, target = master_app
    with app.app_context():
        org = db.session.query(Organization).first()
        _add(org.id, "Alpha Corp", 1, 100.0)
        _add(org.id, "Beta Labs", 2, 40.0)
        db.session.commit()
        assert rewrite_master_from_db(org.id) == 2
    assert _workbook_companies(target) == {"Alpha Corp", "Beta Labs"}


def test_deleting_a_deal_removes_it_from_the_workbook(client, master_app):
    """The regression that matters: a deleted deal must not come back."""
    app, target = master_app
    with app.app_context():
        org = db.session.query(Organization).first()
        _add(org.id, "Alpha Corp", 1, 100.0)
        doomed = _add(org.id, "Beta Labs", 2, 40.0)
        db.session.commit()
        rewrite_master_from_db(org.id)
        doomed_id = doomed.id
    assert "Beta Labs" in _workbook_companies(target)

    client.post(f"/data/deals/{doomed_id}/delete", follow_redirects=True)
    assert _workbook_companies(target) == {"Alpha Corp"}


def test_editing_a_deal_updates_the_workbook(client, master_app):
    app, target = master_app
    with app.app_context():
        org = db.session.query(Organization).first()
        deal = _add(org.id, "Alpha Corp", 1, 100.0)
        db.session.commit()
        rewrite_master_from_db(org.id)
        deal_id = deal.id

    client.post(f"/data/deals/{deal_id}/edit", data={
        "company_name": "Alpha Corporation", "deal_date": "2026-04-01", "amount": "100",
        "sector": "Fintech", "subsector": "", "business_model": "", "round_type": "",
        "funding_round_size": "", "investors_raw": "", "lead_investor": "", "source_url": "",
    }, follow_redirects=True)
    assert _workbook_companies(target) == {"Alpha Corporation"}


def test_rewrite_refuses_to_empty_the_workbook(master_app):
    """An empty database is far likelier to be broken than a real wipe."""
    app, target = master_app
    with app.app_context():
        org = db.session.query(Organization).first()
        _add(org.id, "Alpha Corp", 1, 100.0)
        db.session.commit()
        rewrite_master_from_db(org.id)

        db.session.query(FundingDeal).delete()
        db.session.commit()
        assert rewrite_master_from_db(org.id) == 0
    assert _workbook_companies(target) == {"Alpha Corp"}  # untouched


# ───────────────────────── near-duplicate review ─────────────────────────

def test_finds_a_near_duplicate_pair(app):
    with app.app_context():
        org = db.session.query(Organization).first()
        _add(org.id, "Riverline AI", 10, 0.825)
        _add(org.id, "Riverline AI", 11, 0.830)  # a day later, rounded amount
        db.session.commit()
        assert len(find_candidates(org.id)) == 1


def test_ignores_pairs_outside_the_windows(app):
    with app.app_context():
        org = db.session.query(Organization).first()
        _add(org.id, "Riverline AI", 1, 5.0)
        _add(org.id, "Riverline AI", 20, 5.0)     # same name, far apart in time
        _add(org.id, "Completely Other", 2, 5.0)  # same date, unrelated name
        db.session.commit()
        assert find_candidates(org.id) == []


def test_dismissing_a_pair_keeps_it_from_returning(client, app):
    with app.app_context():
        org = db.session.query(Organization).first()
        a = _add(org.id, "Riverline AI", 10, 0.825)
        b = _add(org.id, "Riverline AI", 11, 0.830)
        db.session.commit()
        hashes = {"hash_a": a.dedup_hash, "hash_b": b.dedup_hash}

    client.post("/data/dedup-review/dismiss", data=hashes, follow_redirects=True)
    with app.app_context():
        org = db.session.query(Organization).first()
        assert db.session.query(DedupIgnore).count() == 1
        assert find_candidates(org.id) == []


def test_merging_deletes_only_the_dropped_row(client, app):
    with app.app_context():
        org = db.session.query(Organization).first()
        keep = _add(org.id, "Riverline AI", 10, 0.825)
        drop = _add(org.id, "Riverline AI", 11, 0.830)
        db.session.commit()
        keep_id, drop_id = keep.id, drop.id

    client.post("/data/dedup-review/merge",
                data={"keep_id": str(keep_id), "drop_id": str(drop_id)},
                follow_redirects=True)
    with app.app_context():
        assert db.session.get(FundingDeal, drop_id) is None
        assert db.session.get(FundingDeal, keep_id) is not None


def test_merge_rejects_keeping_and_dropping_the_same_row(client, app):
    with app.app_context():
        org = db.session.query(Organization).first()
        deal = _add(org.id, "Riverline AI", 10, 0.825)
        db.session.commit()
        deal_id = deal.id

    client.post("/data/dedup-review/merge",
                data={"keep_id": str(deal_id), "drop_id": str(deal_id)},
                follow_redirects=True)
    with app.app_context():
        assert db.session.get(FundingDeal, deal_id) is not None
