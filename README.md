Sample project for demonstration only.

# Stripe Checkout and Webhook Sample

This FastAPI sample creates a Stripe Checkout Session in test mode, verifies webhook signatures, records event IDs for idempotent processing, tracks order states in SQLite, and compares local orders with Stripe PaymentIntents in a daily reconciliation report.

## Architecture

```text
Customer -> FastAPI /orders -> SQLite (pending order)
                         \-> Stripe Checkout Session
Stripe -> /webhooks/stripe -> HMAC signature check -> unique event ID -> SQLite status
Stripe PaymentIntents + SQLite orders -> sample.reconcile -> JSON difference report
```

## Run locally

1. Create a virtual environment and install the pinned dependencies:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set a Stripe **test mode** secret key and webhook signing secret. The secret key must start with `sk_test_`. Never use a live key with this sample.

3. Start the API:

   ```sh
   .venv/bin/uvicorn sample.main:app --reload
   ```

4. Create a Checkout Session:

   ```sh
   curl -X POST http://localhost:8000/orders \
     -H 'Content-Type: application/json' \
     -d '{"amount_cents":2500,"currency":"usd"}'
   ```

   Open the returned `checkout_url` and complete a test payment using a Stripe test card.

## Test-mode webhook forwarding

Install and authenticate the [Stripe CLI](https://docs.stripe.com/stripe-cli), then run:

```sh
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Copy the CLI's `whsec_...` signing secret into `.env` as `STRIPE_WEBHOOK_SECRET`, restart the API, and trigger test events when needed:

```sh
stripe trigger checkout.session.completed
```

Stripe CLI generated events may not refer to an order created by this sample; unmatched events are recorded for deduplication and do not change any order. A real Checkout flow created at `/orders` carries the sample order ID in session metadata.

## Reconciliation

With a test-mode key configured, reconcile UTC orders created on a date:

```sh
.venv/bin/python -m sample.reconcile --date 2026-11-10
```

The JSON output lists local orders without a PaymentIntent, local PaymentIntents missing from Stripe, Stripe PaymentIntents without a local order, and amount/currency mismatches. Stripe list pagination beyond the first 100 PaymentIntents is not implemented.

## Tests

The tests are offline and use an in-memory SQLite database plus a fake Stripe list response. They do not need Stripe credentials or network access:

```sh
.venv/bin/pytest -q
```

## Limitations

- This is a demonstration, not a production payment service. It does not implement authentication, authorization, inventory, tax, shipping, customer management, or a frontend.
- The order endpoint accepts an amount supplied by the caller; a real store must derive prices from trusted server-side product data.
- SQLite is suitable for the sample only. Production deployments need stronger concurrency, backup, migration, and operational practices.
- Webhook signature checks enforce a 300-second timestamp tolerance. Event IDs are persisted uniquely; event processing and order updates share one database transaction.
- Reconciliation currently fetches only the first 100 Stripe PaymentIntents and matches orders with a known PaymentIntent ID. Expand pagination and define business-specific settlement rules before production use.
- Refund status is terminal for the modeled states. Partial refunds and refund reversals are not represented.
- No real payment, email, or external message is sent by the automated tests.
