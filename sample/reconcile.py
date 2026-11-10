"""Compare local orders with Stripe PaymentIntents created on a requested date."""
import argparse
import json
import os
from datetime import date, datetime, time, timedelta, timezone

import stripe
from dotenv import load_dotenv
from sqlalchemy import select

from .database import default_database_url, make_session_factory
from .models import Order


def reconcile(day: date, *, session_factory=None, stripe_client=None):
    load_dotenv()
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key.startswith("sk_test_"):
        raise RuntimeError("STRIPE_SECRET_KEY must start with sk_test_ (test mode only)")
    sessions = session_factory or make_session_factory(default_database_url())
    client = stripe_client or stripe
    if hasattr(client, "api_key"):
        client.api_key = key
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    # Stripe list APIs use Unix timestamps. created is inclusive/exclusive by convention here.
    records = client.PaymentIntent.list(created={"gte": int(start.timestamp()), "lt": int(end.timestamp())}, limit=100)
    stripe_items = records.data if hasattr(records, "data") else records["data"]
    stripe_by_id = {item.id if hasattr(item, "id") else item["id"]: item for item in stripe_items}
    with sessions() as db:
        local = db.scalars(select(Order).where(Order.created_at >= start, Order.created_at < end)).all()
    local_by_pi = {o.payment_intent_id: o for o in local if o.payment_intent_id}
    local_unlinked = [o for o in local if not o.payment_intent_id]
    missing_remote = sorted(pi for pi in local_by_pi if pi not in stripe_by_id)
    missing_local = sorted(pi for pi in stripe_by_id if pi not in local_by_pi)
    mismatches = []
    for pi in sorted(set(local_by_pi) & set(stripe_by_id)):
        order, record = local_by_pi[pi], stripe_by_id[pi]
        amount = record.amount if hasattr(record, "amount") else record.get("amount")
        currency = record.currency if hasattr(record, "currency") else record.get("currency")
        if order.amount_cents != amount or order.currency != currency:
            mismatches.append({"payment_intent_id": pi, "local_amount_cents": order.amount_cents,
                               "stripe_amount_cents": amount, "local_currency": order.currency,
                               "stripe_currency": currency})
    return {"date": day.isoformat(), "local_orders": len(local), "stripe_payment_intents": len(stripe_items),
            "local_missing_payment_intent": sorted(o.id for o in local_unlinked),
            "missing_on_stripe": missing_remote, "missing_locally": missing_local,
            "amount_or_currency_mismatch": mismatches}


def main():
    parser = argparse.ArgumentParser(description="Reconcile local orders with Stripe test-mode PaymentIntents")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="UTC date in YYYY-MM-DD format")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.date), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
