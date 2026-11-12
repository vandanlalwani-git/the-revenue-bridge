"""Facts files for the AI notes: notes/facts/YYYY-MM.json, one per reporting month (PLAN §5.1).

Each fact has an ID (F1, F2, ...), a plain label, an exact value and its displayed form under
definitions §7, using pipeline/display.py only. Facts carry metrics, never event labels: nothing
here reads the answer key.

The facts pre-compute two things the write-up must disclose: the top 5 customer movers, and each
headline metric's change against its trailing 3-month average (the 3 calendar months before,
with months that had none counted as 0).

Usage: python -m notes.build_facts [--data data] [--out notes/facts]
"""
from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path

from pipeline import display
from pipeline.run import build, connect

ROOT = Path(__file__).resolve().parents[1]
FIRST_REPORTING_MONTH = "2025-01"


# ---- displayed forms (definitions §7) ------------------------------------------------
def money(dollars: Fraction) -> dict:
    """Whole dollars, plus $k to 1 decimal for figures of $10,000 or more."""
    whole = display.whole_dollars(dollars)
    out = {"value": whole, "display": f"${whole:,}" if whole >= 0 else f"-${-whole:,}"}
    if abs(dollars) >= 10_000:
        out["display_k"] = f"${display.thousands(dollars)}k"
    return out


def pct(numer, denom) -> dict | None:
    if not denom:
        return None
    p = display.percent(numer, denom)
    return {"value": float(p), "display": f"{p}%"}


def points(numer: Fraction) -> dict:
    """Percentage points to 1 decimal place, from a difference already in percent."""
    p = display.percent(numer, 100)
    return {"value": float(p), "display": f"{p} pts"}


def count(n: int) -> dict:
    return {"value": int(n), "display": f"{int(n):,}"}


def avg_count(x: Fraction) -> dict:
    v = display.round_half_away(Fraction(x) * 10)
    shown = f"{v / 10:,.1f}"
    return {"value": v / 10, "display": shown}


def times(ratio: Fraction) -> dict:
    """A ratio to 1 decimal place, e.g. 2.4x. Notes write it as "2.4×" or "2.4 times"."""
    v = display.round_half_away(ratio * 10)
    return {"value": v / 10, "display": f"{v / 10:.1f}×"}


def text(s: str) -> dict:
    return {"value": s, "display": s}


class Facts:
    def __init__(self, month: str):
        self.month, self.items = month, []

    def add(self, group: str, label: str, shown: dict | None, **extra):
        if shown is None:
            return None
        fid = f"F{len(self.items) + 1}"
        self.items.append({"id": fid, "group": group, "label": label, **shown, **extra})
        return fid


