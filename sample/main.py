import json
import os
import uuid
from contextlib import asynccontextmanager

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import default_database_url, make_session_factory
from .models import Order, ProcessedEvent
from .signatures import SignatureError, verify_signature

load_dotenv()


class OrderInput(BaseModel):
    amount_cents: int = Field(gt=0, le=10_000_000)
    currency: str = Field(default="usd", min_length=3, max_length=3, pattern="^[a-zA-Z]{3}$")


def create_app(*, session_factory=None, stripe_client=None, settings=None):
    cfg = settings or {}
    secret_key = cfg.get("STRIPE_SECRET_KEY", os.getenv("STRIPE_SECRET_KEY", ""))
    webhook_secret = cfg.get("STRIPE_WEBHOOK_SECRET", os.getenv("STRIPE_WEBHOOK_SECRET", ""))
    base_url = cfg.get("APP_BASE_URL", os.getenv("APP_BASE_URL", "http://localhost:8000"))
    sessions = session_factory or make_session_factory(cfg.get("DATABASE_URL", default_database_url()))
    client = stripe_client or stripe

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not secret_key.startswith("sk_test_"):
            raise RuntimeError("STRIPE_SECRET_KEY must start with sk_test_ (test mode only)")
        if not webhook_secret:
            raise RuntimeError("STRIPE_WEBHOOK_SECRET must be configured")
        yield

    app = FastAPI(title="Stripe Checkout Webhook Sample", lifespan=lifespan)
    app.state.session_factory = sessions

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/orders", status_code=201)
    def create_order(body: OrderInput):
        order_id = str(uuid.uuid4())
        currency = body.currency.lower()
        with sessions() as db:
            order = Order(id=order_id, amount_cents=body.amount_cents, currency=currency, status="pending")
            db.add(order)
            db.commit()
        try:
            if hasattr(client, "api_key"):
                client.api_key = secret_key
            session = client.checkout.Session.create(
                mode="payment",
                success_url=f"{base_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{base_url}/cancel",
                line_items=[{"price_data": {"currency": currency, "unit_amount": body.amount_cents,
                    "product_data": {"name": f"Sample order {order_id}"}}, "quantity": 1}],
                metadata={"order_id": order_id},
            )
        except Exception as exc:
            # Keep the pending row for investigation; never report a checkout that was not created.
            raise HTTPException(status_code=502, detail="Could not create Checkout Session") from exc
        with sessions() as db:
            order = db.get(Order, order_id)
            order.checkout_session_id = session.id
            db.commit()
        return {"order_id": order_id, "status": "pending", "checkout_url": session.url}

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")):
        if not stripe_signature:
            raise HTTPException(status_code=400, detail="Missing Stripe-Signature")
        raw = await request.body()
        try:
            verify_signature(raw, stripe_signature, webhook_secret)
            event = json.loads(raw)
        except (SignatureError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="Invalid webhook signature or payload")

        event_id = event.get("id")
        event_type = event.get("type")
        obj = event.get("data", {}).get("object", {})
        if not event_id or not event_type:
            raise HTTPException(status_code=400, detail="Invalid event")
        try:
            with sessions() as db:
                if db.scalar(select(ProcessedEvent).where(ProcessedEvent.event_id == event_id)):
                    return {"received": True, "duplicate": True}
                meta = obj.get("metadata") or {}
                order_id = meta.get("order_id")
                order = db.get(Order, order_id) if order_id else None
                if order is None:
                    session_id = obj.get("id") if event_type == "checkout.session.completed" else None
                    payment_intent = obj.get("payment_intent")
                    if session_id:
                        order = db.scalar(select(Order).where(Order.checkout_session_id == session_id))
                    if order is None and payment_intent:
                        order = db.scalar(select(Order).where(Order.payment_intent_id == payment_intent))
                if event_type in {"checkout.session.completed", "charge.refunded", "payment_intent.payment_failed"} and order:
                    payment_intent_id = obj.get("payment_intent")
                    if event_type == "checkout.session.completed":
                        order.checkout_session_id = obj.get("id", order.checkout_session_id)
                        if payment_intent_id:
                            order.payment_intent_id = payment_intent_id
                        # Refund is terminal; a late completion must not resurrect it.
                        if order.status != "refunded":
                            order.status = "paid"
                    elif event_type == "charge.refunded":
                        if payment_intent_id:
                            order.payment_intent_id = payment_intent_id
                        order.status = "refunded"
                    elif order.status == "pending":
                        order.status = "failed"
                db.add(ProcessedEvent(event_id=event_id, event_type=event_type))
                db.commit()
        except IntegrityError:
            # Concurrent Stripe retries race on the unique event id; the winner already applied it.
            return {"received": True, "duplicate": True}
        return {"received": True, "duplicate": False}

    return app


app = create_app()
