-- 20_mrr_movements.sql
-- ============================================================================
-- HEADER (Vandan, in your own words): what this computes, the judgement call,
-- one edge case.
--
--
-- ----------------------------------------------------------------------------
-- Draft notes (Claude), for you to replace:
--   Computes: the MRR bridge. Each customer's month-end MRR is compared with
--     the prior month-end and classed as new, reactivation, expansion,
--     contraction or churn (voluntary / involuntary). Expansion and contraction
--     are split by driver per subscription item (definitions §2).
--   Judgement call: movements are per customer, so a plan switch is never
--     counted as churn plus new. Each item delta gets exactly one driver, by
--     priority: plan change > interval switch > price change > quantity >
--     discount. Monthly -> annual is contraction (§1.11).
--   Edge case: opening and closing come straight from mrr_month_end, never from
--     the movements, so the tie check is a real test. Customers who start and
--     churn inside one month are invisible to month-end snapshots; they are
--     counted separately as the blind spot (§2.5).
-- ============================================================================

CREATE OR REPLACE TABLE customer_mrr AS
SELECT month, customer, sum(arr_cents) AS arr_cents, bool_or(past_due) AS past_due
FROM mrr_month_end
GROUP BY ALL
HAVING sum(arr_cents) > 0;                               -- paying customers only (§3.1)

CREATE OR REPLACE TABLE mrr_movements AS
WITH months AS (
    SELECT month, month_end, prev_month_end, lag(month) OVER (ORDER BY month) AS prev_month
    FROM calendar
),
keys AS (                                                -- customers paying at either end of the month
    SELECT m.month, c.customer FROM months m JOIN customer_mrr c ON c.month = m.month
    UNION
    SELECT m.month, c.customer FROM months m JOIN customer_mrr c ON c.month = m.prev_month
),
pairs AS (
    SELECT k.month, k.customer,
           coalesce(p.arr_cents, 0) AS opening_arr_cents,
           coalesce(c.arr_cents, 0) AS closing_arr_cents
    FROM keys k
    JOIN months m USING (month)
    LEFT JOIN customer_mrr c ON c.month = k.month AND c.customer = k.customer
    LEFT JOIN customer_mrr p ON p.month = m.prev_month AND p.customer = k.customer
),
first_paid AS (
    SELECT customer, min(month) AS first_month FROM customer_mrr GROUP BY 1
),
ending AS (                                              -- why the customer's last subscription to end in the month ended
    SELECT m.month, s.customer, arg_max(s.cancellation_reason, s.ended_at) AS reason
    FROM months m
    JOIN stg_subscriptions s ON s.ended_at > m.prev_month_end AND s.ended_at <= m.month_end
    GROUP BY ALL
)
SELECT p.month, p.customer, p.opening_arr_cents, p.closing_arr_cents,
       p.closing_arr_cents - p.opening_arr_cents AS arr_delta_cents,
       CASE WHEN p.opening_arr_cents = 0 AND f.first_month = p.month THEN 'new'
            WHEN p.opening_arr_cents = 0                              THEN 'reactivation'
            WHEN p.closing_arr_cents = 0                              THEN 'churn'
            WHEN p.closing_arr_cents > p.opening_arr_cents            THEN 'expansion'
            WHEN p.closing_arr_cents < p.opening_arr_cents            THEN 'contraction'
            ELSE 'flat' END AS movement,
       CASE WHEN p.closing_arr_cents = 0
            THEN CASE WHEN e.reason = 'payment_failed' THEN 'involuntary' ELSE 'voluntary' END
       END AS churn_type
FROM pairs p
JOIN first_paid f USING (customer)
LEFT JOIN ending e ON e.month = p.month AND e.customer = p.customer;

