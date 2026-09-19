"""Workspace resolution.

This is a single-user, single-workspace app: there is always exactly one
``Organization`` row (the "workspace"), created on first boot along with the
canonical sector taxonomy and, best-effort, the Inc42 history import. Every
data query in the app scopes to this workspace's id.
"""
from __future__ import annotations

from flask import g

from .extensions import db
from .models import Organization


def ensure_workspace() -> Organization:
    """Return the workspace, creating and seeding it if this is the first boot."""
    org = db.session.query(Organization).first()
    if org is not None:
        return org

    org = Organization(name="My Workspace", slug="workspace")
    db.session.add(org)
    db.session.flush()

    from .seeds import bootstrap_org
    bootstrap_org(org.id)
    db.session.commit()

    # Auto-import the full Inc42 history so the workspace is immediately
    # useful. Best-effort: a missing workbook must not block boot — the user
    # can still import manually from the Data page.
    try:
        from .ingestion.inc42 import ensure_org_has_data
        ensure_org_has_data(org.id)
    except Exception:
        pass
    return org


def require_org() -> Organization:
    """Return the workspace, cached per-request."""
    if "workspace" not in g:
        g.workspace = ensure_workspace()
    return g.workspace
