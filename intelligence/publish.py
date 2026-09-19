"""Static-site publishing engine for the public 24IB research site.

The site (``24IB-Private-Market-Research/``) is the public, GitHub-Pages-ready
archive. It is **weekly + monthly only** by design — quarterly/annual reports are
generated and served in-app but never published here.

Two entry points:

* :func:`publish_report` — incremental. Called automatically every time a weekly
  or monthly report is generated. It (re)writes that report's page, repairs the
  pager links on its immediate neighbours (so the previously-latest report's
  "Next →" now points at the new edition), and regenerates the affected archive
  index plus the home page's "Latest Intelligence" cards. The most-recent
  week/month is detected from the data, not hard-coded.
* :func:`rebuild_site` — full rebuild of every published page from the reports
  already stored in the database. Used by ``publish_site.py``.

Design notes
------------
* **Self-contained.** The constant page chrome (CSS, nav, the bespoke home copy)
  lives as verbatim assets under ``publish_assets/``; only the data-driven
  regions are regenerated. The publisher therefore never depends on previously
  generated output and cannot drift from the reference markup.
* **Fault-tolerant.** Every file write is atomic (temp file + ``os.replace``) so a
  crash mid-publish can never leave a half-written page. Publishing is best-effort
  from the caller's perspective: a failure is logged and surfaced as a return
  value, never propagated into report generation.
"""
from __future__ import annotations

import calendar
import logging
import os
import re
import tempfile
from pathlib import Path

from flask import current_app

from sqlalchemy import select

from .clock import utcnow
from .extensions import db
from .models import PeriodType, Report, ReportStatus

log = logging.getLogger("intelligence.publish")

# The site publishes these cadences only; anything else is silently skipped.
WEB_SECTIONS = ("weekly", "monthly")

# Period-key shapes, used to reject anything that could escape the output dir.
_KEY_RE = {
    "weekly": re.compile(r"^\d{4}-W\d{2}$"),
    "monthly": re.compile(r"^\d{4}-\d{2}$"),
}

_ASSETS = Path(__file__).resolve().parent / "publish_assets"

# Inline SVG glyphs (verbatim from the reference markup).
_ARROW_PREV = "M224 128a8 8 0 0 1-8 8H59.3l58.4 58.3a8 8 0 0 1-11.4 11.4l-72-72a8 8 0 0 1 0-11.4l72-72a8 8 0 0 1 11.4 11.4L59.3 120H216a8 8 0 0 1 8 8Z"
_ARROW_NEXT = "M221.7 133.7l-72 72a8 8 0 0 1-11.4-11.4L196.7 136H40a8 8 0 0 1 0-16h156.7l-58.4-58.3a8 8 0 0 1 11.4-11.4l72 72a8 8 0 0 1 0 11.4Z"
_ARROW_UPRIGHT = "M200 64v104a8 8 0 0 1-16 0V83.3L69.7 197.7a8 8 0 0 1-11.4-11.4L172.7 72H88a8 8 0 0 1 0-16h104a8 8 0 0 1 8 8Z"

_asset_cache: dict[str, str] = {}


def _svg(path: str) -> str:
    return f'<svg class="ic" viewBox="0 0 256 256" aria-hidden="true"><path d="{path}"/></svg>'


def _asset(name: str) -> str:
    """Read a publish asset, preserving its exact bytes/newlines (cached)."""
    if name not in _asset_cache:
        with open(_ASSETS / name, encoding="utf-8", newline="") as fh:
            _asset_cache[name] = fh.read()
    return _asset_cache[name]


def default_site_dir() -> Path:
    """The configured output directory, or the repo default outside app context."""
    try:
        return Path(current_app.config["PUBLISH_DIR"])
    except Exception:
        return Path(__file__).resolve().parent.parent / "24IB-Private-Market-Research"


# ───────────────────────── atomic file IO ─────────────────────────

def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (no partial files on crash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)  # atomic on POSIX and Windows
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ───────────────────────── chrome fragments ─────────────────────────

def _nav_links(section: str) -> str:
    home = '<a href="../index.html">Home</a>'
    weekly = ('<a href="index.html" class="active">Weekly</a>' if section == "weekly"
              else '<a href="../weekly/index.html">Weekly</a>')
    monthly = ('<a href="index.html" class="active">Monthly</a>' if section == "monthly"
               else '<a href="../monthly/index.html">Monthly</a>')
    return f"    {home}\n    {weekly}\n    {monthly}\n"


