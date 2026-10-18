import hashlib
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import select, func
import membership_bootstrap as membership
from test_membership import make_client, register_login, post, post_stripe, stripe_event

SECRET = "test-only-digistore-secret-32-characters"


def setup(tmp_path, monkeypatch, mode="live"):
    monkeypatch.setenv("DIGISTORE24_IPN_SECRET", SECRET)
    monkeypatch.setenv("DIGISTORE24_MERCHANT_ID", "12345")
    monkeypatch.setenv("DIGISTORE24_MODE", mode)
    return make_client(tmp_path, monkeypatch)


def event(order="ORDER1", **extra):
    now = membership._now()
    data = dict(event="on_payment", order_id=order, product_id="750685", merchant_id="12345",
                api_mode="live", email="buyer@example.com", billing_type="subscription",
                billing_status="paying", transaction_type="payment", transaction_amount="13.00",
                transaction_currency="GBP", payment_id=f"PAY-{order}", pay_sequence_no="1",
                transaction_date=datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"),
                next_payment_at=datetime.fromtimestamp(membership._add_calendar_month(now), timezone.utc).strftime("%Y-%m-%d"))
    data.update(extra)
    return data


def ipn(client, data, secret=SECRET):
    signed = {**data, "sha_sign": membership._digistore_signature(data, secret)}
    return client.post("/api/v1/membership/digistore-ipn", content=urlencode(signed),
                       headers={"content-type": "application/x-www-form-urlencoded"})


def claim(client, order="ORDER1", mode="live", token=None):
    return post(client, "/api/v1/membership/digistore-claim", {
        "order_id": order, "token": token or membership._digistore_token(order, mode, SECRET)})


def test_signature_matches_vendor_published_vector():
    data = {"buyer_email": "claus@domain-xyz.de", "payment_id": "PAYID-39-22012", "order_id": "273732",
            "transaction_amount": "17.00", "transaction_currency": "USD"}
    assert membership._digistore_signature(data, "xxxxx") == (
        "342770076245D14ED7DF4D2E5D82216D7EDF8F9E7969B5964C9C5DCB53E962BBECD545E90422B5329C69554FD8B1A7E7410736615FCA7FB5CBB3624CC016E4BC")
    assert membership._digistore_signature({**data, "empty": "", "zero": "0"}, "xxxxx") != membership._digistore_signature(data, "xxxxx")


def test_verified_payment_claim_and_duplicate(tmp_path, monkeypatch):
    client, engine = setup(tmp_path, monkeypatch)
    register_login(client, "buyer@example.com", "Buyer")
    data = event()
    first = ipn(client, data)
    assert first.status_code == 200
    assert first.text.startswith("OK\nthankyou_url: https://www.growthintel.app/digistore-access#")
    assert "password:" not in first.text
    assert ipn(client, data).text == first.text
    assert claim(client).json()["status"] == "active"
    me = client.get("/api/v1/membership/me").json()
    assert me["billing_provider"] == "digistore24"
    assert client.get("/api/v1/membership/billing-customer").json()["provider"] == "digistore24"
    assert client.get("/api/v1/top-stocks").status_code == 200
    with engine.connect() as db:
        assert db.execute(select(func.count()).select_from(membership.digistore_events)).scalar_one() == 1
    with engine.begin() as db:
        assert membership._membership_metrics(db)["active_premium_users"] == 1


def test_bad_signature_merchant_mode_product_and_missing_configuration(tmp_path, monkeypatch):
    client, engine = setup(tmp_path, monkeypatch)
    assert ipn(client, event(), "wrong").status_code == 403
    assert ipn(client, event(merchant_id="other")).status_code == 403
    assert ipn(client, event(api_mode="test")).status_code == 409
    assert ipn(client, event(product_id="other")).status_code == 422
    assert ipn(client, event(product_id_2="another")).status_code == 422
    assert ipn(client, event(next_payment_at="bad-date")).status_code == 422
    monkeypatch.delenv("DIGISTORE24_IPN_SECRET")
    assert ipn(client, event()).status_code == 503
    with engine.connect() as db:
        assert db.execute(select(func.count()).select_from(membership.digistore_events)).scalar_one() == 0