-- Drivers: item-level deltas for customers in expansion or contraction (§2.4).
CREATE OR REPLACE TABLE mrr_drivers AS
WITH months AS (
    SELECT month, lag(month) OVER (ORDER BY month) AS prev_month FROM calendar
),
moved AS (
    SELECT mv.month, m.prev_month, mv.customer, mv.movement
    FROM mrr_movements mv JOIN months m USING (month)
    WHERE mv.movement IN ('expansion', 'contraction')
),
cur AS (SELECT i.* FROM mrr_item_month_end i JOIN moved USING (month, customer)),
prv AS (SELECT i.*, moved.month AS for_month FROM mrr_item_month_end i
        JOIN moved ON i.month = moved.prev_month AND i.customer = moved.customer),
base_change AS (                                         -- did the customer's base plan change this month?
    SELECT moved.month, moved.customer,
           (SELECT any_value(product) FROM prv WHERE prv.for_month = moved.month AND prv.customer = moved.customer AND NOT is_seat)
        <> (SELECT any_value(product) FROM cur WHERE cur.month = moved.month AND cur.customer = moved.customer AND NOT is_seat)
           AS plan_changed
    FROM moved
),
items AS (
    SELECT coalesce(c.month, p.for_month) AS month,
           coalesce(c.customer, p.customer) AS customer,
           coalesce(c.subscription_item, p.subscription_item) AS subscription_item,
           p.subscription AS prev_subscription, c.subscription AS cur_subscription,
           p.product AS prev_product, c.product AS cur_product,
           p.interval AS prev_interval, c.interval AS cur_interval,
           p.unit_amount AS prev_unit, c.unit_amount AS cur_unit,
           p.quantity AS prev_qty, c.quantity AS cur_qty,
           p.recurring_discount AS prev_discount, c.recurring_discount AS cur_discount,
           coalesce(c.arr_cents, 0) - coalesce(p.arr_cents, 0) AS arr_delta_cents
    FROM cur c
    FULL JOIN prv p ON p.for_month = c.month AND p.subscription_item = c.subscription_item
)
SELECT i.month, i.customer, mv.movement, i.subscription_item,
       CASE WHEN i.prev_product IS NOT NULL AND i.cur_product IS NOT NULL THEN
                CASE WHEN i.prev_product  <> i.cur_product  THEN 'plan_change'
                     WHEN i.prev_interval <> i.cur_interval THEN 'interval_switch'
                     WHEN i.prev_unit     <> i.cur_unit     THEN 'price_change'
                     WHEN i.prev_qty      <> i.cur_qty      THEN 'quantity_change'
                     WHEN i.cur_discount  <  i.prev_discount THEN 'discount_ended'
                     WHEN i.cur_discount  >  i.prev_discount THEN 'discount_started'
                     ELSE 'none' END
            WHEN b.plan_changed THEN 'plan_change'       -- seat item added or dropped with a plan change
            ELSE 'quantity_change' END AS driver,         -- seat item added or removed on its own
       i.arr_delta_cents
FROM items i
JOIN mrr_movements mv USING (month, customer)
JOIN base_change b USING (month, customer)
WHERE i.arr_delta_cents <> 0;

