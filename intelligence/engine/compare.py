"""Period comparison — diff two already-generated report payloads.

Pure function over two ``Report.payload`` dicts (see ``aggregate.build_payload``
for their shape). No new queries: everything needed is already persisted on the
reports being compared, so a comparison is cheap regardless of how large the
underlying period is.
"""
from __future__ import annotations


def _pct_delta(curr: float, prev: float) -> float | None:
    if not prev:
        return None
    return round((curr - prev) / prev * 100, 1)


def diff_payloads(a: dict, b: dict) -> dict:
    """Diff payload ``a`` (baseline) against payload ``b`` (comparison).

    Returns headline-stat deltas plus a sector table joined by sector name
    (a sector present in only one side still appears, with a zero on the other).
    """
    sa, sb = a.get("stats", {}), b.get("stats", {})
    headline = []
    for key, label in (
        ("total_deals", "Deals"),
        ("total_capital_mn", "Capital deployed"),
        ("sector_count", "Sectors active"),
        ("avg_ticket_mn", "Average ticket"),
    ):
        va, vb = sa.get(key) or 0, sb.get(key) or 0
        headline.append({
            "label": label, "a": va, "b": vb,
            "delta": _pct_delta(vb, va),
        })

    sectors_a = {r["s"]: r for r in a.get("sectors", [])}
    sectors_b = {r["s"]: r for r in b.get("sectors", [])}
    names = sorted(set(sectors_a) | set(sectors_b),
                   key=lambda s: max(sectors_a.get(s, {}).get("amt", 0),
                                      sectors_b.get(s, {}).get("amt", 0)),
                   reverse=True)
    sectors = []
    for name in names:
        ra = sectors_a.get(name, {"amt": 0, "deals": 0})
        rb = sectors_b.get(name, {"amt": 0, "deals": 0})
        sectors.append({
            "name": name, "amt_a": ra["amt"], "amt_b": rb["amt"],
            "deals_a": ra["deals"], "deals_b": rb["deals"],
            "delta": _pct_delta(rb["amt"], ra["amt"]),
        })

    return {"headline": headline, "sectors": sectors}
