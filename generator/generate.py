"""Seeded simulation of a B2B SaaS company's Stripe account.

Writes Stripe-shaped tables (integer cents, Unix epoch seconds, USD) to data/raw/*.parquet.

Day 1 scope: monthly plans, Scale seat add-ons, renewals, voluntary cancellation at
period end, every charge succeeds. Day 2 adds annual billing, trials, coupons,
proration, dunning, refunds, reactivations and the planted events.

Usage:  python -m generator.generate [--seed N] [--out data/raw]
"""
from __future__ import annotations

import argparse
import calendar
import heapq
import json
import math
import random
import string
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import yaml

from generator.schema import TABLES

ROOT = Path(__file__).resolve().parents[1]
BASE62 = string.digits + string.ascii_letters

ADJECTIVES = ["Bright", "Quiet", "Swift", "North", "Clear", "Bold", "Silver", "Open", "True", "Blue",
              "Iron", "Green", "Prime", "Little", "Golden", "Rapid", "Steady", "Wild", "Lucid", "Civic"]
NOUNS = ["Harbor", "Ledger", "Field", "Signal", "Forge", "Orbit", "Canvas", "Summit", "Grove", "Beacon",
         "Anchor", "Atlas", "Meadow", "Circuit", "Bridge", "Pillar", "Comet", "Delta", "Quarry", "Vista"]
SUFFIXES = ["Labs", "Studio", "Health", "Logistics", "Analytics", "Partners", "Systems", "Works", "AI", "Co"]


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def add_months(anchor: datetime, n: int) -> datetime:
    """Stripe-style billing anchor: same day-of-month, clamped to the month's last day."""
    y, m = divmod(anchor.month - 1 + n, 12)
    year, month = anchor.year + y, m + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def poisson(rng: random.Random, lam: float) -> int:
    # Knuth's method; fine for the small means used here.
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


@dataclass
class Item:
    id: str
    price: dict
    quantity: int
    created: int


@dataclass
class Sub:
    id: str
    customer: str
    plan: str
    anchor: datetime
    items: list[Item]
    created: int
    period_index: int = 0
    period_start: datetime | None = None
    period_end: datetime | None = None
    status: str = "active"
    cancel_at_period_end: bool = False
    canceled_at: int | None = None
    ended_at: int | None = None
    cancellation_reason: str | None = None


@dataclass(order=True)
class Event:
    at: int
    seq: int
    kind: str = field(compare=False)
    sub: Sub | None = field(default=None, compare=False)