-- The bridge. Opening and closing come from mrr_month_end, independent of the movements.
CREATE OR REPLACE TABLE mrr_bridge AS
WITH totals AS (
    SELECT c.month, coalesce(sum(m.arr_cents), 0) AS closing
    FROM calendar c LEFT JOIN mrr_month_end m USING (month)
    GROUP BY c.month
),
mv AS (
    SELECT month,
           sum(arr_delta_cents)  FILTER (WHERE movement = 'new')                                AS new,
           sum(arr_delta_cents)  FILTER (WHERE movement = 'reactivation')                       AS reactivation,
           sum(arr_delta_cents)  FILTER (WHERE movement = 'expansion')                          AS expansion,
           -sum(arr_delta_cents) FILTER (WHERE movement = 'contraction')                        AS contraction,
           -sum(arr_delta_cents) FILTER (WHERE movement = 'churn' AND churn_type = 'voluntary')   AS churn_voluntary,
           -sum(arr_delta_cents) FILTER (WHERE movement = 'churn' AND churn_type = 'involuntary') AS churn_involuntary,
           count(*) FILTER (WHERE movement = 'new')                                             AS new_customers,
           count(*) FILTER (WHERE movement = 'reactivation')                                    AS reactivated_customers,
           count(*) FILTER (WHERE movement = 'expansion')                                       AS expanded_customers,
           count(*) FILTER (WHERE movement = 'contraction')                                     AS contracted_customers,
           count(*) FILTER (WHERE movement = 'churn' AND churn_type = 'voluntary')              AS churned_voluntary_customers,
           count(*) FILTER (WHERE movement = 'churn' AND churn_type = 'involuntary')            AS churned_involuntary_customers
    FROM mrr_movements GROUP BY month
),
blind_spot AS (                                          -- paid inside the month, not paying at either month-end
    SELECT c.month, count(DISTINCT s.customer) AS customers
    FROM calendar c
    JOIN stg_subscriptions s ON s.ended_at > c.prev_month_end AND s.ended_at <= c.month_end
    JOIN stg_lines l ON l.subscription = s.subscription AND l.amount > 0
                    AND l.period_start > c.prev_month_end AND l.period_start <= c.month_end
    WHERE NOT EXISTS (SELECT 1 FROM customer_mrr cm WHERE cm.customer = s.customer
                      AND cm.month IN (c.month, strftime(strptime(c.month, '%Y-%m') - INTERVAL 1 MONTH, '%Y-%m')))
    GROUP BY c.month
)
SELECT t.month,
       coalesce(lag(t.closing) OVER (ORDER BY t.month), 0) AS opening_arr_cents,
       coalesce(mv.new, 0)               AS new_arr_cents,
       coalesce(mv.reactivation, 0)      AS reactivation_arr_cents,
       coalesce(mv.expansion, 0)         AS expansion_arr_cents,
       coalesce(mv.contraction, 0)       AS contraction_arr_cents,
       coalesce(mv.churn_voluntary, 0)   AS churn_voluntary_arr_cents,
       coalesce(mv.churn_involuntary, 0) AS churn_involuntary_arr_cents,
       t.closing                         AS closing_arr_cents,
       t.closing - (coalesce(lag(t.closing) OVER (ORDER BY t.month), 0)
                    + coalesce(mv.new, 0) + coalesce(mv.reactivation, 0) + coalesce(mv.expansion, 0)
                    - coalesce(mv.contraction, 0) - coalesce(mv.churn_voluntary, 0)
                    - coalesce(mv.churn_involuntary, 0)) AS unexplained_arr_cents,
       coalesce(mv.new_customers, 0)                 AS new_customers,
       coalesce(mv.reactivated_customers, 0)         AS reactivated_customers,
       coalesce(mv.expanded_customers, 0)            AS expanded_customers,
       coalesce(mv.contracted_customers, 0)          AS contracted_customers,
       coalesce(mv.churned_voluntary_customers, 0)   AS churned_voluntary_customers,
       coalesce(mv.churned_involuntary_customers, 0) AS churned_involuntary_customers,
       coalesce(b.customers, 0)                      AS blind_spot_customers
FROM totals t
LEFT JOIN mv USING (month)
LEFT JOIN blind_spot b USING (month);

CREATE OR REPLACE TABLE mrr_bridge_drivers AS
SELECT month, movement, driver, sum(arr_delta_cents) AS arr_delta_cents, count(DISTINCT customer) AS customers
FROM mrr_drivers GROUP BY ALL;

-- For reading only: the bridge in MRR dollars (ARR cents / 1200), unrounded.
CREATE OR REPLACE VIEW mrr_bridge_usd AS
SELECT month,
       opening_arr_cents / 1200.0           AS opening,
       new_arr_cents / 1200.0               AS new,
       reactivation_arr_cents / 1200.0      AS reactivation,
       expansion_arr_cents / 1200.0         AS expansion,
       -contraction_arr_cents / 1200.0      AS contraction,
       -churn_voluntary_arr_cents / 1200.0  AS churn_vol,
       -churn_involuntary_arr_cents / 1200.0 AS churn_invol,
       closing_arr_cents / 1200.0           AS closing,
       unexplained_arr_cents                AS unexplained_cents
FROM mrr_bridge;
