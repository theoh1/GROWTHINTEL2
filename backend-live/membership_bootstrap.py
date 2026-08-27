"""Server-authoritative Growth Intel bank-transfer memberships.

Membership data uses the backend's configured SQLAlchemy database. SQLite is
available only when explicitly enabled for local development/tests; production
must use the persistent DATABASE_URL already supported by the backend.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parseaddr
from html import escape

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

PRICE_PENCE = 1300
PRICE_CURRENCY = "GBP"
SESSION_COOKIE = "gi_session"
SESSION_SECONDS = 60 * 60 * 24 * 14
PUBLIC_PATHS = {"/api/v1/health", "/api/v1/status"}
PREMIUM_PLAN_VERSION = "premium_v1"
PREMIUM_FEATURE_IDS = (
    "home",
    "canslim",
    "engine",
    "early-view",
    "breakouts",
    "market",
    "news",
    "portfolio",
    "smart-money",
    "creator-intel",
    "upcoming",
    "pennie",
    "alerts",
    "learn",
    "ai-assistant",
    "settings",
)

metadata = MetaData()

users = Table(
    "membership_users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("name", String(100), nullable=False),
    Column("password_hash", String(256), nullable=False),
    Column("gi_reference", String(8), nullable=False, unique=True),
    Column("membership_state", String(24), nullable=False, default="NONE"),
    Column("membership_expires_at", BigInteger),
    Column("membership_started_at", BigInteger),
    Column("membership_last_verified_at", BigInteger),
    Column("membership_cancel_at_period_end", Integer, nullable=False, default=0),
    Column("plan_version", String(32)),
    Column("affiliate_referral_code", String(64)),
    Column("affiliate_referral_source", String(64)),
    Column("affiliate_referral_landing_url", String(500)),
    Column("affiliate_referral_captured_at", BigInteger),
    Column("created_at", BigInteger, nullable=False),
    CheckConstraint(
        "membership_state IN ('NONE','PENDING_PAYMENT','ACTIVE','EXPIRED')",
        name="ck_membership_state",
    ),
)

sessions = Table(
    "membership_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("membership_users.id", ondelete="CASCADE"), nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)

payments = Table(
    "membership_payment_requests",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("membership_users.id"), nullable=False),
    Column("amount_pence", Integer, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(16), nullable=False),
    Column("pending_guard", String(16)),
    Column("created_at", BigInteger, nullable=False),
    Column("reviewed_at", BigInteger),
    Column("reviewed_by", Integer, ForeignKey("membership_users.id")),
    Column("admin_note", String(500)),
    Column("affiliate_referral_code", String(64)),
    UniqueConstraint("user_id", "pending_guard", name="uq_membership_pending"),
    CheckConstraint("status IN ('PENDING','APPROVED','REJECTED')", name="ck_membership_payment_status"),
)

audits = Table(
    "membership_audit_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("actor_user_id", Integer, ForeignKey("membership_users.id")),
    Column("action", String(64), nullable=False),
    Column("target_user_id", Integer, ForeignKey("membership_users.id")),
    Column("payment_id", Integer),
    Column("detail", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("ip_address", String(64)),
)

entitlements = Table(
    "membership_entitlements",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("membership_users.id"), nullable=False),
    Column("feature_id", String(80), nullable=False),
    Column("plan_version", String(32), nullable=False),
    Column("granted_at", BigInteger, nullable=False),
    Column("revoked_at", BigInteger),
    UniqueConstraint("user_id", "feature_id", "plan_version", name="uq_membership_entitlement_feature"),
)

integrity_snapshots = Table(
    "membership_integrity_snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("total_users", Integer, nullable=False),
    Column("active_premium_users", Integer, nullable=False),
    Column("pending_users", Integer, nullable=False),
    Column("expired_users", Integer, nullable=False),
    Column("last_membership_event_at", BigInteger),
    Column("status", String(24), nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)

support_tickets = Table(
    "support_tickets",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ticket_ref", String(16), nullable=False, unique=True),
    Column("user_id", Integer, ForeignKey("membership_users.id")),
    Column("customer_email", String(254), nullable=False),
    Column("customer_name", String(100)),
    Column("account_plan", String(32)),
    Column("membership_state", String(24)),
    Column("current_page", String(500)),
    Column("category", String(64), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("original_problem", Text, nullable=False),
    Column("ai_troubleshooting", Text, nullable=False),
    Column("ai_summary", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("assigned_status", String(32), nullable=False),
    Column("notification_status", String(16), nullable=False),
    Column("notification_attempts", Integer, nullable=False, default=0),
    Column("notification_error", Text),
    Column("sms_sent_at", BigInteger),
    Column("customer_access_hash", String(64)),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("customer_last_reply_at", BigInteger),
    Column("admin_last_reply_at", BigInteger),
    CheckConstraint(
        "status IN ('OPEN','AI_HANDLING','HUMAN_REQUESTED','HUMAN_REVIEWING','WAITING_FOR_CUSTOMER','RESOLVED','CLOSED')",
        name="ck_support_ticket_status",
    ),
    CheckConstraint(
        "priority IN ('Critical','High','Medium','Low')",
        name="ck_support_ticket_priority",
    ),
    CheckConstraint(
        "notification_status IN ('PENDING','SENT','FAILED','RETRYING','NOT_CONFIGURED')",
        name="ck_support_notification_status",
    ),
)

support_messages = Table(
    "support_messages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ticket_id", Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False),
    Column("sender", String(24), nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    CheckConstraint("sender IN ('customer','ai','admin','system')", name="ck_support_message_sender"),
)

support_notification_events = Table(
    "support_notification_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ticket_id", Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False),
    Column("channel", String(24), nullable=False),
    Column("status", String(16), nullable=False),
    Column("provider", String(40)),
    Column("error", Text),
    Column("created_at", BigInteger, nullable=False),
)

email_subscribers = Table(
    "email_subscribers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("status", String(24), nullable=False, default="subscribed"),
    Column("preferences", Text, nullable=False, default="{}"),
    Column("signup_source", String(80), nullable=False),
    Column("consented_at", BigInteger, nullable=False),
    Column("unsubscribed_at", BigInteger),
    Column("referral_code", String(24), nullable=False, unique=True),
    Column("referring_code", String(24)),
    Column("management_token_hash", String(64), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    CheckConstraint("status IN ('subscribed','unsubscribed')", name="ck_email_subscriber_status"),
)

email_referrals = Table(
    "email_referrals",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("referrer_id", Integer, ForeignKey("email_subscribers.id", ondelete="CASCADE"), nullable=False),
    Column("subscriber_id", Integer, ForeignKey("email_subscribers.id", ondelete="CASCADE"), nullable=False),
    Column("referral_code", String(24), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("subscriber_id", name="uq_email_referral_subscriber"),
)

email_delivery_logs = Table(
    "email_delivery_logs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("subscriber_id", Integer, ForeignKey("email_subscribers.id", ondelete="SET NULL")),
    Column("recipient_email", String(254), nullable=False),
    Column("email_type", String(64), nullable=False),
    Column("subject", String(180), nullable=False),
    Column("provider", String(40), nullable=False),
    Column("status", String(24), nullable=False),
    Column("provider_message_id", String(120)),
    Column("error", Text),
    Column("metadata_json", Text, nullable=False, default="{}"),
    Column("created_at", BigInteger, nullable=False),
)

Index("ix_membership_payment_user_created", payments.c.user_id, payments.c.created_at)
Index("ix_membership_user_affiliate_referral", users.c.affiliate_referral_code)
Index("ix_membership_entitlements_user_feature", entitlements.c.user_id, entitlements.c.feature_id)
Index("ix_support_ticket_user_updated", support_tickets.c.user_id, support_tickets.c.updated_at)
Index("ix_support_ticket_email_updated", support_tickets.c.customer_email, support_tickets.c.updated_at)
Index("ix_support_ticket_status_updated", support_tickets.c.status, support_tickets.c.updated_at)
Index("ix_email_subscribers_status", email_subscribers.c.status)
Index("ix_email_subscribers_referring_code", email_subscribers.c.referring_code)
Index("ix_email_delivery_logs_subscriber_type", email_delivery_logs.c.subscriber_id, email_delivery_logs.c.email_type)

_lock = threading.RLock()
_attempts: dict[str, deque[float]] = defaultdict(deque)
_engine = None


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _now() -> int:
    return int(time.time())


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def _configure_storage():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        if _truthy("MEMBERSHIP_ALLOW_SQLITE"):
            try:
                from app.db.session import active_database_url

                database_url = active_database_url
            except Exception:
                database_url = ""
        if not database_url:
            raise RuntimeError(
                "Persistent membership storage is not configured. Set DATABASE_URL to a reachable PostgreSQL database. "
                "Set MEMBERSHIP_ALLOW_SQLITE=true only for local development or tests."
            )

    normalized_url = _normalize_database_url(database_url)
    sqlite = normalized_url.startswith("sqlite")
    if sqlite and not _truthy("MEMBERSHIP_ALLOW_SQLITE"):
        raise RuntimeError(
            "Persistent membership storage is not configured. Set DATABASE_URL to a reachable PostgreSQL database. "
            "Set MEMBERSHIP_ALLOW_SQLITE=true only for local development or tests."
        )

    kwargs = {"future": True, "pool_pre_ping": True}
    if sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(normalized_url, **kwargs)
    metadata.create_all(engine)
    _ensure_membership_columns(engine, sqlite)
    return engine


def _ensure_membership_columns(engine, sqlite: bool) -> None:
    """Add membership columns for existing installs without destructive migrations."""
    statements = (
        ("membership_users", "affiliate_referral_code", "VARCHAR(64)"),
        ("membership_users", "affiliate_referral_source", "VARCHAR(64)"),
        ("membership_users", "affiliate_referral_landing_url", "VARCHAR(500)"),
        ("membership_users", "affiliate_referral_captured_at", "BIGINT" if not sqlite else "INTEGER"),
        ("membership_users", "membership_started_at", "BIGINT" if not sqlite else "INTEGER"),
        ("membership_users", "membership_last_verified_at", "BIGINT" if not sqlite else "INTEGER"),
        ("membership_users", "membership_cancel_at_period_end", "INTEGER DEFAULT 0"),
        ("membership_users", "plan_version", "VARCHAR(32)"),
        ("membership_payment_requests", "affiliate_referral_code", "VARCHAR(64)"),
    )
    with engine.begin() as connection:
        for table_name, column_name, column_type in statements:
            if sqlite:
                existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}
                if column_name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            else:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                )
        if not sqlite:
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_membership_user_affiliate_referral "
                "ON membership_users (affiliate_referral_code)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_membership_entitlements_user_feature "
                "ON membership_entitlements (user_id, feature_id)"
            )


def _validate_configuration() -> None:
    required = (
        "MEMBERSHIP_ADMIN_EMAILS",
        "MEMBERSHIP_ALLOWED_ORIGINS",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing required membership configuration: " + ", ".join(missing))

    admins = [value.strip() for value in os.environ["MEMBERSHIP_ADMIN_EMAILS"].split(",") if value.strip()]
    if any(parseaddr(value)[1] != value or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) for value in admins):
        raise RuntimeError("MEMBERSHIP_ADMIN_EMAILS contains an invalid email address")

    origins = [value.strip().rstrip("/") for value in os.environ["MEMBERSHIP_ALLOWED_ORIGINS"].split(",") if value.strip()]
    if not _truthy("MEMBERSHIP_ALLOW_SQLITE") and any(not value.startswith("https://") for value in origins):
        raise RuntimeError("Production MEMBERSHIP_ALLOWED_ORIGINS entries must use HTTPS")


def _password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify(password: str, encoded: str) -> bool:
    try:
        _, salt, _ = encoded.split("$", 2)
        return hmac.compare_digest(_password(password, bytes.fromhex(salt)), encoded)
    except (TypeError, ValueError):
        return False


def _reference(connection) -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    for _ in range(64):
        value = secrets.randbits(25)
        ref = "GI-" + "".join(alphabet[(value >> (5 * shift)) & 31] for shift in range(4, -1, -1))
        if connection.execute(select(users.c.id).where(users.c.gi_reference == ref)).first() is None:
            return ref
    raise RuntimeError("Unable to allocate membership reference")


def _add_calendar_month(timestamp: int) -> int:
    current = datetime.fromtimestamp(timestamp, timezone.utc)
    year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    day = min(current.day, calendar.monthrange(year, month)[1])
    return int(current.replace(year=year, month=month, day=day).timestamp())


def _audit(connection, action, actor=None, target=None, payment=None, detail=None, ip=None):
    connection.execute(
        audits.insert().values(
            actor_user_id=actor,
            action=action,
            target_user_id=target,
            payment_id=payment,
            detail=json.dumps(detail or {}, separators=(",", ":")),
            created_at=_now(),
            ip_address=ip,
        )
    )


def _database_unavailable():
    raise HTTPException(
        503,
        {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Membership database is temporarily unavailable. Customer accounts and Premium records have not been changed.",
            "safe_state": "Stored membership data remains the source of truth; retry once the database reconnects.",
        },
    )


@contextmanager
def _db_begin():
    try:
        with _engine.begin() as connection:
            yield connection
    except HTTPException:
        raise
    except IntegrityError:
        raise
    except (OperationalError, DBAPIError):
        _database_unavailable()


@contextmanager
def _db_connect():
    try:
        with _engine.connect() as connection:
            yield connection
    except HTTPException:
        raise
    except IntegrityError:
        raise
    except (OperationalError, DBAPIError):
        _database_unavailable()


def _json_error(error: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": error.detail}, status_code=error.status_code)


def _grant_premium_entitlements(connection, user_id: int, now: int) -> None:
    existing = set(
        connection.execute(
            select(entitlements.c.feature_id).where(
                and_(
                    entitlements.c.user_id == user_id,
                    entitlements.c.plan_version == PREMIUM_PLAN_VERSION,
                    entitlements.c.revoked_at.is_(None),
                )
            )
        ).scalars()
    )
    rows = [
        {"user_id": user_id, "feature_id": feature_id, "plan_version": PREMIUM_PLAN_VERSION, "granted_at": now}
        for feature_id in PREMIUM_FEATURE_IDS
        if feature_id not in existing
    ]
    if rows:
        connection.execute(entitlements.insert(), rows)
        _audit(
            connection,
            "PREMIUM_ENTITLEMENTS_GRANTED",
            target=user_id,
            detail={"plan_version": PREMIUM_PLAN_VERSION, "feature_count": len(rows)},
        )


def _membership_metrics(connection) -> dict:
    now = _now()
    total_users = int(connection.execute(select(func.count()).select_from(users)).scalar() or 0)
    active_premium = int(
        connection.execute(
            select(func.count()).select_from(users).where(
                and_(users.c.membership_state == "ACTIVE", users.c.membership_expires_at > now)
            )
        ).scalar()
        or 0
    )
    pending_users = int(
        connection.execute(
            select(func.count()).select_from(users).where(users.c.membership_state == "PENDING_PAYMENT")
        ).scalar()
        or 0
    )
    expired_users = int(
        connection.execute(
            select(func.count()).select_from(users).where(
                or_(users.c.membership_state == "EXPIRED", and_(users.c.membership_state == "ACTIVE", users.c.membership_expires_at <= now))
            )
        ).scalar()
        or 0
    )
    last_event_at = connection.execute(select(func.max(audits.c.created_at))).scalar()
    last_snapshot = connection.execute(
        select(integrity_snapshots).order_by(integrity_snapshots.c.created_at.desc()).limit(1)
    ).mappings().first()
    status = "HEALTHY"
    warnings = []
    if last_snapshot and int(last_snapshot["total_users"] or 0) > 0 and total_users == 0:
        status = "CRITICAL"
        warnings.append("Membership user count collapsed to zero compared with the previous integrity snapshot.")
    detail = {"warnings": warnings}
    connection.execute(
        integrity_snapshots.insert().values(
            total_users=total_users,
            active_premium_users=active_premium,
            pending_users=pending_users,
            expired_users=expired_users,
            last_membership_event_at=last_event_at,
            status=status,
            detail=json.dumps(detail, separators=(",", ":")),
            created_at=now,
        )
    )
    return {
        "status": status,
        "database": "reachable",
        "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "total_users": total_users,
        "active_premium_users": active_premium,
        "pending_users": pending_users,
        "expired_users": expired_users,
        "last_membership_event_at": datetime.fromtimestamp(last_event_at, timezone.utc).isoformat() if last_event_at else None,
        "backup": {
            "provider": os.environ.get("MEMBERSHIP_BACKUP_PROVIDER", "database-provider-native"),
            "status": os.environ.get("MEMBERSHIP_BACKUP_STATUS", "manual-check-required"),
            "last_successful_backup_at": os.environ.get("MEMBERSHIP_LAST_SUCCESSFUL_BACKUP_AT") or None,
        },
        "warnings": warnings,
    }


def _email(value) -> str:
    value = str(value or "").strip().lower()
    if len(value) > 254 or parseaddr(value)[1] != value or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(422, "Enter a valid email address")
    return value


def _name(value) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if not 2 <= len(value) <= 100:
        raise HTTPException(422, "Name must be 2-100 characters")
    return value


def _referral_code(value) -> str | None:
    value = str(value or "").strip().upper()
    if not value:
        return None
    value = re.sub(r"[^A-Z0-9_-]", "", value)[:64]
    return value or None


def _short_text(value, limit: int) -> str | None:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if not value:
        return None
    return value[:limit]


def _allowed_origins() -> set[str]:
    return {x.strip().rstrip("/") for x in os.environ.get("MEMBERSHIP_ALLOWED_ORIGINS", "").split(",") if x.strip()}


def _check_origin(request: Request) -> None:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin not in _allowed_origins():
        raise HTTPException(403, "Request origin is not allowed")


def _rate(request: Request, key: str, limit: int = 10, window: int = 60) -> None:
    address = request.client.host if request.client else "unknown"
    bucket = _attempts[f"{key}:{address}"]
    now = time.monotonic()
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, "Too many attempts; please try again later")
    bucket.append(now)


def _session_user(request: Request, connection):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    query = (
        select(users)
        .join(sessions, sessions.c.user_id == users.c.id)
        .where(
            and_(
                sessions.c.token_hash == hashlib.sha256(token.encode()).hexdigest(),
                sessions.c.expires_at > _now(),
            )
        )
    )
    return connection.execute(query).mappings().first()


def _require_user(request: Request, connection):
    user = _session_user(request, connection)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def _is_admin(user) -> bool:
    allowed = {x.strip().lower() for x in os.environ.get("MEMBERSHIP_ADMIN_EMAILS", "").split(",") if x.strip()}
    return bool(user and user["email"].lower() in allowed)


def _public_user(user):
    state, expiry = user["membership_state"], user["membership_expires_at"]
    if state == "ACTIVE" and expiry and expiry <= _now():
        state = "EXPIRED"
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "gi_reference": user["gi_reference"],
        "membership_state": state,
        "membership_expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat() if expiry else None,
        "membership_cancel_at_period_end": bool(user.get("membership_cancel_at_period_end")) if hasattr(user, "get") else bool(user["membership_cancel_at_period_end"]),
        "plan_version": user.get("plan_version") if hasattr(user, "get") else user["plan_version"],
        "affiliate_referral_code": user.get("affiliate_referral_code") if hasattr(user, "get") else user["affiliate_referral_code"],
        "is_admin": _is_admin(user),
    }


async def _json(request: Request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "A JSON request body is required")


def _redact_sensitive(value: str, limit: int = 6000) -> str:
    text = str(value or "")[:limit]
    text = re.sub(r"(?i)(password|passcode|api[_ -]?key|secret|token)\s*[:=]\s*\S+", r"\1: [redacted]", text)
    text = re.sub(r"\b(?:\d[ -]*?){13,19}\b", "[redacted card number]", text)
    text = re.sub(r"\b\d{3,4}\b(?=\s*(?:cvv|cvc|security code)\b)", "[redacted]", text, flags=re.I)
    return text.strip()


def _ticket_ref(connection) -> str:
    for _ in range(64):
        ref = "GI-" + str(secrets.randbelow(900000) + 100000)
        if connection.execute(select(support_tickets.c.id).where(support_tickets.c.ticket_ref == ref)).first() is None:
            return ref
    raise RuntimeError("Unable to allocate support ticket reference")


def _support_category(message: str) -> str:
    text = message.lower()
    categories = [
        ("account", ("sign in", "login", "password", "account", "create account", "email")),
        ("premium_access", ("premium", "membership", "subscription", "access", "approved", "expired")),
        ("payment", ("payment", "bank", "transfer", "reference", "gi-", "paid", "refund")),
        ("stale_data", ("stale", "old data", "missing data", "not loading", "refresh", "backend", "live data")),
        ("canslim", ("canslim", "screener", "score")),
        ("engine", ("engine", "ai engine", "setup")),
        ("market", ("market", "index", "vix", "nasdaq", "s&p")),
        ("creator_intel", ("creator", "youtube", "video")),
        ("upcoming", ("upcoming", "ipo")),
        ("pennie", ("pennie", "penny")),
        ("alerts", ("alert", "notification")),
        ("portfolio", ("portfolio", "holding")),
        ("smart_money", ("smart money", "institution", "insider")),
        ("learn", ("learn", "lesson", "video library")),
    ]
    for category, needles in categories:
        if any(needle in text for needle in needles):
            return category
    return "general"


def _support_priority(message: str, category: str, membership_state: str | None = None) -> str:
    text = message.lower()
    if any(term in text for term in ("paid but", "cannot access premium", "locked out", "charged", "approved but")):
        return "High"
    if category in {"payment", "premium_access"} and membership_state == "ACTIVE":
        return "High"
    if any(term in text for term in ("data down", "nothing loads", "application error", "client-side exception")):
        return "High"
    if category in {"stale_data", "account"}:
        return "Medium"
    return "Low"


def _support_ai_answer(message: str, user=None, health: dict | None = None) -> dict:
    clean = _redact_sensitive(message, 3000)
    category = _support_category(clean)
    membership_state = user["membership_state"] if user else None
    priority = _support_priority(clean, category, membership_state)
    docs_url = "https://www.growthintel.app/docs"
    opening = "I’ll help you check this step by step."
    if category == "account":
        steps = [
            "Use the same email address you created the GrowthIntel account with.",
            "Check the password carefully, including capital letters.",
            "If the browser looks stuck, open GrowthIntel in a private window and try signing in again.",
            "Never send your password in support chat.",
        ]
    elif category == "premium_access":
        state = membership_state or "unknown"
        steps = [
            f"Your visible membership state is {state}; I will not guess payment approval beyond what the account system returns.",
            "If you recently paid, Premium only activates after the admin payment request is approved.",
            "Refresh the page after approval, then sign out and sign in again if the old screen remains.",
            "If Premium still stays locked, request human support so the payment reference can be checked.",
        ]
    elif category == "payment":
        steps = [
            "Use your personal GI reference exactly as shown on the membership page.",
            "Do not share full banking screenshots or sensitive payment details in chat.",
            "After sending payment, click the payment-sent button once so the request can be reviewed.",
            "If you paid but no request appears, request human support with the GI reference and payment date only.",
        ]
    elif category == "stale_data":
        service = "unknown"
        if health and isinstance(health, dict):
            service = health.get("status") or health.get("backend") or "checked"
        steps = [
            f"Current safe service status: {service}. I will not claim live market data is fresh without timestamps.",
            "Use the section refresh button if available and check the displayed last-updated time.",
            "If the page says stale, degraded or unavailable, the app should not treat old data as live.",
            "If the same feature stays stale after refresh, request human support and include the section name.",
        ]
    elif category == "creator_intel":
        steps = [
            "Creator Intel depends on valid YouTube channel IDs and API quota.",
            "Manual refresh should update the last scan time when the provider responds.",
            "If no recent videos appear, the likely causes are quota, provider failure, or no matched stock mentions.",
            "Request human support if official channel videos are visible on YouTube but GrowthIntel still shows no scan activity.",
        ]
    elif category in {"canslim", "engine", "market", "alerts", "portfolio", "smart_money", "upcoming", "pennie", "learn"}:
        label = category.replace("_", " ").title()
        steps = [
            f"Open the {label} section and check the status or last-updated label first.",
            "Try the section refresh/rescan control if available.",
            f"Review the related docs at {docs_url} for expected behaviour.",
            "If the problem repeats after refresh, request human support with the exact section and what you expected to happen.",
        ]
    else:
        steps = [
            "Tell me which page you were on and what you expected to happen.",
            "Try refreshing once and check whether you are signed in.",
            f"Search the documentation at {docs_url} if this is a feature question.",
            "If those steps do not solve it, request human support and I will create a ticket.",
        ]
    answer = opening + "\n\n" + "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps)) + "\n\nDid this solve the issue?"
    return {
        "answer": answer,
        "category": category,
        "priority": priority,
        "can_escalate": True,
        "status": "AI_HANDLING",
        "disclaimer": "Never send passwords or full payment card details.",
    }


def _support_summary(problem: str, transcript: list, category: str, priority: str) -> str:
    attempted = []
    for item in transcript:
        sender = str(item.get("sender") or item.get("role") or "").lower()
        body = _redact_sensitive(str(item.get("body") or item.get("content") or ""), 1200)
        if sender in {"ai", "assistant", "growthintel ai"} and body:
            attempted.append(body[:260])
    attempted_text = " | ".join(attempted[-3:]) or "AI gave standard GrowthIntel troubleshooting and asked if the issue was solved."
    problem_text = _redact_sensitive(problem, 1200)
    return "\n".join(
        [
            f"Problem: {problem_text}",
            "What user expected: GrowthIntel should work normally for the affected account or feature.",
            "What actually happened: User reports the issue is not solved.",
            f"Steps AI attempted: {attempted_text}",
            "Results: Customer requested human support.",
            f"Likely cause: {category.replace('_', ' ')} issue; verify account/service state server-side.",
            "Recommended human action: Open the ticket, review account and service status, then reply inside GrowthIntel Support.",
            f"Priority: {priority}",
        ]
    )


def _sms_body(ticket_ref: str, category: str, priority: str, summary: str) -> str:
    first = summary.splitlines()[0].replace("Problem: ", "")[:90]
    admin_url = os.environ.get("SUPPORT_ADMIN_URL", "https://www.growthintel.app/admin/support").strip()
    return f"GrowthIntel Support #{ticket_ref}\n{first}\nCategory: {category}. Priority: {priority}.\nOpen ticket: {admin_url}"


def _post_json(url: str, payload: dict, headers: dict | None = None, auth: tuple[str, str] | None = None) -> tuple[bool, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **(headers or {})}, method="POST")
    if auth:
        raw = f"{auth[0]}:{auth[1]}".encode("utf-8")
        import base64

        request.add_header("authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(500).decode("utf-8", errors="replace")
            return 200 <= response.status < 300, body
    except urllib.error.HTTPError as error:
        return False, error.read(500).decode("utf-8", errors="replace")
    except Exception as error:
        return False, str(error)[:500]


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_PREFERENCES = {
    "weekly_growth_brief": False,
    "alert_emails": False,
    "product_updates": False,
    "re_engagement": False,
    "marketing": False,
}


def _email_now_iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _email_base_url() -> str:
    return os.environ.get("EMAIL_BASE_URL", "https://www.growthintel.app").strip().rstrip("/") or "https://www.growthintel.app"


def _normalize_preferences(value) -> dict:
    incoming = value if isinstance(value, dict) else {}
    return {key: bool(incoming.get(key, False)) for key in EMAIL_PREFERENCES}


def _load_preferences(row) -> dict:
    try:
        parsed = json.loads(row["preferences"] or "{}")
    except Exception:
        parsed = {}
    return {**EMAIL_PREFERENCES, **{key: bool(parsed.get(key)) for key in EMAIL_PREFERENCES}}


def _email_address(value) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) > 254 or not EMAIL_RE.match(normalized) or parseaddr(normalized)[1] != normalized:
        raise HTTPException(400, "Enter a valid email address.")
    return normalized


def _email_referral_code(connection) -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    for _ in range(64):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if connection.execute(select(email_subscribers.c.id).where(email_subscribers.c.referral_code == code)).first() is None:
            return code
    raise RuntimeError("Unable to allocate email referral code")


def _email_token() -> str:
    return secrets.token_urlsafe(32)


def _subscriber_payload(connection, row, token: str | None = None) -> dict:
    referral_count = int(
        connection.execute(
            select(func.count()).select_from(email_referrals).where(email_referrals.c.referrer_id == row["id"])
        ).scalar()
        or 0
    )
    payload = {
        "ok": True,
        "email": row["email"],
        "status": row["status"],
        "preferences": _load_preferences(row),
        "signup_source": row["signup_source"],
        "consent_at": _email_now_iso(row["consented_at"]),
        "unsubscribed_at": _email_now_iso(row["unsubscribed_at"]),
        "referral_code": row["referral_code"],
        "referral_url": f"{_email_base_url()}/growth-brief?ref={row['referral_code']}",
        "referral_count": referral_count,
    }
    if token:
        payload["management_token"] = token
    return payload


def _find_email_subscriber_by_token(connection, email: str, token: str):
    if not token:
        return None
    return connection.execute(
        select(email_subscribers).where(
            and_(
                email_subscribers.c.email == _email_address(email),
                email_subscribers.c.management_token_hash == _hash_token(token),
            )
        )
    ).mappings().first()


def _apply_email_referral(connection, subscriber_id: int, subscriber_email: str, referring_code: str | None) -> None:
    code = re.sub(r"[^A-Z0-9]", "", str(referring_code or "").strip().upper())[:24]
    if not code:
        return
    already = connection.execute(select(email_referrals.c.id).where(email_referrals.c.subscriber_id == subscriber_id)).first()
    if already:
        return
    referrer = connection.execute(select(email_subscribers).where(email_subscribers.c.referral_code == code)).mappings().first()
    if not referrer or referrer["id"] == subscriber_id or referrer["email"] == subscriber_email:
        return
    connection.execute(
        update(email_subscribers)
        .where(email_subscribers.c.id == subscriber_id)
        .values(referring_code=code, updated_at=_now())
    )
    try:
        connection.execute(
            email_referrals.insert().values(
                referrer_id=referrer["id"],
                subscriber_id=subscriber_id,
                referral_code=code,
                created_at=_now(),
            )
        )
    except IntegrityError:
        pass


def _email_sender() -> tuple[str, str]:
    raw = os.environ.get("EMAIL_FROM_ADDRESS", "GrowthIntel <hello@growthintel.app>").strip()
    name, address = parseaddr(raw)
    return name or "GrowthIntel", address or raw


def _email_urls(email: str, token: str) -> tuple[str, str]:
    query = urllib.parse.urlencode({"email": email, "token": token})
    base_url = _email_base_url()
    return f"{base_url}/email-preferences?{query}", f"{base_url}/email-preferences?{query}&unsubscribe=1"


def _email_button(label: str, href: str) -> str:
    safe_label = str(label or "Open GrowthIntel")[:80]
    return (
        f'<a href="{escape(href, quote=True)}" '
        'style="display:inline-block;background:#25e6a7;color:#03110d;font-weight:700;'
        'text-decoration:none;padding:13px 18px;border-radius:8px;">'
        f"{escape(safe_label)}</a>"
    )


def _email_layout(title: str, preview: str, body: str, preferences_url: str, unsubscribe_url: str) -> str:
    return f"""<!doctype html>
