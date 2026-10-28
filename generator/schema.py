"""Raw table schemas, modelled on Stripe API objects.

Money is integer cents. Timestamps are Unix epoch seconds (UTC), as Stripe returns them.
Column names flatten Stripe's nested fields, e.g. price.recurring.interval -> recurring_interval.
Subscriptions keep current_period_* on the subscription (Stripe API before 2025-03-31).
Tables hold current state only, like an API export; history lives in invoices and line items.
"""

TABLES = {
    "customers": {
        "id": "VARCHAR",
        "created": "BIGINT",
        "name": "VARCHAR",
        "email": "VARCHAR",
        "currency": "VARCHAR",
    },
    "products": {
        "id": "VARCHAR",
        "name": "VARCHAR",
        "created": "BIGINT",
        "active": "BOOLEAN",
    },
    "prices": {
        "id": "VARCHAR",
        "product": "VARCHAR",
        "nickname": "VARCHAR",
        "unit_amount": "BIGINT",
        "currency": "VARCHAR",
        "recurring_interval": "VARCHAR",        # month | year
        "recurring_interval_count": "INTEGER",
        "active": "BOOLEAN",
        "created": "BIGINT",
    },
    "coupons": {
        "id": "VARCHAR",
        "name": "VARCHAR",
        "percent_off": "DOUBLE",
        "amount_off": "BIGINT",
        "duration": "VARCHAR",                  # forever | repeating | once
        "duration_in_months": "INTEGER",
        "created": "BIGINT",
    },
    "discounts": {
        "id": "VARCHAR",
        "coupon": "VARCHAR",
        "customer": "VARCHAR",
        "subscription": "VARCHAR",
        "start": "BIGINT",
        "end": "BIGINT",                        # NULL for forever
    },
    "subscriptions": {
        "id": "VARCHAR",
        "customer": "VARCHAR",
        "status": "VARCHAR",                    # trialing | active | past_due | canceled
        "created": "BIGINT",
        "start_date": "BIGINT",
        "billing_cycle_anchor": "BIGINT",
        "current_period_start": "BIGINT",
        "current_period_end": "BIGINT",
        "cancel_at_period_end": "BOOLEAN",
        "canceled_at": "BIGINT",
        "ended_at": "BIGINT",
        "cancellation_reason": "VARCHAR",       # cancellation_requested | payment_failed
        "trial_start": "BIGINT",
        "trial_end": "BIGINT",
        "collection_method": "VARCHAR",
    },
    "subscription_items": {
        "id": "VARCHAR",
        "subscription": "VARCHAR",
        "price": "VARCHAR",
        "quantity": "INTEGER",
        "created": "BIGINT",
    },
    "invoices": {
        "id": "VARCHAR",
        "customer": "VARCHAR",
        "subscription": "VARCHAR",
        "billing_reason": "VARCHAR",            # subscription_create | subscription_cycle | subscription_update
        "status": "VARCHAR",                    # open | paid | uncollectible | void
        "created": "BIGINT",
        "finalized_at": "BIGINT",
        "paid_at": "BIGINT",
        "marked_uncollectible_at": "BIGINT",
        "subtotal": "BIGINT",
        "total_discount_amount": "BIGINT",
        "total": "BIGINT",
        "amount_due": "BIGINT",
        "amount_paid": "BIGINT",
        "amount_remaining": "BIGINT",
        "currency": "VARCHAR",
    },
    "invoice_line_items": {
        "id": "VARCHAR",
        "invoice": "VARCHAR",
        "subscription": "VARCHAR",
        "subscription_item": "VARCHAR",
        "price": "VARCHAR",
        "type": "VARCHAR",                      # subscription
        "proration": "BOOLEAN",
        "quantity": "INTEGER",
        "amount": "BIGINT",                     # before discounts, as in Stripe
        "discount_amount": "BIGINT",
        "discount": "VARCHAR",
        "period_start": "BIGINT",
        "period_end": "BIGINT",
        "currency": "VARCHAR",
    },
    "charges": {
        "id": "VARCHAR",
        "customer": "VARCHAR",
        "invoice": "VARCHAR",
        "amount": "BIGINT",
        "created": "BIGINT",
        "status": "VARCHAR",                    # succeeded | failed
        "failure_code": "VARCHAR",
        "currency": "VARCHAR",
    },
    "refunds": {
        "id": "VARCHAR",
        "charge": "VARCHAR",
        "amount": "BIGINT",
        "created": "BIGINT",
        "reason": "VARCHAR",
        "status": "VARCHAR",
        "currency": "VARCHAR",
    },
}
