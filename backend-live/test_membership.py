from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

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
    monkeypatch.setenv("MEMBERSHIP_ENFORCE_API_ACCESS", "true")
    monkeypatch.setenv("BANK_TRANSFER_ACCOUNT_NAME", "Test Growth Intel")
    monkeypatch.setenv("BANK_TRANSFER_SORT_CODE", "00-11-22")
    monkeypatch.setenv("BANK_TRANSFER_ACCOUNT_NUMBER", "12345678")
    monkeypatch.setenv("BANK_TRANSFER_BANK_NAME", "Lloyds Bank")
    membership._attempts.clear()
    app = FastAPI()

    @app.get("/api/v1/top-stocks")
    async def protected():
        return {"secret": True}

    membership.install_membership(app)
    return TestClient(app, base_url=ORIGIN), engine


def post(client, path, body):
    return client.post(path, json=body, headers={"Origin": ORIGIN})


def register_login(client, email, name):
    registered = post(client, "/api/v1/membership/register", {"email": email, "name": name, "password": PASSWORD})
    assert registered.status_code == 200
    reference = registered.json()["gi_reference"]
    assert reference.startswith("GI-")
    assert len(reference) == 8
    assert post(client, "/api/v1/membership/login", {"email": email, "password": PASSWORD}).status_code == 200
    return registered.json()


