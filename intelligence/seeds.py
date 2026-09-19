"""Org bootstrap: seed the canonical sector taxonomy for a new organisation.

Sector normalisation itself is global (see ``ingestion.sector_normalizer``); this
table is seeded for display, email-group grouping and data-quality reference. No
demo accounts or sample deals are created — data arrives via the Inc42 import /
refresh buttons or CSV upload.
"""
from __future__ import annotations

from sqlalchemy import select

from .extensions import db
from .ingestion import sector_normalizer as sn
from .models import Sector


def bootstrap_org(org_id: int) -> None:
    """Give a new org the canonical sector taxonomy (idempotent)."""
    if db.session.scalar(select(Sector.id).where(Sector.organization_id == org_id)):
        return
    for canonical, aliases in sn.CANONICAL_ALIASES.items():
        s = Sector(
            organization_id=org_id,
            canonical=canonical,
            email_group=sn.get_email_group(canonical),
            is_impact=sn.is_impact(canonical),
            accent=sn.SECTOR_ACCENTS.get(canonical, "#1b4a36"),
        )
        s.aliases = aliases
        db.session.add(s)
    db.session.flush()
