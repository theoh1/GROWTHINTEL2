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
from collections import defaultdict, deque
from datetime import datetime, timezone
from email.utils import parseaddr

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
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

PRICE_PENCE = 1300
PRICE_CURRENCY = "GBP"
SESSION_COOKIE = "gi_session"
SESSION_SECONDS = 60 * 60 * 24 * 14
PUBLIC_PATHS = {"/api/v1/health", "/api/v1/status"}

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

Index("ix_membership_payment_user_created", payments.c.user_id, payments.c.created_at)

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
    return engine


def _validate_configuration() -> None:
    required = (
        "BANK_TRANSFER_ACCOUNT_NAME",
        "BANK_TRANSFER_SORT_CODE",
        "BANK_TRANSFER_ACCOUNT_NUMBER",
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
        "is_admin": _is_admin(user),
    }


async def _json(request: Request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "A JSON request body is required")


def install_membership(real_app) -> None:
    global _engine
    _validate_configuration()
    _engine = _configure_storage()

    @real_app.middleware("http")
    async def membership_enforcement(request: Request, call_next):
        protected = (
            request.url.path.startswith("/api/v1/")
            and request.url.path not in PUBLIC_PATHS
            and not request.url.path.startswith("/api/v1/membership/")
        )
        if protected:
            with _engine.begin() as connection:
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
                    return JSONResponse({"detail": "An active Growth Intel membership is required"}, status_code=403)
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

        try:
            with _lock, _engine.begin() as connection:
                if connection.execute(select(users.c.id).where(users.c.email == email)).first():
                    raise HTTPException(409, "An account with that email already exists")
                result = connection.execute(
                    users.insert().values(
                        email=email,
                        name=name,
                        password_hash=_password(password),
                        gi_reference=_reference(connection),
                        membership_state="NONE",
                        created_at=_now(),
                    )
                )
                user_id = result.inserted_primary_key[0]
                _audit(connection, "USER_REGISTERED", user_id, user_id, detail={"email": email}, ip=request.client.host if request.client else None)
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
        with _engine.begin() as connection:
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
        with _engine.begin() as connection:
            if token:
                connection.execute(sessions.delete().where(sessions.c.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @real_app.get("/api/v1/membership/me")
    async def me(request: Request):
        with _engine.connect() as connection:
            return _public_user(_require_user(request, connection))

    @real_app.get("/api/v1/membership/bank-transfer")
    async def bank_transfer(request: Request):
        with _engine.connect() as connection:
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
                with _engine.begin() as connection:
                    user = _require_user(request, connection)
                    result = connection.execute(
                        payments.insert().values(
                            user_id=user["id"],
                            amount_pence=PRICE_PENCE,
                            currency=PRICE_CURRENCY,
                            status="PENDING",
                            pending_guard="PENDING",
                            created_at=_now(),
                        )
                    )
                    payment_id = result.inserted_primary_key[0]
                    active = user["membership_state"] == "ACTIVE" and user["membership_expires_at"] and user["membership_expires_at"] > _now()
                    if not active:
                        connection.execute(update(users).where(users.c.id == user["id"]).values(membership_state="PENDING_PAYMENT"))
                    _audit(connection, "PAYMENT_SUBMITTED", user["id"], user["id"], payment_id, {"amount_pence": PRICE_PENCE}, request.client.host if request.client else None)
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
        with _engine.connect() as connection:
            user = _require_user(request, connection)
            rows = connection.execute(select(payments).where(payments.c.user_id == user["id"]).order_by(payments.c.created_at.desc())).mappings().all()
        return [{key: value for key, value in row.items() if key != "pending_guard"} for row in rows]

    @real_app.get("/api/v1/membership/admin/payments")
    async def admin_search(request: Request, q: str = ""):
        with _engine.connect() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            term = f"%{q.strip()[:100].lower()}%"
            query = (
                select(payments, users.c.email, users.c.name, users.c.gi_reference, users.c.membership_state, users.c.membership_expires_at)
                .join(users, users.c.id == payments.c.user_id)
                .where(or_(users.c.gi_reference.ilike(term), users.c.email.ilike(term), users.c.name.ilike(term)))
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
        with _engine.begin() as connection:
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
                connection.execute(update(users).where(users.c.id == user["id"]).values(membership_state="ACTIVE", membership_expires_at=_add_calendar_month(base)))
            elif user["membership_state"] != "ACTIVE":
                rejected_state = "EXPIRED" if user["membership_expires_at"] and user["membership_expires_at"] <= now else "NONE"
                connection.execute(update(users).where(users.c.id == user["id"]).values(membership_state=rejected_state))
            _audit(connection, "PAYMENT_" + status, admin["id"], user["id"], payment_id, {"note": note}, request.client.host if request.client else None)
        return {"id": payment_id, "status": status}

    @real_app.get("/api/v1/membership/admin/audit-logs")
    async def audit_logs(request: Request):
        with _engine.connect() as connection:
            admin = _require_user(request, connection)
            if not _is_admin(admin):
                raise HTTPException(403, "Administrator access required")
            return [dict(row) for row in connection.execute(select(audits).order_by(audits.c.created_at.desc()).limit(250)).mappings().all()]
