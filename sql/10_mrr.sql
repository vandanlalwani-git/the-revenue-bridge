-- 10_mrr.sql
-- ============================================================================
-- HEADER
--   What it does: works out each subscription's MRR at every month-end, rebuilt
--     from invoice lines. Stripe's subscription table only shows today, so the
--     history has to come from the invoices. Kept as yearly cents (ARR) so an
--     annual plan never leaves a fraction of a cent; MRR = ARR / 12 on screen.
--   Judgement call: when several invoice lines cover the month-end, the line
--     that started most recently wins. After a mid-month upgrade that is the
--     upgrade line, so the new price counts. MRR uses list price x quantity,
--     never the part-period amount actually charged. Customers whose payment is
--     failing stay in MRR, flagged past_due, until they are actually cancelled.
--   Edge case: a free-trial line counts as $0, a one-off ("once") coupon does
--     not lower MRR, and credit lines (refunds of unused time) never set the
--     price. Monthly -> annual: the annual line counts x1, so MRR drops to 10/12.
-- ============================================================================

CREATE OR REPLACE TABLE mrr_item_month_end AS
WITH covering AS (
    SELECT c.month, c.month_end, l.*,
           row_number() OVER (PARTITION BY c.month, l.subscription_item
                              ORDER BY l.period_start DESC, l.proration DESC, l.line) AS rank
    FROM calendar c
    JOIN stg_lines l
      ON l.period_start <= c.month_end AND c.month_end < l.period_end
     AND l.amount >= 0                                   -- credit lines never govern
    JOIN stg_subscriptions s
      ON s.subscription = l.subscription
     AND (s.ended_at IS NULL OR s.ended_at > c.month_end)  -- subscription still live
),
governing AS (
    SELECT *,
           CASE WHEN amount = 0 THEN 0 ELSE unit_amount * quantity END AS list_amount,
           CASE WHEN amount = 0                    THEN 0
                WHEN coupon_duration IS NULL       THEN 0
                WHEN coupon_duration = 'once'      THEN 0   -- one-off coupons never touch MRR
                WHEN discount_start <= month_end AND (discount_end IS NULL OR month_end < discount_end)
                     THEN round_half_up(unit_amount * quantity * CAST(round(percent_off * 100) AS BIGINT), 10000)
                ELSE 0 END                                              AS recurring_discount,
           CASE WHEN interval = 'month' THEN 12 ELSE 1 END              AS periods_per_year
    FROM covering
    WHERE rank = 1
)
SELECT month, month_end, customer, subscription, subscription_item, line, product, is_seat, interval,
       unit_amount, quantity, list_amount, recurring_discount, periods_per_year,
       (list_amount - recurring_discount) * periods_per_year AS arr_cents
FROM governing;

-- Per subscription, with the at-risk flag: an open dunning spell at the month-end (§1.9).
CREATE OR REPLACE TABLE mrr_month_end AS
SELECT m.month, m.month_end, m.customer, m.subscription,
       sum(m.arr_cents) AS arr_cents,
       EXISTS (SELECT 1 FROM stg_dunning d
               WHERE d.subscription = m.subscription
                 AND d.first_failed_at <= m.month_end
                 AND (d.resolved_at IS NULL OR d.resolved_at > m.month_end)) AS past_due
FROM mrr_item_month_end m
GROUP BY m.month, m.month_end, m.customer, m.subscription;
