"""Generator invariants: deterministic, internally consistent, nothing after the simulation end."""
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from generator.generate import Simulation, ts

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "scenario.yaml").read_text())


def simulate(seed):
    sim = Simulation(CFG, seed)
    sim.run()
    return sim


@pytest.fixture(scope="module")
def sim():
    return simulate(CFG["seed"])


def test_same_seed_same_data(sim):
    assert simulate(CFG["seed"]).rows == sim.rows


def test_dev_seed_differs(sim):
    assert simulate(CFG["dev_seed"]).rows["customers"] != sim.rows["customers"]


def test_invoice_totals_equal_line_items(sim):
    lines = defaultdict(int)
    for line in sim.rows["invoice_line_items"]:
        lines[line["invoice"]] += line["amount"] - line["discount_amount"]
    for inv in sim.rows["invoices"]:
        assert inv["amount_due"] == lines[inv["id"]]
        assert inv["amount_paid"] + inv["amount_remaining"] == inv["amount_due"]


def test_line_periods_are_contiguous(sim):
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
        assert sub["canceled_at"] is None or sub["canceled_at"] <= end


def test_subscription_status_matches_end_state(sim):
    for sub in sim.rows["subscriptions"]:
        assert (sub["status"] == "canceled") == (sub["ended_at"] is not None)
        if sub["ended_at"] is not None:
            assert sub["canceled_at"] is not None and sub["canceled_at"] < sub["ended_at"]
