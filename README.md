# 24IB Intelligence Platform

A production-grade web application that turns Inc42 funding-deal data into
**beautiful, automated intelligence reports** — weekly, monthly, quarterly, and
annual — viewable in the browser, exportable to PDF, and shareable by link.

Built fresh with **Flask + Jinja2 HTML templates** (server-rendered, no JS
charting — the reports render identically in the browser and in print/PDF).

---

## Why this exists

The original project had two disconnected halves: Python scripts that scraped
data into Excel files, and a single gorgeous hand-coded HTML report whose numbers
were **typed in by hand every week**. This platform closes that gap: data lives in
a real database, and one engine generates the report for *any* period on demand.

## What you get

- **One report engine, four cadences.** Weekly / monthly / quarterly / annual, all
  from the same data. Each report shows a headline period plus a trailing trend
  window, with **prior-period deltas** on every headline metric.
- **The full editorial report**: headline stats, deal-momentum bar chart, sector
  capital-flow bars, sector×period heatmap, ticket-size distribution, investor
  league table, and the transaction ledger.
- **Single-user, no login.** One workspace, no accounts — open it and it's
  ready to use.
- **A real ledger.** Search across companies/investors/subsectors, filter by
  sector, sort any column, choose your page size, edit or delete any deal,
  bulk-reassign sectors, bulk-delete, and export the current view to CSV.
- **Explore the data.** Company, sector and investor pages with full deal
  history and a monthly momentum chart; global search from the top bar (press
  <kbd>/</kbd>); pin anything to your dashboard; keep private notes per report.
- **Compare any two periods.** Side-by-side headline deltas and a
  sector-by-sector capital diff.
- **Publish control.** Weekly/monthly editions auto-publish to the public site;
  unpublish any edition and its page is removed and its neighbours relinked.
- **Data ingestion.** CSV/spreadsheet import with fuzzy sector normalisation,
  deterministic deduplication, and per-row rejection reasons in the job log.
- **Duplicate review.** Fuzzy near-duplicate detection (same company, ±5 days,
  ±10% amount) with merge/dismiss, for the ones exact dedup can't catch.
- **Observability.** Every ingestion/generation is a `JobRun` with logs; an
  Activity page, a `/health` probe reporting counts and last-job status, and
  actionable data-quality flags on the dashboard.
- **REST API + OpenAPI + docs.** Bearer-key authenticated `/api/v1` with paged,
  filterable `/deals` and `/reports` endpoints.
- **PDF export + public share links.** Self-contained, portable HTML documents.

---

## Quick start

```bash
python -m pip install -r requirements.txt
cp .env.example .env          # optional; sensible defaults work out of the box
python run.py
```

Open <http://127.0.0.1:5000> — that's it. This is a single-user, no-login app:
there's always exactly one workspace, created automatically on first boot. The
full ~3-year Inc42 history is **imported automatically** the first time you run
it — no button required. The SQLite database and the canonical Excel master
(`instance/Inc42_Funding_Master_Data.xlsx`) are created automatically on first
boot; the master is seeded from the workbook shipped with the project and kept
in sync as new data arrives.

### Getting data in (Inc42)
On the **Data** page:
- **Import 3-year history** — loads the full Inc42 Funding Galore history from
  the master workbook. The canonical master lives in the instance folder
  (`instance/Inc42_Funding_Master_Data.xlsx`); on first use it is seeded once
  from a shipped/nearby copy, then always read from and written back to that
  instance location. Sectors are normalised to the canonical taxonomy and
  duplicates skipped.
- **Refresh from Inc42** — live-scrapes the latest N weeks (default 10) directly
  from inc42.com, compares against the database, and inserts only new records.
  (Needs network + Playwright/Chromium; falls back to a clear error otherwise.)

### Generate reports
- **UI:** Dashboard → *Generate report* → pick a period (leave the key blank for
  the current one, or use `2026-W14`, `2026-03`, `2026-Q1`, `2026`).
- **CLI:** `flask --app run generate weekly --key 2026-W14`
- **API:** `GET /api/v1/reports/weekly/2026-W14` (with a bearer key).

### Import your own data
Data → *Import deals (CSV)*. Recognised columns (any order): `company, sector,
amount, date, round, investors, lead, url`. Amounts accept `$15 Mn`, `1.5 Bn`,
`150K`. Uploads must be `.csv`, `.tsv`, or `.txt`. Rows that can't be imported
are listed with their reason in the job log (Activity → the import run).

