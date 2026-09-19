"""Single source of "now" for the platform.

``datetime.utcnow()`` is deprecated (and slated for removal) on modern Python,
but the database stores naive datetimes throughout — and mixing naive and
timezone-aware values raises ``TypeError`` on comparison. This helper returns a
*naive* UTC timestamp (aware ``now(UTC)`` with the tzinfo dropped), giving us the
non-deprecated code path while keeping every persisted/compared datetime naive
and mutually comparable.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC time as a naive ``datetime`` (no tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
