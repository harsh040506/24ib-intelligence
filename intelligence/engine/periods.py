"""Period resolution — the generalisation that lets one engine produce
weekly / monthly / quarterly / annual reports.

Every report has two windows:

* **headline window** — the period actually being reported on (one week, one
  month, one quarter, one year). All the headline stats, the sector breakdown,
  the ticket distribution, the investor league and the ledger are scoped here.
* **trend window** — a trailing series of *buckets* used for the bar chart and
  the sector x bucket heatmap, giving the reader recent context.

The bucket granularity adapts to the period:
    weekly     -> trailing ISO weeks
    monthly    -> trailing calendar months
    quarterly  -> trailing quarters
    annual     -> the 12 months of the year
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

# Edition numbering anchor (mirrors the original TWTW scheme):
# ISO week 15 of 2026 == edition 27. One edition per ISO week.
_ANCHOR_YEAR, _ANCHOR_WEEK, _ANCHOR_EDITION = 2026, 15, 27


@dataclass
class Bucket:
    key: str          # machine key, e.g. "2026-W14"
    label: str        # short axis label, e.g. "W14" or "May"
    start: date
    end: date         # inclusive


@dataclass
class ResolvedPeriod:
    period_type: str
    period_key: str             # 2026-W14 | 2026-05 | 2026-Q1 | 2026
    title: str
    subtitle: str
    headline_start: date
    headline_end: date          # inclusive
    buckets: list[Bucket] = field(default_factory=list)
    edition_no: int | None = None
    prev_key: str | None = None  # period_key of the immediately preceding period

    @property
    def headline_bucket(self) -> Bucket | None:
        """The bucket within the trend window that equals the headline period."""
        for b in self.buckets:
            if b.start == self.headline_start and b.end == self.headline_end:
                return b
        return self.buckets[-1] if self.buckets else None


# ───────────────────────── ISO week helpers ─────────────────────────

def _iso_week_bounds(year: int, week: int) -> tuple[date, date]:
    monday = date.fromisocalendar(year, week, 1)
    return monday, monday + timedelta(days=6)


def _edition_for(year: int, week: int) -> int:
    weeks_between = (date.fromisocalendar(year, week, 1) - date.fromisocalendar(_ANCHOR_YEAR, _ANCHOR_WEEK, 1)).days // 7
    return _ANCHOR_EDITION + weeks_between


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def _quarter_bounds(year: int, q: int) -> tuple[date, date]:
    start_month = (q - 1) * 3 + 1
    s, _ = _month_bounds(year, start_month)
    _, e = _month_bounds(year, start_month + 2)
    return s, e


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


# ───────────────────────── public API ─────────────────────────

def resolve(period_type: str, anchor: date | None = None, *, trend_len: int | None = None) -> ResolvedPeriod:
    """Resolve a (period_type, anchor date) into a fully-specified ResolvedPeriod.

    ``anchor`` is any date inside the desired period (defaults to today).
    """
    anchor = anchor or date.today()
    period_type = period_type.lower()

    if period_type == "weekly":
        return _resolve_weekly(anchor, trend_len or 10)
    if period_type == "monthly":
        return _resolve_monthly(anchor, trend_len or 12)
    if period_type == "quarterly":
        return _resolve_quarterly(anchor, trend_len or 6)
    if period_type == "annual":
        return _resolve_annual(anchor)
    raise ValueError(f"Unknown period_type: {period_type!r}")


def parse_key(period_type: str, key: str) -> date:
    """Turn a stored period_key back into an anchor date inside that period."""
    period_type = period_type.lower()
    if period_type == "weekly":
        y, w = key.split("-W")
        return date.fromisocalendar(int(y), int(w), 3)
    if period_type == "monthly":
        y, m = key.split("-")
        return date(int(y), int(m), 15)
    if period_type == "quarterly":
        y, q = key.split("-Q")
        return _quarter_bounds(int(y), int(q))[0] + timedelta(days=20)
    if period_type == "annual":
        return date(int(key), 7, 1)
    raise ValueError(f"Unknown period_type: {period_type!r}")


# ───────────────────────── resolvers ─────────────────────────

def _resolve_weekly(anchor: date, trend_len: int) -> ResolvedPeriod:
    iso = anchor.isocalendar()
    year, week = iso.year, iso.week
    h_start, h_end = _iso_week_bounds(year, week)

    buckets: list[Bucket] = []
    for i in range(trend_len - 1, -1, -1):
        d = h_start - timedelta(weeks=i)
        wi = d.isocalendar()
        s, e = _iso_week_bounds(wi.year, wi.week)
        buckets.append(Bucket(key=f"{wi.year}-W{wi.week:02d}", label=f"W{wi.week}", start=s, end=e))

    pw = (h_start - timedelta(weeks=1)).isocalendar()
    return ResolvedPeriod(
        period_type="weekly",
        period_key=f"{year}-W{week:02d}",
        title=f"Weekly Financing Intelligence — Week {week}, {year}",
        subtitle=f"{h_start:%d %b} – {h_end:%d %b %Y}",
        headline_start=h_start,
        headline_end=h_end,
        buckets=buckets,
        edition_no=_edition_for(year, week),
        prev_key=f"{pw.year}-W{pw.week:02d}",
    )


def _resolve_monthly(anchor: date, trend_len: int) -> ResolvedPeriod:
    year, month = anchor.year, anchor.month
    h_start, h_end = _month_bounds(year, month)

    buckets: list[Bucket] = []
    for i in range(trend_len - 1, -1, -1):
        y, m = _shift_month(year, month, -i)
        s, e = _month_bounds(y, m)
        buckets.append(Bucket(key=f"{y}-{m:02d}", label=calendar.month_abbr[m], start=s, end=e))

    py, pm = _shift_month(year, month, -1)
    return ResolvedPeriod(
        period_type="monthly",
        period_key=f"{year}-{month:02d}",
        title=f"Monthly Financing Intelligence — {calendar.month_name[month]} {year}",
        subtitle=f"{h_start:%d %b} – {h_end:%d %b %Y}",
        headline_start=h_start,
        headline_end=h_end,
        buckets=buckets,
        prev_key=f"{py}-{pm:02d}",
    )


def _resolve_quarterly(anchor: date, trend_len: int) -> ResolvedPeriod:
    year = anchor.year
    q = _quarter_of(anchor.month)
    h_start, h_end = _quarter_bounds(year, q)

    # Trend = trailing quarters.
    buckets: list[Bucket] = []
    qi = year * 4 + (q - 1)
    for i in range(trend_len - 1, -1, -1):
        idx = qi - i
        yy, qq = idx // 4, idx % 4 + 1
        s, e = _quarter_bounds(yy, qq)
        buckets.append(Bucket(key=f"{yy}-Q{qq}", label=f"Q{qq} '{yy % 100:02d}", start=s, end=e))

    pidx = qi - 1
    return ResolvedPeriod(
        period_type="quarterly",
        period_key=f"{year}-Q{q}",
        title=f"Quarterly Financing Intelligence — Q{q} {year}",
        subtitle=f"{h_start:%d %b} – {h_end:%d %b %Y}",
        headline_start=h_start,
        headline_end=h_end,
        buckets=buckets,
        prev_key=f"{pidx // 4}-Q{pidx % 4 + 1}",
    )


def _resolve_annual(anchor: date) -> ResolvedPeriod:
    year = anchor.year
    h_start, h_end = date(year, 1, 1), date(year, 12, 31)
    buckets = []
    for m in range(1, 13):
        s, e = _month_bounds(year, m)
        buckets.append(Bucket(key=f"{year}-{m:02d}", label=calendar.month_abbr[m], start=s, end=e))
    return ResolvedPeriod(
        period_type="annual",
        period_key=f"{year}",
        title=f"Annual Financing Intelligence — {year} in Review",
        subtitle=f"Full year {year}",
        headline_start=h_start,
        headline_end=h_end,
        buckets=buckets,
        prev_key=f"{year - 1}",
    )