def _site_nav(section: str) -> str:
    return (
        '<nav class="site-nav"><div class="site-nav-inner">\n'
        '  <a class="nav-brand" href="../index.html"><span class="nav-mark">24<span>IB</span></span>'
        '<span class="nav-desc">Intelligence</span></a>\n'
        '  <div class="nav-links">\n'
        f'{_nav_links(section)}'
        '  </div>\n'
        '</div></nav>\n'
    )


def _pretty(section: str, key: str) -> str:
    if section == "weekly":
        y, w = key.split("-W")
        return f"Week {int(w)} &middot; {y}"
    y, m = key.split("-")
    return f"{calendar.month_abbr[int(m)]} {y}"


def _pager(section: str, prev_key: str | None, next_key: str | None) -> str:
    if prev_key:
        prev = (f'<a class="pg prev" href="{prev_key}.html"><span>{_svg(_ARROW_PREV)} '
                f'Previous</span><b>{_pretty(section, prev_key)}</b></a>')
    else:
        prev = (f'<span class="pg prev disabled"><span>{_svg(_ARROW_PREV)} '
                f'Previous</span><b>&mdash;</b></span>')
    if next_key:
        nxt = (f'<a class="pg next" href="{next_key}.html"><span>Next {_svg(_ARROW_NEXT)}'
               f'</span><b>{_pretty(section, next_key)}</b></a>')
    else:
        nxt = (f'<span class="pg next disabled"><span>Next {_svg(_ARROW_NEXT)}'
               f'</span><b>&mdash;</b></span>')
    return ('<nav class="rep-pager">\n'
            f'  {prev}\n'
            '  <a class="pg index" href="index.html"><b>All Reports</b></a>\n'
            f'  {nxt}\n'
            '</nav>')


def _footer(section: str, year: int) -> str:
    if section == "weekly":
        links = ('<a href="../index.html">Home</a><a href="index.html">Weekly</a>'
                 '<a href="../monthly/index.html">Monthly</a>')
    else:
        links = ('<a href="../index.html">Home</a><a href="../weekly/index.html">Weekly</a>'
                 '<a href="index.html">Monthly</a>')
    return (
        '<footer class="site-foot"><div class="foot-inner">\n'
        '  <div>\n'
        '    <div class="foot-brand">24<span>IB</span> Research</div>\n'
        f'    <div class="foot-links">{links}</div>\n'
        '  </div>\n'
        '  <div class="foot-meta">Weekly Intelligence: Indian Private Markets<br>'
        'Institutional-grade deal data &amp; sector intelligence<br>'
        f'&copy; {year} 24IB Research</div>\n'
        '</div></footer>\r\n'  # trailing CRLF matches the reference output
    )


def _wrap_report(core_html: str, *, section: str, prev_key: str | None,
                 next_key: str | None, year: int) -> str:
    """Inject the site chrome into an engine-rendered report at three seams."""
    head_chrome = _asset("report_chrome_head.html")
    html = core_html.replace("</style>\n</head>", "</style>\n" + head_chrome + "</head>", 1)
    html = html.replace("<body>\n", "<body>\r\n" + _site_nav(section), 1)
    tail = _pager(section, prev_key, next_key) + _footer(section, year)
    html = html.replace("</script>\n</body>", "</script>\n" + tail + "</body>", 1)
    return html


# ───────────────────────── index (data-driven) pages ─────────────────────────

def _ndash(text: str) -> str:
    return text.replace("–", "&ndash;")


def _archive_card(rec: dict) -> str:
    return (
        f'      <a class="rep-card" href="{rec["key"]}.html">\n'
        f'        <div class="rc-top"><span class="rc-num">{rec["year"]}</span>'
        f'<span class="rc-arrow">{_svg(_ARROW_UPRIGHT)}</span></div>\n'
        f'        <div class="rc-title">{rec["title"]}</div>\n'
        f'        <div class="rc-range">{rec["range"]}</div>\n'
        f'        <div class="rc-stats"><div><b>{rec["raised"]}</b><span>Raised</span></div>'
        f'<div><b>{rec["deals"]}</b><span>Deals</span></div></div>\n'
        f'      </a>'
    )


def _archive_sections(recs: list[dict]) -> str:
    by_year: dict[int, list[dict]] = {}
    for r in recs:
        by_year.setdefault(r["year"], []).append(r)
    blocks = []
    for year in sorted(by_year, reverse=True):
        cards = sorted(by_year[year], key=lambda r: r["key"], reverse=True)
        body = "".join(_archive_card(c) for c in cards)
        blocks.append(
            '    <section class="year-group">\n'
            f'      <div class="year-label">{year} <span>{len(cards)} Reports</span></div>\n'
            '      <div class="card-grid">\n'
            f'{body}      </div>\n'
            '    </section>'
        )
    return "".join(blocks)


