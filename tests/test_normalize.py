"""Tests for ingestion normalisation — the trust boundary for all external data."""
from __future__ import annotations

import math
from datetime import date

from intelligence.ingestion.normalize import parse_amount, parse_date, sanitize_date_string


class TestParseAmount:
    def test_millions(self):
        assert parse_amount("$15 Mn") == 15.0

    def test_billions_to_millions(self):
        assert parse_amount("1.5 Bn") == 1500.0

    def test_thousands_to_millions(self):
        assert parse_amount("150K") == 0.15

    def test_bare_number_is_millions(self):
        assert parse_amount("42") == 42.0

    def test_undisclosed_dashes(self):
        assert parse_amount("–") == 0.0
        assert parse_amount("-") == 0.0
        assert parse_amount("") == 0.0
        assert parse_amount(None) == 0.0

    def test_garbage_is_zero_not_exception(self):
        assert parse_amount("undisclosed") == 0.0
        assert parse_amount("n/a") == 0.0

    def test_negative_clamped_to_zero(self):
        # Negative funding is nonsensical and corrupts aggregates; treat as 0.
        assert parse_amount(-50) == 0.0
        assert parse_amount("-50 Mn") == 0.0

    def test_non_finite_rejected(self):
        # NaN/inf would poison every downstream sum and percentage.
        assert parse_amount(float("nan")) == 0.0
        assert parse_amount(float("inf")) == 0.0
        result = parse_amount(123.4)
        assert math.isfinite(result)

    def test_bool_is_not_an_amount(self):
        assert parse_amount(True) == 0.0


class TestParseDate:
    def test_common_formats(self):
        assert parse_date("15 May 2026") == date(2026, 5, 15)
        assert parse_date("2026-05-15") == date(2026, 5, 15)
        assert parse_date("15-May-26") == date(2026, 5, 15)

    def test_passthrough_date_object(self):
        assert parse_date(date(2026, 1, 1)) == date(2026, 1, 1)

    def test_unparseable_returns_none(self):
        assert parse_date("not a date") is None
        assert parse_date(None) is None

    def test_sanitises_known_typos(self):
        assert sanitize_date_string("15 May 2025*") == "15 May 2025"
        assert sanitize_date_string("15 May 20205") == "15 May 2025"
