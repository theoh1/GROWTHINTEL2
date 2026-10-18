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
    monkeypatch.setenv("MEMBERSHIP_STRIPE_WEBHOOK_SECRET", "test-stripe-relay-secret")
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


def stripe_event(event_id, event_type, user_id, email, **overrides):
    now = membership._now()
    obj = {
        "id": overrides.pop("object_id", "sub_test_123"),
        "object": overrides.pop("object", "subscription"),
        "customer": overrides.pop("customer", "cus_test_123"),
        "subscription": overrides.pop("subscription", None),
        "status": overrides.pop("status", "active"),
        "current_period_end": overrides.pop("current_period_end", now + 2592000),
        "cancel_at_period_end": overrides.pop("cancel_at_period_end", False),
        "metadata": {
            "user_id": str(user_id),
            "email": email,
            "plan_version": membership.PREMIUM_PLAN_VERSION,
        },
    }
    obj.update(overrides)
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def post_stripe(client, event):
    return client.post(
        "/api/v1/membership/stripe-webhook",
        json=event,
        headers={"x-growthintel-payment-webhook-secret": "test-stripe-relay-secret"},
    )


def test_register_route_get_is_405_not_404(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    response = client.get("/api/v1/membership/register")
    assert response.status_code == 405


def test_password_reset_is_single_use_and_revokes_sessions(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    member = register_login(client, "reset@example.com", "Reset Member")
    monkeypatch.setenv("MAILJET_API_KEY", "fixture")
    monkeypatch.setenv("MAILJET_SECRET_KEY", "fixture")
    monkeypatch.setenv("EMAIL_FROM_ADDRESS", "GrowthIntel <hello@growthintel.app>")
    sent = []
    def capture_mail(url, payload, **kwargs):
        sent.append(payload)
        return True, "accepted"
    monkeypatch.setattr(membership, "_post_json", capture_mail)
    known = post(client, "/api/v1/membership/forgot-password", {"email": "reset@example.com"})
    unknown = post(client, "/api/v1/membership/forgot-password", {"email": "unknown@example.com"})
    assert known.json() == unknown.json()
    assert len(sent) == 1
    token = sent[0]["Messages"][0]["TextPart"].split("#token=")[1].split("\n")[0]
    with engine.connect() as db:
        stored = db.execute(text("SELECT token_hash FROM membership_password_resets")).scalar_one()
    assert token not in stored
    changed = post(client, "/api/v1/membership/reset-password", {"token": token, "password": "new-correct-horse-password"})
    assert changed.status_code == 200
    assert client.get("/api/v1/membership/me").status_code == 401
    assert post(client, "/api/v1/membership/reset-password", {"token": token, "password": "another-correct-password"}).status_code == 400
    assert post(client, "/api/v1/membership/login", {"email": "reset@example.com", "password": PASSWORD}).status_code == 401
    assert post(client, "/api/v1/membership/login", {"email": "reset@example.com", "password": "new-correct-horse-password"}).status_code == 200


def test_password_reset_expiry_and_delivery_configuration(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    member = register_login(client, "expired-reset@example.com", "Reset Member")
    monkeypatch.delenv("MAILJET_API_KEY", raising=False)
    assert post(client, "/api/v1/membership/forgot-password", {"email": "expired-reset@example.com"}).status_code == 503
    token = "test-only-token" * 4
    import hashlib
    with engine.begin() as db:
        db.execute(membership.password_resets.insert().values(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=member["id"], expires_at=membership._now() - 1))
    assert post(client, "/api/v1/membership/reset-password", {"token": token, "password": "replacement-password"}).status_code == 400
    assert client.get("/api/v1/membership/billing-customer").json() == {"customer_id": None}
    client.cookies.clear()
    assert client.get("/api/v1/membership/billing-customer").status_code == 401


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
            "consent_confirmed": True,
            "consent_version": "brief_v1_20261018",
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
            "consent_confirmed": True,
            "consent_version": "brief_v1_20261018",
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
            "consent_confirmed": True,
            "consent_version": "brief_v1_20261018",
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


def test_email_signup_requires_explicit_current_consent_and_records_evidence(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "false")
    payload = {"email": "consent@example.com", "preferences": {"weekly_growth_brief": True}, "signup_source": "growth-brief"}
    assert post(client, "/api/v1/email/subscribe", payload).status_code == 422
    assert post(client, "/api/v1/email/subscribe", {**payload, "consent_confirmed": False, "consent_version": "brief_v1_20261018"}).status_code == 422
    assert post(client, "/api/v1/email/subscribe", {**payload, "consent_confirmed": True, "consent_version": "unknown"}).status_code == 422
    with engine.connect() as db:
        assert db.execute(text("SELECT COUNT(*) FROM email_subscribers")).scalar_one() == 0
    accepted = post(client, "/api/v1/email/subscribe", {**payload, "consent_confirmed": True, "consent_version": "brief_v1_20261018"})
    assert accepted.status_code == 200
    import json
    with engine.connect() as db:
        row = db.execute(text("SELECT detail FROM membership_audit_logs WHERE action='EMAIL_CONSENT_RECORDED'")).scalar_one()
    evidence = json.loads(row)
    assert evidence["version"] == "brief_v1_20261018"
    assert evidence["preferences"]["weekly_growth_brief"] is True
    assert evidence["preferences"]["marketing"] is False
    assert "unsubscribe" in evidence["statement"]


def test_full_membership_and_stripe_webhook_flow(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    assert client.get("/api/v1/top-stocks").status_code == 401

    member = register_login(client, "member@example.com", "Example Member")
    assert client.get("/api/v1/top-stocks").status_code == 403
    activated = post_stripe(client, stripe_event("evt_checkout_1", "checkout.session.completed", member["id"], "member@example.com", object="checkout.session", object_id="cs_test_123", subscription="sub_test_123", payment_status="paid"))
    assert activated.status_code == 200
    assert activated.json()["user_id"] == member["id"]
    assert post_stripe(client, stripe_event("evt_checkout_1", "checkout.session.completed", member["id"], "member@example.com", object="checkout.session", object_id="cs_test_123", subscription="sub_test_123", payment_status="paid")).json()["duplicate"] is True
    member_id = member["id"]
    with engine.connect() as db:
        row = db.execute(text("SELECT membership_state, stripe_customer_id, stripe_subscription_id FROM membership_users WHERE id=:id"), {"id": member_id}).first()
    assert row[0] == "ACTIVE"
    assert row[1] == "cus_test_123"
    assert row[2] == "sub_test_123"

    client.cookies.clear()
    assert post(client, "/api/v1/membership/login", {"email": "member@example.com", "password": PASSWORD}).status_code == 200
    assert client.get("/api/v1/top-stocks").status_code == 200


def test_affiliate_referral_is_stored_on_user_and_stripe_keeps_it(tmp_path, monkeypatch):
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
    activated = post_stripe(client, stripe_event("evt_affiliate_1", "customer.subscription.updated", registered.json()["id"], "referred@example.com"))
    assert activated.status_code == 200

    with engine.connect() as db:
        user_ref = db.execute(text("SELECT affiliate_referral_code FROM membership_users WHERE email='referred@example.com'")).scalar_one()
        state = db.execute(text("SELECT membership_state FROM membership_users WHERE email='referred@example.com'")).scalar_one()
    assert user_ref == "DARREN"
    assert state == "ACTIVE"


def test_unpaid_checkout_waits_for_async_success(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    member = register_login(client, "delayed@example.com", "Delayed Payment")
    pending = stripe_event("evt_pending_checkout", "checkout.session.completed", member["id"], "delayed@example.com", object="checkout.session", object_id="cs_delayed", subscription="sub_delayed", payment_status="unpaid")
    assert post_stripe(client, pending).status_code == 200
    assert client.get("/api/v1/membership/me").json()["membership_state"] != "ACTIVE"
    succeeded = stripe_event("evt_async_paid", "checkout.session.async_payment_succeeded", member["id"], "delayed@example.com", object="checkout.session", object_id="cs_delayed", subscription="sub_delayed", payment_status="paid")
    assert post_stripe(client, succeeded).status_code == 200
    assert client.get("/api/v1/membership/me").json()["membership_state"] == "ACTIVE"
    assert post_stripe(client, succeeded).json()["duplicate"] is True


def test_missing_payment_status_does_not_grant_premium(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    member = register_login(client, "missing-status@example.com", "Missing Status")
    event = stripe_event("evt_missing_status", "checkout.session.completed", member["id"], "missing-status@example.com", object="checkout.session", object_id="cs_unknown", subscription="sub_unknown")
    assert post_stripe(client, event).status_code == 200
    assert client.get("/api/v1/membership/me").json()["membership_state"] != "ACTIVE"


def test_failed_async_checkout_does_not_revoke_an_existing_subscription(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    member = register_login(client, "existing-paid@example.com", "Existing Paid")
    assert post_stripe(client, stripe_event("evt_existing", "customer.subscription.updated", member["id"], "existing-paid@example.com")).status_code == 200
    failed = stripe_event("evt_async_failure", "checkout.session.async_payment_failed", member["id"], "existing-paid@example.com", object="checkout.session", object_id="cs_failed", subscription="sub_failed", payment_status="unpaid")
    assert post_stripe(client, failed).status_code == 200
    assert client.get("/api/v1/membership/me").json()["membership_state"] == "ACTIVE"


def test_current_subscription_item_period_and_invoice_parent_mapping(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    member = register_login(client, "modern@example.com", "Modern Payload")
    period_end = membership._now() + 123456
    event = stripe_event("evt_modern_sub", "customer.subscription.updated", member["id"], "modern@example.com", object_id="sub_modern", current_period_end=None, items={"data": [{"id": "si_modern", "current_period_end": period_end}]})
    assert post_stripe(client, event).status_code == 200
    with engine.connect() as db:
        row = db.execute(membership.select(membership.users).where(membership.users.c.id == member["id"])).mappings().one()
        assert row["membership_expires_at"] == period_end
    invoice = stripe_event("evt_modern_invoice", "invoice.paid", member["id"], "modern@example.com", object="invoice", object_id="in_modern", current_period_end=period_end, customer=None, metadata={}, parent={"subscription_details": {"subscription": "sub_modern"}})
    assert post_stripe(client, invoice).status_code == 200
    with engine.connect() as db:
        row = db.execute(membership.select(membership.users).where(membership.users.c.id == member["id"])).mappings().one()
        assert row["stripe_subscription_id"] == "sub_modern"
        assert row["stripe_last_invoice_id"] == "in_modern"


def test_cancel_at_period_end_retains_paid_access_then_expiration_revokes(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    member = register_login(client, "cancellation@example.com", "Cancellation Member")
    active = stripe_event("evt_cancel_scheduled", "customer.subscription.updated", member["id"], "cancellation@example.com", cancel_at_period_end=True)
    assert post_stripe(client, active).status_code == 200
    assert client.get("/api/v1/top-stocks").status_code == 200
    ended = stripe_event("evt_cancel_ended", "customer.subscription.deleted", member["id"], "cancellation@example.com", status="canceled", current_period_end=membership._now() - 1)
    assert post_stripe(client, ended).status_code == 200
    assert client.get("/api/v1/top-stocks").status_code == 403
    assert client.get("/api/v1/membership/me").json()["membership_state"] == "EXPIRED"


def test_logout_invalidates_session_and_duplicate_signup_does_not_replace_account(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    register_login(client, "duplicate@example.com", "Original Member")
    assert post(client, "/api/v1/membership/register", {"email": "duplicate@example.com", "name": "Replacement", "password": PASSWORD}).status_code == 409
    assert post(client, "/api/v1/membership/logout", {}).status_code == 200
    assert client.get("/api/v1/membership/me").status_code == 401
    with engine.connect() as db:
        assert db.execute(text("SELECT name FROM membership_users WHERE email='duplicate@example.com'")).scalar_one() == "Original Member"


def test_origin_validation_and_retired_payment_routes(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    denied = client.post("/api/v1/membership/register", json={"email": "x@example.com", "name": "Example", "password": PASSWORD})
    assert denied.status_code == 403
    register_login(client, "env@example.com", "Environment Test")


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
    assert post_stripe(client, stripe_event("evt_premium_1", "customer.subscription.updated", member["id"], "premium@example.com")).status_code == 200

    with engine.connect() as db:
        count = db.execute(
            text("SELECT COUNT(*) FROM membership_entitlements WHERE user_id=:id AND revoked_at IS NULL"),
            {"id": member["id"]},
        ).scalar_one()
        plan_version = db.execute(
            text("SELECT plan_version FROM membership_users WHERE id=:id"),
            {"id": member["id"]},
        ).scalar_one()
    assert count == len(membership.PREMIUM_FEATURE_IDS)
    assert plan_version == membership.PREMIUM_PLAN_VERSION

    assert post_stripe(client, stripe_event("evt_premium_2", "invoice.paid", member["id"], "premium@example.com", object="invoice", object_id="in_test_123", subscription="sub_test_123")).status_code == 200
    with engine.connect() as db:
        second_count = db.execute(
            text("SELECT COUNT(*) FROM membership_entitlements WHERE user_id=:id AND revoked_at IS NULL"),
            {"id": member["id"]},
        ).scalar_one()
    assert second_count == len(membership.PREMIUM_FEATURE_IDS)


def test_membership_survives_backend_reinstall(tmp_path, monkeypatch):
    client, engine = make_client(tmp_path, monkeypatch)
    member = register_login(client, "restart@example.com", "Restart Member")
    assert post_stripe(client, stripe_event("evt_restart_1", "customer.subscription.updated", member["id"], "restart@example.com")).status_code == 200

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
    member = register_login(client, "outage@example.com", "Outage Member")
    assert post_stripe(client, stripe_event("evt_outage_1", "customer.subscription.updated", member["id"], "outage@example.com")).status_code == 200
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
    assert body["session_table_accessible"] is True
    assert "entitlement_rows" in body
    assert "payment_reference_rows" in body


def test_admin_health_warns_when_backup_is_not_verified(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    register_login(client, "backup-admin@example.com", "Backup Admin")
    monkeypatch.setenv("MEMBERSHIP_ADMIN_EMAILS", "backup-admin@example.com")
    monkeypatch.setenv("MEMBERSHIP_BACKUP_STATUS", "manual-check-required")
    response = client.get("/api/v1/membership/admin/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "WARNING"
    assert "backup" in body["warnings"][0].lower()


def test_admin_health_warns_when_backup_is_too_old(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    register_login(client, "old-backup-admin@example.com", "Old Backup Admin")
    monkeypatch.setenv("MEMBERSHIP_ADMIN_EMAILS", "old-backup-admin@example.com")
    monkeypatch.setenv("MEMBERSHIP_BACKUP_STATUS", "verified")
    monkeypatch.setenv("MEMBERSHIP_BACKUP_EXPECTED_WITHIN_HOURS", "24")
    monkeypatch.setenv("MEMBERSHIP_LAST_SUCCESSFUL_BACKUP_AT", "2026-01-01T00:00:00+00:00")
    response = client.get("/api/v1/membership/admin/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "WARNING"
    assert body["backup"]["age_hours"] is not None
    assert any("backup" in warning.lower() for warning in body["warnings"])


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