### Editing data
The Data page is a full ledger: search, filter by sector, sort any column, and
edit or delete individual deals. Select rows to bulk-delete or bulk-reassign a
sector, and use *Export CSV* to download exactly what you're looking at.

> **The workbook mirrors the database.** `instance/Inc42_Funding_Master_Data.xlsx`
> is what *Import 3-year history* reads back, so every in-app edit and deletion
> is written through to it. Without that, a deleted deal would reappear on the
> next import.

*Review duplicates* finds near-duplicates that exact deduplication misses — the
same company reported twice with a rounded amount or a date a day or two off.

### Server-side PDF (optional)
The PDF button works without setup by serving a print-ready page (use the
browser's *Print → Save as PDF*). For one-click server-side PDFs:

```bash
pip install playwright && playwright install chromium
```

### Publishing the public site
`24IB-Private-Market-Research/` is the public, GitHub-Pages-ready archive — a
plain static folder (no git required to maintain it). It is **weekly + monthly
only** — quarterly/annual reports are generated
and served in-app but never published here.

**It maintains itself.** Every time a weekly or monthly report is generated — UI,
`flask generate`, the scheduler, or an API-triggered run — that report is written
to the site automatically (`PUBLISH_ON_GENERATE`, on by default). The publisher:

- renders the report with the **same engine** the app uses, then injects the site
  chrome (top nav, prev/next pager, footer) so the published page is the in-app
  report plus navigation;
- **detects the latest week/month from the data** and repairs the pager on the
  affected neighbours — when a new week lands, the previously-latest report is
  rewritten so its disabled "Next →" becomes a live link to the new edition;
- regenerates the affected archive card grid and the home page's "Latest
  Intelligence" cards from the same data.

Publishing is best-effort and fully isolated: a failure is logged and never rolls
back or breaks report generation. Every page is written atomically. Configure it
with `PUBLISH_ON_GENERATE` (set `0` to disable) and `PUBLISH_DIR` (output path).

To rebuild the whole site from the database in one pass (e.g. first-time
population or after restoring a backup):

```bash
python publish_site.py                 # rebuild the live site in place
python publish_site.py ./_preview      # rebuild into another directory (dry run)
```

`404.html` and `.nojekyll` are written if missing; the constant page chrome lives
verbatim in `intelligence/publish_assets/`, so the publisher never depends on
previously generated output.

### Hosting it for free, updating itself
`scripts/weekly_update.py` is the unattended version of the whole loop — scrape
Inc42, generate whatever weekly/monthly report is now due, publish the affected
pages, and mirror the database back into the Excel master. It is driven by
`.github/workflows/weekly-update.yml`, a GitHub Actions cron that runs every
Monday and deploys the result to GitHub Pages. Both are free on a public
repository; no server, no laptop, nothing to run by hand.

The script rebuilds a period **only** when it has no report yet or when a deal
landed in it since the last build, so an unchanged week produces no commit and
no republished page.

To run it on your own fork: make the repository public (Actions minutes and
Pages are free there), set **Settings → Actions → General → Workflow
permissions** to *Read and write*, and set **Settings → Pages → Source** to
*GitHub Actions*. Then trigger the workflow once from the Actions tab.

```bash
python scripts/weekly_update.py                 # what the cron runs
python scripts/weekly_update.py --skip-refresh  # rebuild only, no scrape
python scripts/weekly_update.py --force         # rebuild even if unchanged
```

---

## Architecture

```
run.py                 entry point: boots the web app + scheduler
config.py              env-driven config + fail-fast prod validation
publish_site.py        build the public static site (24IB-Private-Market-Research/)
scripts/weekly_update.py   unattended weekly run: scrape → generate → publish
.github/workflows/     GitHub Actions cron that runs the above every Monday
24IB-Private-Market-Research/   published static site: report pages + indexes
instance/              runtime data: SQLite db + canonical Inc42 master workbook
intelligence/
├── __init__.py        app factory: blueprints, extensions, error handlers, CLI
├── extensions.py      db singleton
├── clock.py           single source of "now" (naive-UTC helper)
├── security.py        rate limiter (protects the REST API)
├── models.py          SQLAlchemy: the workspace, deals, sectors, investors,
│                       reports, schedules, job runs
├── tenancy.py         resolves the single workspace (no accounts, no RBAC)
├── engine/            THE REPORT ENGINE
│   ├── periods.py     resolve weekly/monthly/quarterly/annual → headline+trend
│   ├── aggregate.py   deals → typed report payload (+ prior-period deltas)
│   ├── service.py     generate + persist + audit (JobRun); render to HTML
│   └── pdf.py         HTML → PDF (Playwright, graceful fallback)
├── ingestion/         data in: normalisation, dedup, connectors
│   ├── normalize.py        amount/date/field normalisation
│   ├── sector_normalizer.py canonical sector mapping (alias + fuzzy match)
│   ├── connectors.py       CSV import + the generic ingest_deals seam
│   └── inc42.py            Inc42 history backfill, live refresh, Excel master
├── publish.py         static-site publisher: auto-publish + full rebuild (weekly/monthly)
├── publish_assets/    verbatim page chrome/skeletons used by the publisher
├── scheduler.py       APScheduler dispatch for automated generation
├── seeds.py           canonical sector-taxonomy bootstrap for the workspace
├── dashboard/ reports/ data/ api/   feature blueprints
├── templates/         Jinja2 (base shell + the editorial report document)
└── static/css/        design system: tokens.css, app.css, report.css
```

### How a report is built
`resolve(period_type, date)` → headline + trend buckets → `build_payload(org, period)`
aggregates deals (trend, sectors, pivot, tickets, investors, ledger) → stored as
JSON on a `Report` row → rendered by
`templates/reports/document.html` (a self-contained editorial document with
pure-CSS/JS charts; untrusted deal data is HTML-escaped before DOM insertion).

### Extending to live data
Implement a connector that returns rows of
`{company, sector, amount, date, round, investors, lead, url}` and pass them to
`ingestion.connectors.ingest_deals(org_id, rows, source=...)`. Normalisation,
dedup, and the audit record are handled for you. `ingestion/inc42.py`
(`backfill_history` / `refresh_latest`) is the live, working example of this
seam.

---

## Production notes
- Point `DATABASE_URL` at Postgres (`postgresql+psycopg://…`).
- Set a strong `SECRET_KEY` (≥32 chars) and `FLASK_ENV=production`. Config
  validation **fails fast** on a weak/missing secret and forces `Secure` cookies
  + HSTS.
- Run behind a WSGI server (e.g. `gunicorn run:app`) and a reverse proxy. Set
  `PROXY_FIX_HOPS` to the number of trusted proxies so client IPs (used for API
  rate limiting) and the request scheme are read from the forwarding headers and
  cannot be spoofed.
- There is no login wall — this is a single-user app. Don't expose it on the
  open internet without putting your own access control (a reverse-proxy
  basic-auth, a VPN, etc.) in front of it.
- Move the scheduler to a dedicated worker (or swap APScheduler for Celery/RQ) at
  scale. The in-process rate limiters are per-worker; front the app with a shared
  limiter (Redis / gateway) when running multiple workers.

## Tests
```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```
88 tests covering:
- normalisation, period resolution, and the report XSS-escaping regression;
- the HTTP surface (security headers, workspace bootstrap, API auth);
- static-site publishing (auto-publish, neighbour-pager repair, weekly/monthly
  scope, `is_published` filtering, failure isolation);
- the ledger (search, filters, sort-key injection, CSV export, edit validation,
  duplicate-identity rejection, `javascript:` URL stripping, bulk actions,
  open-redirect refusal);
- report publish/unpublish round-trips and the comparison view;
- explore profiles, search, favourites, and the paged/filterable REST API;
- **data integrity** — that an in-app delete or edit reaches the Excel master,
  and that a rewrite refuses to empty it.

## Verified
Boot, Inc42 history import (sector-normalised, deduplicated), generation for all
four periods, in-app report view, public share page, REST API (auth enforced),
OpenAPI/docs, `/health`, and PDF export all pass end-to-end. The static-site
publisher (`publish_site.py`) reproduces the committed `24IB-Private-Market-Research/`
site byte-for-byte (92/93 report pages plus every index page; the lone exception
is one reference file that carries stray editor `CRLF` bytes the publisher emits
consistently).
