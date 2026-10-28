-- 00_staging.sql
-- ============================================================================
-- HEADER (Vandan, in your own words): what this computes, the judgement call,
-- one edge case.
--
--
-- ----------------------------------------------------------------------------
-- Draft notes (Claude), for you to replace:
--   Computes: typed staging tables over the raw Stripe export. Each invoice line
--     is joined to its price, product and coupon so later files read one row per
--     line. Also builds the month calendar and the shared rounding macro.
--   Judgement call: month-ends are 23:59:59 UTC on the last day (definitions §1.4),
--     so anything stamped exactly at that second belongs to the month.
--   Edge case: a line's discount may be a `once` coupon. It is kept here, and
--     10_mrr.sql excludes it from MRR (definitions §1.6).
-- ============================================================================

SET TimeZone = 'UTC';

-- numer / denom rounded half away from zero, in integer arithmetic (definitions §4.2).
-- The generator uses the same rule, so prorations, refunds and revenue all agree.
CREATE OR REPLACE MACRO round_half_up(numer, denom) AS
    CASE WHEN numer >= 0 THEN (2 * numer + denom) // (2 * denom)
         ELSE -((2 * (-numer) + denom) // (2 * denom)) END;

CREATE OR REPLACE MACRO month_of(t) AS strftime(to_timestamp(t) AT TIME ZONE 'UTC', '%Y-%m');

-- One row per month from the first invoice to the last.
-- month_end = E(t); prev_month_end = E(t-1).
CREATE OR REPLACE TABLE calendar AS
WITH bounds AS (
    SELECT date_trunc('month', to_timestamp(min(created)) AT TIME ZONE 'UTC') AS first_month,
           date_trunc('month', to_timestamp(max(created)) AT TIME ZONE 'UTC') AS last_month
    FROM raw.invoices
)
SELECT strftime(m.range, '%Y-%m')                        AS month,
       CAST(epoch(m.range) AS BIGINT) - 1                 AS prev_month_end,
       CAST(epoch(m.range + INTERVAL 1 MONTH) AS BIGINT) - 1 AS month_end
FROM bounds, range(bounds.first_month, bounds.last_month + INTERVAL 1 MONTH, INTERVAL 1 MONTH) AS m;

CREATE OR REPLACE TABLE stg_subscriptions AS
SELECT id AS subscription, customer, status, created, canceled_at, ended_at, cancellation_reason,
       trial_start, trial_end
FROM raw.subscriptions;

CREATE OR REPLACE TABLE stg_lines AS
SELECT l.id                              AS line,
       l.invoice,
       i.customer,
       l.subscription,
       l.subscription_item,
       l.price,
       pr.name                           AS product,
       pr.name LIKE '%seat%'             AS is_seat,
       p.recurring_interval              AS interval,
       p.unit_amount,
       l.quantity,
       l.amount,                         -- before discounts, as Stripe reports it
       l.discount_amount,
       l.amount - l.discount_amount      AS net_amount,
       l.proration,
       l.period_start,
       l.period_end,
       i.billing_reason,
       i.finalized_at,
       d.id                              AS discount,
       c.id                              AS coupon,
       c.duration                        AS coupon_duration,
       c.percent_off,
       d.start                           AS discount_start,
       d."end"                           AS discount_end
FROM raw.invoice_line_items l
JOIN raw.invoices i  ON i.id = l.invoice
JOIN raw.prices p    ON p.id = l.price
JOIN raw.products pr ON pr.id = p.product
LEFT JOIN raw.discounts d ON d.id = l.discount
LEFT JOIN raw.coupons c   ON c.id = d.coupon;

-- One row per invoice that ever had a failed charge: the dunning spell.
-- It is open from the first failure until the invoice is paid or written off.
CREATE OR REPLACE TABLE stg_dunning AS
SELECT i.id AS invoice, i.subscription, i.customer, i.status,
       min(ch.created)                              AS first_failed_at,
       coalesce(i.paid_at, i.marked_uncollectible_at) AS resolved_at
FROM raw.invoices i
JOIN raw.charges ch ON ch.invoice = i.id AND ch.status = 'failed'
GROUP BY ALL;