def test_register_route_get_is_405_not_404(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    response = client.get("/api/v1/membership/register")
    assert response.status_code == 405


def test_api_enforcement_is_opt_in(tmp_path, monkeypatch):
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
    monkeypatch.delenv("MEMBERSHIP_ENFORCE_API_ACCESS", raising=False)

    app = FastAPI()

    @app.get("/api/v1/top-stocks")
    async def public_data_route():
        return {"ok": True}

    membership.install_membership(app)
    client = TestClient(app, base_url=ORIGIN)
    assert client.get("/api/v1/top-stocks").status_code == 200


def test_email_signup_preferences_referral_unsubscribe_and_cron(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    monkeypatch.setenv("EMAIL_PROVIDER", "mailjet")
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "false")
    monkeypatch.setenv("EMAIL_BASE_URL", "https://www.growthintel.app")
    monkeypatch.setenv("EMAIL_CRON_SECRET", "test-cron-secret")

    first = post(
        client,
        "/api/v1/email/subscribe",
        {
            "email": "subscriber@example.com",
            "preferences": {"weekly_growth_brief": True, "marketing": True},
            "signup_source": "growth-brief",
        },
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["created"] is True
    assert first_payload["email_delivery_status"] == "prepared"
    assert first_payload["referral_url"].startswith("https://www.growthintel.app/growth-brief?ref=")

    duplicate = post(
        client,
        "/api/v1/email/subscribe",
        {
            "email": "subscriber@example.com",
            "preferences": {"weekly_growth_brief": True, "marketing": True},
            "signup_source": "growth-brief",
        },
    )
    assert duplicate.status_code == 200
    duplicate_payload = duplicate.json()
    assert duplicate_payload["created"] is False
    assert duplicate_payload["referral_code"] == first_payload["referral_code"]

    referred = post(
        client,
        "/api/v1/email/subscribe",
        {
            "email": "friend@example.com",
            "preferences": {"weekly_growth_brief": True},
            "signup_source": "growth-brief",
            "referring_code": duplicate_payload["referral_code"],
        },
    )
    assert referred.status_code == 200

    token = duplicate_payload["management_token"]
    owner = client.get("/api/v1/email/preferences", params={"email": "subscriber@example.com", "token": token})
    assert owner.status_code == 200
    assert owner.json()["referral_count"] == 1

    updated = client.put(
        "/api/v1/email/preferences",
        headers={"Origin": ORIGIN},
        json={
            "email": "subscriber@example.com",
            "management_token": token,
            "preferences": {"weekly_growth_brief": False, "alert_emails": True},
            "signup_source": "settings",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["preferences"]["alert_emails"] is True
    assert updated.json()["preferences"]["weekly_growth_brief"] is False

    alert = post(
        client,
        "/api/v1/email/alert-event",
        {
            "email": "subscriber@example.com",
            "management_token": token,
            "title": "Test alert",
            "details": ["Volume changed"],
            "alert_id": "alert-1",
        },
    )
    assert alert.status_code == 200
    assert alert.json()["status"] == "prepared"
    duplicate_alert = post(
        client,
        "/api/v1/email/alert-event",
        {
            "email": "subscriber@example.com",
            "management_token": token,
            "title": "Test alert",
            "details": ["Volume changed"],
            "alert_id": "alert-1",
        },
    )
    assert duplicate_alert.json()["queued"] is False

    assert client.post("/api/v1/cron/weekly-growth-brief", json={"dry_run": True}).status_code == 403
    cron = client.post(
        "/api/v1/cron/weekly-growth-brief",
        headers={"Authorization": "Bearer test-cron-secret"},
        json={"dry_run": True},
    )
    assert cron.status_code == 200
    assert cron.json()["prepared"] == 1

    unsubscribed = post(
        client,
        "/api/v1/email/unsubscribe",
        {"email": "subscriber@example.com", "management_token": token},
    )
    assert unsubscribed.status_code == 200
    assert unsubscribed.json()["status"] == "unsubscribed"
    assert all(value is False for value in unsubscribed.json()["preferences"].values())


def test_full_membership_and_admin_flow(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    assert client.get("/api/v1/top-stocks").status_code == 401

    member = register_login(client, "member@example.com", "Example Member")
    assert client.get("/api/v1/top-stocks").status_code == 403

    bank = client.get("/api/v1/membership/bank-transfer").json()
    assert bank == {
        "bank_name": "Lloyds Bank",
        "account_name": "Test Growth Intel",
        "sort_code": "00-11-22",
        "account_number": "12345678",
        "reference": member["gi_reference"],
        "amount_pence": 1300,
        "amount_display": "£13.00",
        "currency": "GBP",
        "standing_order_available": False,
    }

    pending = post(client, "/api/v1/membership/payment-requests", {})
    assert pending.status_code == 201
    assert pending.json()["status"] == "PENDING"
    assert post(client, "/api/v1/membership/payment-requests", {}).status_code == 409
    assert client.get("/api/v1/membership/admin/payments").status_code == 403
    client.cookies.clear()

    register_login(client, "admin@example.com", "Membership Admin")
    found = client.get("/api/v1/membership/admin/payments", params={"q": member["gi_reference"]}).json()
    assert len(found) == 1
    assert found[0]["email"] == "member@example.com"
    approved = post(client, f"/api/v1/membership/admin/payments/{found[0]['id']}/approve", {"note": "Matched statement"})
    assert approved.json()["status"] == "APPROVED"
    assert post(client, f"/api/v1/membership/admin/payments/{found[0]['id']}/approve", {}).status_code == 409
    assert any(row["action"] == "PAYMENT_APPROVED" for row in client.get("/api/v1/membership/admin/audit-logs").json())

    member_id = found[0]["user_id"]
    with engine.connect() as db:
        first_expiry = db.execute(text("SELECT membership_expires_at FROM membership_users WHERE id=:id"), {"id": member_id}).scalar_one()

    client.cookies.clear()
    assert post(client, "/api/v1/membership/login", {"email": "member@example.com", "password": PASSWORD}).status_code == 200
    assert client.get("/api/v1/top-stocks").status_code == 200

    second = post(client, "/api/v1/membership/payment-requests", {}).json()
    client.cookies.clear()
    post(client, "/api/v1/membership/login", {"email": "admin@example.com", "password": PASSWORD})
    assert post(client, f"/api/v1/membership/admin/payments/{second['id']}/approve", {}).status_code == 200
    with engine.connect() as db:
        second_expiry = db.execute(text("SELECT membership_expires_at FROM membership_users WHERE id=:id"), {"id": member_id}).scalar_one()
    assert second_expiry == membership._add_calendar_month(first_expiry)


def test_affiliate_referral_is_stored_on_user_payment_and_admin_view(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    registered = post(
        client,
        "/api/v1/membership/register",
        {
            "email": "referred@example.com",
            "name": "Referred Member",
            "password": PASSWORD,
            "affiliate_referral_code": "darren",
            "affiliate_referral_source": "affiliate-link",
            "affiliate_referral_landing_url": "https://www.growthintel.app/?ref=darren",
        },
    )
    assert registered.status_code == 200
    assert registered.json()["affiliate_referral_code"] == "DARREN"
    assert post(client, "/api/v1/membership/login", {"email": "referred@example.com", "password": PASSWORD}).status_code == 200
    pending = post(client, "/api/v1/membership/payment-requests", {})
    assert pending.status_code == 201

    with engine.connect() as db:
        user_ref = db.execute(text("SELECT affiliate_referral_code FROM membership_users WHERE email='referred@example.com'")).scalar_one()
        payment_ref = db.execute(text("SELECT affiliate_referral_code FROM membership_payment_requests")).scalar_one()
    assert user_ref == "DARREN"
    assert payment_ref == "DARREN"

    client.cookies.clear()
    register_login(client, "admin@example.com", "Membership Admin")
    rows = client.get("/api/v1/membership/admin/payments", params={"q": "DARREN"}).json()
    assert len(rows) == 1
    assert rows[0]["affiliate_referral_code"] == "DARREN"
    assert rows[0]["user_affiliate_referral_code"] == "DARREN"


def test_origin_validation_and_environment_only_bank_details(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    denied = client.post("/api/v1/membership/register", json={"email": "x@example.com", "name": "Example", "password": PASSWORD})
    assert denied.status_code == 403
    register_login(client, "env@example.com", "Environment Test")
    monkeypatch.delenv("BANK_TRANSFER_ACCOUNT_NUMBER")
    assert client.get("/api/v1/membership/bank-transfer").status_code == 503


def test_calendar_month_and_production_refuses_sqlite(tmp_path, monkeypatch):
    january_31 = int(datetime(2027, 1, 31, 12, tzinfo=timezone.utc).timestamp())
    assert datetime.fromtimestamp(membership._add_calendar_month(january_31), timezone.utc).date().isoformat() == "2027-02-28"

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


def test_premium_entitlements_are_persisted_once(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    member = register_login(client, "premium@example.com", "Premium Member")
    assert post(client, "/api/v1/membership/payment-requests", {}).status_code == 201
    client.cookies.clear()

    register_login(client, "admin@example.com", "Membership Admin")
    payment = client.get("/api/v1/membership/admin/payments", params={"q": member["gi_reference"]}).json()[0]
    assert post(client, f"/api/v1/membership/admin/payments/{payment['id']}/approve", {}).status_code == 200

    with engine.connect() as db:
        count = db.execute(
            text("SELECT COUNT(*) FROM membership_entitlements WHERE user_id=:id AND revoked_at IS NULL"),
            {"id": payment["user_id"]},
        ).scalar_one()
        plan_version = db.execute(
            text("SELECT plan_version FROM membership_users WHERE id=:id"),
            {"id": payment["user_id"]},
        ).scalar_one()
    assert count == len(membership.PREMIUM_FEATURE_IDS)
    assert plan_version == membership.PREMIUM_PLAN_VERSION

    client.cookies.clear()
    assert post(client, "/api/v1/membership/login", {"email": "premium@example.com", "password": PASSWORD}).status_code == 200
    assert post(client, "/api/v1/membership/payment-requests", {}).status_code == 201
    client.cookies.clear()
    post(client, "/api/v1/membership/login", {"email": "admin@example.com", "password": PASSWORD})
    second = next(row for row in client.get("/api/v1/membership/admin/payments", params={"q": member["gi_reference"]}).json() if row["status"] == "PENDING")
    assert post(client, f"/api/v1/membership/admin/payments/{second['id']}/approve", {}).status_code == 200
    with engine.connect() as db:
        second_count = db.execute(
            text("SELECT COUNT(*) FROM membership_entitlements WHERE user_id=:id AND revoked_at IS NULL"),
            {"id": payment["user_id"]},
        ).scalar_one()
    assert second_count == len(membership.PREMIUM_FEATURE_IDS)


def test_membership_survives_backend_reinstall(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    register_login(client, "restart@example.com", "Restart Member")
    assert post(client, "/api/v1/membership/payment-requests", {}).status_code == 201
    client.cookies.clear()
    register_login(client, "admin@example.com", "Membership Admin")
    payment = client.get("/api/v1/membership/admin/payments", params={"q": "restart@example.com"}).json()[0]
    assert post(client, f"/api/v1/membership/admin/payments/{payment['id']}/approve", {}).status_code == 200

    app = FastAPI()

    @app.get("/api/v1/top-stocks")
    async def protected_after_restart():
        return {"secret": True}

    membership.install_membership(app)
    restarted = TestClient(app, base_url=ORIGIN)
    assert post(restarted, "/api/v1/membership/login", {"email": "restart@example.com", "password": PASSWORD}).status_code == 200
    assert restarted.get("/api/v1/top-stocks").status_code == 200
    with engine.connect() as db:
        assert db.execute(text("SELECT COUNT(*) FROM membership_users WHERE email='restart@example.com'")).scalar_one() == 1


class BrokenEngine:
    def begin(self):
        raise OperationalError("SELECT 1", {}, Exception("offline"))

    def connect(self):
        raise OperationalError("SELECT 1", {}, Exception("offline"))


def test_database_outage_is_not_user_not_found_or_downgrade(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    register_login(client, "outage@example.com", "Outage Member")
    assert post(client, "/api/v1/membership/payment-requests", {}).status_code == 201
    client.cookies.clear()
    register_login(client, "admin@example.com", "Membership Admin")
    payment = client.get("/api/v1/membership/admin/payments", params={"q": "outage@example.com"}).json()[0]
    assert post(client, f"/api/v1/membership/admin/payments/{payment['id']}/approve", {}).status_code == 200
    client.cookies.clear()
    assert post(client, "/api/v1/membership/login", {"email": "outage@example.com", "password": PASSWORD}).status_code == 200

    membership._engine = BrokenEngine()
    response = client.get("/api/v1/membership/me")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"

    with engine.connect() as db:
        state = db.execute(text("SELECT membership_state FROM membership_users WHERE email='outage@example.com'")).scalar_one()
    assert state == "ACTIVE"


def test_admin_health_counts_without_exposing_emails(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    register_login(client, "health-member@example.com", "Health Member")
    client.cookies.clear()
    register_login(client, "admin@example.com", "Membership Admin")
    response = client.get("/api/v1/membership/admin/health")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "reachable"
    assert body["total_users"] == 2
    assert "active_premium_users" in body
    assert "health-member@example.com" not in str(body)


def test_support_ai_and_logged_in_human_escalation(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    register_login(client, "support-member@example.com", "Support Member")

    ai = post(client, "/api/v1/support/ai", {"message": "Premium is approved but I cannot access the app"})
    assert ai.status_code == 200
    assert ai.json()["can_escalate"] is True
    assert ai.json()["category"] == "premium_access"

    created = post(
        client,
        "/api/v1/support/tickets",
        {
            "problem": "Premium is approved but I still cannot access the app",
            "current_page": "https://www.growthintel.app/",
            "transcript": [
                {"sender": "customer", "body": "Premium is approved but I cannot access the app"},
                {"sender": "ai", "body": "Check membership state and refresh."},
            ],
        },
    )
    assert created.status_code == 201
    ticket = created.json()
    assert ticket["ticket_ref"].startswith("GI-")
    assert ticket["status"] == "HUMAN_REQUESTED"
    assert ticket["notification_status"] in {"NOT_CONFIGURED", "FAILED", "SENT"}
    assert "password" not in ticket["ai_summary"].lower()

    history = client.get("/api/v1/support/tickets")
    assert history.status_code == 200
    assert history.json()[0]["ticket_ref"] == ticket["ticket_ref"]

    client.cookies.clear()
    register_login(client, "admin@example.com", "Membership Admin")
    admin_rows = client.get("/api/v1/support/admin/tickets", params={"q": ticket["ticket_ref"]})
    assert admin_rows.status_code == 200
    assert len(admin_rows.json()) == 1
    detail = client.get(f"/api/v1/support/admin/tickets/{ticket['ticket_ref']}")
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) >= 2
    replied = post(client, f"/api/v1/support/admin/tickets/{ticket['ticket_ref']}/reply", {"message": "I am checking this now."})
    assert replied.status_code == 200
    assert replied.json()["status"] == "WAITING_FOR_CUSTOMER"
    resolved = post(client, f"/api/v1/support/admin/tickets/{ticket['ticket_ref']}/status", {"status": "RESOLVED"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"


def test_anonymous_support_ticket_requires_contact_and_redacts_sensitive_data(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    created = post(
        client,
        "/api/v1/support/tickets",
        {
            "name": "Anon Customer",
            "email": "anon@example.com",
            "problem": "I cannot sign in and my password: secret123 should be redacted",
            "transcript": [{"sender": "customer", "body": "card 4242 4242 4242 4242 and password: secret123"}],
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["customer_email"] == "anon@example.com"
    assert body["access_token"]
    assert "secret123" not in str(body)
    assert "4242 4242 4242 4242" not in str(body)
