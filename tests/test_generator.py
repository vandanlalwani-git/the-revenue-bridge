"""Generator invariants: deterministic, internally consistent, nothing after the simulation end."""
from collections import defaultdict

from generator.generate import DAY, ts
from tests.conftest import CFG, simulate


def test_same_seed_same_data(sim):
    again = simulate(CFG["seed"])
    assert again.rows == sim.rows and again.truth == sim.truth


def test_dev_seed_differs(sim):
    assert simulate(CFG["dev_seed"]).rows["customers"] != sim.rows["customers"]


def test_invoice_totals_equal_line_items(sim):
    lines = defaultdict(int)
    for line in sim.rows["invoice_line_items"]:
        lines[line["invoice"]] += line["amount"] - line["discount_amount"]
    for inv in sim.rows["invoices"]:
        assert inv["amount_due"] == lines[inv["id"]] == inv["total"]
        assert inv["amount_paid"] + inv["amount_remaining"] == inv["amount_due"]


def test_cycle_lines_are_contiguous(sim):
    periods = defaultdict(list)
    for line in sim.rows["invoice_line_items"]:
        if not line["proration"]:
            periods[line["subscription_item"]].append((line["period_start"], line["period_end"]))
    for spans in periods.values():
        spans.sort()
        for (_, prev_end), (start, _) in zip(spans, spans[1:]):
            assert start == prev_end


def test_nothing_after_simulation_end(sim):
    end = ts(sim.end)
    for table in ("customers", "invoices", "charges", "refunds"):
        assert all(r["created"] <= end for r in sim.rows[table])
    for sub in sim.rows["subscriptions"]:
        assert sub["ended_at"] is None or sub["ended_at"] <= end


def test_subscription_status_matches_end_state(sim):
    for sub in sim.rows["subscriptions"]:
        assert (sub["status"] == "canceled") == (sub["ended_at"] is not None)
        if sub["ended_at"] is not None:
            assert sub["canceled_at"] <= sub["ended_at"]


def test_charges_match_invoice_outcome(sim):
    charges = defaultdict(list)
    for ch in sim.rows["charges"]:
        charges[ch["invoice"]].append(ch)
    subs = {s["id"]: s for s in sim.rows["subscriptions"]}
    retries = len(CFG["dunning"]["retry_days"])
    for inv in sim.rows["invoices"]:
        chs = sorted(charges[inv["id"]], key=lambda c: c["created"])
        assert inv["attempt_count"] == len(chs)
        if inv["status"] == "paid" and inv["amount_due"] > 0:
            assert [c["status"] for c in chs].count("succeeded") == 1 and chs[-1]["status"] == "succeeded"
        if inv["status"] == "uncollectible":
            # Day 0 attempt plus every retry failed; written off and cancelled 14 days after the first failure.
            assert len(chs) == retries + 1 and all(c["status"] == "failed" for c in chs)
            assert inv["marked_uncollectible_at"] - chs[0]["created"] == CFG["dunning"]["retry_days"][-1] * DAY
            sub = subs[inv["subscription"]]
            assert sub["ended_at"] == inv["marked_uncollectible_at"]
            assert sub["cancellation_reason"] == "payment_failed"


def test_refunds_never_exceed_charge(sim):
    charges = {c["id"]: c for c in sim.rows["charges"]}
    refunded = defaultdict(int)
    for re in sim.rows["refunds"]:
        assert charges[re["charge"]]["status"] == "succeeded"
        refunded[re["charge"]] += re["amount"]
    assert all(total <= charges[cid]["amount"] for cid, total in refunded.items())
