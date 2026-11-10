import hashlib
import hmac
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from sample.database import Base
from sample.main import create_app
from sample.models import Order, ProcessedEvent
from sample.reconcile import reconcile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SECRET = "whsec_test_local_only"


def setup_app():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=__import__("sqlalchemy").pool.StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(session_factory=factory, settings={"STRIPE_SECRET_KEY": "sk_test_local_only", "STRIPE_WEBHOOK_SECRET": SECRET})
    return app, factory


def signature(payload, timestamp=1_800_000_000, secret=SECRET):
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def event(event_id, kind, obj):
    return json.dumps({"id": event_id, "type": kind, "data": {"object": obj}}, separators=(",", ":")).encode()


def post(client, raw, sig=None):
    return client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": sig or signature(raw, int(datetime.now(timezone.utc).timestamp()))})


def add_order(factory, *, status="pending", pi="pi_123", created=None):
    with factory() as db:
        row = Order(id="ord_1", amount_cents=2500, currency="usd", status=status, payment_intent_id=pi,
                    created_at=created or datetime.now(timezone.utc))
        db.add(row)
        db.commit()


def test_valid_signature_advances_order_to_paid():
    app, factory = setup_app()
    add_order(factory)
    raw = event("evt_paid", "checkout.session.completed", {"id": "cs_1", "payment_intent": "pi_123", "metadata": {"order_id": "ord_1"}})
    with TestClient(app) as client:
        response = post(client, raw)
    assert response.status_code == 200
    with factory() as db:
        assert db.get(Order, "ord_1").status == "paid"


def test_order_creates_checkout_session_without_network():
    app, factory = setup_app()
    class FakeSessionAPI:
        @staticmethod
        def create(**kwargs):
            assert kwargs["mode"] == "payment"
            assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 1200
            return SimpleNamespace(id="cs_fake", url="https://checkout.example/session")
    client_sdk = SimpleNamespace(checkout=SimpleNamespace(Session=FakeSessionAPI))
    app = create_app(session_factory=factory, stripe_client=client_sdk,
                     settings={"STRIPE_SECRET_KEY": "sk_test_local_only", "STRIPE_WEBHOOK_SECRET": SECRET})
    with TestClient(app) as client:
        response = client.post("/orders", json={"amount_cents": 1200, "currency": "usd"})
    assert response.status_code == 201
    assert response.json()["checkout_url"] == "https://checkout.example/session"
    with factory() as db:
        row = db.get(Order, response.json()["order_id"])
        assert (row.amount_cents, row.status, row.checkout_session_id) == (1200, "pending", "cs_fake")


def test_invalid_signature_rejected():
    app, _ = setup_app()
    raw = event("evt_bad", "checkout.session.completed", {})
    with TestClient(app) as client:
        response = post(client, raw, "t=1800000000,v1=bad")
    assert response.status_code == 400


def test_expired_timestamp_rejected():
    app, _ = setup_app()
    raw = event("evt_old", "checkout.session.completed", {})
    with TestClient(app) as client:
        response = post(client, raw, signature(raw, 1))
    assert response.status_code == 400


def test_duplicate_event_is_applied_once():
    app, factory = setup_app()
    add_order(factory)
    raw = event("evt_once", "checkout.session.completed", {"id": "cs_1", "payment_intent": "pi_123", "metadata": {"order_id": "ord_1"}})
    with TestClient(app) as client:
        assert post(client, raw).json() == {"received": True, "duplicate": False}
        assert post(client, raw).json() == {"received": True, "duplicate": True}
    with factory() as db:
        assert len(db.scalars(select(ProcessedEvent)).all()) == 1
        assert db.get(Order, "ord_1").status == "paid"


def test_out_of_order_refund_then_completion_stays_refunded():
    app, factory = setup_app()
    add_order(factory)
    refund = event("evt_refund", "charge.refunded", {"payment_intent": "pi_123", "metadata": {"order_id": "ord_1"}})
    completed = event("evt_late_paid", "checkout.session.completed", {"id": "cs_1", "payment_intent": "pi_123", "metadata": {"order_id": "ord_1"}})
    with TestClient(app) as client:
        assert post(client, refund).status_code == 200
        assert post(client, completed).status_code == 200
    with factory() as db:
        assert db.get(Order, "ord_1").status == "refunded"


def test_payment_failure_marks_pending_order_failed():
    app, factory = setup_app()
    add_order(factory)
    raw = event("evt_failed", "payment_intent.payment_failed", {"id": "pi_123", "metadata": {"order_id": "ord_1"}})
    with TestClient(app) as client:
        assert post(client, raw).status_code == 200
    with factory() as db:
        assert db.get(Order, "ord_1").status == "failed"


def test_reconcile_reports_remote_and_amount_differences():
    app, factory = setup_app()
    add_order(factory, created=datetime(2026, 11, 10, 12, tzinfo=timezone.utc))
    class FakePaymentIntents:
        @staticmethod
        def list(**kwargs):
            return SimpleNamespace(data=[SimpleNamespace(id="pi_123", amount=2400, currency="usd"),
                                         SimpleNamespace(id="pi_remote_only", amount=500, currency="usd")])
    # The production entry point enforces test mode; provide a test key without any network call.
    import os
    old = os.environ.get("STRIPE_SECRET_KEY")
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_offline"
    try:
        report = reconcile(date(2026, 11, 10), session_factory=factory, stripe_client=SimpleNamespace(PaymentIntent=FakePaymentIntents))
    finally:
        if old is None:
            os.environ.pop("STRIPE_SECRET_KEY", None)
        else:
            os.environ["STRIPE_SECRET_KEY"] = old
    assert report["missing_locally"] == ["pi_remote_only"]
    assert report["amount_or_currency_mismatch"] == [{"payment_intent_id": "pi_123", "local_amount_cents": 2500,
                                                       "stripe_amount_cents": 2400, "local_currency": "usd", "stripe_currency": "usd"}]
