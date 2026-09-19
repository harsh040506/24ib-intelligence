"""Inc42 funding-data ingestion + the canonical Excel master store.

The Excel master (``Inc42_Funding_Master_Data.xlsx``) lives inside the app's
``instance/`` folder and is the durable mirror of the database:

* **First run** — if a workspace has no deals, the full history is imported
  automatically from the master (seeded from the file shipped with the project),
  so you never have to click "import 3-year history" yourself.
* **Every addition** — deals added later (live refresh, CSV upload) are appended
  back into the master, so the Excel always matches the database.

Live scraping mirrors the reference ``scraper.py``: article-URL discovery needs a
browser (the tag page uses a JS "Load More") via Playwright, then each article's
table is parsed with requests + BeautifulSoup. The live path degrades
gracefully — failures are recorded on the ``JobRun`` rather than crashing.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from flask import current_app

from ..clock import utcnow
from ..extensions import db
from ..models import FundingDeal, JobRun, RunStatus
from .connectors import ingest_deals

INC42_FUNDING_URL = "https://inc42.com/tag/funding-galore/"
MASTER_FILENAME = "Inc42_Funding_Master_Data.xlsx"
MASTER_SHEET = "Funding Master Data"
MASTER_COLUMNS = [
    "Week No", "Year", "Date", "Name", "Sector", "Subsector", "Business Model",
    "Funding Round Size", "Amount (USD Mn)", "Round Type", "Investors",
    "Lead Investor", "Source URL",
]

_HEADER_HINTS = ("date", "week", "sl", "s.no", "sr.no", "no.")
_SKIP_MARKERS = ("source:", "note:", "only disclosed", "*including", "**mix")


# ───────────────────────── canonical master location ─────────────────────────

def canonical_master_path() -> Path:
    """The master workbook's home inside the app instance folder."""
    return Path(current_app.instance_path) / MASTER_FILENAME


def _seed_master_candidates() -> list[Path]:
    """Ordered locations to search for an initial copy of the master workbook.

    Precedence: an explicit ``INC42_MASTER_PATH`` override wins, then the
    application root, then its parent directory. This lets the shipped workbook
    be picked up from beside or one level above the app without configuration,
    while still allowing operators to point at an arbitrary path in production.
    """
    configured = current_app.config.get("INC42_MASTER_PATH")
    base = Path(current_app.root_path).parent  # repository root (parent of the package)
    out = []
    if configured:
        out.append(Path(configured))
    out += [base / MASTER_FILENAME, base.parent / MASTER_FILENAME]
    return out


def master_excel_path() -> Path | None:
    """Return the canonical master path, seeding it from a shipped copy if needed.

    The canonical home is the instance folder (the writable, durable mirror of
    the database). On first use we copy a read-only shipped/seed workbook into
    that location once, then always operate on the instance copy. Returns
    ``None`` when no seed can be found, which callers treat as "history import
    unavailable" rather than an error.
    """
    canonical = canonical_master_path()
    if canonical.exists():
        return canonical
    for cand in _seed_master_candidates():
        if cand and cand.exists():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cand, canonical)
            current_app.logger.info("Seeded Inc42 master into instance: %s", canonical)
            return canonical
    return None


# ───────────────────────── read (backfill) ─────────────────────────