def _archive_html(section: str, recs: list[dict]) -> str:
    total = len(recs)
    if section == "weekly":
        lede = ("Tracking weekly capital deployment, deal volume, and institutional "
                "conviction across the Indian startup ecosystem &mdash; "
                f"{total} editions from January 2025 to the present.")
    else:
        lede = ("Monthly roll-ups of Indian private-market financing &mdash; aggregate "
                f"capital, deal counts, and sector rotation across {total} editions.")
    html = _asset(f"archive_{section}.html")
    html = html.replace("<!--LEDE-->", lede, 1)
    html = html.replace("<!--SECTIONS-->", _archive_sections(recs), 1)
    return html


def _feat_card(rec: dict) -> str:
    section = rec["section"]
    dark = " dark" if section == "weekly" else ""
    label = "Latest Weekly" if section == "weekly" else "Latest Monthly"
    return (
        f'    <a class="feat-card{dark}" href="{section}/{rec["key"]}.html">\n'
        f'      <div class="fc-tag">{label} &middot; {rec["title_long"]}</div>\n'
        f'      <div class="fc-title">{rec["doc_title"]}</div>\n'
        f'      <div class="fc-range">{_ndash(rec["range"])}</div>\n'
        f'      <div class="fc-stats"><div><b>{rec["raised"]}</b><span>Raised</span></div>'
        f'<div><b>{rec["deals"]}</b><span>Deals</span></div></div>\n'
        f'      <div class="fc-go">Read report {_svg(_ARROW_NEXT)}</div>\n'
        f'    </a>'
    )


def _home_html(latest_weekly: dict, latest_monthly: dict) -> str:
    grid = ('<div class="latest-grid">\n'
            + _feat_card(latest_weekly) + "\n"
            + _feat_card(latest_monthly) + "\n  </div>")
    return _asset("home.html").replace("<!--LATEST_GRID-->", grid, 1)


# ───────────────────────── records from stored reports ─────────────────────────

def _record(report: Report) -> dict:
    meta, stats = report.payload["meta"], report.payload["stats"]
    section, key = report.period_type.value, report.period_key
    if section == "weekly":
        title = f"Week {key.split('-W')[1]}"  # zero-padded in archive cards
    else:
        title = calendar.month_name[int(key.split('-')[1])]
    return {
        "key": key,
        "section": section,
        "year": int(key[:4]),
        "title": title,
        "title_long": meta["title"].split("— ")[-1],
        "doc_title": f"{section.capitalize()} Financing Intelligence",
        "range": meta["subtitle"],
        "raised": stats["total_capital_label"],
        "deals": stats["total_deals"],
        "report": report,
    }


def _section_records(org_id: int, section: str) -> list[dict]:
    """Ordered (oldest→newest) records for every publishable report in a section.

    Publishable = stored, READY, marked ``is_published``, and carrying at least
    one deal. Empty periods are intentionally excluded (the site has no empty
    pages); an explicitly unpublished report is excluded regardless of status."""
    rows = db.session.scalars(
        select(Report).where(
            Report.organization_id == org_id,
            Report.period_type == PeriodType(section),
            Report.status == ReportStatus.READY,
            Report.is_published == True,  # noqa: E712
        )
    ).all()
    recs = [
        _record(r) for r in rows
        if r.payload and (r.payload.get("stats") or {}).get("total_deals", 0) > 0
        and _KEY_RE[section].match(r.period_key or "")
    ]
    recs.sort(key=lambda r: r["key"])
    return recs


def _write_report_page(rec: dict, recs: list[dict], site_dir: Path, year: int) -> None:
    section, key = rec["section"], rec["key"]
    if not _KEY_RE[section].match(key):  # defence-in-depth against path escape
        raise ValueError(f"Refusing to publish unsafe period key: {key!r}")
    i = next(k for k, r in enumerate(recs) if r["key"] == key)
    prev_key = recs[i - 1]["key"] if i > 0 else None
    next_key = recs[i + 1]["key"] if i < len(recs) - 1 else None
    from .engine.service import render_report_html  # local import avoids a cycle

    core = render_report_html(rec["report"])
    page = _wrap_report(core, section=section, prev_key=prev_key,
                        next_key=next_key, year=year)
    _atomic_write(site_dir / section / f"{key}.html", page)


def _ensure_static(site_dir: Path) -> None:
    """Make sure the always-present static files exist."""
    nojekyll = site_dir / ".nojekyll"
    if not nojekyll.exists():
        _atomic_write(nojekyll, "")
    notfound = site_dir / "404.html"
    if not notfound.exists():
        _atomic_write(notfound, _asset("404.html"))


