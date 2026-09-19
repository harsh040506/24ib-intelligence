"""Shared pytest fixtures.

Each test gets a fully isolated application instance backed by a throwaway
SQLite file (not in-memory — Flask-SQLAlchemy's connection pooling makes a single
shared in-memory database fragile across requests). The app is single-user (no
login wall), so the workspace is just there from the first request — the Inc42
auto-import is stubbed out so boot is fast and hermetic rather than pulling the
real multi-year workbook.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the platform package root is importable when pytest is invoked from
# anywhere (the package uses ``from config import Config``).
_PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(_PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_ROOT))

from config import Config  # noqa: E402
from intelligence import create_app  # noqa: E402
from intelligence.extensions import db  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Disable the real history import so boot is instant and deterministic.
    monkeypatch.setattr("intelligence.ingestion.inc42.master_excel_path", lambda: None)

    # Redirect the canonical workbook into the throwaway directory too. Stubbing
    # master_excel_path only blocks the *read* path; the write path resolves
    # canonical_master_path() from the app's instance folder, which is the real
    # one. The ledger routes call sync_master_best_effort on every edit and
    # delete, so without this a ledger test silently rewrites the live master
    # with its own two throwaway rows.
    monkeypatch.setattr(
        "intelligence.ingestion.inc42.canonical_master_path",
        lambda: tmp_path / "Inc42_Funding_Master_Data.xlsx",
    )

    class TestConfig(Config):
        ENV = "testing"
        TESTING = True
        SECRET_KEY = "test-secret-key-that-is-definitely-long-enough-xx"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
        ENABLE_SCHEDULER = False
        # Never touch the real published site from the test suite.
        PUBLISH_ON_GENERATE = False
        PUBLISH_DIR = tmp_path / "site"

    application = create_app(TestConfig)
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def workspace(app):
    """The single auto-created Organization row."""
    from intelligence.models import Organization
    with app.app_context():
        return db.session.query(Organization).first()