def _rows_from_master(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[MASTER_SHEET] if MASTER_SHEET in wb.sheetnames else wb.active
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = {h: i for i, h in enumerate(headers)}

    def g(row, col):
        i = idx.get(col)
        return row[i] if i is not None and i < len(row) else None

    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        name = g(r, "Name")
        if not name:
            continue
        round_size = g(r, "Funding Round Size")
        amount = g(r, "Amount (USD Mn)")
        if amount in (None, ""):
            amount = round_size
        rows.append({
            "company": name, "sector": g(r, "Sector") or "",
            "subsector": g(r, "Subsector") or "",
            "business_model": g(r, "Business Model") or "",
            "funding_round_size": g(r, "Funding Round Size") or "",
            "amount": amount,
            "date": g(r, "Date"), "round": g(r, "Round Type") or "",
            "investors": g(r, "Investors") or "", "lead": g(r, "Lead Investor") or "",
            "url": g(r, "Source URL") or "",
        })
    wb.close()
    return rows


def backfill_history(org_id: int) -> JobRun:
    """Import the full Inc42 history from the master workbook (no Excel re-sync)."""
    path = master_excel_path()
    if not path:
        run = JobRun(organization_id=org_id, kind="inc42_backfill", status=RunStatus.FAILED,
                     detail="Master workbook not found.")
        run.append_log("Inc42 master workbook not found in instance/ or alongside the app.")
        run.finished_at = utcnow()
        db.session.add(run); db.session.commit()
        return run
    return ingest_deals(org_id, _rows_from_master(path), source="inc42_backfill", sync_excel=False)


def ensure_org_has_data(org_id: int) -> JobRun | None:
    """Auto-import the history the first time a workspace has no deals."""
    has = db.session.query(FundingDeal.id).filter_by(organization_id=org_id).first()
    if has:
        return None
    if master_excel_path() is None:
        return None
    return backfill_history(org_id)


# ───────────────────────── write (keep Excel in sync) ─────────────────────────

def _existing_master_keys(ws) -> set:
    keys = set()
    headers = {c.value: i for i, c in enumerate(next(ws.iter_rows(min_row=1, max_row=1)))}
    ni, di, ai = headers.get("Name"), headers.get("Date"), headers.get("Amount (USD Mn)")
    if ni is None or di is None or ai is None:
        return keys
    for r in ws.iter_rows(min_row=2, values_only=True):
        if ni >= len(r) or r[ni] is None:
            continue
        d = r[di]
        dkey = d.strftime("%Y-%m-%d") if isinstance(d, datetime) else str(d)
        amt = r[ai]
        akey = f"{float(amt):.2f}" if isinstance(amt, (int, float)) else "0"
        keys.add((str(r[ni]).strip().lower(), dkey, akey))
    return keys


def append_deals_to_master(deals: list[FundingDeal]) -> int:
    """Append new deals to the canonical master workbook (deduplicated)."""
    if not deals:
        return 0
    from openpyxl import Workbook, load_workbook

    path = canonical_master_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        wb = load_workbook(path)
        ws = wb[MASTER_SHEET] if MASTER_SHEET in wb.sheetnames else wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = MASTER_SHEET
        ws.append(MASTER_COLUMNS)

    existing = _existing_master_keys(ws)
    added = 0
    for d in deals:
        dkey = d.deal_date.strftime("%Y-%m-%d")
        akey = f"{float(d.amount_usd_mn or 0):.2f}"
        key = (d.company_name.strip().lower(), dkey, akey)
        if key in existing:
            continue
        ws.append(_master_row(d))
        existing.add(key)
        added += 1
    if added:
        wb.save(path)
    wb.close()
    return added


def _master_row(d: FundingDeal) -> list:
    """One workbook row for a deal, in ``MASTER_COLUMNS`` order."""
    iso = d.deal_date.isocalendar()
    return [
        iso[1], iso[0], datetime(d.deal_date.year, d.deal_date.month, d.deal_date.day),
        d.company_name, d.sector_canonical, d.subsector or "", d.business_model or "",
        d.funding_round_size or "", round(float(d.amount_usd_mn or 0), 2), d.round_type or "",
        d.investors_raw or "", d.lead_investor or "", d.source_url or "",
    ]


def rewrite_master_from_db(org_id: int) -> int:
    """Rebuild the Excel master so it exactly mirrors the database.

    :func:`append_deals_to_master` only ever *adds* rows, so edits and deletions
    made in the app would leave the workbook stale. That matters beyond
    tidiness: the workbook is what "Import 3-year history" reads back, so a
    deleted deal would be resurrected on the next import. Rewriting the sheet
    from the database keeps that round-trip honest.

    The save is atomic (temp file + ``os.replace``) so an interrupted write can
    never truncate the canonical workbook. Returns the number of rows written.
    """
    from openpyxl import Workbook

    deals = (
        db.session.query(FundingDeal)
        .filter_by(organization_id=org_id)
        .order_by(FundingDeal.deal_date, FundingDeal.id)
        .all()
    )
    if not deals:
        # Never blow away the canonical history just because the database looks
        # empty — that is far likelier to be a fresh or broken DB than a real
        # "delete everything" intent, and the workbook is the only other copy.
        current_app.logger.warning("Master rewrite skipped: no deals in the database.")
        return 0

    path = canonical_master_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    wb = Workbook()
    ws = wb.active
    ws.title = MASTER_SHEET
    ws.append(MASTER_COLUMNS)
    for d in deals:
        ws.append(_master_row(d))
    try:
        wb.save(tmp)
    finally:
        wb.close()
    os.replace(tmp, path)
    return len(deals)


def sync_master_best_effort(org_id: int) -> None:
    """Mirror the database into the Excel master, swallowing any failure.

    Called after in-app edits/deletions. Keeping the workbook in step is
    important for data integrity but must never turn a successful database
    mutation into a user-visible error, so failures are logged and dropped.
    """
    try:
        n = rewrite_master_from_db(org_id)
        if n:
            current_app.logger.info("Excel master rewritten from the database (%d rows).", n)
    except Exception:  # pragma: no cover - defensive
        current_app.logger.exception("Excel master sync failed for org %s", org_id)


# ───────────────────────── live refresh ─────────────────────────

def _discover_article_urls(weeks: int) -> list[str]:
    from playwright.sync_api import sync_playwright

    urls: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"))
        page.goto(INC42_FUNDING_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1500)
        for _ in range(weeks + 4):
            for a in page.query_selector_all("div.card-wrapper a.recommended-block-head"):
                href = a.get_attribute("href")
                if href and href not in urls:
                    urls.append(href)
            if len(urls) >= weeks:
                break
            more = page.query_selector("#load-more-feed")
            if not more or not more.is_visible():
                break
            try:
                more.click()
                page.wait_for_timeout(4000)
            except Exception:
                break
        browser.close()
    return urls[:weeks]


def _header_field(text: str) -> str | None:
    """Map an Inc42 table header cell to one of our internal field names.

    Order matters — more specific labels (sub-sector, lead investor, round size)
    are tested before their generic prefixes (sector, investor, round).
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    if any(k in t for k in ("startup", "company", "name")):
        return "company"
    if "date" in t:
        return "date"
    if "sub" in t and "sector" in t:           # sub-sector / subsector / sub sector
        return "subsector"
    if "sector" in t or "industry" in t:
        return "sector"
    if "business" in t or t == "model":
        return "business_model"
    if "size" in t or "amount" in t or "funding" in t:
        return "funding_round_size"
    if "lead" in t:
        return "lead"
    if "investor" in t:
        return "investors"
    if "round" in t or "stage" in t:
        return "round"
    return None


def _build_header_map(header_cells: list[str]) -> dict[str, int]:
    """First matching column wins for each field (header → column index)."""
    mapping: dict[str, int] = {}
    for i, cell in enumerate(header_cells):
        field = _header_field(cell)
        if field and field not in mapping:
            mapping[field] = i
    return mapping


# Legacy positional layout, used only when a table ships without a usable header.
_LEGACY_POSITIONS = {
    "date": 0, "company": 1, "sector": 2, "subsector": 3, "business_model": 4,
    "funding_round_size": 5, "round": 6, "investors": 7, "lead": 8,
}


def _parse_article(url: str) -> list[dict]:
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")
    if not rows:
        return []

    # Detect + parse the header so columns are mapped by NAME, not position. This
    # keeps ingestion aligned with the Excel master even when Inc42 reorders or
    # adds columns, and stops valid rows being dropped by rigid index assumptions.
    first = rows[0]
    header_cells = [c.get_text(strip=True) for c in first.find_all(["th", "td"])]
    first_txt = " ".join(header_cells).lower()
    is_header = bool(first.find("th")) or any(k in first_txt for k in _HEADER_HINTS)
    colmap = _build_header_map(header_cells) if is_header else {}
    if not {"company"} <= set(colmap):       # need at least a company column
        colmap = _LEGACY_POSITIONS
        start = 1 if is_header else 0
    else:
        start = 1

    def cell(cols, field):
        i = colmap.get(field)
        return cols[i].get_text(strip=True) if i is not None and i < len(cols) else ""

    out = []
    for row in rows[start:]:
        cols = row.find_all("td")
        if any(c.get("colspan") for c in cols) or not cols:
            continue
        if "Source: Inc42" in cols[0].get_text():
            continue
        if any(m in row.get_text().lower() for m in _SKIP_MARKERS):
            continue
        company = cell(cols, "company")
        if not company:
            continue
        size = cell(cols, "funding_round_size")
        out.append({
            "date": cell(cols, "date"), "company": company,
            "sector": cell(cols, "sector"), "subsector": cell(cols, "subsector"),
            "business_model": cell(cols, "business_model"),
            "funding_round_size": size, "amount": size,
            "round": cell(cols, "round"), "investors": cell(cols, "investors"),
            "lead": cell(cols, "lead"), "url": url,
        })
    return out


def refresh_latest(org_id: int, weeks: int = 10) -> JobRun:
    """Live-scrape the latest ``weeks`` reports; insert only new deals (DB + Excel)."""
    run = JobRun(organization_id=org_id, kind="inc42_refresh", status=RunStatus.RUNNING)
    db.session.add(run)
    run.append_log(f"Discovering the {weeks} most recent Inc42 reports…")
    db.session.flush()

    try:
        urls = _discover_article_urls(weeks)
    except Exception as exc:
        run.status = RunStatus.FAILED
        run.detail = "Could not reach Inc42 (browser/network unavailable)."
        run.append_log(f"ERROR discovering URLs: {exc}")
        run.finished_at = utcnow()
        db.session.commit()
        return run

    if not urls:
        # A zero-URL scrape is never a legitimate "nothing to do" — Inc42's tag
        # page always lists past editions. It means the selectors in
        # _discover_article_urls no longer match the live markup. Reporting
        # SUCCESS here would let an automated run stay green while the site
        # quietly stopped updating, so this is a hard failure.
        run.status = RunStatus.FAILED
        run.detail = ("No articles found on the Inc42 tag page — the page markup has "
                      "likely changed (check the selectors in _discover_article_urls).")
        run.append_log(run.detail)
        run.finished_at = utcnow()
        db.session.commit()
        return run

    run.append_log(f"Found {len(urls)} reports. Parsing deal tables…")
    rows: list[dict] = []
    for i, url in enumerate(urls):
        try:
            deals = _parse_article(url)
            rows.extend(deals)
            run.append_log(f"  [{i+1}/{len(urls)}] {len(deals)} deals — {url.split('/')[-2][:48]}")
        except Exception as exc:
            run.append_log(f"  [{i+1}/{len(urls)}] failed: {exc}")
        time.sleep(0.4)
    db.session.commit()

    if not rows:
        # Articles were reachable but none yielded a deal row: the tables moved
        # or their markup changed. Same reasoning as the empty-URL case — fail
        # loudly rather than record a green run that ingested nothing.
        run.status = RunStatus.FAILED
        run.detail = (f"Parsed 0 deal rows from {len(urls)} Inc42 article(s) — the deal-table "
                      "markup has likely changed (check _parse_article).")
        run.append_log(run.detail)
        run.finished_at = utcnow()
        db.session.commit()
        return run

    ingest = ingest_deals(org_id, rows, source="inc42_refresh", sync_excel=True)
    run.status = ingest.status
    run.detail = f"Scraped {len(rows)} deals from {len(urls)} reports — {ingest.detail}"
    run.append_log(run.detail)
    run.finished_at = utcnow()
    db.session.commit()
    return run
