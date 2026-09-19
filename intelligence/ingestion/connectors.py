"""Ingestion connectors.

A connector turns an external source into normalised ``FundingDeal`` rows. They
all funnel through ``ingest_deals`` which handles normalisation + dedup + the
``JobRun`` audit record, so adding a new source (an API, a scraper) only means
producing a list of raw dicts.

Shipped connectors:
* ``ingest_csv`` — upload a CSV/spreadsheet export (works for anyone, no creds).
* ``ingestion.inc42`` — the live web source: history backfill + weekly refresh.
"""
from __future__ import annotations

import csv
import io

from ..clock import utcnow
from ..extensions import db
from ..models import FundingDeal, JobRun, RunStatus
from .normalize import normalize_sector, parse_amount, parse_date

# Accepted CSV headers (case-insensitive) → internal field.
_FIELD_ALIASES = {
    "company": "company", "name": "company", "startup": "company",
    "sector": "sector", "industry": "sector",
    "subsector": "subsector", "sub-sector": "subsector", "sub sector": "subsector",
    "business model": "business_model", "business_model": "business_model", "model": "business_model",
    "funding round size": "funding_round_size", "round size": "funding_round_size",
    "amount": "amount", "amount (usd mn)": "amount", "funding": "amount", "size": "amount", "amount_usd_mn": "amount",
    "date": "date", "deal date": "date",
    "round": "round", "round type": "round", "stage": "round",
    "investors": "investors", "investor": "investors",
    "lead": "lead", "lead investor": "lead",
    "url": "url", "source": "url", "source url": "url",
}


def ingest_deals(org_id: int, rows: list[dict], *, source: str = "manual",
                 sync_excel: bool = True) -> JobRun:
    """Normalise + dedup-insert a batch of raw deal dicts. Returns the JobRun.

    When ``sync_excel`` is true the newly-inserted deals are also appended to the
    canonical Excel master so the workbook always mirrors the database. (Backfill
    sets this false because its rows came *from* the workbook.)
    """
    run = JobRun(organization_id=org_id, kind=f"ingest_{source}", status=RunStatus.RUNNING)
    db.session.add(run)
    inserted = skipped = enriched = invalid = 0
    new_deals: list[FundingDeal] = []
    run.append_log(f"Ingesting {len(rows)} candidate rows from {source}…")

    try:
        for row_no, raw in enumerate(rows, start=1):
            company = (raw.get("company") or "").strip()
            d = parse_date(raw.get("date"))
            if not company or not d:
                skipped += 1
                invalid += 1
                if invalid <= 20:  # bounded — a bad bulk import shouldn't flood the log
                    reason = "missing company name" if not company else f"unparseable date {raw.get('date')!r}"
                    snippet = company or str(raw.get("date") or "")[:60]
                    run.append_log(f"  row {row_no}: skipped ({reason}) — {snippet!r}")
                elif invalid == 21:
                    run.append_log("  … further invalid rows not logged individually")
                continue
            round_size_raw = (str(raw.get("funding_round_size") or "")).strip()
            # Prefer an explicit amount; fall back to parsing the raw round-size string.
            amount = parse_amount(raw.get("amount"))
            if not amount and round_size_raw:
                amount = parse_amount(round_size_raw)
            sector_raw = (raw.get("sector") or "").strip()
            canonical = normalize_sector(org_id, sector_raw)
            dedup = FundingDeal.make_hash(company, d, amount)

            existing = db.session.query(FundingDeal).filter_by(
                organization_id=org_id, dedup_hash=dedup).first()
            if existing:
                # Back-fill columns added after this row was first ingested, without
                # clobbering values that are already present.
                changed = False
                for attr, val in (
                    ("subsector", (raw.get("subsector") or "").strip()),
                    ("business_model", (raw.get("business_model") or "").strip()),
                    ("funding_round_size", round_size_raw),
                ):
                    if val and not getattr(existing, attr, ""):
                        setattr(existing, attr, val)
                        changed = True
                if changed:
                    enriched += 1
                else:
                    skipped += 1
                continue

            deal = FundingDeal(
                organization_id=org_id,
                company_name=company,
                sector_canonical=canonical,
                sector_raw=sector_raw,
                subsector=(raw.get("subsector") or "").strip(),
                business_model=(raw.get("business_model") or "").strip(),
                funding_round_size=round_size_raw,
                amount_usd_mn=amount,
                round_type=(raw.get("round") or "").strip(),
                deal_date=d,
                investors_raw=(raw.get("investors") or "").strip(),
                lead_investor=(raw.get("lead") or "").strip(),
                source_url=(raw.get("url") or "").strip(),
                dedup_hash=dedup,
            )
            db.session.add(deal)
            new_deals.append(deal)
            inserted += 1

        run.detail = (f"{inserted} inserted, {enriched} enriched, "
                      f"{skipped} skipped ({invalid} invalid, {skipped - invalid} duplicate).")
        run.append_log(run.detail)
        run.status = RunStatus.SUCCESS
    except Exception as exc:  # pragma: no cover
        run.status = RunStatus.FAILED
        run.append_log(f"ERROR: {exc}")
        db.session.rollback()
        db.session.add(run)
        raise
    finally:
        run.finished_at = utcnow()
        db.session.commit()

    if sync_excel and new_deals:
        try:
            from .inc42 import append_deals_to_master
            n = append_deals_to_master(new_deals)
            if n:
                run.append_log(f"Synced {n} new deal(s) into the Excel master.")
                db.session.commit()
        except Exception as exc:  # pragma: no cover
            run.append_log(f"Excel sync skipped: {exc}")
            db.session.commit()
    return run


def ingest_csv(org_id: int, file_storage) -> JobRun:
    """Parse an uploaded CSV (Werkzeug FileStorage) into deal rows and ingest."""
    raw_bytes = file_storage.read()
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        mapped = {}
        for k, v in r.items():
            if k is None:
                continue
            field = _FIELD_ALIASES.get(k.strip().lower())
            if field:
                mapped[field] = v
        rows.append(mapped)
    return ingest_deals(org_id, rows, source="csv")