<html>
  <body style="margin:0;background:#04070d;color:#eef7f5;font-family:Inter,Arial,sans-serif;">
    <span style="display:none!important;opacity:0;color:transparent;height:0;width:0;overflow:hidden;">{escape(preview[:180])}</span>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#04070d;padding:24px 12px;">
      <tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#09121c;border:1px solid rgba(129,244,216,.18);border-radius:14px;overflow:hidden;">
          <tr><td style="padding:24px 24px 12px;">
            <div style="font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#25e6a7;">GrowthIntel</div>
            <h1 style="margin:12px 0 8px;font-size:28px;line-height:1.15;color:#ffffff;">{escape(title)}</h1>
          </td></tr>
          <tr><td style="padding:0 24px 22px;color:#cfe2df;font-size:16px;line-height:1.55;">{body}</td></tr>
          <tr><td style="padding:18px 24px;background:#071019;color:#8ea8a4;font-size:12px;line-height:1.5;">
            You are receiving this because you opted in to GrowthIntel emails. GrowthIntel provides research tools and educational information, not personalised financial advice.
            <br><a href="{escape(preferences_url, quote=True)}" style="color:#25e6a7;">Manage preferences</a>
            &nbsp;|&nbsp;<a href="{escape(unsubscribe_url, quote=True)}" style="color:#25e6a7;">Unsubscribe</a>
            <br><a href="https://www.growthintel.app/" style="color:#8ea8a4;">growthintel.app</a>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _confirmation_template(email: str, token: str, referral_url: str) -> dict:
    preferences_url, unsubscribe_url = _email_urls(email, token)
    base_url = _email_base_url()
    body = (
        "<p>Your GrowthIntel email preferences are saved.</p>"
        "<p>You can return for CAN SLIM screening, alerts, market news and GrowthIntel research tools.</p>"
        f"<p>{_email_button('Open GrowthIntel', base_url + '/')}</p>"
        f'<p style="color:#9fb2c4;font-size:14px;">Your referral link:<br><a style="color:#25e6a7;" href="{escape(referral_url, quote=True)}">{escape(referral_url)}</a></p>'
    )
    return {
        "subject": "GrowthIntel email preferences confirmed",
        "html": _email_layout("You are signed up", "Your GrowthIntel email preferences are saved.", body, preferences_url, unsubscribe_url),
    }