# ---- the metrics, as exact numbers per month ----------------------------------------------
def load(con):
    q = lambda sql: con.execute(sql).fetchall()
    rows = lambda sql: [dict(zip([d[0] for d in con.description], r)) for r in con.execute(sql).fetchall()]
    by_month = lambda sql: {r["month"]: r for r in rows(sql)}
    months = [m for (m,) in q("SELECT month FROM calendar ORDER BY month")]
    data = {
        "months": months,
        "bridge": by_month("SELECT * FROM mrr_bridge"),
        "retention": by_month("SELECT * FROM retention_month"),
        "revenue": by_month("SELECT * FROM revenue_month"),
        "recon": by_month("SELECT * FROM reconciliation_month"),
        "ecl": by_month("SELECT * FROM ecl_month"),
        "drivers": rows("SELECT * FROM mrr_bridge_drivers ORDER BY month, movement, driver"),
        "refunds": rows("SELECT * FROM refund_split ORDER BY month, amount_cents DESC, refund"),
        "cohorts": rows("SELECT * FROM cohort_retention ORDER BY cohort, k"),
        "movers": rows("""
            WITH plan AS (                                -- base plan at the month-end, or the one before if churned
                SELECT month, customer, any_value(product) FILTER (WHERE NOT is_seat) AS product
                FROM mrr_item_month_end GROUP BY ALL
            ),
            main_driver AS (
                SELECT month, customer, arg_max(driver, abs(arr_delta_cents)) AS driver
                FROM mrr_drivers GROUP BY ALL
            )
            SELECT mv.month, mv.customer, mv.movement, mv.churn_type, mv.arr_delta_cents,
                   coalesce(pc.product, pp.product) AS plan, d.driver
            FROM mrr_movements mv
            LEFT JOIN plan pc ON pc.month = mv.month AND pc.customer = mv.customer AND mv.closing_arr_cents > 0
            LEFT JOIN plan pp ON pp.month = strftime(strptime(mv.month, '%Y-%m') - INTERVAL 1 MONTH, '%Y-%m')
                             AND pp.customer = mv.customer
            LEFT JOIN main_driver d ON d.month = mv.month AND d.customer = mv.customer
            WHERE mv.arr_delta_cents <> 0
            ORDER BY mv.month, abs(mv.arr_delta_cents) DESC, mv.customer"""),
        "churn_by_plan": rows("""
            WITH plan AS (
                SELECT month, customer, any_value(product) FILTER (WHERE NOT is_seat) AS product
                FROM mrr_item_month_end GROUP BY ALL
            )
            SELECT mv.month, p.product AS plan, mv.churn_type, count(*) AS customers,
                   sum(mv.opening_arr_cents) AS arr_cents
            FROM mrr_movements mv
            JOIN plan p ON p.customer = mv.customer
                       AND p.month = strftime(strptime(mv.month, '%Y-%m') - INTERVAL 1 MONTH, '%Y-%m')
            WHERE mv.movement = 'churn'
            GROUP BY ALL ORDER BY mv.month, p.product, mv.churn_type"""),
        "dunning": by_month("""
            SELECT c.month,
                   (SELECT count(DISTINCT d.customer) FROM stg_dunning d
                    WHERE d.first_failed_at > c.prev_month_end AND d.first_failed_at <= c.month_end) AS entering,
                   (SELECT count(*) FROM customer_mrr m WHERE m.month = c.month AND m.past_due) AS in_dunning,
                   (SELECT coalesce(sum(m.arr_cents), 0) FROM customer_mrr m WHERE m.month = c.month AND m.past_due) AS at_risk_arr
            FROM calendar c"""),
    }
    return data


def headline(data, m) -> dict:
    """Headline metrics for month m as exact values: (kind, value). Kinds: mrr, usd, count, pct."""
    b, r, rv, e, d = data["bridge"][m], data["retention"][m], data["revenue"][m], data["ecl"][m], data["dunning"][m]
    mrr = lambda arr: ("mrr", Fraction(arr, 1200))
    usd = lambda cents: ("usd", Fraction(cents, 100))
    ratio = lambda n, dn: ("pct", Fraction(n) * 100 / dn if dn else None)
    return {
        "Closing MRR": mrr(b["closing_arr_cents"]),
        "New MRR": mrr(b["new_arr_cents"]),
        "Expansion MRR": mrr(b["expansion_arr_cents"]),
        "Contraction MRR": mrr(b["contraction_arr_cents"]),
        "Voluntary churn MRR": mrr(b["churn_voluntary_arr_cents"]),
        "Involuntary churn MRR": mrr(b["churn_involuntary_arr_cents"]),
        "Reactivation MRR": mrr(b["reactivation_arr_cents"]),
        "New customers": ("count", b["new_customers"]),
        "Churned customers": ("count", b["churned_voluntary_customers"] + b["churned_involuntary_customers"]),
        "Involuntarily churned customers": ("count", b["churned_involuntary_customers"]),
        "Customers expanding": ("count", b["expanded_customers"]),
        "Logo churn": ratio(r["churned_customers"], r["opening_customers"]),
        "Net MRR churn": ratio(r["churn_arr_cents"] + r["contraction_arr_cents"] - r["expansion_arr_cents"], r["opening_arr_cents"]),
        "NRR (12-month)": ratio(r["nrr_arr_cents"], r["nrr_base_arr_cents"]),
        "GRR (12-month)": ratio(r["grr_arr_cents"], r["nrr_base_arr_cents"]),
        "Net revenue": usd(rv["net_revenue_cents"]),
        "Billings": usd(rv["billings_cents"]),
        "Net cash collected": usd(rv["net_cash_cents"]),
        "Refunds paid": usd(rv["refunds_total_cents"]),
        "Bad-debt expense": usd(e["bad_debt_expense_cents"]),
        "Customers entering dunning": ("count", d["entering"]),
        "Customers in dunning at month-end": ("count", d["in_dunning"]),
        "At-risk MRR (customers in dunning)": mrr(d["at_risk_arr"]),
    }


