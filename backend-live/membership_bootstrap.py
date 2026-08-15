"""Server-authoritative membership and UK bank-transfer routes.

This module deliberately uses only the Python standard library plus FastAPI so it
can be installed over the downloaded backend archive without changing that
archive's dependency set.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

PRICE_PENCE = 1300
PRICE_CURRENCY = "GBP"
SESSION_COOKIE = "gi_session"
SESSION_SECONDS = 60 * 60 * 24 * 14
MEMBERSHIP_DAYS = 30
PUBLIC_PATHS = {"/api/v1/health", "/api/v1/status"}
PROTECTED_PREFIXES = (
    "/api/v1/ai/", "/api/v1/top-stocks", "/api/v1/refresh",
    "/api/v1/watchlist", "/api/v1/history", "/api/v1/alerts",
    "/api/v1/news", "/api/v1/earnings", "/api/v1/stock-news",
    "/api/v1/sector-news", "/api/v1/breakouts", "/api/v1/scan-breakouts",
    "/api/v1/portfolio", "/api/v1/market-overview", "/api/v1/smart-money",
    "/api/v1/creator-intel", "/api/v1/early-view",
)
_lock = threading.RLock()
_attempts: dict[str, deque[float]] = defaultdict(deque)


def _now() -> int:
    return int(time.time())


def _db_path() -> str:
    path = os.environ.get("MEMBERSHIP_DB_PATH", "membership.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(_db_path(), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def _init_db() -> None:
    with _connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          name TEXT NOT NULL, password_hash TEXT NOT NULL,
          gi_reference TEXT NOT NULL UNIQUE, membership_state TEXT NOT NULL DEFAULT 'NONE'
            CHECK(membership_state IN ('NONE','PENDING_PAYMENT','ACTIVE','EXPIRED')),
          membership_expires_at INTEGER, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_requests (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), amount_pence INTEGER NOT NULL,
          currency TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED')),
          created_at INTEGER NOT NULL, reviewed_at INTEGER, reviewed_by INTEGER REFERENCES users(id), admin_note TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_payment ON payment_requests(user_id) WHERE status='PENDING';
        CREATE TABLE IF NOT EXISTS audit_logs (
          id INTEGER PRIMARY KEY, actor_user_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
          target_user_id INTEGER REFERENCES users(id), payment_id INTEGER, detail TEXT NOT NULL,
          created_at INTEGER NOT NULL, ip_address TEXT
        );
        CREATE INDEX IF NOT EXISTS users_gi_reference ON users(gi_reference);
        CREATE INDEX IF NOT EXISTS payments_user_created ON payment_requests(user_id, created_at DESC);
        """)


def _password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$", 2)
        return hmac.compare_digest(_password(password, bytes.fromhex(salt)), encoded)
    except (ValueError, TypeError):
        return False


def _reference(db: sqlite3.Connection) -> str:
    # 25 bits of randomness represented in five unambiguous base32 characters;
    # the database uniqueness constraint and retry make collisions harmless.
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    for _ in range(64):
        value = secrets.randbits(25)
        suffix = "".join(alphabet[(value >> (5 * shift)) & 31] for shift in range(4, -1, -1))
        ref = f"GI-{suffix}"
        if not db.execute("SELECT 1 FROM users WHERE gi_reference=?", (ref,)).fetchone():
            return ref
    raise RuntimeError("Unable to allocate membership reference")


def _audit(db, action, actor=None, target=None, payment=None, detail=None, ip=None):
    db.execute("INSERT INTO audit_logs(actor_user_id,action,target_user_id,payment_id,detail,created_at,ip_address) VALUES(?,?,?,?,?,?,?)",
               (actor, action, target, payment, json.dumps(detail or {}, separators=(",", ":")), _now(), ip))


def _email(value: object) -> str:
    value = str(value or "").strip().lower()
    if len(value) > 254 or parseaddr(value)[1] != value or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(422, "Enter a valid email address")
    return value


def _body_text(value: object, label: str, low: int, high: int) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if not low <= len(value) <= high:
        raise HTTPException(422, f"{label} must be {low}-{high} characters")
    return value


def _allowed_origins() -> set[str]:
    return {x.strip().rstrip("/") for x in os.environ.get("MEMBERSHIP_ALLOWED_ORIGINS", "").split(",") if x.strip()}


def _check_origin(request: Request) -> None:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin or origin not in _allowed_origins():
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


def _session_user(request: Request, db: sqlite3.Connection):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    return db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (digest, _now())).fetchone()