def _weekly_brief_template(email: str, token: str) -> dict:
    preferences_url, unsubscribe_url = _email_urls(email, token)
    base_url = _email_base_url()
    body = (
        "<p>This week, review the market direction first, then check top-ranked growth setups, important earnings and useful GrowthIntel tools.</p>"
        "<ul><li>Important growth-stock developments</li><li>Upcoming earnings to watch</li><li>Useful market and tool updates</li></ul>"
        f"<p>{_email_button('Read the weekly brief', base_url + '/')}</p>"
    )
    return {
        "subject": "Weekly Growth Brief from GrowthIntel",
        "html": _email_layout("Weekly Growth Brief", "Growth-stock developments, earnings and useful tools for the week.", body, preferences_url, unsubscribe_url),
    }


def _re_engagement_template(email: str, token: str) -> dict:
    preferences_url, unsubscribe_url = _email_urls(email, token)
    base_url = _email_base_url()
    body = (
        "<p>A useful place to restart is the market overview: check whether conditions are supportive before reviewing individual stocks.</p>"
        f"<p>{_email_button('Review GrowthIntel', base_url + '/')}</p>"
    )
    return {
        "subject": "A useful GrowthIntel check-in",
        "html": _email_layout("A quick research check-in", "A simple way to restart your GrowthIntel research flow.", body, preferences_url, unsubscribe_url),
    }


