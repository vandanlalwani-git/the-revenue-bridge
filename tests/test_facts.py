"""Day 6: the facts files the AI notes are written from (PLAN §5.1)."""
import json
import re
from fractions import Fraction

import pytest

from notes import build_facts
from pipeline import display


@pytest.fixture(scope="module")
def data(marts):
    return build_facts.load(marts)


@pytest.fixture(scope="module")
def facts(data):
    months = [m for m in data["months"] if m >= build_facts.FIRST_REPORTING_MONTH]
    return {m: build_facts.build_month(data, m) for m in months}


def by_label(month_facts, label):
    hits = [f for f in month_facts["facts"] if f["label"] == label]
    assert len(hits) == 1, label
    return hits[0]


def group(month_facts, name):
    return {f["label"]: f["value"] for f in month_facts["facts"] if f["group"] == name}


def test_eighteen_months_with_unique_sequential_ids(facts):
    assert list(facts) == [f"{y}-{m:02d}" for y in (2025, 2026) for m in range(1, 13)][:18]
    for month, mf in facts.items():
        assert [f["id"] for f in mf["facts"]] == [f"F{i}" for i in range(1, len(mf["facts"]) + 1)], month
        assert all(isinstance(f["display"], str) and f["display"] for f in mf["facts"]), month


def test_no_event_labels_or_answer_key_content(facts):
    banned = re.compile(r"\bE[1-8]\b|planted|answer key|event|promo|price rise|card expir|control month", re.I)
    for month, mf in facts.items():
        assert not banned.search(json.dumps(mf)), month


def test_displayed_mrr_bridge_adds_up_and_matches_the_mart(facts, data):
    for month, mf in facts.items():
        g = group(mf, "mrr_bridge")
        total = (g["Opening MRR"] + g["New MRR"] + g["Reactivation MRR"] + g["Expansion MRR"]
                 - g["Contraction MRR (reduces MRR)"] - g["Voluntary churn MRR (reduces MRR)"]
                 - g["Involuntary churn MRR (failed payments, reduces MRR)"])
        assert total == g["Closing MRR"], month
        closing = display.whole_dollars(Fraction(data["bridge"][month]["closing_arr_cents"], 1200))
        assert g["Closing MRR"] == closing and g["Unexplained (must be 0)"] == 0, month


@pytest.mark.parametrize("name,first,last", [("r1_mrr_to_revenue", "R1 Closing MRR", "R1 Net revenue"),
                                             ("r2_revenue_to_billings", "R2 Net revenue", "R2 Billings"),
                                             ("r3_billings_to_cash", "R3 Billings", "R3 Net cash collected")])
def test_displayed_reconciliations_add_up(facts, name, first, last):
    for month, mf in facts.items():
        g = group(mf, name)
        middle = sum(v for k, v in g.items() if k not in (first, last))
        assert g[first] + middle == g[last], (month, name)


def test_r3_receivables_line_has_the_right_sign(facts, data):
    # The label says an increase subtracts, so a fall in receivables must be shown as a positive add.
    for month, mf in facts.items():
        change = data["recon"][month]["ar_change_cents"]
        shown = by_label(mf, "R3 Change in receivables (an increase subtracts)")["value"]
        # Largest-remainder display may move a component by $1 so the bridge adds up (§7.3).
        assert abs(shown - display.whole_dollars(Fraction(-change, 100))) <= 1, month
        assert shown * change <= 0, month


def test_refunds_split_and_largest_refund(facts, data):
    for month, mf in facts.items():
        refunds = [r for r in data["refunds"] if r["month"] == month]
        assert by_label(mf, "Refunds issued")["value"] == len(refunds)
        if not refunds:
            continue
        assert all(r["delivered_cents"] + r["unearned_cents"] == r["amount_cents"] for r in refunds)
        big = max(refunds, key=lambda r: r["amount_cents"])
        assert by_label(mf, "Largest single refund")["value"] == display.whole_dollars(Fraction(big["amount_cents"], 100))
        assert by_label(mf, "Largest single refund: customer")["value"] == big["customer"]


def test_trailing_average_uses_three_calendar_months_with_zeros(facts, data):
    months = data["months"]
    for month, mf in facts.items():
        i = months.index(month)
        prior = [data["bridge"][m]["churned_involuntary_customers"] for m in months[i - 3:i]]
        avg = Fraction(sum(prior), 3)
        shown = by_label(mf, "Involuntarily churned customers: trailing 3-month average")["value"]
        expected = display.round_half_away(avg * 10) / 10 if avg.denominator != 1 else int(avg)
        assert shown == expected, month


def test_top_movers_are_the_five_largest_changes(facts, marts):
    for month, mf in facts.items():
        sizes = [abs(Fraction(a, 1200)) for (a,) in marts.execute(
            "SELECT arr_delta_cents FROM mrr_movements WHERE month = ? AND arr_delta_cents <> 0 "
            "ORDER BY abs(arr_delta_cents) DESC LIMIT 5", [month]).fetchall()]
        shown = [f["value"] for f in mf["facts"] if f["label"].endswith("MRR change (size)")]
        assert shown == [display.whole_dollars(s) for s in sizes], month


def test_every_number_is_shown_as_digits(facts):
    for month, mf in facts.items():
        for f in mf["facts"]:
            if f["group"] in ("top_movers", "refunds") and isinstance(f["value"], str):
                continue
            assert re.fullmatch(r"-?\$?[\d,]+(\.\d)?(%| pts|×)?", f["display"]), (month, f)


def test_times_facts_are_value_over_trailing_average(facts, data):
    months = data["months"]
    for month, mf in facts.items():
        i = months.index(month)
        billings = [data["revenue"][m]["billings_cents"] for m in months[i - 3:i + 1]]
        ratio = Fraction(billings[-1]) / (Fraction(sum(billings[:3]), 3))
        shown = by_label(mf, "Billings: times its trailing average")
        assert shown["value"] == display.round_half_away(ratio * 10) / 10, month
        assert shown["display"] == f"{shown['value']:.1f}×", month