def _require_user(request: Request, db):
    user = _session_user(request, db)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def _is_admin(user) -> bool:
    admins = {x.strip().lower() for x in os.environ.get("MEMBERSHIP_ADMIN_EMAILS", "").split(",") if x.strip()}
    return bool(user and user["email"].lower() in admins)


def _public_user(user):
    state = user["membership_state"]
    expiry = user["membership_expires_at"]
    if state == "ACTIVE" and expiry and expiry <= _now():
        state = "EXPIRED"
    return {"id": user["id"], "email": user["email"], "name": user["name"], "gi_reference": user["gi_reference"],
            "membership_state": state, "membership_expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat() if expiry else None,
            "is_admin": _is_admin(user)}


async def _json(request: Request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "A JSON request body is required")


def install_membership(real_app) -> None:
    _init_db()

    @real_app.middleware("http")
    async def membership_enforcement(request: Request, call_next):
        path = request.url.path
        if path.startswith(PROTECTED_PREFIXES) and path not in PUBLIC_PATHS:
            with _connect() as db:
                user = _session_user(request, db)
                if not user:
                    return JSONResponse({"detail": "Authentication required"}, status_code=401)
                if user["membership_state"] != "ACTIVE" or not user["membership_expires_at"] or user["membership_expires_at"] <= _now():
                    if user["membership_state"] == "ACTIVE":
                        db.execute("UPDATE users SET membership_state='EXPIRED' WHERE id=?", (user["id"],))
                    return JSONResponse({"detail": "An active Growth Intel membership is required"}, status_code=403)
        return await call_next(request)

    @real_app.post("/api/v1/membership/register")
    async def register(request: Request):
        _check_origin(request); _rate(request, "register", 5, 300)
        data = await _json(request); email = _email(data.get("email")); name = _body_text(data.get("name"), "Name", 2, 100)
        password = str(data.get("password") or "")
        if len(password) < 12 or len(password) > 128:
            raise HTTPException(422, "Password must be 12-128 characters")
        with _lock, _connect() as db:
            if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise HTTPException(409, "An account with that email already exists")
            cur = db.execute("INSERT INTO users(email,name,password_hash,gi_reference,created_at) VALUES(?,?,?,?,?)", (email, name, _password(password), _reference(db), _now()))
            _audit(db, "USER_REGISTERED", cur.lastrowid, cur.lastrowid, detail={"email": email}, ip=request.client.host if request.client else None)
            user = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return _public_user(user)

    @real_app.post("/api/v1/membership/login")
    async def login(request: Request):
        _check_origin(request); _rate(request, "login", 8, 300); data = await _json(request); email = _email(data.get("email"))
        with _connect() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not user or not _verify(str(data.get("password") or ""), user["password_hash"]):
                raise HTTPException(401, "Invalid email or password")
            token = secrets.token_urlsafe(32); now = _now()
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user["id"], now + SESSION_SECONDS, now))
            _audit(db, "USER_LOGGED_IN", user["id"], user["id"], ip=request.client.host if request.client else None)
        response = JSONResponse(_public_user(user)); response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=True, samesite="strict", path="/")
        return response

    @real_app.post("/api/v1/membership/logout")
    async def logout(request: Request):
        _check_origin(request); token = request.cookies.get(SESSION_COOKIE, "")
        with _connect() as db:
            if token: db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response = JSONResponse({"ok": True}); response.delete_cookie(SESSION_COOKIE, path="/"); return response

    @real_app.get("/api/v1/membership/me")
    async def me(request: Request):
        with _connect() as db: return _public_user(_require_user(request, db))

    @real_app.get("/api/v1/membership/bank-transfer")
    async def bank_transfer(request: Request):
        with _connect() as db: user = _require_user(request, db)
        values = {k: os.environ.get(k, "").strip() for k in ("BANK_TRANSFER_ACCOUNT_NAME", "BANK_TRANSFER_SORT_CODE", "BANK_TRANSFER_ACCOUNT_NUMBER")}
        if not all(values.values()): raise HTTPException(503, "Bank-transfer details are not configured")
        return {"account_name": values["BANK_TRANSFER_ACCOUNT_NAME"], "sort_code": values["BANK_TRANSFER_SORT_CODE"],
                "account_number": values["BANK_TRANSFER_ACCOUNT_NUMBER"], "reference": user["gi_reference"], "amount_pence": PRICE_PENCE,
                "amount_display": "£13.00", "currency": PRICE_CURRENCY, "standing_order_available": bool(user["membership_expires_at"])}

    @real_app.post("/api/v1/membership/payment-requests")
    async def request_payment(request: Request):
        _check_origin(request); _rate(request, "payment", 6, 300)
        with _lock, _connect() as db:
            user = _require_user(request, db)
            try:
                cur = db.execute("INSERT INTO payment_requests(user_id,amount_pence,currency,status,created_at) VALUES(?,?,?,'PENDING',?)", (user["id"], PRICE_PENCE, PRICE_CURRENCY, _now()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "A payment is already awaiting verification")
            if user["membership_state"] != "ACTIVE": db.execute("UPDATE users SET membership_state='PENDING_PAYMENT' WHERE id=?", (user["id"],))
            _audit(db, "PAYMENT_SUBMITTED", user["id"], user["id"], cur.lastrowid, {"amount_pence": PRICE_PENCE}, request.client.host if request.client else None)
        return JSONResponse({"id": cur.lastrowid, "status": "PENDING", "amount_pence": PRICE_PENCE, "message": "Payment submitted for manual verification. Access is not yet active."}, status_code=201)

    @real_app.get("/api/v1/membership/payments")
    async def payments(request: Request):
        with _connect() as db:
            user = _require_user(request, db); rows = db.execute("SELECT id,amount_pence,currency,status,created_at,reviewed_at,admin_note FROM payment_requests WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        return [dict(x) for x in rows]

    @real_app.get("/api/v1/membership/admin/payments")
    async def admin_search(request: Request, q: str = ""):
        with _connect() as db:
            admin = _require_user(request, db)
            if not _is_admin(admin): raise HTTPException(403, "Administrator access required")
            term = f"%{q.strip()[:100]}%"
            rows = db.execute("SELECT p.*,u.email,u.name,u.gi_reference,u.membership_state,u.membership_expires_at FROM payment_requests p JOIN users u ON u.id=p.user_id WHERE u.gi_reference LIKE ? OR u.email LIKE ? OR u.name LIKE ? ORDER BY p.created_at DESC LIMIT 100", (term, term, term)).fetchall()
        return [dict(x) for x in rows]

    @real_app.post("/api/v1/membership/admin/payments/{payment_id}/{decision}")
    async def admin_decide(payment_id: int, decision: str, request: Request):
        _check_origin(request)
        if decision not in {"approve", "reject"}: raise HTTPException(404, "Unknown decision")
        data = await _json(request); note = str(data.get("note") or "").strip()[:500]
        with _lock, _connect() as db:
            admin = _require_user(request, db)
            if not _is_admin(admin): raise HTTPException(403, "Administrator access required")
            payment = db.execute("SELECT * FROM payment_requests WHERE id=?", (payment_id,)).fetchone()
            if not payment: raise HTTPException(404, "Payment request not found")
            if payment["status"] != "PENDING": raise HTTPException(409, "Payment has already been reviewed")
            user = db.execute("SELECT * FROM users WHERE id=?", (payment["user_id"],)).fetchone(); now = _now()
            status = decision.upper() + "D" if decision == "approve" else "REJECTED"
            db.execute("UPDATE payment_requests SET status=?,reviewed_at=?,reviewed_by=?,admin_note=? WHERE id=?", (status, now, admin["id"], note, payment_id))
            if decision == "approve":
                base = user["membership_expires_at"] if user["membership_state"] == "ACTIVE" and user["membership_expires_at"] and user["membership_expires_at"] > now else now
                db.execute("UPDATE users SET membership_state='ACTIVE',membership_expires_at=? WHERE id=?", (base + MEMBERSHIP_DAYS * 86400, user["id"]))
            else:
                remaining = db.execute("SELECT 1 FROM payment_requests WHERE user_id=? AND status='PENDING'", (user["id"],)).fetchone()
                if not remaining and user["membership_state"] != "ACTIVE": db.execute("UPDATE users SET membership_state='NONE' WHERE id=?", (user["id"],))
            _audit(db, "PAYMENT_" + status, admin["id"], user["id"], payment_id, {"note": note}, request.client.host if request.client else None)
        return {"id": payment_id, "status": status}

    @real_app.get("/api/v1/membership/admin/audit-logs")
    async def audit_logs(request: Request):
        with _connect() as db:
            admin = _require_user(request, db)
            if not _is_admin(admin): raise HTTPException(403, "Administrator access required")
            return [dict(x) for x in db.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 250").fetchall()]