def _alert_email_template(email: str, token: str, title: str, details: list[str]) -> dict:
    preferences_url, unsubscribe_url = _email_urls(email, token)
    base_url = _email_base_url()
    items = "".join(f"<li>{escape(str(item)[:240])}</li>" for item in details[:4])
    body = f"<p>{escape(str(title)[:240])}</p><ul>{items}</ul><p>{_email_button('Open Alerts', base_url + '/alerts')}</p>"
    return {
        "subject": f"GrowthIntel alert: {str(title or 'Selected alert')[:90]}",
        "html": _email_layout("GrowthIntel alert", "A selected GrowthIntel alert was triggered.", body, preferences_url, unsubscribe_url),
    }


def _send_growth_email(connection, subscriber, email_type: str, template: dict, metadata_value: dict | None = None) -> dict:
    provider = os.environ.get("EMAIL_PROVIDER", "mailjet").strip().lower() or "mailjet"
    enabled = _truthy("EMAIL_SEND_ENABLED")
    result = {"status": "prepared", "provider": "disabled", "message_id": None}
    if enabled:
        if provider != "mailjet":
            result = {"status": "failed", "provider": provider, "error": "Only Mailjet is configured for the live lightweight backend"}
        else:
            api_key = os.environ.get("MAILJET_API_KEY", "").strip()
            secret = os.environ.get("MAILJET_SECRET_KEY", "").strip()
            sender_name, sender_email = _email_sender()
            if not api_key or not secret or not sender_email:
                result = {"status": "failed", "provider": "mailjet", "error": "Mailjet credentials or EMAIL_FROM_ADDRESS are not configured"}
            else:
                ok, detail = _post_json(
                    "https://api.mailjet.com/v3.1/send",
                    {
                        "Messages": [
                            {
                                "From": {"Email": sender_email, "Name": sender_name},
                                "To": [{"Email": subscriber["email"]}],
                                "Subject": str(template.get("subject") or "GrowthIntel")[:120],
                                "HTMLPart": str(template.get("html") or ""),
                            }
                        ]
                    },
                    auth=(api_key, secret),
                )
                result = {
                    "status": "sent" if ok else "failed",
                    "provider": "mailjet",
                    "message_id": None,
                    "error": None if ok else detail[:500],
                }
    connection.execute(
        email_delivery_logs.insert().values(
            subscriber_id=subscriber["id"],
            recipient_email=subscriber["email"],
            email_type=email_type[:64],
            subject=str(template.get("subject") or "GrowthIntel")[:180],
            provider=result.get("provider", provider)[:40],
            status=result.get("status", "prepared")[:24],
            provider_message_id=result.get("message_id"),
            error=_short_text(result.get("error"), 1000),
            metadata_json=json.dumps(metadata_value or {}, separators=(",", ":")),
            created_at=_now(),
        )
    )
    return result