# ───────────────────────── public API ─────────────────────────

def publish_report(report: Report, *, site_dir: Path | None = None) -> bool:
    """Incrementally publish one weekly/monthly report and its dependent pages.

    Returns ``True`` if the report was published, ``False`` if it was skipped
    (wrong cadence, not ready, or empty). Raises only on a genuine IO failure —
    callers in the request/generation path wrap this so it can never break
    report generation.
    """
    section = report.period_type.value
    if section not in WEB_SECTIONS:
        log.debug("publish: skipping %s report %s (site is weekly/monthly only)",
                  section, report.period_key)
        return False
    if report.status != ReportStatus.READY or not report.payload:
        log.info("publish: skipping %s %s — not READY", section, report.period_key)
        return False
    if not report.is_published:
        log.info("publish: skipping %s %s — unpublished", section, report.period_key)
        return False
    if (report.payload.get("stats") or {}).get("total_deals", 0) <= 0:
        log.info("publish: skipping %s %s — no deals in period", section, report.period_key)
        return False

    site_dir = Path(site_dir) if site_dir else default_site_dir()
    org_id = report.organization_id
    year = utcnow().year  # footer copyright tracks the publish date, not the period

    weekly = _section_records(org_id, "weekly")
    monthly = _section_records(org_id, "monthly")
    recs = weekly if section == "weekly" else monthly

    idx = next((k for k, r in enumerate(recs) if r["key"] == report.period_key), None)
    if idx is None:  # shouldn't happen given the guards above
        log.warning("publish: %s %s not found among publishable records",
                    section, report.period_key)
        return False

    # The report itself plus any immediate neighbour whose pager link to it
    # may have changed (a newly-arrived latest flips the prior report's
    # disabled "Next →" into a live link).
    targets = {idx}
    if idx > 0:
        targets.add(idx - 1)
    if idx < len(recs) - 1:
        targets.add(idx + 1)
    for t in sorted(targets):
        _write_report_page(recs[t], recs, site_dir, year)

    _atomic_write(site_dir / section / "index.html", _archive_html(section, recs))

    # Make sure the other section's archive exists so cross-links never 404.
    other, other_recs = (("monthly", monthly) if section == "weekly"
                         else ("weekly", weekly))
    if other_recs and not (site_dir / other / "index.html").exists():
        _atomic_write(site_dir / other / "index.html", _archive_html(other, other_recs))

    if weekly and monthly:
        _atomic_write(site_dir / "index.html", _home_html(weekly[-1], monthly[-1]))
    else:
        log.info("publish: home page needs both a weekly and a monthly report; "
                 "deferring until both exist")

    _ensure_static(site_dir)
    log.info("publish: wrote %s %s (+%d neighbour page(s), archive, home)",
             section, report.period_key, len(targets) - 1)
    return True


def remove_report_page(report: Report, *, site_dir: Path | None = None) -> None:
    """Delete a single report's page file after it's unpublished.

    ``rebuild_site``/``publish_report`` only ever *write* pages for currently
    publishable reports — an unpublished report's now-stale file would
    otherwise keep serving at its old URL even though it's gone from every
    index/pager. Best-effort: a missing file (already removed, never
    published) is not an error.
    """
    section = report.period_type.value
    if section not in WEB_SECTIONS or not _KEY_RE[section].match(report.period_key or ""):
        return
    site_dir = Path(site_dir) if site_dir else default_site_dir()
    path = site_dir / section / f"{report.period_key}.html"
    path.unlink(missing_ok=True)


def rebuild_site(org_id: int, *, site_dir: Path | None = None) -> dict:
    """Rebuild every published page from the reports already in the database."""
    site_dir = Path(site_dir) if site_dir else default_site_dir()
    weekly = _section_records(org_id, "weekly")
    monthly = _section_records(org_id, "monthly")
    year = utcnow().year  # footer copyright tracks the publish date, not the period
    counts: dict[str, int] = {}
    for section, recs in (("weekly", weekly), ("monthly", monthly)):
        for rec in recs:
            _write_report_page(rec, recs, site_dir, year)
        if recs:
            _atomic_write(site_dir / section / "index.html", _archive_html(section, recs))
        counts[section] = len(recs)
    if weekly and monthly:
        _atomic_write(site_dir / "index.html", _home_html(weekly[-1], monthly[-1]))
    _ensure_static(site_dir)
    log.info("rebuild_site: %s", counts)
    return counts
