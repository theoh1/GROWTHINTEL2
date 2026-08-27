from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).parent))
import membership_bootstrap as membership


ORIGIN = "https://growthintel.example"
PASSWORD = "correct-horse-battery-staple"


def make_client(tmp_path, monkeypatch):
    database = tmp_path / "members.db"
    engine = create_engine(f"sqlite:///{database}")
    session_module = types.ModuleType("app.db.session")
    session_module.engine = engine
    session_module.active_database_url = f"sqlite:///{database}"
    session_module.using_database_fallback = False
    monkeypatch.setitem(sys.modules, "app", types.ModuleType("app"))
    monkeypatch.setitem(sys.modules, "app.db", types.ModuleType("app.db"))
    monkeypatch.setitem(sys.modules, "app.db.session", session_module)
    monkeypatch.setenv("MEMBERSHIP_ALLOW_SQLITE", "true")
    monkeypatch.setenv("MEMBERSHIP_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("MEMBERSHIP_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("BANK_TRANSFER_ACCOUNT_NAME", "Test Growth Intel")
    monkeypatch.setenv("BANK_TRANSFER_SORT_CODE", "00-11-22")
    monkeypatch.setenv("BANK_TRANSFER_ACCOUNT_NUMBER", "12345678")
    membership._attempts.clear()
    app = FastAPI()
    @app.get("/api/v1/top-stocks")
    async def protected(): return {"secret": True}
    membership.install_membership(app)
    return TestClient(app, base_url="https://growthintel.example"), engine


def post(client, path, body):
    return client.post(path, json=body, headers={"Origin": ORIGIN})


def register_login(client, email, name):
    registered = post(client, "/api/v1/membership/register", {"email": email, "name": name, "password": PASSWORD})
    assert registered.status_code == 200
    assert registered.json()["gi_reference"].startswith("GI-")
    assert len(registered.json()["gi_reference"]) == 8
    logged_in = post(client, "/api/v1/membership/login", {"email": email, "password": PASSWORD})
    assert logged_in.status_code == 200
    return registered.json()


def test_full_membership_and_admin_flow(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    assert client.get("/api/v1/top-stocks").status_code == 401

    member = register_login(client, "member@example.com", "Example Member")
    assert client.get("/api/v1/top-stocks").status_code == 403
    bank = client.get("/api/v1/membership/bank-transfer").json()
    assert bank == {"account_name": "Test Growth Intel", "sort_code": "00-11-22", "account_number": "12345678",
                    "reference": member["gi_reference"], "amount_pence": 1300, "amount_display": "£13.00",
                    "currency": "GBP", "standing_order_available": False}
    pending = post(client, "/api/v1/membership/payment-requests", {})
    assert pending.status_code == 201 and pending.json()["status"] == "PENDING" and pending.json()["amount_pence"] == 1300
    assert post(client, "/api/v1/membership/payment-requests", {}).status_code == 409
    assert client.get("/api/v1/membership/admin/payments").status_code == 403
    member.cookies.clear()

    register_login(client, "admin@example.com", "Membership Admin")
    found = client.get("/api/v1/membership/admin/payments", params={"q": member["gi_reference"]}).json()
    assert len(found) == 1 and found[0]["email"] == "member@example.com"
    approved = post(client, f"/api/v1/membership/admin/payments/{found[0]['id']}/approve", {"note": "Matched statement"})
    assert approved.json()["status"] == "APPROVED"
    assert post(client, f"/api/v1/membership/admin/payments/{found[0]['id']}/approve", {}).status_code == 409
    assert len(client.get("/api/v1/membership/admin/payments", params={"q": "Example Member"}).json()) == 1
    assert len(client.get("/api/v1/membership/admin/payments", params={"q": "member@example.com"}).json()) == 1
    logs = client.get("/api/v1/membership/admin/audit-logs").json()
    assert any(row["action"] == "PAYMENT_APPROVED" for row in logs)
    member_id = found[0]["user_id"]
    with engine.connect() as db:
        first_expiry = db.execute(text("SELECT membership_expires_at FROM membership_users WHERE id=:id"), {"id": member_id}).scalar_one()

    client.cookies.clear(); post(client, "/api/v1/membership/login", {"email": "member@example.com", "password": PASSWORD})
    assert client.get("/api/v1/top-stocks").status_code == 200
    second = post(client, "/api/v1/membership/payment-requests", {}).json()
    client.cookies.clear(); post(client, "/api/v1/membership/login", {"email": "admin@example.com", "password": PASSWORD})
    assert post(client, f"/api/v1/membership/admin/payments/{second['id']}/approve", {}).status_code == 200
    with engine.connect() as db:
        second_expiry = db.execute(text("SELECT membership_expires_at FROM membership_users WHERE id=:id"), {"id": member_id}).scalar_one()
    assert second_expiry == membership._add_calendar_month(first_expiry)

    client.cookies.clear(); post(client, "/api/v1/membership/login", {"email": "member@example.com", "password": PASSWORD})
    third = post(client, "/api/v1/membership/payment-requests", {}).json()
    client.cookies.clear(); post(client, "/api/v1/membership/login", {"email": "admin@example.com", "password": PASSWORD})
    rejected = post(client, f"/api/v1/membership/admin/payments/{third['id']}/reject", {"note": "No matching transfer"})
    assert rejected.json()["status"] == "REJECTED"
    assert any(row["action"] == "PAYMENT_REJECTED" for row in client.get("/api/v1/membership/admin/audit-logs").json())

    with engine.begin() as db:
        db.execute(text("UPDATE membership_users SET membership_state='ACTIVE', membership_expires_at=:expiry WHERE id=:id"), {"expiry": membership._now() - 1, "id": member_id})
    client.cookies.clear(); post(client, "/api/v1/membership/login", {"email": "member@example.com", "password": PASSWORD})
    assert client.get("/api/v1/top-stocks").status_code == 403
    assert client.get("/api/v1/membership/me").json()["membership_state"] == "EXPIRED"


def test_origin_validation_and_environment_only_bank_details(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    denied = client.post("/api/v1/membership/register", json={"email": "x@example.com", "name": "Example", "password": PASSWORD})
    assert denied.status_code == 403
    register_login(client, "env@example.com", "Environment Test")
    monkeypatch.delenv("BANK_TRANSFER_ACCOUNT_NUMBER")
    assert client.get("/api/v1/membership/bank-transfer").status_code == 503


def test_calendar_month_and_expired_restart():
    january_31 = int(datetime(2027, 1, 31, 12, tzinfo=timezone.utc).timestamp())
    assert datetime.fromtimestamp(membership._add_calendar_month(january_31), timezone.utc).date().isoformat() == "2027-02-28"


def test_production_refuses_sqlite(tmp_path, monkeypatch):
    database = tmp_path / "unsafe.db"
    module = types.ModuleType("app.db.session")
    module.engine = create_engine(f"sqlite:///{database}")
    module.active_database_url = f"sqlite:///{database}"
    module.using_database_fallback = False
    monkeypatch.setitem(sys.modules, "app.db.session", module)
    monkeypatch.delenv("MEMBERSHIP_ALLOW_SQLITE", raising=False)
    try:
        membership._configure_storage()
        raise AssertionError("SQLite should be refused unless explicitly enabled")
    except RuntimeError as error:
        assert "Persistent membership storage" in str(error)


def test_required_configuration_validation(monkeypatch):
    monkeypatch.delenv("MEMBERSHIP_ALLOWED_ORIGINS", raising=False)
    try:
        membership._validate_configuration()
        raise AssertionError("Missing origins should stop startup")
    except RuntimeError as error:
        assert "MEMBERSHIP_ALLOWED_ORIGINS" in str(error)