def _send_preference_campaign(connection, preference_key: str, email_type: str, template_builder, limit: int, offset: int, dry_run: bool) -> dict:
    safe_limit = max(1, min(int(limit or 100), 250))
    safe_offset = max(0, int(offset or 0))
    rows = connection.execute(
        select(email_subscribers)
        .where(email_subscribers.c.status == "subscribed")
        .order_by(email_subscribers.c.id.asc())
        .offset(safe_offset)
        .limit(safe_limit)
    ).mappings().all()
    prepared = sent = failed = skipped = 0
    for row in rows:
        preferences = _load_preferences(row)
        if not preferences.get(preference_key):
            skipped += 1
            continue
        prepared += 1
        if dry_run:
            continue
        token = _email_token()
        connection.execute(update(email_subscribers).where(email_subscribers.c.id == row["id"]).values(management_token_hash=_hash_token(token), updated_at=_now()))
        subscriber = connection.execute(select(email_subscribers).where(email_subscribers.c.id == row["id"])).mappings().one()
        result = _send_growth_email(connection, subscriber, email_type, template_builder(subscriber["email"], token), {"preference": preference_key})
        if result.get("status") == "sent":
            sent += 1
        elif result.get("status") == "failed":
            failed += 1
    return {
        "ok": True,
        "email_type": email_type,
        "preference_key": preference_key,
        "limit": safe_limit,
        "offset": safe_offset,
        "prepared": prepared,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "dry_run": dry_run,
        "provider": os.environ.get("EMAIL_PROVIDER", "mailjet") or "mailjet",
        "send_enabled": _truthy("EMAIL_SEND_ENABLED"),
    }


def _authorize_email_cron(request: Request) -> None:
    expected = os.environ.get("EMAIL_CRON_SECRET", "").strip() or os.environ.get("CRON_SECRET", "").strip()
    auth = request.headers.get("authorization") or ""
    bearer = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
    supplied = request.headers.get("x-growthintel-cron-secret") or bearer or request.query_params.get("secret")
    if not expected or not hmac.compare_digest(str(supplied or ""), expected):
        raise HTTPException(403, "Cron endpoint is protected.")


