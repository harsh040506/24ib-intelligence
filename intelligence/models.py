"""Database models for the 24IB Intelligence Platform.

Design notes
------------
* Single workspace: every business record hangs off the one ``Organization``
  row (see ``tenancy.py``) — there are no accounts or per-user permissions.
* Companies and investors are free text on ``FundingDeal`` (``company_name``,
  ``investors_raw``, ``lead_investor``) rather than normalised entity tables —
  ``explore/routes.py`` groups by these fields directly. ``Sector`` is the one
  first-class taxonomy table (not a hardcoded dict), with canonical name +
  alias matching.
* Deduplication: ``FundingDeal.dedup_hash`` is a deterministic hash of
  (company, date, amount) used to keep the ledger append-only but idempotent.
* Reports are persisted with their fully-computed JSON payload, so a report is
  reproducible and cheap to re-render (and shareable by token).
"""
from __future__ import annotations

import enum
import hashlib
import json
import secrets
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from .clock import utcnow as _utcnow  # naive UTC; see intelligence.clock
from .extensions import db


# ───────────────────────── helpers ─────────────────────────


def _token(n: int = 24) -> str:
    """Generate a URL-safe, cryptographically-random token.

    Used for report share tokens and API key material, so ``secrets`` (not
    ``random``) is mandatory — these values are unguessable bearer secrets.
    """
    return secrets.token_urlsafe(n)


# ───────────────────────── enums ─────────────────────────