def test_duplicate_keys_and_json_rejected(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch)
    assert client.post("/api/v1/membership/digistore-ipn", json=event()).status_code == 415
    assert client.post("/api/v1/membership/digistore-ipn", content="email=a&email=b",
                       headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 400


def test_claim_requires_private_link_and_matching_account(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch)
    assert ipn(client, event()).status_code == 200
    assert claim(client).status_code == 401
    register_login(client, "other@example.com", "Other")
    assert claim(client).status_code == 403
    assert claim(client, token="0" * 64).status_code == 403


def test_cancellation_keeps_paid_access_until_last_paid_day(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch)
    register_login(client, "buyer@example.com", "Buyer")
    data = event()
    ipn(client, data)
    claim(client)
    assert ipn(client, {**data, "event": "on_rebill_cancelled"}).status_code == 200
    assert client.get("/api/v1/top-stocks").status_code == 200
    assert client.get("/api/v1/membership/me").json()["membership_cancel_at_period_end"]
    assert ipn(client, {**data, "event": "last_paid_day"}).status_code == 200
    assert client.get("/api/v1/top-stocks").status_code == 403
    ipn(client, data)  # replay cannot reactivate an expired order
    assert client.get("/api/v1/top-stocks").status_code == 403
    ipn(client, {**data, "event": "on_rebill_resumed"})
    assert client.get("/api/v1/top-stocks").status_code == 403


def test_one_order_does_not_revoke_other_order_or_stripe(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch)
    user = register_login(client, "buyer@example.com", "Buyer")
    for order in ("ORDER1", "ORDER2"):
        ipn(client, event(order))
        claim(client, order)
    ipn(client, event("ORDER1", event="on_chargeback"))
    assert client.get("/api/v1/top-stocks").status_code == 200
    assert post_stripe(client, stripe_event("evt_ds_other", "invoice.paid", user["id"], "buyer@example.com")).status_code == 200
    ipn(client, event("ORDER2", event="last_paid_day"))
    assert client.get("/api/v1/top-stocks").status_code == 200


def test_test_orders_never_grant_live_access(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch, mode="test")
    register_login(client, "buyer@example.com", "Buyer")
    assert ipn(client, event(api_mode="test")).status_code == 200
    assert claim(client, mode="test").json()["status"] == "test_verified"
    assert client.get("/api/v1/top-stocks").status_code == 403


def test_connection_test_signed_but_does_not_create_order(tmp_path, monkeypatch):
    client, engine = setup(tmp_path, monkeypatch)
    assert ipn(client, {"event": "connection_test", "merchant_id": "12345", "api_mode": "test"}).text == "OK"
    with engine.connect() as db:
        assert db.execute(select(func.count()).select_from(membership.digistore_orders)).scalar_one() == 0


def test_testmode_connection_probe_rolls_back_all_fixture_rows(tmp_path, monkeypatch):
    client, engine = setup(tmp_path, monkeypatch, mode="test")
    response = ipn(client, {"event": "connection_test", "merchant_id": "12345", "api_mode": "test"})
    assert response.status_code == 200 and response.text == "OK"
    with engine.connect() as db:
        for table in (membership.users, membership.digistore_orders, membership.digistore_events, membership.audits):
            assert db.execute(select(func.count()).select_from(table)).scalar_one() == 0
    assert ipn(client, event(api_mode="test")).status_code == 200


def test_refunded_payment_cannot_be_replayed_with_changed_metadata(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch)
    register_login(client, "buyer@example.com", "Buyer")
    data = event()
    ipn(client, {**data, "event": "on_refund"})
    ipn(client, {**data, "event_label": "payment"})
    assert claim(client).json()["status"] == "inactive"


def test_new_payment_after_suspension_restores_access(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch)
    register_login(client, "buyer@example.com", "Buyer")
    ipn(client, event())
    claim(client)
    ipn(client, event(event="on_payment_missed", pay_sequence_no="2"))
    assert client.get("/api/v1/top-stocks").status_code == 403
    original_now = membership._now()
    monkeypatch.setattr(membership, "_now", lambda: original_now + 2 * 86400)
    ipn(client, event(payment_id="PAY-NEW", pay_sequence_no="2"))
    assert client.get("/api/v1/top-stocks").status_code == 200


def test_same_day_recovery_and_late_missed_payment_do_not_revoke_new_payment(tmp_path, monkeypatch):
    client, _ = setup(tmp_path, monkeypatch)
    register_login(client, "buyer@example.com", "Buyer")
    ipn(client, event())
    claim(client)
    missed = event(event="on_payment_missed", pay_sequence_no="2")
    ipn(client, missed)
    assert client.get("/api/v1/top-stocks").status_code == 403
    ipn(client, event(payment_id="PAY-SECOND", pay_sequence_no="2"))
    assert client.get("/api/v1/top-stocks").status_code == 200
    ipn(client, {**missed, "event_label": "late duplicate failure"})
    assert client.get("/api/v1/top-stocks").status_code == 200
    ipn(client, event(event="last_paid_day", pay_sequence_no="1"))
    assert client.get("/api/v1/top-stocks").status_code == 200