def _send_support_sms(message: str) -> dict:
    provider = os.environ.get("SMS_PROVIDER", "").strip().lower()
    to_number = os.environ.get("SUPPORT_SMS_TO", "").strip()
    if not provider or not to_number:
        return {"status": "NOT_CONFIGURED", "provider": provider or "none", "error": "SMS provider or SUPPORT_SMS_TO is not configured"}
    if provider == "twilio":
        sid = os.environ.get("SMS_API_KEY", "").strip()
        token = os.environ.get("SMS_API_SECRET", "").strip()
        from_number = os.environ.get("SMS_FROM", "").strip()
        if not sid or not token or not from_number:
            return {"status": "FAILED", "provider": "twilio", "error": "Twilio SMS credentials are incomplete"}
        encoded = urllib.parse.urlencode({"To": to_number, "From": from_number, "Body": message}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json",
            data=encoded,
            headers={"content-type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        import base64

        request.add_header("authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode("ascii"))
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return {"status": "SENT" if response.status < 300 else "FAILED", "provider": "twilio", "response": response.read(300).decode("utf-8", errors="replace")}
        except Exception as error:
            return {"status": "FAILED", "provider": "twilio", "error": str(error)[:500]}
    if provider == "webhook":
        ok, detail = _post_json(os.environ.get("SMS_WEBHOOK_URL", "").strip(), {"to": to_number, "message": message}, headers={"x-api-key": os.environ.get("SMS_API_KEY", "")})
        return {"status": "SENT" if ok else "FAILED", "provider": "webhook", "error": None if ok else detail}
    return {"status": "FAILED", "provider": provider, "error": "Unsupported SMS_PROVIDER"}


def _send_support_fallback_email(subject: str, message: str) -> dict:
    to_email = os.environ.get("SUPPORT_FALLBACK_EMAIL", "").strip()
    api_key = os.environ.get("MAILJET_API_KEY", "").strip()
    secret = os.environ.get("MAILJET_SECRET_KEY", "").strip()
    from_email = os.environ.get("SUPPORT_EMAIL_FROM_ADDRESS", "").strip() or os.environ.get("EMAIL_FROM_ADDRESS", "GrowthIntel Support <support@growthintel.app>").strip()
    if not to_email or not api_key or not secret:
        return {"status": "NOT_CONFIGURED", "provider": "mailjet"}
    name, email = parseaddr(from_email)
    ok, detail = _post_json(
        "https://api.mailjet.com/v3.1/send",
        {
            "Messages": [
                {
                    "From": {"Email": email or "support@growthintel.app", "Name": name or "GrowthIntel Support"},
                    "To": [{"Email": to_email}],
                    "Subject": subject[:120],
                    "TextPart": message[:4000],
                }
            ]
        },
        auth=(api_key, secret),
    )
    return {"status": "SENT" if ok else "FAILED", "provider": "mailjet", "error": None if ok else detail}


def _record_support_notification(connection, ticket_id: int, channel: str, result: dict) -> None:
    connection.execute(
        support_notification_events.insert().values(
            ticket_id=ticket_id,
            channel=channel,
            status=result.get("status", "FAILED"),
            provider=result.get("provider"),
            error=_short_text(result.get("error") or result.get("response"), 800),
            created_at=_now(),
        )
    )


def install_membership(real_app) -> None:
    global _engine
    _validate_configuration()
    _engine = _configure_storage()

    if _truthy("MEMBERSHIP_ENFORCE_API_ACCESS"):
        @real_app.middleware("http")
        async def membership_enforcement(request: Request, call_next):
            protected = (
                request.url.path.startswith("/api/v1/")
                and request.url.path not in PUBLIC_PATHS
                and not request.url.path.startswith("/api/v1/membership/")
                and not request.url.path.startswith("/api/v1/support/")
                and not request.url.path.startswith("/api/v1/email/")
                and not request.url.path.startswith("/api/v1/cron/")
            )
            if protected:
                try:
                    with _db_begin() as connection:
                        user = _session_user(request, connection)
                        if not user:
                            return JSONResponse({"detail": "Authentication required"}, status_code=401)
                        active = (
                            user["membership_state"] == "ACTIVE"
                            and user["membership_expires_at"]
                            and user["membership_expires_at"] > _now()
                        )
                        if not active:
                            if user["membership_state"] == "ACTIVE":
                                connection.execute(update(users).where(users.c.id == user["id"]).values(membership_state="EXPIRED"))
                                _audit(connection, "PREMIUM_EXPIRED", target=user["id"], detail={"reason": "expiry_threshold"})
                            return JSONResponse({"detail": "An active Growth Intel membership is required"}, status_code=403)
                except HTTPException as error:
                    return _json_error(error)
            return await call_next(request)

    @real_app.post("/api/v1/membership/register")
    async def register(request: Request):
        _check_origin(request)
        _rate(request, "register", 5, 300)
        data = await _json(request)
        email = _email(data.get("email"))
        name = _name(data.get("name"))
        password = str(data.get("password") or "")
        if not 12 <= len(password) <= 128:
            raise HTTPException(422, "Password must be 12-128 characters")
        referral_code = _referral_code(data.get("affiliate_referral_code") or data.get("referral_code") or data.get("ref"))
        referral_source = _short_text(data.get("affiliate_referral_source") or data.get("referral_source") or "landing-ref", 64) if referral_code else None
        referral_url = _short_text(data.get("affiliate_referral_landing_url") or data.get("landing_url"), 500) if referral_code else None

        try:
            with _lock, _db_begin() as connection:
                if connection.execute(select(users.c.id).where(users.c.email == email)).first():
                    raise HTTPException(409, "An account with that email already exists")
                result = connection.execute(
                    users.insert().values(
                        email=email,
                        name=name,
                        password_hash=_password(password),
                        gi_reference=_reference(connection),
                        membership_state="NONE",
                        affiliate_referral_code=referral_code,
                        affiliate_referral_source=referral_source,
                        affiliate_referral_landing_url=referral_url,
                        affiliate_referral_captured_at=_now() if referral_code else None,
                        created_at=_now(),
                    )
                )
                user_id = result.inserted_primary_key[0]
                _audit(
                    connection,
                    "USER_REGISTERED",
                    user_id,
                    user_id,
                    detail={"email": email, "affiliate_referral_code": referral_code},
                    ip=request.client.host if request.client else None,
                )
                user = connection.execute(select(users).where(users.c.id == user_id)).mappings().one()
        except IntegrityError:
            raise HTTPException(409, "An account with that email already exists")
        return _public_user(user)

    @real_app.post("/api/v1/membership/login")
    async def login(request: Request):
        _check_origin(request)
        _rate(request, "login", 8, 300)
        data = await _json(request)
        email = _email(data.get("email"))
        with _db_begin() as connection:
            user = connection.execute(select(users).where(users.c.email == email)).mappings().first()
            if not user or not _verify(str(data.get("password") or ""), user["password_hash"]):
                raise HTTPException(401, "Invalid email or password")
            token, now = secrets.token_urlsafe(32), _now()
            connection.execute(sessions.delete().where(sessions.c.expires_at <= now))
            connection.execute(
                sessions.insert().values(
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    user_id=user["id"],
                    expires_at=now + SESSION_SECONDS,
                    created_at=now,
                )
            )
            _audit(connection, "USER_LOGGED_IN", user["id"], user["id"], ip=request.client.host if request.client else None)
        response = JSONResponse(_public_user(user))
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=True, samesite="strict", path="/")
        return response

    @real_app.post("/api/v1/membership/logout")
    async def logout(request: Request):
        _check_origin(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        with _db_begin() as connection:
            if token:
                connection.execute(sessions.delete().where(sessions.c.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @real_app.get("/api/v1/membership/me")
    async def me(request: Request):
        with _db_connect() as connection:
            return _public_user(_require_user(request, connection))

    @real_app.post("/api/v1/email/subscribe")
    async def email_subscribe(request: Request):
        _check_origin(request)
        _rate(request, "email-subscribe", 8, 300)
        data = await _json(request)
        email = _email_address(data.get("email"))
        preferences = _normalize_preferences(data.get("preferences"))
        signup_source = _short_text(data.get("signup_source") or "unknown", 80) or "unknown"
        referring_code = data.get("referring_code") or data.get("ref")
        token = _email_token()
        now = _now()
        created = False
        with _lock, _db_begin() as connection:
            existing = connection.execute(select(email_subscribers).where(email_subscribers.c.email == email)).mappings().first()
            if existing:
                connection.execute(
                    update(email_subscribers)
                    .where(email_subscribers.c.id == existing["id"])
                    .values(
                        status="subscribed",
                        preferences=json.dumps(preferences, separators=(",", ":")),
                        signup_source=signup_source,
                        consented_at=existing["consented_at"] or now,
                        unsubscribed_at=None,
                        management_token_hash=_hash_token(token),
                        updated_at=now,
                    )
                )
                subscriber_id = existing["id"]
            else:
                created = True
                result = connection.execute(
                    email_subscribers.insert().values(
                        email=email,
                        status="subscribed",
                        preferences=json.dumps(preferences, separators=(",", ":")),
                        signup_source=signup_source,
                        consented_at=now,
                        unsubscribed_at=None,
                        referral_code=_email_referral_code(connection),
                        referring_code=None,
                        management_token_hash=_hash_token(token),
                        created_at=now,
                        updated_at=now,
                    )
                )
                subscriber_id = result.inserted_primary_key[0]
            subscriber = connection.execute(select(email_subscribers).where(email_subscribers.c.id == subscriber_id)).mappings().one()
            _apply_email_referral(connection, subscriber["id"], subscriber["email"], referring_code)
            subscriber = connection.execute(select(email_subscribers).where(email_subscribers.c.id == subscriber_id)).mappings().one()
            payload = _subscriber_payload(connection, subscriber, token)
            payload["created"] = created
            payload["message"] = "Thanks - your email preferences are saved."
            referral_url = payload["referral_url"]
            send_result = _send_growth_email(connection, subscriber, "subscription_confirmation", _confirmation_template(email, token, referral_url), {"referral_url": referral_url})
            payload["email_delivery_status"] = send_result.get("status")
            return payload

    @real_app.get("/api/v1/email/preferences")
    async def email_preferences(request: Request, email: str = "", token: str = ""):
        with _db_connect() as connection:
            subscriber = _find_email_subscriber_by_token(connection, email, token)
            if not subscriber:
                raise HTTPException(403, "Preference link is invalid or expired.")
            return _subscriber_payload(connection, subscriber, token)

    @real_app.put("/api/v1/email/preferences")
    async def email_preferences_update(request: Request):
        _check_origin(request)
        _rate(request, "email-preferences", 12, 300)
        data = await _json(request)
        preferences = _normalize_preferences(data.get("preferences"))
        signup_source = _short_text(data.get("signup_source") or "settings", 80) or "settings"
        with _db_begin() as connection:
            subscriber = _find_email_subscriber_by_token(
                connection,
                str(data.get("email") or ""),
                str(data.get("management_token") or data.get("token") or ""),
            )
            if not subscriber:
                raise HTTPException(403, "Preference link is invalid or expired.")
            now = _now()
            values = {
                "preferences": json.dumps(preferences, separators=(",", ":")),
                "signup_source": signup_source,
                "updated_at": now,
            }
            if subscriber["status"] == "unsubscribed" and any(preferences.values()):
                values.update({"status": "subscribed", "unsubscribed_at": None, "consented_at": now})
            connection.execute(update(email_subscribers).where(email_subscribers.c.id == subscriber["id"]).values(**values))
            updated = connection.execute(select(email_subscribers).where(email_subscribers.c.id == subscriber["id"])).mappings().one()
            return {**_subscriber_payload(connection, updated, str(data.get("management_token") or data.get("token") or "")), "message": "Email preferences updated."}

    @real_app.post("/api/v1/email/unsubscribe")
    async def email_unsubscribe(request: Request):
        data = await _json(request)
        token = str(data.get("management_token") or data.get("token") or "")
        with _db_begin() as connection:
            subscriber = _find_email_subscriber_by_token(connection, str(data.get("email") or ""), token)
            if not subscriber:
                raise HTTPException(403, "Unsubscribe link is invalid or expired.")
            now = _now()
            connection.execute(
                update(email_subscribers)
                .where(email_subscribers.c.id == subscriber["id"])
                .values(
                    status="unsubscribed",
                    preferences=json.dumps({key: False for key in EMAIL_PREFERENCES}, separators=(",", ":")),
                    unsubscribed_at=now,
                    updated_at=now,
                )
            )
            updated = connection.execute(select(email_subscribers).where(email_subscribers.c.id == subscriber["id"])).mappings().one()
            return {**_subscriber_payload(connection, updated, token), "message": "You have been unsubscribed."}

    @real_app.post("/api/v1/email/alert-event")
    async def email_alert_event(request: Request):
        _check_origin(request)
        _rate(request, "email-alert-event", 20, 300)
        data = await _json(request)
        token = str(data.get("management_token") or data.get("token") or "")
        alert_id = str(data.get("alert_id") or "").strip()[:120]
        with _db_begin() as connection:
            subscriber = _find_email_subscriber_by_token(connection, str(data.get("email") or ""), token)
            if not subscriber:
                raise HTTPException(403, "Email alert preferences are not connected.")
            preferences = _load_preferences(subscriber)
            if subscriber["status"] != "subscribed" or not preferences.get("alert_emails"):
                return {"ok": True, "queued": False, "reason": "Alert email delivery is disabled."}
            if alert_id:
                existing = connection.execute(
                    select(email_delivery_logs.c.id)
                    .where(
                        and_(
                            email_delivery_logs.c.subscriber_id == subscriber["id"],
                            email_delivery_logs.c.email_type == "alert",
                            email_delivery_logs.c.metadata_json.like(f'%"{alert_id}"%'),
                        )
                    )
                    .limit(1)
                ).first()
                if existing:
                    return {"ok": True, "queued": False, "reason": "Alert email already prepared."}
            details = data.get("details") if isinstance(data.get("details"), list) else []
            template = _alert_email_template(subscriber["email"], token, str(data.get("title") or "GrowthIntel alert"), [str(item) for item in details])
            result = _send_growth_email(connection, subscriber, "alert", template, {"alert_id": alert_id, "source": data.get("source")})
            return {"ok": True, "queued": result.get("status") != "failed", "status": result.get("status"), "reason": result.get("error")}

    @real_app.post("/api/v1/cron/weekly-growth-brief")
    async def cron_weekly_growth_brief(request: Request):
        _authorize_email_cron(request)
        data = {}
        try:
            data = await request.json()
        except Exception:
            data = {}
        with _db_begin() as connection:
            return _send_preference_campaign(
                connection,
                "weekly_growth_brief",
                "weekly_growth_brief",
                _weekly_brief_template,
                int(data.get("limit") or 100),
                int(data.get("offset") or 0),
                bool(data.get("dry_run", False)),
            )

    @real_app.post("/api/v1/cron/re-engagement")
    async def cron_re_engagement(request: Request):
        _authorize_email_cron(request)
        data = {}
        try:
            data = await request.json()
        except Exception:
            data = {}
        with _db_begin() as connection:
            return _send_preference_campaign(
                connection,
                "re_engagement",
                "re_engagement",
                _re_engagement_template,
                int(data.get("limit") or 75),
                int(data.get("offset") or 0),
                bool(data.get("dry_run", False)),
            )

    @real_app.get("/api/v1/membership/bank-transfer")
    async def bank_transfer(request: Request):
        with _db_connect() as connection:
            user = _require_user(request, connection)
        values = {
            key: os.environ.get(key, "").strip()
            for key in ("BANK_TRANSFER_ACCOUNT_NAME", "BANK_TRANSFER_SORT_CODE", "BANK_TRANSFER_ACCOUNT_NUMBER")
        }
        if not all(values.values()):
            raise HTTPException(503, "Membership payment details are not configured")
        return {
            "bank_name": os.environ.get("BANK_TRANSFER_BANK_NAME", "Lloyds Bank").strip() or "Lloyds Bank",
            "account_name": values["BANK_TRANSFER_ACCOUNT_NAME"],
            "sort_code": values["BANK_TRANSFER_SORT_CODE"],
            "account_number": values["BANK_TRANSFER_ACCOUNT_NUMBER"],
            "reference": user["gi_reference"],
            "amount_pence": PRICE_PENCE,
            "amount_display": "£13.00",
            "currency": PRICE_CURRENCY,
            "standing_order_available": bool(user["membership_expires_at"]),
        }

    @real_app.post("/api/v1/membership/payment-requests")
    async def request_payment(request: Request):
        _check_origin(request)
        _rate(request, "payment", 6, 300)
        with _lock:
            try:
                with _db_begin() as connection:
                    user = _require_user(request, connection)
                    result = connection.execute(
                        payments.insert().values(
                            user_id=user["id"],
                            amount_pence=PRICE_PENCE,
                            currency=PRICE_CURRENCY,
                            status="PENDING",
                            pending_guard="PENDING",
                            affiliate_referral_code=user["affiliate_referral_code"],
                            created_at=_now(),
                        )
                    )
                    payment_id = result.inserted_primary_key[0]
                    active = user["membership_state"] == "ACTIVE" and user["membership_expires_at"] and user["membership_expires_at"] > _now()
                    if not active:
                        connection.execute(update(users).where(users.c.id == user["id"]).values(membership_state="PENDING_PAYMENT"))
                    _audit(
                        connection,
                        "PAYMENT_SUBMITTED",
                        user["id"],
                        user["id"],
                        payment_id,
                        {"amount_pence": PRICE_PENCE, "affiliate_referral_code": user["affiliate_referral_code"]},
                        request.client.host if request.client else None,
                    )
            except IntegrityError:
                raise HTTPException(409, "A payment is already awaiting verification")
        return JSONResponse(
            {
                "id": payment_id,
                "status": "PENDING",
                "amount_pence": PRICE_PENCE,
                "message": "Payment submitted for manual verification. Access is not yet active.",
            },
            status_code=201,
        )

    @real_app.get("/api/v1/membership/payments")
    async def payment_history(request: Request):
        with _db_connect() as connection:
            user = _require_user(request, connection)
            rows = connection.execute(select(payments).where(payments.c.user_id == user["id"]).order_by(payments.c.created_at.desc())).mappings().all()
        return [{key: value for key, value in row.items() if key != "pending_guard"} for row in rows]

    @real_app.get("/api/v1/membership/admin/payments")
    async def admin_search(request: Request, q: str = ""):
        with _db_connect() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            term = f"%{q.strip()[:100].lower()}%"
            query = (
                select(
                    payments,
                    users.c.email,
                    users.c.name,
                    users.c.gi_reference,
                    users.c.membership_state,
                    users.c.membership_expires_at,
                    users.c.affiliate_referral_code.label("user_affiliate_referral_code"),
                    users.c.affiliate_referral_source,
                    users.c.affiliate_referral_captured_at,
                )
                .join(users, users.c.id == payments.c.user_id)
                .where(or_(
                    users.c.gi_reference.ilike(term),
                    users.c.email.ilike(term),
                    users.c.name.ilike(term),
                    users.c.affiliate_referral_code.ilike(term),
                    payments.c.affiliate_referral_code.ilike(term),
                ))
                .order_by(payments.c.created_at.desc())
                .limit(100)
            )
            rows = connection.execute(query).mappings().all()
        return [{key: value for key, value in row.items() if key != "pending_guard"} for row in rows]

    @real_app.post("/api/v1/membership/admin/payments/{payment_id}/{decision}")
    async def admin_decide(payment_id: int, decision: str, request: Request):
        _check_origin(request)
        if decision not in {"approve", "reject"}:
            raise HTTPException(404, "Unknown decision")
        note = str((await _json(request)).get("note") or "").strip()[:500]
        with _db_begin() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            status, now = ("APPROVED" if decision == "approve" else "REJECTED"), _now()
            claimed = connection.execute(
                update(payments)
                .where(and_(payments.c.id == payment_id, payments.c.status == "PENDING"))
                .values(status=status, pending_guard=None, reviewed_at=now, reviewed_by=admin["id"], admin_note=note)
            )
            if claimed.rowcount != 1:
                exists = connection.execute(select(payments.c.id).where(payments.c.id == payment_id)).first()
                raise HTTPException(409 if exists else 404, "Payment has already been reviewed" if exists else "Payment request not found")
            payment = connection.execute(select(payments).where(payments.c.id == payment_id)).mappings().one()
            user = connection.execute(select(users).where(users.c.id == payment["user_id"])).mappings().one()
            if decision == "approve":
                base = user["membership_expires_at"] if user["membership_state"] == "ACTIVE" and user["membership_expires_at"] and user["membership_expires_at"] > now else now
                connection.execute(
                    update(users).where(users.c.id == user["id"]).values(
                        membership_state="ACTIVE",
                        membership_expires_at=_add_calendar_month(base),
                        membership_started_at=user["membership_started_at"] or now,
                        membership_last_verified_at=now,
                        membership_cancel_at_period_end=0,
                        plan_version=PREMIUM_PLAN_VERSION,
                    )
                )
                _grant_premium_entitlements(connection, user["id"], now)
            elif user["membership_state"] != "ACTIVE":
                rejected_state = "EXPIRED" if user["membership_expires_at"] and user["membership_expires_at"] <= now else "NONE"
                connection.execute(update(users).where(users.c.id == user["id"]).values(membership_state=rejected_state))
            _audit(connection, "PAYMENT_" + status, admin["id"], user["id"], payment_id, {"note": note}, request.client.host if request.client else None)
        return {"id": payment_id, "status": status}

    def _support_ticket_response(connection, row, include_messages: bool = False, access_token: str | None = None) -> dict:
        payload = dict(row)
        for key in ("created_at", "updated_at", "customer_last_reply_at", "admin_last_reply_at", "sms_sent_at"):
            if payload.get(key):
                payload[key] = datetime.fromtimestamp(int(payload[key]), timezone.utc).isoformat()
        if include_messages:
            messages = connection.execute(
                select(support_messages)
                .where(support_messages.c.ticket_id == row["id"])
                .order_by(support_messages.c.created_at.asc())
            ).mappings().all()
            payload["messages"] = [
                {
                    "id": message["id"],
                    "sender": message["sender"],
                    "body": message["body"],
                    "created_at": datetime.fromtimestamp(int(message["created_at"]), timezone.utc).isoformat(),
                }
                for message in messages
            ]
        if access_token:
            payload["access_token"] = access_token
        payload.pop("customer_access_hash", None)
        return payload

    def _support_user_for_request(request: Request, connection):
        try:
            return _session_user(request, connection)
        except Exception:
            return None

    @real_app.post("/api/v1/support/ai")
    async def support_ai(request: Request):
        _check_origin(request)
        _rate(request, "support-ai", 20, 300)
        data = await _json(request)
        message = _redact_sensitive(str(data.get("message") or data.get("problem") or ""), 3000)
        if len(message) < 3:
            raise HTTPException(422, "Please describe what you need help with.")
        health = None
        with _db_connect() as connection:
            user = _support_user_for_request(request, connection)
        return _support_ai_answer(message, user, health)

    @real_app.post("/api/v1/support/tickets")
    async def create_support_ticket(request: Request):
        _check_origin(request)
        _rate(request, "support-ticket", 3, 900)
        data = await _json(request)
        problem = _redact_sensitive(str(data.get("problem") or data.get("message") or ""), 4000)
        if len(problem) < 8:
            raise HTTPException(422, "Please include a short description of the problem.")
        transcript = data.get("transcript") if isinstance(data.get("transcript"), list) else []
        clean_transcript = []
        for item in transcript[-30:]:
            if not isinstance(item, dict):
                continue
            sender = str(item.get("sender") or item.get("role") or "customer").lower()
            sender = "ai" if sender in {"assistant", "growthintel ai"} else "admin" if sender == "growthintel support" else "customer"
            clean_transcript.append({"sender": sender if sender in {"customer", "ai", "admin"} else "customer", "body": _redact_sensitive(item.get("body") or item.get("content") or "", 2500)})
        access_token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        with _lock, _db_begin() as connection:
            user = _support_user_for_request(request, connection)
            if user:
                email = user["email"]
                name = user["name"]
                plan = user["plan_version"] or ("free" if user["membership_state"] != "ACTIVE" else PREMIUM_PLAN_VERSION)
                membership_state = user["membership_state"]
            else:
                email = _email(data.get("email"))
                name = _name(data.get("name"))
                plan = "anonymous"
                membership_state = "NO_ACCOUNT"
            category = _support_category(problem)
            priority = _support_priority(problem, category, membership_state)
            summary = _support_summary(problem, clean_transcript, category, priority)
            now = _now()
            result = connection.execute(
                support_tickets.insert().values(
                    ticket_ref=_ticket_ref(connection),
                    user_id=user["id"] if user else None,
                    customer_email=email,
                    customer_name=name,
                    account_plan=plan,
                    membership_state=membership_state,
                    current_page=_short_text(data.get("current_page"), 500),
                    category=category,
                    priority=priority,
                    original_problem=problem,
                    ai_troubleshooting=json.dumps(clean_transcript[-10:], separators=(",", ":")),
                    ai_summary=summary,
                    status="HUMAN_REQUESTED",
                    assigned_status="unassigned",
                    notification_status="PENDING",
                    notification_attempts=0,
                    customer_access_hash=token_hash,
                    created_at=now,
                    updated_at=now,
                    customer_last_reply_at=now,
                )
            )
            ticket_id = result.inserted_primary_key[0]
            for item in clean_transcript:
                if item["body"]:
                    connection.execute(support_messages.insert().values(ticket_id=ticket_id, sender=item["sender"], body=item["body"], created_at=now))
            connection.execute(support_messages.insert().values(ticket_id=ticket_id, sender="customer", body=problem, created_at=now))
            ticket = connection.execute(select(support_tickets).where(support_tickets.c.id == ticket_id)).mappings().one()
            sms_result = _send_support_sms(_sms_body(ticket["ticket_ref"], category, priority, summary))
            notification_status = sms_result.get("status", "FAILED")
            attempts = 0 if notification_status == "NOT_CONFIGURED" else 1
            _record_support_notification(connection, ticket_id, "sms", sms_result)
            if notification_status != "SENT":
                email_result = _send_support_fallback_email(f"GrowthIntel Support {ticket['ticket_ref']}", _sms_body(ticket["ticket_ref"], category, priority, summary) + "\n\n" + summary)
                _record_support_notification(connection, ticket_id, "email", email_result)
                if email_result.get("status") == "SENT":
                    notification_status = "SENT"
                elif notification_status != "NOT_CONFIGURED":
                    notification_status = "FAILED"
            connection.execute(
                update(support_tickets)
                .where(support_tickets.c.id == ticket_id)
                .values(
                    notification_status=notification_status,
                    notification_attempts=attempts,
                    notification_error=None if notification_status == "SENT" else _short_text(sms_result.get("error"), 800),
                    sms_sent_at=now if sms_result.get("status") == "SENT" else None,
                )
            )
            _audit(connection, "SUPPORT_TICKET_CREATED", user["id"] if user else None, user["id"] if user else None, detail={"ticket_ref": ticket["ticket_ref"], "category": category, "priority": priority}, ip=request.client.host if request.client else None)
            ticket = connection.execute(select(support_tickets).where(support_tickets.c.id == ticket_id)).mappings().one()
            return JSONResponse(_support_ticket_response(connection, ticket, True, access_token), status_code=201)

    @real_app.get("/api/v1/support/tickets")
    async def support_ticket_history(request: Request):
        with _db_connect() as connection:
            user = _require_user(request, connection)
            rows = connection.execute(
                select(support_tickets)
                .where(support_tickets.c.user_id == user["id"])
                .order_by(support_tickets.c.updated_at.desc())
                .limit(100)
            ).mappings().all()
            return [_support_ticket_response(connection, row, False) for row in rows]

    @real_app.get("/api/v1/support/tickets/{ticket_ref}")
    async def support_ticket_detail(ticket_ref: str, request: Request, token: str = ""):
        with _db_connect() as connection:
            row = connection.execute(select(support_tickets).where(support_tickets.c.ticket_ref == ticket_ref)).mappings().first()
            if not row:
                raise HTTPException(404, "Support ticket not found")
            user = _support_user_for_request(request, connection)
            token_ok = token and row["customer_access_hash"] == hashlib.sha256(token.encode()).hexdigest()
            if not token_ok and not (user and (user["id"] == row["user_id"] or _is_admin(user))):
                raise HTTPException(403, "You can only view your own support tickets")
            return _support_ticket_response(connection, row, True)

    @real_app.post("/api/v1/support/tickets/{ticket_ref}/messages")
    async def support_customer_reply(ticket_ref: str, request: Request, token: str = ""):
        _check_origin(request)
        _rate(request, "support-reply", 12, 300)
        data = await _json(request)
        body = _redact_sensitive(str(data.get("message") or ""), 4000)
        if len(body) < 2:
            raise HTTPException(422, "Message is too short")
        with _db_begin() as connection:
            row = connection.execute(select(support_tickets).where(support_tickets.c.ticket_ref == ticket_ref)).mappings().first()
            if not row:
                raise HTTPException(404, "Support ticket not found")
            user = _support_user_for_request(request, connection)
            token_ok = token and row["customer_access_hash"] == hashlib.sha256(token.encode()).hexdigest()
            if not token_ok and not (user and (user["id"] == row["user_id"] or _is_admin(user))):
                raise HTTPException(403, "You can only reply to your own support tickets")
            now = _now()
            connection.execute(support_messages.insert().values(ticket_id=row["id"], sender="customer", body=body, created_at=now))
            connection.execute(update(support_tickets).where(support_tickets.c.id == row["id"]).values(status="HUMAN_REQUESTED", updated_at=now, customer_last_reply_at=now))
            row = connection.execute(select(support_tickets).where(support_tickets.c.id == row["id"])).mappings().one()
            return _support_ticket_response(connection, row, True)

    @real_app.get("/api/v1/support/admin/tickets")
    async def support_admin_tickets(request: Request, status: str = "", q: str = ""):
        with _db_connect() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            query = select(support_tickets).order_by(support_tickets.c.updated_at.desc()).limit(200)
            if status:
                query = query.where(support_tickets.c.status == status[:32])
            if q.strip():
                term = f"%{q.strip()[:100].lower()}%"
                query = query.where(or_(support_tickets.c.ticket_ref.ilike(term), support_tickets.c.customer_email.ilike(term), support_tickets.c.category.ilike(term)))
            rows = connection.execute(query).mappings().all()
            return [_support_ticket_response(connection, row, False) for row in rows]

    @real_app.get("/api/v1/support/admin/tickets/{ticket_ref}")
    async def support_admin_ticket_detail(ticket_ref: str, request: Request):
        with _db_connect() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            row = connection.execute(select(support_tickets).where(support_tickets.c.ticket_ref == ticket_ref)).mappings().first()
            if not row:
                raise HTTPException(404, "Support ticket not found")
            return _support_ticket_response(connection, row, True)

    @real_app.post("/api/v1/support/admin/tickets/{ticket_ref}/reply")
    async def support_admin_reply(ticket_ref: str, request: Request):
        _check_origin(request)
        data = await _json(request)
        body = _redact_sensitive(str(data.get("message") or ""), 4000)
        if len(body) < 2:
            raise HTTPException(422, "Message is too short")
        with _db_begin() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            row = connection.execute(select(support_tickets).where(support_tickets.c.ticket_ref == ticket_ref)).mappings().first()
            if not row:
                raise HTTPException(404, "Support ticket not found")
            now = _now()
            connection.execute(support_messages.insert().values(ticket_id=row["id"], sender="admin", body=body, created_at=now))
            connection.execute(update(support_tickets).where(support_tickets.c.id == row["id"]).values(status="WAITING_FOR_CUSTOMER", assigned_status="admin_replied", updated_at=now, admin_last_reply_at=now))
            _audit(connection, "SUPPORT_ADMIN_REPLIED", admin["id"], row["user_id"], detail={"ticket_ref": ticket_ref}, ip=request.client.host if request.client else None)
            row = connection.execute(select(support_tickets).where(support_tickets.c.id == row["id"])).mappings().one()
            return _support_ticket_response(connection, row, True)

    @real_app.post("/api/v1/support/admin/tickets/{ticket_ref}/status")
    async def support_admin_status(ticket_ref: str, request: Request):
        _check_origin(request)
        data = await _json(request)
        next_status = str(data.get("status") or "").upper()
        allowed = {"HUMAN_REVIEWING", "WAITING_FOR_CUSTOMER", "RESOLVED", "CLOSED", "HUMAN_REQUESTED", "OPEN"}
        if next_status not in allowed:
            raise HTTPException(422, "Unsupported ticket status")
        with _db_begin() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            row = connection.execute(select(support_tickets).where(support_tickets.c.ticket_ref == ticket_ref)).mappings().first()
            if not row:
                raise HTTPException(404, "Support ticket not found")
            now = _now()
            connection.execute(update(support_tickets).where(support_tickets.c.id == row["id"]).values(status=next_status, updated_at=now))
            _audit(connection, "SUPPORT_STATUS_CHANGED", admin["id"], row["user_id"], detail={"ticket_ref": ticket_ref, "status": next_status}, ip=request.client.host if request.client else None)
            row = connection.execute(select(support_tickets).where(support_tickets.c.id == row["id"])).mappings().one()
            return _support_ticket_response(connection, row, True)

    @real_app.get("/api/v1/membership/health")
    async def membership_health():
        with _db_connect() as connection:
            connection.execute(select(func.count()).select_from(users)).scalar()
        return {
            "status": "HEALTHY",
            "database": "reachable",
            "checked_at": datetime.fromtimestamp(_now(), timezone.utc).isoformat(),
        }

    @real_app.get("/api/v1/membership/admin/health")
    async def admin_health(request: Request):
        with _db_begin() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            return _membership_metrics(connection)

    @real_app.get("/api/v1/membership/admin/audit-logs")
    async def audit_logs(request: Request):
        with _db_connect() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            return [dict(row) for row in connection.execute(select(audits).order_by(audits.c.created_at.desc()).limit(250)).mappings().all()]