class Simulation:
    def __init__(self, cfg: dict, seed: int):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.id_rng = random.Random(seed + 1)
        self.used_ids: set[str] = set()
        self.rows: dict[str, list[dict]] = {t: [] for t in TABLES}
        self.queue: list[Event] = []
        self.seq = 0
        sim = cfg["simulation"]
        self.start = datetime.combine(sim["start"], datetime.min.time(), tzinfo=timezone.utc)
        end_day = sim["end"]
        self.end = datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59, tzinfo=timezone.utc)
        self.prices: dict[tuple[str, str], dict] = {}   # (plan or plan_seats, interval) -> price row
        self.subs: dict[str, Sub] = {}

    # ---- helpers -------------------------------------------------------
    def new_id(self, prefix: str) -> str:
        while True:
            candidate = prefix + "_" + "".join(self.id_rng.choice(BASE62) for _ in range(14))
            if candidate not in self.used_ids:
                self.used_ids.add(candidate)
                return candidate

    def push(self, at: int, kind: str, sub: Sub | None = None) -> None:
        self.seq += 1
        heapq.heappush(self.queue, Event(at, self.seq, kind, sub))

    # ---- catalog -------------------------------------------------------
    def create_catalog(self) -> None:
        created = ts(self.start)
        for key, plan in self.cfg["plans"].items():
            prod = {"id": self.new_id("prod"), "name": plan["name"], "created": created, "active": True}
            self.rows["products"].append(prod)
            for interval, amount in (("month", plan["monthly_price"]), ("year", plan["annual_price"])):
                self.add_price(key, prod["id"], f"{plan['name']} {interval}ly", amount, interval, created)
            if "seats" in plan:
                seats = plan["seats"]
                seat_prod = {"id": self.new_id("prod"), "name": f"{plan['name']} extra seat",
                             "created": created, "active": True}
                self.rows["products"].append(seat_prod)
                for interval, amount in (("month", seats["monthly_price"]), ("year", seats["annual_price"])):
                    self.add_price(f"{key}_seats", seat_prod["id"], f"{plan['name']} seat {interval}ly",
                                   amount, interval, created)

    def add_price(self, key: str, product: str, nickname: str, amount: int, interval: str, created: int) -> None:
        row = {"id": self.new_id("price"), "product": product, "nickname": nickname, "unit_amount": amount,
               "currency": "usd", "recurring_interval": interval, "recurring_interval_count": 1,
               "active": True, "created": created}
        self.rows["prices"].append(row)
        self.prices[(key, interval)] = row

    # ---- acquisition ---------------------------------------------------
    def schedule_signups(self) -> None:
        acq = self.cfg["acquisition"]
        month_start, m = self.start, 0
        while month_start <= self.end:
            month_end = add_months(month_start, 1)
            lam = acq["signups_first_month"] * (1 + acq["monthly_growth"]) ** m
            span = ts(month_end) - ts(month_start)
            for _ in range(poisson(self.rng, lam)):
                self.push(ts(month_start) + self.rng.randrange(span), "signup")
            month_start, m = month_end, m + 1

    def pick_plan(self) -> str:
        plans = self.cfg["plans"]
        return self.rng.choices(list(plans), weights=[p["mix"] for p in plans.values()])[0]

    def on_signup(self, at: int) -> None:
        name = f"{self.rng.choice(ADJECTIVES)} {self.rng.choice(NOUNS)} {self.rng.choice(SUFFIXES)}"
        slug = name.lower().replace(" ", "")
        cus = {"id": self.new_id("cus"), "created": at, "name": name,
               "email": f"billing@{slug}{len(self.rows['customers'])}.example", "currency": "usd"}
        self.rows["customers"].append(cus)

        plan_key = self.pick_plan()
        items = [Item(self.new_id("si"), self.prices[(plan_key, "month")], 1, at)]
        seats_cfg = self.cfg["plans"][plan_key].get("seats")
        if seats_cfg:
            # Geometric draw with the configured mean; 0 extra seats means no seat item.
            p = 1 / (1 + seats_cfg["extra_seats_mean"])
            extra = int(math.log(1 - self.rng.random()) / math.log(1 - p))
            if extra > 0:
                items.append(Item(self.new_id("si"), self.prices[(f"{plan_key}_seats", "month")], extra, at))

        anchor = datetime.fromtimestamp(at, tz=timezone.utc)
        sub = Sub(self.new_id("sub"), cus["id"], plan_key, anchor, items, created=at)
        self.subs[sub.id] = sub
        self.start_period(sub, "subscription_create")

    # ---- billing cycle -------------------------------------------------
    def start_period(self, sub: Sub, billing_reason: str) -> None:
        sub.period_start = add_months(sub.anchor, sub.period_index)
        sub.period_end = add_months(sub.anchor, sub.period_index + 1)
        self.invoice(sub, billing_reason)
        self.push(ts(sub.period_end), "renewal", sub)
        # The customer may decide to cancel at some point during this period.
        if self.rng.random() < self.cfg["plans"][sub.plan]["monthly_churn"]:
            span = ts(sub.period_end) - ts(sub.period_start)
            self.push(ts(sub.period_start) + self.rng.randrange(span), "cancel_request", sub)

    def on_cancel_request(self, sub: Sub, at: int) -> None:
        if sub.ended_at is None:
            sub.cancel_at_period_end = True
            sub.canceled_at = at
            sub.cancellation_reason = "cancellation_requested"

    def on_renewal(self, sub: Sub, at: int) -> None:
        if sub.cancel_at_period_end:
            sub.status = "canceled"
            sub.ended_at = at
            return
        sub.period_index += 1
        self.start_period(sub, "subscription_cycle")

    def invoice(self, sub: Sub, billing_reason: str) -> None:
        at = ts(sub.period_start)
        inv_id = self.new_id("in")
        subtotal = 0
        for item in sub.items:
            amount = item.price["unit_amount"] * item.quantity
            subtotal += amount
            self.rows["invoice_line_items"].append({
                "id": self.new_id("il"), "invoice": inv_id, "subscription": sub.id,
                "subscription_item": item.id, "price": item.price["id"], "type": "subscription",
                "proration": False, "quantity": item.quantity, "amount": amount,
                "discount_amount": 0, "discount": None,
                "period_start": at, "period_end": ts(sub.period_end), "currency": "usd",
            })
        self.rows["invoices"].append({
            "id": inv_id, "customer": sub.customer, "subscription": sub.id,
            "billing_reason": billing_reason, "status": "paid", "created": at, "finalized_at": at,
            "paid_at": at, "marked_uncollectible_at": None, "subtotal": subtotal,
            "total_discount_amount": 0, "total": subtotal, "amount_due": subtotal,
            "amount_paid": subtotal, "amount_remaining": 0, "currency": "usd",
        })
        self.rows["charges"].append({
            "id": self.new_id("ch"), "customer": sub.customer, "invoice": inv_id, "amount": subtotal,
            "created": at, "status": "succeeded", "failure_code": None, "currency": "usd",
        })

    # ---- run -----------------------------------------------------------
    def run(self) -> None:
        self.create_catalog()
        self.schedule_signups()
        end = ts(self.end)
        while self.queue and self.queue[0].at <= end:
            ev = heapq.heappop(self.queue)
            if ev.kind == "signup":
                self.on_signup(ev.at)
            elif ev.kind == "renewal":
                self.on_renewal(ev.sub, ev.at)
            elif ev.kind == "cancel_request":
                self.on_cancel_request(ev.sub, ev.at)
        self.snapshot(self.subs.values())

    def snapshot(self, subs) -> None:
        """Write subscription state as of the simulation end, like an API export."""
        for sub in sorted(subs, key=lambda s: s.created):
            self.rows["subscriptions"].append({
                "id": sub.id, "customer": sub.customer, "status": sub.status, "created": sub.created,
                "start_date": sub.created, "billing_cycle_anchor": ts(sub.anchor),
                "current_period_start": ts(sub.period_start), "current_period_end": ts(sub.period_end),
                "cancel_at_period_end": sub.cancel_at_period_end, "canceled_at": sub.canceled_at,
                "ended_at": sub.ended_at, "cancellation_reason": sub.cancellation_reason,
                "trial_start": None, "trial_end": None, "collection_method": "charge_automatically",
            })
            for item in sub.items:
                self.rows["subscription_items"].append({
                    "id": item.id, "subscription": sub.id, "price": item.price["id"],
                    "quantity": item.quantity, "created": item.created,
                })


def write_parquet(rows: dict[str, list[dict]], out: Path) -> None:
    # Bulk-load through newline-delimited JSON with an explicit schema; row inserts are slow.
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        for table, columns in TABLES.items():
            src = Path(tmp) / f"{table}.ndjson"
            src.write_text("".join(json.dumps({c: r[c] for c in columns}) + "\n" for r in rows[table]))
            cols = ", ".join(f"'{c}': '{t}'" for c, t in columns.items())
            select = ", ".join(f'"{c}"' for c in columns)
            con.execute(f"""
                COPY (SELECT {select} FROM read_json('{src}', format = 'newline_delimited', columns = {{{cols}}}))
                TO '{out / table}.parquet' (FORMAT parquet)
            """)
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, help="override scenario seed (e.g. dev_seed)")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "raw")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "scenario.yaml").read_text())
    seed = args.seed if args.seed is not None else cfg["seed"]
    sim = Simulation(cfg, seed)
    sim.run()
    write_parquet(sim.rows, args.out)
    counts = ", ".join(f"{t}={len(r)}" for t, r in sim.rows.items())
    print(f"seed={seed} -> {args.out}\n{counts}")


if __name__ == "__main__":
    main()