class PeriodType(str, enum.Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class ReportStatus(str, enum.Enum):
    PENDING = "pending"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


# ───────────────────────── identity & tenancy ─────────────────────────

class Organization(db.Model):
    """The workspace. There is always exactly one row — see ``tenancy.py``."""
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    deals: Mapped[list["FundingDeal"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    reports: Mapped[list["Report"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="organization", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Organization {self.slug}>"


class ApiKey(db.Model):
    """A bearer credential for the REST API (``/api/v1/...``).

    This is the one deliberate exception to the app's "no auth" posture: the
    web UI has no login wall, but the REST API is opt-in and machine-facing
    (the user's own scripts/integrations), so a lightweight bearer key guards
    it from being hit by anything on the network without the user's consent.
    Only a hash of the secret is ever stored — see :meth:`issue`/:meth:`verify`.
    """
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="default")
    prefix: Mapped[str] = mapped_column(String(12), index=True)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    organization: Mapped["Organization"] = relationship(back_populates="api_keys")

    @classmethod
    def issue(cls, org_id: int, name: str = "default") -> tuple["ApiKey", str]:
        """Mint a new API key, returning ``(unsaved record, raw secret)``.

        Only a salted *hash* of the key is persisted; the raw value is returned
        exactly once for the caller to show the user and is never recoverable
        afterwards. The first 10 chars are stored separately as an indexed,
        non-secret ``prefix`` so lookups don't require hashing every stored key.
        The caller is responsible for adding and committing the record.
        """
        raw = "ib_" + _token(24)
        rec = cls(
            organization_id=org_id,
            name=name,
            prefix=raw[:10],
            key_hash=generate_password_hash(raw),
        )
        return rec, raw

    def verify(self, raw: str) -> bool:
        """Constant-time check that ``raw`` matches this (non-revoked) key.

        Revocation is checked first so a revoked key fails fast and immutably,
        regardless of whether the secret is otherwise correct.
        """
        return not self.revoked and check_password_hash(self.key_hash, raw)


# ───────────────────────── domain entities ─────────────────────────

class Sector(db.Model):
    """Canonical sector taxonomy. Aliases are stored as a JSON list of strings."""
    __tablename__ = "sectors"
    __table_args__ = (UniqueConstraint("organization_id", "canonical", name="uq_sector"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    canonical: Mapped[str] = mapped_column(String(120), nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    email_group: Mapped[str] = mapped_column(String(120), default="General")
    is_impact: Mapped[bool] = mapped_column(Boolean, default=False)
    accent: Mapped[str] = mapped_column(String(9), default="#1b4a36")

    @property
    def aliases(self) -> list[str]:
        return json.loads(self.aliases_json or "[]")

    @aliases.setter
    def aliases(self, vals: list[str]) -> None:
        self.aliases_json = json.dumps(vals)


class FundingDeal(db.Model):
    """A single funding transaction. Append-only, deduplicated by ``dedup_hash``."""
    __tablename__ = "funding_deals"
    __table_args__ = (UniqueConstraint("organization_id", "dedup_hash", name="uq_deal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)

    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sector_canonical: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    sector_raw: Mapped[str] = mapped_column(String(160), default="")
    # Inc42 master columns mirrored 1:1 so the DB is the source of truth for the workbook.
    subsector: Mapped[str] = mapped_column(String(160), default="")
    business_model: Mapped[str] = mapped_column(String(60), default="")  # B2B / B2C / …
    funding_round_size: Mapped[str] = mapped_column(String(80), default="")  # original string, e.g. "$15 Mn"
    amount_usd_mn: Mapped[float] = mapped_column(Float, default=0.0)
    round_type: Mapped[str] = mapped_column(String(80), default="")
    deal_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    investors_raw: Mapped[str] = mapped_column(Text, default="")
    lead_investor: Mapped[str] = mapped_column(String(200), default="")
    source_url: Mapped[str] = mapped_column(String(500), default="")
    dedup_hash: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="deals")

    @staticmethod
    def make_hash(company: str, deal_date: date, amount: float) -> str:
        """Deterministic dedup key over (company, date, amount).

        Normalises company name (trim + lowercase) and pins the amount to 2dp so
        the same logical deal arriving from different sources (CSV, scrape,
        backfill) hashes identically and is skipped by the unique constraint.
        This is a dedup fingerprint, not a security hash — SHA-1 is chosen purely
        for speed and stability, and collision resistance is irrelevant here.
        """
        key = f"{company.strip().lower()}|{deal_date.isoformat()}|{amount:.2f}"
        return hashlib.sha1(key.encode()).hexdigest()[:40]

    @property
    def investor_list(self) -> list[str]:
        return [x.strip() for x in self.investors_raw.split(",") if x.strip()]


# ───────────────────────── reports & automation ─────────────────────────

class Report(db.Model):
    """A generated intelligence report for one period (e.g. week 14 of 2026).

    The fully-computed payload (see ``engine.aggregate.build_payload``) is
    persisted as JSON on this row, not recomputed on every view — a report is
    therefore reproducible, cheap to re-render, and safe to share by token even
    if the underlying deal data later changes (a regenerate is an explicit,
    separate action). ``status``/``error`` capture the outcome of the
    generation job so a failed build can surface a clear reason in the UI
    instead of a blank page. ``is_published`` gates inclusion in the public
    static site (see ``publish.py``) independently of ``status`` — a report can
    be READY but intentionally kept off the public site.
    """
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    period_type: Mapped[PeriodType] = mapped_column(Enum(PeriodType), nullable=False)
    period_key: Mapped[str] = mapped_column(String(40), nullable=False)  # e.g. 2026-W14, 2026-05, 2026-Q1, 2026
    title: Mapped[str] = mapped_column(String(240), default="")
    edition_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ReportStatus] = mapped_column(Enum(ReportStatus), default=ReportStatus.PENDING)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")  # see `payload` property — the full computed report
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # set only when status == FAILED
    # Unguessable token for the public share link (`/r/<token>`); independent of
    # publish state so a private draft can still be shared 1:1 with someone who
    # has the link, without appearing in the public site's indexes.
    share_token: Mapped[str] = mapped_column(String(48), default=_token, unique=True, index=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")  # free-form personal notes, not shown on the public page
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # set when status becomes READY

    organization: Mapped["Organization"] = relationship(back_populates="reports")

    @property
    def payload(self) -> dict:
        """Decode the stored JSON payload back into the report's data dict."""
        return json.loads(self.payload_json or "{}")

    @payload.setter
    def payload(self, data: dict) -> None:
        """Serialise ``data`` to JSON for storage.

        ``default=str`` lets ``date``/``datetime`` values inside the payload
        (produced by ``aggregate.build_payload``) serialise without callers
        having to hand-roll a JSON encoder for every report field.
        """
        self.payload_json = json.dumps(data, default=str)


class ReportSchedule(db.Model):
    """An automated-generation cron rule, dispatched by ``scheduler.py``.

    Not currently exposed anywhere in the UI/API that creates rows here — the
    table and dispatch logic exist so the feature can be wired up later
    without a schema change, but as of this app's current scope every report
    is generated on demand (UI button, CLI, or API call), not on a timer.
    """
    __tablename__ = "report_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    period_type: Mapped[PeriodType] = mapped_column(Enum(PeriodType), nullable=False)
    cron: Mapped[str] = mapped_column(String(80), default="0 8 * * MON")  # default: Mon 08:00
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    recipients: Mapped[str] = mapped_column(Text, default="")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class DedupIgnore(db.Model):
    """A fuzzy-duplicate pair the user has dismissed as not actually a duplicate.

    Keyed by the pair's two ``FundingDeal.dedup_hash`` values (not row ids —
    stable even if the rows are later edited) so a dismissed pair never
    resurfaces in the review queue.
    """
    __tablename__ = "dedup_ignores"
    __table_args__ = (UniqueConstraint("organization_id", "hash_a", "hash_b", name="uq_dedup_ignore"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    hash_a: Mapped[str] = mapped_column(String(40), nullable=False)
    hash_b: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Favorite(db.Model):
    """A pinned report / company / sector / investor for quick navigation."""
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("organization_id", "kind", "ref", name="uq_favorite"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # report | company | sector | investor
    ref: Mapped[str] = mapped_column(String(240), nullable=False)  # report id (as str) or entity name
    label: Mapped[str] = mapped_column(String(240), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class JobRun(db.Model):
    """Observability: a record of every ingestion / generation job."""
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)  # ingest_csv, generate_report, scheduled
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.QUEUED)
    detail: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def append_log(self, line: str) -> None:
        stamp = _utcnow().strftime("%H:%M:%S")
        self.log = (self.log or "") + f"[{stamp}] {line}\n"


# expose a list of all models for tooling / introspection
__all__ = [
    "Organization", "ApiKey",
    "Sector", "FundingDeal",
    "Report", "ReportSchedule", "JobRun", "Favorite", "DedupIgnore",
    "PeriodType", "ReportStatus", "RunStatus",
]
