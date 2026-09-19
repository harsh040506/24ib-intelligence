"""Tests for period resolution — the engine's calendar backbone."""
from __future__ import annotations

from datetime import date

import pytest

from intelligence.engine.periods import parse_key, resolve


def test_weekly_key_and_window():
    p = resolve("weekly", date(2026, 4, 1))  # ISO week 14 of 2026
    assert p.period_key == "2026-W14"
    assert p.headline_start.weekday() == 0  # Monday
    assert (p.headline_end - p.headline_start).days == 6
    assert p.prev_key == "2026-W13"


def test_monthly_key_and_window():
    p = resolve("monthly", date(2026, 5, 20))
    assert p.period_key == "2026-05"
    assert p.headline_start == date(2026, 5, 1)
    assert p.headline_end == date(2026, 5, 31)
    assert p.prev_key == "2026-04"


def test_quarterly_key():
    p = resolve("quarterly", date(2026, 8, 1))
    assert p.period_key == "2026-Q3"
    assert p.headline_start == date(2026, 7, 1)
    assert p.headline_end == date(2026, 9, 30)


def test_annual_key():
    p = resolve("annual", date(2026, 6, 15))
    assert p.period_key == "2026"
    assert p.headline_start == date(2026, 1, 1)
    assert p.headline_end == date(2026, 12, 31)


def test_unknown_period_type_raises():
    with pytest.raises(ValueError):
        resolve("daily", date(2026, 1, 1))


@pytest.mark.parametrize("ptype,key", [
    ("weekly", "2026-W14"), ("monthly", "2026-05"),
    ("quarterly", "2026-Q3"), ("annual", "2026"),
])
def test_parse_key_round_trips(ptype, key):
    anchor = parse_key(ptype, key)
    assert resolve(ptype, anchor).period_key == key
