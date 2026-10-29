-- 40_revenue.sql
-- ============================================================================
-- HEADER (Vandan, in your own words): what this computes, the judgement call,
-- one edge case.
--
--
-- ----------------------------------------------------------------------------
-- Draft notes (Claude), for you to replace:
--   Computes: IFRS 15 revenue per invoice line per month, recognised by the
--     second over the service delivered (definitions §4). Also the refund split
--     (D4) and the monthly revenue, billings and cash summary.
--   Judgement call: service ends at min(period_end, ended_at). Days in dunning
--     count as delivered (D5, a stated simplification). An uncollectible invoice
--     does not reverse revenue: its unearned portion (after ended_at) leaves the
--     contract liability on write-off, and its earned portion is a credit loss
--     handled in 50_ledger.sql.
--   Edge case: a refund is unearned only up to the unearned amount still on its
--     invoice once the subscription has ended. A refund on a live subscription
--     (a service credit, E6) is all delivered and reduces revenue in the month
--     it is issued.
-- ============================================================================

-- Revenue recognised on a line up to instant d, with cumulative cent rounding (§4.2).
CREATE OR REPLACE MACRO recognised_to(net, ps, pe, service_end, d) AS
    round_half_up(net * (least(greatest(d, ps), service_end) - ps), pe - ps);

CREATE OR REPLACE TABLE revenue_lines AS
SELECT l.line, l.invoice, l.customer, l.subscription, l.net_amount, l.coupon_duration, l.discount_amount,
       l.period_start, l.period_end,
       greatest(l.period_start, least(l.period_end, coalesce(s.ended_at, l.period_end))) AS service_end,
       i.status AS invoice_status, i.marked_uncollectible_at
FROM stg_lines l
JOIN stg_subscriptions s USING (subscription)
JOIN raw.invoices i ON i.id = l.invoice
WHERE l.net_amount <> 0;

-- Earned and unearned amount of each line over its whole life.
CREATE OR REPLACE TABLE revenue_line_totals AS
SELECT *,
       recognised_to(net_amount, period_start, period_end, service_end, service_end) AS earned_cents,
       net_amount - recognised_to(net_amount, period_start, period_end, service_end, service_end) AS unearned_cents
FROM revenue_lines;

CREATE OR REPLACE TABLE revenue_by_line AS
SELECT r.line, c.month,
       recognised_to(r.net_amount, r.period_start, r.period_end, r.service_end, c.month_end)
     - recognised_to(r.net_amount, r.period_start, r.period_end, r.service_end, c.prev_month_end) AS recognised_cents
FROM revenue_lines r
JOIN calendar c ON c.month_end > r.period_start AND c.prev_month_end < r.service_end;

-- D4: each refund takes the invoice's unearned amount first, once service has ended.
CREATE OR REPLACE TABLE refund_split AS
WITH refunds AS (
    SELECT re.id AS refund, re.amount, re.created, ch.invoice, ch.customer
    FROM raw.refunds re JOIN raw.charges ch ON ch.id = re.charge
),
unearned AS (
    SELECT invoice, sum(unearned_cents) AS cents, max(service_end) AS service_end
    FROM revenue_line_totals WHERE unearned_cents <> 0
    GROUP BY invoice
),
ordered AS (
    SELECT r.*,
           CASE WHEN u.service_end <= r.created THEN u.cents ELSE 0 END AS available,
           coalesce(sum(r.amount) FILTER (WHERE u.service_end <= r.created) OVER (
               PARTITION BY r.invoice ORDER BY r.created, r.refund
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS prior_after_end
    FROM refunds r LEFT JOIN unearned u USING (invoice)
)
SELECT refund, invoice, customer, created, month_of(created) AS month, amount AS amount_cents,
       CAST(least(amount, greatest(coalesce(available, 0) - prior_after_end, 0)) AS BIGINT)          AS unearned_cents,
       CAST(amount - least(amount, greatest(coalesce(available, 0) - prior_after_end, 0)) AS BIGINT) AS delivered_cents
FROM ordered;

-- Monthly summary. Balances are rolled forward here and re-derived independently in 50_ledger.sql.
CREATE OR REPLACE TABLE revenue_month AS
WITH billings AS (
    SELECT month_of(finalized_at) AS month, sum(amount_due) AS cents FROM raw.invoices GROUP BY 1
),
recognised AS (
    SELECT month, sum(recognised_cents) AS cents FROM revenue_by_line GROUP BY 1
),
once_coupons AS (                                        -- one-off coupon share of recognised revenue (for R1)
    SELECT b.month,
           sum(round_half_up(b.recognised_cents * r.discount_amount, r.net_amount)) AS cents
    FROM revenue_by_line b JOIN revenue_lines r USING (line)
    WHERE r.coupon_duration = 'once'
    GROUP BY 1
),
refunds AS (
    SELECT month, sum(delivered_cents) AS delivered, sum(unearned_cents) AS unearned, sum(amount_cents) AS total
    FROM refund_split GROUP BY 1
),
written_off AS (                                         -- unearned portion leaving the contract liability on write-off
    SELECT month_of(marked_uncollectible_at) AS month, sum(unearned_cents) AS unearned, sum(earned_cents) AS earned
    FROM revenue_line_totals WHERE invoice_status = 'uncollectible'
    GROUP BY 1
),
collections AS (
    SELECT month_of(created) AS month, sum(amount) AS cents FROM raw.charges WHERE status = 'succeeded' GROUP BY 1
),
flows AS (
    SELECT c.month,
           coalesce(b.cents, 0)   AS billings_cents,
           coalesce(r.cents, 0)   AS gross_recognised_cents,
           coalesce(o.cents, 0)   AS once_coupon_recognised_cents,
           coalesce(f.delivered, 0) AS refunds_delivered_cents,
           coalesce(f.unearned, 0)  AS refunds_unearned_cents,
           coalesce(w.unearned, 0)  AS unearned_written_off_cents,
           coalesce(w.earned, 0)    AS earned_written_off_cents,
           coalesce(k.cents, 0)   AS collections_cents,
           coalesce(f.total, 0)   AS refunds_total_cents
    FROM calendar c
    LEFT JOIN billings b USING (month)
    LEFT JOIN recognised r USING (month)
    LEFT JOIN once_coupons o USING (month)
    LEFT JOIN refunds f USING (month)
    LEFT JOIN written_off w USING (month)
    LEFT JOIN collections k USING (month)
)
SELECT *,
       gross_recognised_cents - refunds_delivered_cents  AS net_revenue_cents,
       collections_cents - refunds_total_cents           AS net_cash_cents,
       sum(billings_cents - gross_recognised_cents - unearned_written_off_cents - refunds_unearned_cents)
           OVER (ORDER BY month)                         AS contract_liability_close_cents
FROM flows;