def shown(kind: str, value) -> dict | None:
    if value is None:
        return None
    if kind in ("mrr", "usd"):
        return money(value)
    if kind == "count":
        return count(value) if Fraction(value).denominator == 1 else avg_count(value)
    return {"value": float(display.percent(value, 100)), "display": f"{display.percent(value, 100)}%"}


def build_month(data, m: str) -> dict:
    f = Facts(m)
    i = data["months"].index(m)
    b, r, rv, rc, e, d = (data[k][m] for k in ("bridge", "retention", "revenue", "recon", "ecl", "dunning"))
    mrr = lambda arr: display.arr_cents_to_mrr_dollars(arr)
    usd = lambda cents: display.cents_to_dollars(cents)

    # MRR bridge, displayed so the components add up (§7.3). Reductions are shown as positive amounts.
    shown_bridge = display.mrr_bridge_row(b)
    labels = {"opening": "Opening MRR", "new": "New MRR", "reactivation": "Reactivation MRR",
              "expansion": "Expansion MRR", "contraction": "Contraction MRR (reduces MRR)",
              "churn_voluntary": "Voluntary churn MRR (reduces MRR)",
              "churn_involuntary": "Involuntary churn MRR (failed payments, reduces MRR)", "closing": "Closing MRR"}
    for key, label in labels.items():
        v = abs(shown_bridge[key])
        f.add("mrr_bridge", label, {"value": v, "display": f"${v:,}",
                                    **({"display_k": f"${display.thousands(Fraction(v))}k"} if v >= 10_000 else {})})
    f.add("mrr_bridge", "Unexplained (must be 0)", money(mrr(b["unexplained_arr_cents"])))
    for label, n in [("Paying customers at month-end", r["closing_customers"]),
                     ("Paying customers at prior month-end", r["opening_customers"]),
                     ("New customers", b["new_customers"]), ("Reactivated customers", b["reactivated_customers"]),
                     ("Customers expanding", b["expanded_customers"]), ("Customers contracting", b["contracted_customers"]),
                     ("Customers churned voluntarily", b["churned_voluntary_customers"]),
                     ("Customers churned involuntarily (failed payments)", b["churned_involuntary_customers"]),
                     ("Customers who started and left within the month (not in month-end MRR)", b["blind_spot_customers"])]:
        f.add("customers", label, count(n))

    # Drivers of expansion and contraction.
    for row in (x for x in data["drivers"] if x["month"] == m):
        name = row["driver"].replace("_", " ")
        f.add("drivers", f"{row['movement'].capitalize()} from {name}: MRR", money(abs(mrr(row["arr_delta_cents"]))))
        f.add("drivers", f"{row['movement'].capitalize()} from {name}: customers", count(row["customers"]))

    # Retention rates.
    f.add("retention", "Logo churn (customers lost / paying at prior month-end)", pct(r["churned_customers"], r["opening_customers"]))
    f.add("retention", "Gross MRR churn (churn + contraction) / opening MRR",
          pct(r["churn_arr_cents"] + r["contraction_arr_cents"], r["opening_arr_cents"]))
    f.add("retention", "Net MRR churn (churn + contraction - expansion) / opening MRR",
          pct(r["churn_arr_cents"] + r["contraction_arr_cents"] - r["expansion_arr_cents"], r["opening_arr_cents"]))
    f.add("retention", "NRR, 12-month", pct(r["nrr_arr_cents"], r["nrr_base_arr_cents"]))
    f.add("retention", "GRR, 12-month", pct(r["grr_arr_cents"], r["nrr_base_arr_cents"]))

    # Churn by plan and type (plan at the prior month-end).
    for row in (x for x in data["churn_by_plan"] if x["month"] == m):
        kind = "voluntary" if row["churn_type"] == "voluntary" else "involuntary (failed payments)"
        f.add("churn_by_plan", f"{row['plan']} customers churned, {kind}", count(row["customers"]))
        f.add("churn_by_plan", f"{row['plan']} MRR churned, {kind}", money(mrr(row["arr_cents"])))

    # Payment health.
    f.add("payments", "Customers with a first failed payment this month", count(d["entering"]))
    f.add("payments", "Paying customers in dunning at month-end", count(d["in_dunning"]))
    f.add("payments", "At-risk MRR (customers in dunning at month-end)", money(mrr(d["at_risk_arr"])))

    # Revenue, cash and the ledger.
    for label, cents in [("Billings (invoices issued)", rv["billings_cents"]),
                         ("Revenue recognised, before refunds", rv["gross_recognised_cents"]),
                         ("Refunds for service already delivered (reduce revenue)", rv["refunds_delivered_cents"]),
                         ("Net revenue", rv["net_revenue_cents"]),
                         ("Cash collected", rv["collections_cents"]),
                         ("Refunds paid, all", rv["refunds_total_cents"]),
                         ("Net cash collected", rv["net_cash_cents"]),
                         ("Contract liability (deferred revenue) at month-end", rc["cl_close_cents"]),
                         ("Change in contract liability", rc["cl_close_cents"] - rc["cl_open_cents"]),
                         ("Receivables at month-end", rc["ar_close_cents"]),
                         ("Allowance for credit losses at month-end", e["allowance_close_cents"]),
                         ("Bad-debt expense", e["bad_debt_expense_cents"]),
                         ("Written off: earned part (credit loss)", e["earned_written_off_cents"]),
                         ("Written off: unearned part (released from contract liability)", rv["unearned_written_off_cents"])]:
        f.add("revenue_cash", label, money(usd(cents)))

    # Refunds, split, plus the largest one.
    refunds = [x for x in data["refunds"] if x["month"] == m]
    f.add("refunds", "Refunds issued", count(len(refunds)))
    if refunds:
        f.add("refunds", "Refunds: part for service already delivered (reduces revenue)",
              money(usd(sum(x["delivered_cents"] for x in refunds))))
        f.add("refunds", "Refunds: part for service not yet delivered (reduces contract liability)",
              money(usd(sum(x["unearned_cents"] for x in refunds))))
        big = refunds[0]
        f.add("refunds", "Largest single refund", money(usd(big["amount_cents"])))
        f.add("refunds", "Largest single refund: customer", text(big["customer"]))
        f.add("refunds", "Largest single refund: delivered-service part", money(usd(big["delivered_cents"])))
        f.add("refunds", "Largest single refund: not-yet-delivered part", money(usd(big["unearned_cents"])))

    # Reconciliations, displayed so each adds up (§7.3).
    den = rc["r1_denominator"] * 100
    frac = lambda col: Fraction(rc[col], den)
    r1 = display.bridge(frac("r1_closing_mrr"),
                        [("timing", -frac("r1_timing")), ("daycount", frac("r1_daycount_proration")),
                         ("once", -frac("r1_once_coupons")), ("refunds", -frac("r1_refunds_delivered"))],
                        frac("r1_net_revenue"))
    for key, label in [("opening", "Closing MRR"), ("timing", "Timing: closing MRR minus average MRR for the month (subtract)"),
                       ("daycount", "Day count and proration (add; negative means subtract)"),
                       ("once", "One-off coupons (subtract)"), ("refunds", "Refunds for delivered service (subtract)"),
                       ("closing", "Net revenue")]:
        f.add("r1_mrr_to_revenue", f"R1 {label}", {"value": r1[key], "display": f"${r1[key]:,}" if r1[key] >= 0 else f"-${-r1[key]:,}"})
    r2 = display.bridge(usd(rc["net_revenue_cents"]),
                        [("refunds", usd(rc["refunds_delivered_cents"])), ("cl", usd(rc["cl_change_cents"])),
                         ("uwo", usd(rc["unearned_written_off_cents"])), ("ur", usd(rc["refunds_unearned_cents"]))],
                        usd(rc["billings_cents"]))
    for key, label in [("opening", "Net revenue"), ("refunds", "Refunds for delivered service (add back)"),
                       ("cl", "Change in contract liability (add)"), ("uwo", "Unearned part of write-offs (add)"),
                       ("ur", "Unearned part of refunds (add)"), ("closing", "Billings")]:
        f.add("r2_revenue_to_billings", f"R2 {label}", {"value": r2[key], "display": f"${r2[key]:,}" if r2[key] >= 0 else f"-${-r2[key]:,}"})
    r3 = display.bridge(usd(rc["billings_cents"]),
                        [("ar", -usd(rc["ar_change_cents"])), ("ewo", -usd(rc["earned_written_off_cents"])),
                         ("uwo", -usd(rc["unearned_written_off_cents"])), ("ref", -usd(rc["refunds_total_cents"]))],
                        usd(rc["net_cash_cents"]))
    for key, label in [("opening", "Billings"), ("ar", "Change in receivables (an increase subtracts)"),
                       ("ewo", "Earned part of write-offs (subtract)"), ("uwo", "Unearned part of write-offs (subtract)"),
                       ("ref", "Refunds paid (subtract)"), ("closing", "Net cash collected")]:
        f.add("r3_billings_to_cash", f"R3 {label}", {"value": r3[key], "display": f"${r3[key]:,}" if r3[key] >= 0 else f"-${-r3[key]:,}"})

    # Cohorts aged 0-6 months at this month-end (cohort = month of first paid day).
    for row in (x for x in data["cohorts"] if x["month"] == m and x["k"] <= 6):
        c = row["cohort"]
        f.add("cohorts", f"Cohort {c}: customers", count(row["cohort_customers"]))
        f.add("cohorts", f"Cohort {c}: months since start", count(row["k"]))
        f.add("cohorts", f"Cohort {c}: still paying", count(row["retained_customers"]))
        f.add("cohorts", f"Cohort {c}: logo retention", pct(row["retained_customers"], row["cohort_customers"]))
        if row["k"] >= 1:
            prev = next(x for x in data["cohorts"] if x["cohort"] == c and x["k"] == row["k"] - 1)
            change = (Fraction(row["retained_customers"]) - prev["retained_customers"]) * 100 / row["cohort_customers"]
            f.add("cohorts", f"Cohort {c}: change in logo retention since last month", points(change))
        f.add("cohorts", f"Cohort {c}: MRR retention vs its first month-end", pct(row["arr_cents"], row["m0_arr_cents"]))

    # Top 5 movers by size of MRR change.
    for rank, row in enumerate([x for x in data["movers"] if x["month"] == m][:5], start=1):
        move = row["movement"] + (f" ({row['churn_type']})" if row["churn_type"] else "")
        f.add("top_movers", f"Mover {rank}: customer", text(row["customer"]))
        f.add("top_movers", f"Mover {rank}: movement", text(move))
        f.add("top_movers", f"Mover {rank}: plan", text(row["plan"] or "unknown"))
        if row["driver"]:
            f.add("top_movers", f"Mover {rank}: main driver", text(row["driver"].replace("_", " ")))
        f.add("top_movers", f"Mover {rank}: MRR change (size)", money(abs(mrr(row["arr_delta_cents"]))))

    # Each headline metric against its trailing 3-month average (prior calendar months, 0 where none).
    now = headline(data, m)
    prior = [headline(data, data["months"][j]) for j in range(i - 3, i)]
    for name, (kind, value) in now.items():
        values = [p[name][1] for p in prior]
        if value is None or any(v is None for v in values):
            continue
        avg = sum(Fraction(v) for v in values) / 3
        f.add("vs_trailing_3m", f"{name}: trailing 3-month average", shown(kind, avg))
        delta = Fraction(value) - avg
        if kind == "pct":
            f.add("vs_trailing_3m", f"{name}: change vs trailing average", points(delta))
        else:
            f.add("vs_trailing_3m", f"{name}: change vs trailing average", shown(kind, delta))
            if avg:
                f.add("vs_trailing_3m", f"{name}: change vs trailing average, percent", pct(delta, avg))
            if avg > 0 and value >= 0:
                f.add("vs_trailing_3m", f"{name}: times its trailing average", times(Fraction(value) / avg))

    return {"month": m,
            "notes": ["Money is in US dollars. MRR figures are monthly run-rate; revenue follows IFRS 15.",
                      "Each fact's 'display' is how it must be written. 'display_k' is an allowed short form.",
                      "A trailing average covers the 3 calendar months before this month."],
            "facts": f.items}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path, default=ROOT / "notes" / "facts")
    args = ap.parse_args(argv)
    con = connect(args.data)
    build(con)
    data = load(con)
    args.out.mkdir(parents=True, exist_ok=True)
    reporting = [m for m in data["months"] if m >= FIRST_REPORTING_MONTH]
    for m in reporting:
        (args.out / f"{m}.json").write_text(json.dumps(build_month(data, m), indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(reporting)} facts files to {args.out}")


if __name__ == "__main__":
    main()
