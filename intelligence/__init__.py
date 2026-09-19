"""Application factory for the 24IB Intelligence Platform.

This module wires the Flask app together: configuration, extensions, blueprints,
template helpers, security headers, error handling, the CLI, and the in-process
scheduler. It is the single composition root — nothing else imports the concrete
``Flask`` instance, which keeps the app testable (each test builds its own).
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from config import Config
from .clock import utcnow
from .extensions import db

# Content-Security-Policy applied to every response. Scripts and styles are
# inlined throughout the server-rendered templates, so 'unsafe-inline' is
# unavoidable here; the report's DOM-injection XSS risk is instead neutralised at
# the source (values are HTML-escaped before being written via innerHTML). The
# policy still meaningfully constrains the blast radius: it pins framing to the
# same origin (clickjacking), forbids plugins/objects, locks <base>, and limits
# remote origins to the specific font/icon CDNs the UI actually uses.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
    "font-src 'self' data: https://fonts.gstatic.com https://unpkg.com; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    # Ensure instance + export dirs exist. Failing to create the instance dir is
    # fatal (the SQLite DB and Excel master live there), so this is intentionally
    # not guarded — a broken filesystem should surface at boot, not mid-request.
    app.config["EXPORT_DIR"].mkdir(parents=True, exist_ok=True)

    _configure_logging(app)
    _apply_proxy_fix(app)

    # Init extensions.
    db.init_app(app)

    # Blueprints.
    from .dashboard.routes import bp as dashboard_bp
    from .reports.routes import bp as reports_bp
    from .data.routes import bp as data_bp
    from .api.routes import bp as api_bp
    from .explore.routes import bp as explore_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(explore_bp)

    _register_template_helpers(app)
    _register_security_headers(app)
    _register_error_handlers(app)
    _register_cli(app)

    Config.validate(app)

    # Create tables on first boot (idempotent), then ensure the single
    # workspace exists — no demo data is seeded beyond that; content arrives
    # via the Inc42 import / refresh buttons or CSV upload.
    with app.app_context():
        db.create_all()
        _ensure_schema(app)
        from .tenancy import ensure_workspace
        ensure_workspace()

    # Scheduler (in-process). Guarded so the reloader doesn't double-start it.
    if app.config["ENABLE_SCHEDULER"]:
        from .scheduler import start_scheduler
        start_scheduler(app)

    app.logger.info("24IB Intelligence Platform ready.")
    return app


def _ensure_schema(app: Flask) -> None:
    """Add columns introduced after a DB was first created (lightweight migration).

    ``db.create_all()`` never alters existing tables, so newly-added model columns
    must be back-filled onto an already-created database. Adding a column with a
    default is always safe to (re-)run on every boot; it never touches existing
    data, so — unlike a data backfill (e.g. flipping ``is_published`` on old
    rows) — it belongs here rather than in a one-off script.
    """
    from sqlalchemy import inspect, text

    # table -> {column name -> SQL type} (kept minimal; matches the models).
    wanted = {
        "funding_deals": {
            "subsector": "VARCHAR(160)",
            "business_model": "VARCHAR(60)",
            "funding_round_size": "VARCHAR(80)",
        },
        "reports": {
            "notes": "TEXT",
        },
    }
    try:
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())
        for table, columns in wanted.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, sqltype in columns.items():
                if name not in existing:
                    db.session.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype} DEFAULT ''")
                    )
                    app.logger.info("Schema: added %s.%s", table, name)
        db.session.commit()
    except Exception as exc:  # pragma: no cover - never block boot on migration
        db.session.rollback()
        app.logger.warning("Schema ensure skipped: %s", exc)


def _configure_logging(app: Flask) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))
    app.logger.handlers = [handler]
    app.logger.setLevel(logging.INFO if not app.debug else logging.DEBUG)


def _apply_proxy_fix(app: Flask) -> None:
    """Honour reverse-proxy forwarding headers when deployed behind one.

    Without this, ``request.remote_addr`` is the proxy's IP (defeating per-client
    rate limiting) and ``request.scheme`` is ``http`` (defeating Secure-cookie
    and HTTPS-redirect logic). ``PROXY_FIX_HOPS`` is opt-in and counts the number
    of trusted proxies, because blindly trusting ``X-Forwarded-For`` on a
    directly-exposed app would let any client spoof its address.
    """
    hops = int(app.config.get("PROXY_FIX_HOPS", 0) or 0)
    if hops <= 0:
        return
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)


def _register_security_headers(app: Flask) -> None:
    """Attach hardening headers to every response.

    These defend against MIME-sniffing (``X-Content-Type-Options``), clickjacking
    (``X-Frame-Options`` plus the CSP ``frame-ancestors``), referrer leakage, and
    over-broad resource loading (CSP). They are cheap and universal, so they are
    applied app-wide rather than per-route.
    """

    @app.after_request
    def _set_headers(resp):  # noqa: WPS430
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        if app.config.get("SESSION_COOKIE_SECURE"):
            # Signal to browsers/proxies that the site is HTTPS-only. Only sent
            # in production (where Secure cookies are active) to avoid pinning
            # localhost to HTTPS during development.
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return resp


def _register_template_helpers(app: Flask) -> None:
    from .models import PeriodType

    @app.template_filter("ret")
    def ret_filter(val):
        """Format a signed return with sign + 2dp, or em-dash for None."""
        if val is None:
            return "—"
        sign = "+" if val > 0 else ""
        return f"{sign}{val:.2f}%"

    @app.template_filter("retclass")
    def retclass_filter(val):
        if val is None:
            return "val-flat"
        return "val-pos" if val > 0 else ("val-neg" if val < 0 else "val-flat")

    @app.template_filter("num")
    def num_filter(val, places=0):
        if val is None:
            return "—"
        return f"{val:,.{places}f}"

    @app.context_processor
    def inject_globals():
        from .tenancy import require_org
        return {
            "now": utcnow(),
            "PeriodType": PeriodType,
            "brand": "24IB Intelligence",
            "current_org": require_org(),
        }


def _register_error_handlers(app: Flask) -> None:
    """Centralised error handling with content negotiation.

    API clients (``/api/...``) always receive JSON with a machine-readable
    ``error`` field and never an HTML error page, while browser users get the
    styled error templates. Crucially, 500 responses never leak the underlying
    exception — the detail is logged server-side and the user sees a generic
    message — so internal structure (stack traces, SQL) is not exposed.
    """

    def _wants_json() -> bool:
        return request.path.startswith("/api/")

    def _respond(html_template: str, status: int, message: str):
        if _wants_json():
            return jsonify(error=message), status
        return render_template(html_template, error=message), status

    @app.errorhandler(400)
    def bad_request(_e):
        return _respond("errors/404.html", 400, "Bad request.")

    @app.errorhandler(403)
    def forbidden(_e):
        return _respond("errors/403.html", 403, "You do not have access to this resource.")

    @app.errorhandler(404)
    def not_found(_e):
        return _respond("errors/404.html", 404, "Not found.")

    @app.errorhandler(413)
    def too_large(_e):
        return _respond("errors/404.html", 413, "The uploaded file is too large.")

    @app.errorhandler(429)
    def too_many(_e):
        return _respond("errors/403.html", 429, "Too many requests — please slow down.")

    @app.errorhandler(500)
    def server_error(_e):  # pragma: no cover
        # Roll back any half-applied transaction so the next request starts from
        # a clean session rather than inheriting a poisoned/aborted one.
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.exception("Unhandled error")
        return _respond("errors/500.html", 500, "An unexpected error occurred.")


def _register_cli(app: Flask) -> None:
    import click

    from .engine import generate_report
    from .models import Organization

    @app.cli.command("generate")
    @click.argument("period_type")
    @click.option("--key", default=None, help="Period key, e.g. 2026-W14 / 2026-05 / 2026-Q1 / 2026")
    @click.option("--org", default=1, type=int)
    def generate_cmd(period_type, key, org):  # pragma: no cover
        """Generate a report from the CLI: flask generate weekly --key 2026-W14"""
        org_row = db.session.get(Organization, org)
        if not org_row:
            click.echo(f"No organization with id {org}.")
            return
        report = generate_report(org, period_type, period_key=key)
        click.echo(f"Generated report #{report.id}: {report.title} [{report.status.value}]")
