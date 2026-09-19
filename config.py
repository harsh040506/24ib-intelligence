"""Application configuration.

Single source of truth for runtime settings. Everything is environment-driven
so the same code runs locally (SQLite, zero setup) or in production (Postgres,
hardened secret).

Design rationale
----------------
* **Fail fast in production.** Misconfiguration that weakens security (a default
  secret key, cookies that are not ``Secure`` over HTTPS) is treated as a hard
  error in production rather than a silent degradation — a half-secure
  deployment is worse than a refusal to boot, because it gives a false sense of
  safety.
* **Safe by default in development.** Out of the box the app uses SQLite and an
  insecure-but-obvious dev key so a new contributor can run ``python run.py``
  with no setup. None of those defaults survive ``FLASK_ENV=production``.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Sentinel for the development secret key. Centralised so both the default and
# the production validation reference the exact same string (no drift).
_DEV_SECRET = "dev-insecure-key-change-me"

# Hard cap on request bodies. CSV uploads are the only large input the app
# accepts; 16 MB comfortably covers years of deals while bounding the memory a
# single malicious or accidental upload can consume.
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on missing/garbage input.

    Configuration must never crash the process on a typo; an out-of-range or
    non-numeric value degrades to the documented default with the error visible
    only at boot, not at request time.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


class Config:
    """Base configuration shared across environments."""

    ENV = os.getenv("FLASK_ENV", "development")
    SECRET_KEY = os.getenv("SECRET_KEY", _DEV_SECRET)

    # Database — default to a local SQLite file inside the instance folder.
    _default_db = f"sqlite:///{(BASE_DIR / 'instance' / 'intelligence.db').as_posix()}"
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", _default_db)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # ``pool_pre_ping`` transparently recycles connections a database server may
    # have dropped (idle timeouts, restarts), so the first request after an
    # outage recovers instead of erroring with a stale-connection exception.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Server
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = _int("PORT", 5000)

    # Reject oversized request bodies before they are buffered into memory.
    MAX_CONTENT_LENGTH = _MAX_UPLOAD_BYTES

    # Trust N hops of reverse-proxy forwarding headers (X-Forwarded-For/-Proto).
    # 0 (the default) means "no proxy" — required so client IPs used for rate
    # limiting cannot be spoofed when the app is exposed directly.
    PROXY_FIX_HOPS = _int("PROXY_FIX_HOPS", 0)

    # Features
    ENABLE_SCHEDULER = _bool("ENABLE_SCHEDULER", True)

    # Werkzeug's debug reloader watches every imported module's file, including
    # third-party packages. A request that lazily imports a big package for the
    # first time (e.g. Playwright, for PDF export) can make the reloader think
    # source changed and restart mid-request, orphaning Playwright's Node
    # subprocess (a loud but harmless EPIPE crash in the console). Off by
    # default — this app is normally just *run*, not actively edited while
    # running; opt in with FLASK_RELOAD=1 when iterating on templates/code.
    USE_RELOADER = _bool("FLASK_RELOAD", False)

    # Inc42 history workbook used by the "Import history" button. Defaults to the
    # master file shipped alongside / above the app; override via env if needed.
    INC42_MASTER_PATH = os.getenv("INC42_MASTER_PATH", "")

    # Output: generated PDFs / exports
    EXPORT_DIR = BASE_DIR / "instance" / "exports"

    # Public static site (weekly + monthly only). When ``PUBLISH_ON_GENERATE`` is
    # on, every weekly/monthly report is written into ``PUBLISH_DIR`` as it is
    # generated — together with the neighbour pager links, the archive index and
    # the home page — so the published site always tracks the latest data.
    PUBLISH_DIR = Path(os.getenv("PUBLISH_DIR") or (BASE_DIR / "24IB-Private-Market-Research"))
    PUBLISH_ON_GENERATE = _bool("PUBLISH_ON_GENERATE", True)

    # ── Security: session & cookie hardening ──
    # HttpOnly keeps the session cookie out of reach of JavaScript (mitigates
    # token theft via XSS). SameSite=Lax blocks the cookie on cross-site POSTs,
    # a defence-in-depth layer alongside CSRF tokens. ``Secure`` is enabled in
    # production by :meth:`validate` so the cookie is never sent over plain HTTP.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    WTF_CSRF_TIME_LIMIT = None

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @staticmethod
    def validate(app) -> None:
        """Fail fast if production is misconfigured.

        Raises ``RuntimeError`` on any condition that would ship a production
        deployment with a known-weak security posture. Warnings (not errors) are
        used for choices that are merely sub-optimal (SQLite at scale).
        """
        if app.config["ENV"] != "production":
            return

        secret = app.config.get("SECRET_KEY")
        if secret in {_DEV_SECRET, "", None}:
            raise RuntimeError("SECRET_KEY must be set to a strong, unique value in production.")
        if len(str(secret)) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters in production.")

        # Production is assumed to terminate TLS; ensure cookies are HTTPS-only.
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["REMEMBER_COOKIE_SECURE"] = True

        if not 0 < int(app.config["PORT"]) < 65536:
            raise RuntimeError(f"PORT out of range: {app.config['PORT']!r}")

        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
            app.logger.warning("Running production on SQLite — use Postgres for real workloads.")
