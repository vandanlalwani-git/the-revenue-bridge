"""Seeded simulation of a B2B SaaS company's Stripe account.

Writes Stripe-shaped tables (integer cents, Unix epoch seconds, USD) to <out>/raw/*.parquet,
the simulation's true month-end ARR to <out>/truth/, and the planted events to
<out>/answer_key.json.

Covers monthly and annual plans, Scale seat add-ons, trials, coupons (forever, repeating,
once), immediate upgrades with proration, downgrades and seat removals at period end,
monthly-to-annual switches, dunning with write-off on day 14, service-credit refunds,
pro-rata refunds when an annual plan ends early, reactivations, and planted events E1-E8.

Usage:  python -m generator.generate [--seed N] [--out data]
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
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import yaml

from generator.schema import TABLES, TRUTH_TABLES

ROOT = Path(__file__).resolve().parents[1]
BASE62 = string.digits + string.ascii_letters
HOUR, DAY = 3600, 86400
UPGRADE = {"starter": "growth", "growth": "scale"}
DOWNGRADE = {"growth": "starter", "scale": "growth"}

ADJECTIVES = ["Bright", "Quiet", "Swift", "North", "Clear", "Bold", "Silver", "Open", "True", "Blue",
              "Iron", "Green", "Prime", "Little", "Golden", "Rapid", "Steady", "Wild", "Lucid", "Civic"]
NOUNS = ["Harbor", "Ledger", "Field", "Signal", "Forge", "Orbit", "Canvas", "Summit", "Grove", "Beacon",
         "Anchor", "Atlas", "Meadow", "Circuit", "Bridge", "Pillar", "Comet", "Delta", "Quarry", "Vista"]
SUFFIXES = ["Labs", "Studio", "Health", "Logistics", "Analytics", "Partners", "Systems", "Works", "AI", "Co"]


# ---- time and money helpers --------------------------------------------
def utc(value) -> datetime:
    """YAML date/datetime (naive = UTC) -> aware datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    year, month = map(int, str(value).split("-"))
    return datetime(year, month, 1, tzinfo=timezone.utc)


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def from_ts(t: int) -> datetime:
    return datetime.fromtimestamp(t, tz=timezone.utc)


def add_months(anchor: datetime, n: int) -> datetime:
    """Stripe-style billing anchor: same day-of-month, clamped to the month's last day."""
    y, m = divmod(anchor.month - 1 + n, 12)
    year, month = anchor.year + y, m + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def month_window(month) -> tuple[int, int]:
    start = utc(month)
    return ts(start), ts(add_months(start, 1))


def round_half_up(numer: int, denom: int) -> int:
    """numer / denom rounded to an integer, halves away from zero (same as DuckDB round())."""
    if numer < 0:
        return -round_half_up(-numer, denom)
    return (2 * numer + denom) // (2 * denom)


def percent_of(amount: int, percent_off: float) -> int:
    return round_half_up(amount * round(percent_off * 100), 10000)


def poisson(rng: random.Random, lam: float) -> int:
    # Knuth's method; fine for the small means used here.
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


# ---- state -------------------------------------------------------------
@dataclass
class Item:
    id: str
    key: str            # catalog key: starter | growth | scale | scale_seats
    price: dict
    quantity: int
    created: int


@dataclass
class Discount:
    id: str
    coupon: dict
    start: int
    end: int | None

    def active(self, at: int) -> bool:
        return self.start <= at and (self.end is None or at < self.end)


@dataclass
class Customer:
    id: str
    ever_paid: bool = False
    live_sub: "Sub | None" = None
    last_plan: str = "starter"
    churned_at: int | None = None
    churn_reason: str | None = None


@dataclass
class Sub:
    id: str
    customer: Customer
    plan: str
    interval: str
    items: list[Item]
    created: int
    anchor: datetime | None = None
    period_index: int = 0
    period_start: int | None = None
    period_end: int | None = None
    status: str = "active"
    cancel_at_period_end: bool = False
    canceled_at: int | None = None
    ended_at: int | None = None
    cancellation_reason: str | None = None
    trial_start: int | None = None
    trial_end: int | None = None
    discount: Discount | None = None        # percent coupons (forever / repeating)
    once_discount: Discount | None = None   # amount-off coupon for the first paid invoice
    scripted: bool = False                  # planted-event customer: no random behaviour
    promo: bool = False
    card_expired: bool = False
    pending: dict = field(default_factory=dict)
    invoices: list[tuple[dict, int, int]] = field(default_factory=list)   # (invoice row, period start, end)

    @property
    def months(self) -> int:
        return 1 if self.interval == "month" else 12

    def live(self) -> bool:
        return self.ended_at is None and self.status in ("active", "past_due")


@dataclass(order=True)
class Event:
    at: int
    prio: int                               # 0 = lifecycle, 1 = month-end snapshot, 2 = planted setup
    seq: int
    kind: str = field(compare=False)
    sub: Sub | None = field(default=None, compare=False)
    data: dict = field(default_factory=dict, compare=False)


class Simulation:
    def __init__(self, cfg: dict, seed: int):
        self.cfg = cfg
        self.seed = seed
        self.rng = random.Random(seed)
        self.id_rng = random.Random(seed + 1)
        self.used_ids: set[str] = set()
        self.rows: dict[str, list[dict]] = {t: [] for t in TABLES}
        self.truth: dict[str, list[dict]] = {t: [] for t in TRUTH_TABLES}
        self.queue: list[Event] = []
        self.seq = 0
        self.start = utc(cfg["simulation"]["start"])
        end_day = cfg["simulation"]["end"]
        self.end = datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59, tzinfo=timezone.utc)
        self.prices: dict[tuple[str, str, int], dict] = {}   # (key, interval, version) -> price row
        self.coupons: dict[str, dict] = {}
        self.customers: list[Customer] = []
        self.subs: dict[str, Sub] = {}
        self.charge_of: dict[str, dict] = {}     # invoice id -> succeeded charge row
        self.ev = cfg["planted_events"]
        self.hz = cfg["hazards"]
        self.key: dict[str, dict] = {}           # planted-event details for the answer key

    # ---- helpers -------------------------------------------------------
    def new_id(self, prefix: str) -> str:
        while True:
            candidate = prefix + "_" + "".join(self.id_rng.choice(BASE62) for _ in range(14))
            if candidate not in self.used_ids:
                self.used_ids.add(candidate)
                return candidate

    def push(self, at: int, kind: str, sub: Sub | None = None, prio: int = 0, **data) -> None:
        self.seq += 1
        heapq.heappush(self.queue, Event(at, prio, self.seq, kind, sub, data))

    def rand_in(self, start: int, end: int) -> int:
        return start + self.rng.randrange(max(end - start, 1))

    # ---- catalog -------------------------------------------------------
    def create_catalog(self) -> None:
        created = ts(self.start)
        e1 = self.ev["E1_price_rise"]
        for key, plan in self.cfg["plans"].items():
            prod = self.add_product(plan["name"], created)
            self.add_price(key, 1, prod, f"{plan['name']} monthly", plan["monthly_price"], "month", created)
            self.add_price(key, 1, prod, f"{plan['name']} annual", plan["annual_price"], "year", created)
            if key == "starter":
                at = ts(utc(e1["effective"]))
                self.add_price(key, 2, prod, "Starter monthly 2025-09", e1["monthly_price"], "month", at)
                self.add_price(key, 2, prod, "Starter annual 2025-09", e1["annual_price"], "year", at)
            if "seats" in plan:
                seats = plan["seats"]
                seat_prod = self.add_product(f"{plan['name']} extra seat", created)
                self.add_price(f"{key}_seats", 1, seat_prod, f"{plan['name']} seat monthly",
                               seats["monthly_price"], "month", created)
                self.add_price(f"{key}_seats", 1, seat_prod, f"{plan['name']} seat annual",
                               seats["annual_price"], "year", created)
        coupons = [("PROMO50", "Launch promo 50% x 3 months", 50.0, None, "repeating", 3),
                   ("PARTNER20", "Partner 20% forever", 20.0, None, "forever", None),
                   ("LAUNCH50", "$50 off first invoice", None, 5000, "once", None)]
        for cid, name, pct, amount_off, duration, months in coupons:
            row = {"id": cid, "name": name, "percent_off": pct, "amount_off": amount_off,
                   "duration": duration, "duration_in_months": months, "created": created}
            self.rows["coupons"].append(row)
            self.coupons[cid] = row

    def add_product(self, name: str, created: int) -> str:
        prod = {"id": self.new_id("prod"), "name": name, "created": created, "active": True}
        self.rows["products"].append(prod)
        return prod["id"]

    def add_price(self, key, version, product, nickname, amount, interval, created) -> None:
        row = {"id": self.new_id("price"), "product": product, "nickname": nickname, "unit_amount": amount,
               "currency": "usd", "recurring_interval": interval, "recurring_interval_count": 1,
               "active": True, "created": created}
        self.rows["prices"].append(row)
        self.prices[(key, interval, version)] = row

    def price(self, key: str, interval: str, at: int) -> dict:
        version = 2 if key == "starter" and at >= ts(utc(self.ev["E1_price_rise"]["effective"])) else 1
        return self.prices[(key, interval, version)]

    def finalize_catalog(self) -> None:
        # The old Starter prices are archived once the new ones take over.
        for interval in ("month", "year"):
            self.prices[("starter", interval, 1)]["active"] = False

    # ---- scheduling ----------------------------------------------------
    def schedule(self) -> None:
        acq, e3 = self.cfg["acquisition"], self.ev["E3_promo_cohort"]
        promo_start, promo_end = month_window(e3["month"])
        month_start, m = self.start, 0
        while month_start <= self.end:
            month_end = add_months(month_start, 1)
            lam = acq["signups_first_month"] * (1 + acq["monthly_growth"]) ** m
            if ts(month_start) == promo_start:
                lam *= e3["signup_multiplier"]
            for _ in range(poisson(self.rng, lam)):
                self.push(self.rand_in(ts(month_start), ts(month_end)), "signup")
            self.push(ts(month_end) - 1, "month_end", prio=1)
            month_start, m = month_end, m + 1

        for name in ("E2_largest_churn", "E6_large_refund"):
            self.push(ts(utc(self.ev[name]["signup"])), "signup", scripted=name)
        self.push(ts(utc(self.ev["E4_card_expiry"]["at"])), "card_expiry", prio=2)
        self.push(ts(utc(self.ev["E7_winback"]["month"])), "winback", prio=2)

    # ---- acquisition ---------------------------------------------------
    def on_signup(self, at: int, scripted: str | None = None) -> None:
        name = f"{self.rng.choice(ADJECTIVES)} {self.rng.choice(NOUNS)} {self.rng.choice(SUFFIXES)}"
        slug = name.lower().replace(" ", "")
        row = {"id": self.new_id("cus"), "created": at, "name": name,
               "email": f"billing@{slug}{len(self.rows['customers'])}.example", "currency": "usd"}
        self.rows["customers"].append(row)
        cus = Customer(row["id"])
        self.customers.append(cus)

        if scripted:
            ev = self.ev[scripted]
            sub = self.new_subscription(cus, "scale", "month", at, extra_seats=ev["extra_seats"], scripted=True)
            self.key[scripted] = {"customer": cus.id, "subscription": sub.id}
            if scripted == "E2_largest_churn":
                self.push(ts(utc(ev["cancel_request"])), "cancel_request", sub)
            else:
                self.push(ts(utc(ev["refund_at"])), "large_refund", sub)
            return

        plans = self.cfg["plans"]
        plan = self.rng.choices(list(plans), weights=[p["mix"] for p in plans.values()])[0]
        extra = 0
        if "seats" in plans[plan]:
            p = 1 / (1 + plans[plan]["seats"]["extra_seats_mean"])
            extra = int(math.log(1 - self.rng.random()) / math.log(1 - p))

        e3 = self.ev["E3_promo_cohort"]
        promo_start, promo_end = month_window(e3["month"])
        if promo_start <= at < promo_end and self.rng.random() < e3["promo_share"]:
            self.new_subscription(cus, plan, "month", at, extra_seats=extra, coupon="PROMO50", promo=True)
            return

        interval = "year" if self.rng.random() < self.cfg["annual_share"] else "month"
        trial = self.rng.random() < self.cfg["trial"]["share"]
        coupon = None
        if self.rng.random() < self.cfg["coupons"]["partner_share"]:
            coupon = "PARTNER20"
        elif plan != "starter" and self.rng.random() < self.cfg["coupons"]["launch_share"]:
            coupon = "LAUNCH50"
        self.new_subscription(cus, plan, interval, at, extra_seats=extra, trial=trial, coupon=coupon)

    def new_subscription(self, cus: Customer, plan: str, interval: str, at: int, extra_seats: int = 0,
                         trial: bool = False, coupon: str | None = None, promo: bool = False,
                         scripted: bool = False) -> Sub:
        items = [Item(self.new_id("si"), plan, self.price(plan, interval, at), 1, at)]
        if plan == "scale" and extra_seats > 0:
            items.append(Item(self.new_id("si"), "scale_seats", self.price("scale_seats", interval, at),
                              extra_seats, at))
        sub = Sub(self.new_id("sub"), cus, plan, interval, items, created=at, promo=promo, scripted=scripted)
        self.subs[sub.id] = sub
        cus.live_sub = sub
        cus.last_plan = plan

        if coupon:
            c = self.coupons[coupon]
            end = ts(add_months(from_ts(at), c["duration_in_months"])) if c["duration"] == "repeating" else None
            disc = Discount(self.new_id("di"), c, at, end)
            self.rows["discounts"].append({"id": disc.id, "coupon": c["id"], "customer": cus.id,
                                           "subscription": sub.id, "start": at, "end": end})
            if c["duration"] == "once":
                sub.once_discount = disc
            else:
                sub.discount = disc

        if trial:
            sub.status = "trialing"
            sub.trial_start, sub.trial_end = at, at + self.cfg["trial"]["days"] * DAY
            sub.anchor = from_ts(sub.trial_end)
            lines = [self.line(item, item.price, item.quantity, 0, at, sub.trial_end) for item in items]
            self.invoice(sub, "subscription_create", at, lines)
            self.push(sub.trial_end, "trial_end", sub)
        else:
            sub.anchor = from_ts(at)
            self.start_period(sub, "subscription_create")
        return sub

    # ---- billing -------------------------------------------------------
    def line(self, item: Item, price: dict, quantity: int, amount: int, start: int, end: int,
             proration: bool = False) -> dict:
        return {"item": item, "price": price, "quantity": quantity, "amount": amount,
                "start": start, "end": end, "proration": proration}

    def start_period(self, sub: Sub, billing_reason: str) -> None:
        sub.period_start = ts(add_months(sub.anchor, sub.period_index * sub.months))
        sub.period_end = ts(add_months(sub.anchor, (sub.period_index + 1) * sub.months))
        sub.customer.ever_paid = True
        lines = [self.line(item, item.price, item.quantity, item.price["unit_amount"] * item.quantity,
                           sub.period_start, sub.period_end) for item in sub.items]
        inv = self.invoice(sub, billing_reason, sub.period_start, lines)
        sub.invoices.append((inv, sub.period_start, sub.period_end))
        self.push(sub.period_end, "renewal", sub)
        if billing_reason == "subscription_create":
            self.collect(sub, inv, sub.period_start)
        elif inv["amount_due"] > 0:
            # Stripe attempts a renewal charge about an hour after finalising the invoice.
            self.push(sub.period_start + HOUR, "charge_attempt", sub, invoice=inv, attempt=0)
        if not sub.scripted:
            self.plan_period(sub)

    def invoice(self, sub: Sub, billing_reason: str, at: int, lines: list[dict]) -> dict:
        inv_id = self.new_id("in")
        subtotal = discount_total = 0
        once = sub.once_discount if billing_reason in ("subscription_create", "subscription_cycle") else None
        for ln in lines:
            discount_amount, discount_id = 0, None
            if sub.discount and sub.discount.active(at) and ln["amount"] != 0:
                discount_amount = percent_of(ln["amount"], sub.discount.coupon["percent_off"])
                discount_id = sub.discount.id
            if once and ln["amount"] > 0 and not ln["proration"]:
                discount_amount = min(once.coupon["amount_off"], ln["amount"] - discount_amount) + discount_amount
                discount_id = once.id
                sub.once_discount, once = None, None
            subtotal += ln["amount"]
            discount_total += discount_amount
            self.rows["invoice_line_items"].append({
                "id": self.new_id("il"), "invoice": inv_id, "subscription": sub.id,
                "subscription_item": ln["item"].id, "price": ln["price"]["id"], "type": "subscription",
                "proration": ln["proration"], "quantity": ln["quantity"], "amount": ln["amount"],
                "discount_amount": discount_amount, "discount": discount_id,
                "period_start": ln["start"], "period_end": ln["end"], "currency": "usd",
            })
        total = subtotal - discount_total
        row = {"id": inv_id, "customer": sub.customer.id, "subscription": sub.id,
               "billing_reason": billing_reason, "status": "open" if total > 0 else "paid",
               "created": at, "finalized_at": at, "paid_at": None if total > 0 else at,
               "marked_uncollectible_at": None, "attempt_count": 0, "subtotal": subtotal,
               "total_discount_amount": discount_total, "total": total, "amount_due": total,
               "amount_paid": 0, "amount_remaining": total, "currency": "usd"}
        self.rows["invoices"].append(row)
        return row

    def charge(self, inv: dict, at: int, ok: bool, failure_code: str | None = None) -> dict:
        row = {"id": self.new_id("ch"), "customer": inv["customer"], "invoice": inv["id"],
               "amount": inv["amount_due"], "created": at, "status": "succeeded" if ok else "failed",
               "failure_code": failure_code, "currency": "usd"}
        self.rows["charges"].append(row)
        inv["attempt_count"] += 1
        return row

    def collect(self, sub: Sub, inv: dict, at: int) -> None:
        """Immediate successful charge: new subscriptions and proration invoices."""
        if inv["amount_due"] <= 0:
            return
        self.mark_paid(inv, self.charge(inv, at, True), at)

    def mark_paid(self, inv: dict, charge: dict, at: int) -> None:
        inv.update(status="paid", paid_at=at, amount_paid=inv["amount_due"], amount_remaining=0)
        self.charge_of[inv["id"]] = charge

    def on_charge_attempt(self, sub: Sub, at: int, invoice: dict, attempt: int) -> None:
        if invoice["status"] != "open" or sub.ended_at is not None:
            return
        dunning = self.cfg["dunning"]
        if sub.scripted:   # planted-event customers pay on time
            ok, code = True, None
        elif sub.card_expired:
            ok = self.rng.random() < self.ev["E4_card_expiry"]["card_update_per_attempt"]
            code = "expired_card"
            if ok:
                sub.card_expired = False
        elif attempt == 0:
            ok = self.rng.random() >= dunning["base_failure_rate"]
            code = self.rng.choice(["card_declined", "insufficient_funds"])
        else:
            ok = self.rng.random() < dunning["recovery_per_retry"]
            code = "card_declined"
        charge = self.charge(invoice, at, ok, None if ok else code)
        if ok:
            self.mark_paid(invoice, charge, at)
            if sub.status == "past_due":
                sub.status = "active"
            return
        retries = dunning["retry_days"]
        if attempt == 0:
            sub.status = "past_due"
            for n, days in enumerate(retries, start=1):
                self.push(at + days * DAY, "charge_attempt", sub, invoice=invoice, attempt=n)
        elif attempt == len(retries):
            invoice.update(status="uncollectible", marked_uncollectible_at=at)
            self.end_sub(sub, at, "payment_failed")

    # ---- per-period behaviour ------------------------------------------
    def plan_period(self, sub: Sub) -> None:
        """Random decisions for the paid period that just started."""
        ps, pe, hz = sub.period_start, sub.period_end, self.hz
        churn = self.cfg["plans"][sub.plan]["monthly_churn"]
        if sub.interval == "year":
            if self.rng.random() < 1 - (1 - churn) ** 12:
                self.push(self.rand_in(ps, pe), "cancel_request", sub)
            elif self.rng.random() < hz["annual_early_termination"]:
                self.push(self.rand_in(ps + 30 * DAY, pe - 30 * DAY), "early_termination", sub)
            return

        e1 = self.ev["E1_price_rise"]
        lo, hi = (ts(utc(d)) for d in e1["churn_window"])
        if sub.plan == "starter" and lo <= ps < hi:
            churn += e1["churn_boost"]
        if sub.promo and sub.period_index == 2:
            churn = self.ev["E3_promo_cohort"]["cliff_churn"]
        if self.rng.random() < churn:
            self.push(self.rand_in(ps, pe), "cancel_request", sub)
        if sub.plan in UPGRADE and self.rng.random() < hz["upgrade"]:
            self.push(self.rand_in(ps, pe), "upgrade", sub)
        if sub.plan in DOWNGRADE and self.rng.random() < hz["downgrade"]:
            sub.pending["downgrade"] = True
        if sub.plan == "scale":
            if self.rng.random() < hz["seat_add"]:
                self.push(self.rand_in(ps, pe), "seat_add", sub)
            if self.seat_item(sub) and self.rng.random() < hz["seat_remove"]:
                sub.pending["seat_remove"] = self.rng.randint(1, 3)
        if self.rng.random() < hz["service_credit"]:
            inv = sub.invoices[-1][0]
            self.push(self.rand_in(pe, pe + 3 * DAY), "service_credit", sub, invoice=inv)

    def seat_item(self, sub: Sub) -> Item | None:
        return next((i for i in sub.items if i.key == "scale_seats"), None)

    def on_cancel_request(self, sub: Sub, at: int) -> None:
        if sub.ended_at is None and not sub.cancel_at_period_end:
            sub.cancel_at_period_end = True
            sub.canceled_at = at
            sub.cancellation_reason = "cancellation_requested"

    def on_renewal(self, sub: Sub, at: int) -> None:
        if sub.ended_at is not None:
            return
        if sub.cancel_at_period_end:
            self.end_sub(sub, at, "cancellation_requested")
            return
        if sub.pending.pop("downgrade", False):
            sub.plan = DOWNGRADE[sub.plan]
            sub.items[0].key = sub.plan
            sub.items[0].price = self.price(sub.plan, sub.interval, at)
            sub.items = [i for i in sub.items if i.key != "scale_seats" or sub.plan == "scale"]
            sub.pending.pop("seat_remove", None)
        seats = self.seat_item(sub)
        remove = sub.pending.pop("seat_remove", 0)
        if seats and remove:
            seats.quantity -= remove
            if seats.quantity <= 0:
                sub.items.remove(seats)
        for item in sub.items:
            if item.key == "starter":   # E1: Starter moves to the new price at its next renewal
                item.price = self.price("starter", sub.interval, at)

        if sub.interval == "month" and self.switches_to_annual(sub, at):
            sub.interval = "year"
            for item in sub.items:
                item.price = self.price(item.key, "year", at)
            sub.anchor, sub.period_index = from_ts(at), 0
        else:
            sub.period_index += 1
        self.start_period(sub, "subscription_cycle")

    def switches_to_annual(self, sub: Sub, at: int) -> bool:
        if sub.scripted or sub.status != "active" or (sub.discount and sub.discount.active(at)):
            return False
        lo, hi = month_window(self.ev["E5_annual_push"]["month"])
        rate = self.ev["E5_annual_push"]["switch_share"] if lo <= at < hi else self.hz["switch_to_annual"]
        if self.rng.random() < rate:
            if lo <= at < hi:
                self.key.setdefault("E5_annual_push", {"subscriptions": []})["subscriptions"].append(sub.id)
            return True
        return False

    def prorated_change(self, sub: Sub, at: int, item: Item, new_price: dict, new_qty: int) -> dict:
        """Proration lines for an immediate change: credit the unused old, charge the remaining new."""
        remaining, span = sub.period_end - at, sub.period_end - sub.period_start
        lines = []
        if item.quantity > 0:
            old = item.price["unit_amount"] * item.quantity
            lines.append(self.line(item, item.price, item.quantity, -round_half_up(old * remaining, span),
                                   at, sub.period_end, proration=True))
        new = new_price["unit_amount"] * new_qty
        lines.append(self.line(item, new_price, new_qty, round_half_up(new * remaining, span),
                               at, sub.period_end, proration=True))
        return lines

    def can_change(self, sub: Sub) -> bool:
        return sub.ended_at is None and sub.status == "active" and not sub.cancel_at_period_end

    def on_upgrade(self, sub: Sub, at: int) -> None:
        if not self.can_change(sub) or sub.interval != "month" or sub.plan not in UPGRADE:
            return
        base = sub.items[0]
        new_plan = UPGRADE[sub.plan]
        new_price = self.price(new_plan, "month", at)
        lines = self.prorated_change(sub, at, base, new_price, base.quantity)
        base.key, base.price, sub.plan = new_plan, new_price, new_plan
        sub.customer.last_plan = new_plan
        sub.pending.pop("downgrade", None)
        self.collect(sub, self.invoice(sub, "subscription_update", at, lines), at)

    def on_seat_add(self, sub: Sub, at: int) -> None:
        if not self.can_change(sub) or sub.interval != "month" or sub.plan != "scale":
            return
        seats = self.seat_item(sub)
        if seats is None:
            seats = Item(self.new_id("si"), "scale_seats", self.price("scale_seats", "month", at), 0, at)
            sub.items.append(seats)
        new_qty = seats.quantity + self.rng.randint(1, 5)
        lines = self.prorated_change(sub, at, seats, seats.price, new_qty)
        seats.quantity = new_qty
        self.collect(sub, self.invoice(sub, "subscription_update", at, lines), at)

    def on_early_termination(self, sub: Sub, at: int) -> None:
        """Annual plan ended early; the unused term is refunded pro rata."""
        if not self.can_change(sub) or sub.interval != "year":
            return
        inv, ps, pe = sub.invoices[-1]
        if inv["status"] != "paid" or inv["amount_paid"] <= 0:
            return
        refund = 0
        for ln in self.lines_of(inv["id"]):
            net = ln["amount"] - ln["discount_amount"]
            refund += net - round_half_up(net * (at - ps), pe - ps)   # unearned under cumulative rounding
        self.end_sub(sub, at, "cancellation_requested")
        if refund > 0:
            self.refund(inv, refund, at)

    def lines_of(self, invoice_id: str) -> list[dict]:
        return [ln for ln in self.rows["invoice_line_items"] if ln["invoice"] == invoice_id]

    def refund(self, inv: dict, amount: int, at: int) -> dict:
        row = {"id": self.new_id("re"), "charge": self.charge_of[inv["id"]]["id"], "amount": amount,
               "created": at, "reason": "requested_by_customer", "status": "succeeded", "currency": "usd"}
        self.rows["refunds"].append(row)
        return row

    def on_service_credit(self, sub: Sub, at: int, invoice: dict) -> None:
        if invoice["status"] == "paid" and invoice["amount_paid"] > 0:
            self.refund(invoice, invoice["amount_paid"] // 2, at)

    def on_trial_end(self, sub: Sub, at: int) -> None:
        if sub.ended_at is not None:
            return
        if self.rng.random() < self.cfg["trial"]["conversion"]:
            sub.status = "active"
            self.start_period(sub, "subscription_cycle")
        else:
            self.end_sub(sub, at, "cancellation_requested")

    def end_sub(self, sub: Sub, at: int, reason: str) -> None:
        sub.status, sub.ended_at = "canceled", at
        sub.canceled_at = sub.canceled_at or at
        sub.cancellation_reason = reason
        cus = sub.customer
        cus.live_sub = None
        if not cus.ever_paid:
            return
        cus.churned_at, cus.churn_reason = at, reason
        if reason == "cancellation_requested" and not sub.scripted and self.rng.random() < self.hz["reactivation"]:
            self.push(at + self.rng.randint(30, 300) * DAY, "reactivate", customer=cus)

    def on_reactivate(self, at: int, customer: Customer) -> None:
        if customer.live_sub is None:
            self.new_subscription(customer, customer.last_plan, "month", at)

    # ---- planted events ------------------------------------------------
    def on_large_refund(self, sub: Sub, at: int) -> None:
        inv = next(inv for inv, _, pe in reversed(sub.invoices) if pe <= at and inv["status"] == "paid")
        refund = self.refund(inv, inv["amount_paid"], at)
        self.key["E6_large_refund"].update(refund=refund["id"], amount=refund["amount"], invoice=inv["id"])

    def on_card_expiry(self, at: int) -> None:
        eligible = [s for s in self.subs.values() if s.live() and s.status == "active" and s.interval == "month"
                    and not s.cancel_at_period_end and not s.scripted and not s.promo]
        hit = self.rng.sample(eligible, round(len(eligible) * self.ev["E4_card_expiry"]["share"]))
        for sub in hit:
            sub.card_expired = True
        self.key["E4_card_expiry"] = {"subscriptions": sorted(s.id for s in hit)}

    def on_winback(self, at: int) -> None:
        e7 = self.ev["E7_winback"]
        lo, hi = (ts(utc(d)) for d in e7["pool_churned_between"])
        pool = [c for c in self.customers if c.live_sub is None and c.churn_reason == "cancellation_requested"
                and c.churned_at is not None and lo <= c.churned_at < hi]
        back = self.rng.sample(pool, round(len(pool) * e7["share"]))
        start, end = month_window(e7["month"])
        for cus in back:
            self.push(self.rand_in(start, end), "reactivate", customer=cus)
        self.key["E7_winback"] = {"customers": sorted(c.id for c in back), "pool_size": len(pool)}

    # ---- truth ---------------------------------------------------------
    def on_month_end(self, at: int) -> None:
        month = from_ts(at).strftime("%Y-%m")
        for sub in self.subs.values():
            if not sub.live():
                continue
            arr = 0
            for item in sub.items:
                amount = item.price["unit_amount"] * item.quantity
                if sub.discount and sub.discount.active(at):
                    amount -= percent_of(amount, sub.discount.coupon["percent_off"])
                arr += amount * (12 if sub.interval == "month" else 1)
            self.truth["mrr_month_end"].append({
                "month": month, "month_end": at, "customer": sub.customer.id, "subscription": sub.id,
                "arr_cents": arr, "past_due": sub.status == "past_due"})

    # ---- run -----------------------------------------------------------
    def run(self) -> None:
        self.create_catalog()
        self.schedule()
        end = ts(self.end)
        handlers = {
            "renewal": self.on_renewal, "cancel_request": self.on_cancel_request, "upgrade": self.on_upgrade,
            "seat_add": self.on_seat_add, "early_termination": self.on_early_termination,
            "trial_end": self.on_trial_end, "large_refund": self.on_large_refund,
        }
        while self.queue and self.queue[0].at <= end:
            ev = heapq.heappop(self.queue)
            if ev.kind == "signup":
                self.on_signup(ev.at, ev.data.get("scripted"))
            elif ev.kind == "charge_attempt":
                self.on_charge_attempt(ev.sub, ev.at, ev.data["invoice"], ev.data["attempt"])
            elif ev.kind == "service_credit":
                self.on_service_credit(ev.sub, ev.at, ev.data["invoice"])
            elif ev.kind == "reactivate":
                self.on_reactivate(ev.at, ev.data["customer"])
            elif ev.kind == "month_end":
                self.on_month_end(ev.at)
            elif ev.kind == "card_expiry":
                self.on_card_expiry(ev.at)
            elif ev.kind == "winback":
                self.on_winback(ev.at)
            else:
                handlers[ev.kind](ev.sub, ev.at)
        self.finalize_catalog()
        self.snapshot()

    def snapshot(self) -> None:
        """Write subscription state as of the simulation end, like an API export."""
        for sub in sorted(self.subs.values(), key=lambda s: s.created):
            self.rows["subscriptions"].append({
                "id": sub.id, "customer": sub.customer.id, "status": sub.status, "created": sub.created,
                "start_date": sub.created, "billing_cycle_anchor": ts(sub.anchor),
                "current_period_start": sub.period_start if sub.period_start is not None else sub.trial_start,
                "current_period_end": sub.period_end if sub.period_end is not None else sub.trial_end,
                "cancel_at_period_end": sub.cancel_at_period_end, "canceled_at": sub.canceled_at,
                "ended_at": sub.ended_at, "cancellation_reason": sub.cancellation_reason,
                "trial_start": sub.trial_start, "trial_end": sub.trial_end,
                "collection_method": "charge_automatically",
            })
            for item in sub.items:
                self.rows["subscription_items"].append({
                    "id": item.id, "subscription": sub.id, "price": item.price["id"],
                    "quantity": item.quantity, "created": item.created,
                })

    def answer_key(self) -> dict:
        ev = self.ev
        return {
            "seed": self.seed,
            "note": "Planted events and the signals they should produce. Read only by notes/evaluate.py.",
            "events": [
                {"id": "E1", "title": "Starter price rise $29 -> $35 at each renewal from 2025-09-01",
                 "details": {"effective": str(ev["E1_price_rise"]["effective"])},
                 "signals": [
                     {"month": "2025-09", "signal": "expansion, driver price_change, concentrated in Starter"},
                     {"month": "2025-09", "signal": "Starter voluntary churn above trailing 3-month average"},
                     {"month": "2025-10", "signal": "Starter voluntary churn above trailing 3-month average"}]},
                {"id": "E2", "title": "Largest Scale customer churns", "details": self.key["E2_largest_churn"],
                 "signals": [
                     {"month": "2025-11", "signal": "one logo is more than 50% of churned MRR"},
                     {"month": "2025-11", "signal": "gross MRR churn % far above logo churn %"}]},
                {"id": "E3", "title": "Promo cohort: 50% off for 3 months, no trial",
                 "details": {"cohort": ev["E3_promo_cohort"]["month"]},
                 "signals": [
                     {"month": "2026-04", "signal": "expansion, driver discount_ended, from the 2026-01 cohort"},
                     {"month": "2026-04", "signal": "voluntary churn concentrated in the 2026-01 cohort"},
                     {"month": "2026-04", "signal": "logo retention cliff at M3 for the 2026-01 cohort"}]},
                {"id": "E4", "title": "Card-expiry batch; February renewals fail",
                 "details": {"affected_subscriptions": len(self.key["E4_card_expiry"]["subscriptions"]),
                             **self.key["E4_card_expiry"]},
                 # Counts are the primary evidence. At-risk MRR only supports them: an organic
                 # cluster of Scale customers in dunning (2026-04) moves it as much as E4 does.
                 "signals": [
                     {"month": "2026-02", "role": "primary", "signal": "customers entering dunning spike"},
                     {"month": "2026-02", "role": "primary", "signal": "involuntary churn count up (renewal days 1-14)"},
                     {"month": "2026-02", "role": "primary", "signal": "customers in dunning at month-end elevated (renewal days 15-28)"},
                     {"month": "2026-03", "role": "primary", "signal": "involuntary churn count up again (spill-over)"},
                     {"month": "2026-02", "role": "supporting", "signal": "at-risk MRR at month-end elevated"},
                     {"month": "2026-02", "role": "supporting", "signal": "bad-debt expense up"},
                     {"month": "2026-03", "role": "supporting", "signal": "at-risk MRR at month-end back to baseline"}]},
                {"id": "E5", "title": "Annual-prepay push at March renewals",
                 "details": {"switched": len(self.key.get("E5_annual_push", {}).get("subscriptions", [])),
                             **self.key.get("E5_annual_push", {})},
                 "signals": [
                     {"month": "2026-03", "signal": "billings and cash far above revenue"},
                     {"month": "2026-03", "signal": "deferred revenue jumps"},
                     {"month": "2026-03", "signal": "contraction, driver interval_switch"},
                     {"month": "2026-03", "signal": "logo churn unaffected"}]},
                {"id": "E6", "title": "Large refund of delivered service; customer stays",
                 "details": self.key["E6_large_refund"],
                 "signals": [
                     {"month": "2026-04", "signal": "delivered-portion refunds reduce net revenue and net cash; "
                                                   "measured against the month's gross figures, not as an absolute fall"},
                     {"month": "2026-04", "signal": "MRR unaffected"}]},
                {"id": "E7", "title": "Win-back campaign", "details": self.key["E7_winback"],
                 "signals": [{"month": "2026-05", "signal": "reactivation MRR well above trailing 3-month average"}]},
                {"id": "E8", "title": "Control month, nothing planted", "details": {},
                 "signals": [{"month": "2026-06", "signal": "none; no unsupported anomaly claims"}]},
            ],
        }


def write_parquet(rows: dict[str, list[dict]], schema: dict, out: Path) -> None:
    # Bulk-load through newline-delimited JSON with an explicit schema; row inserts are slow.
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        for table, columns in schema.items():
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
    parser.add_argument("--out", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "scenario.yaml").read_text())
    seed = args.seed if args.seed is not None else cfg["seed"]
    sim = Simulation(cfg, seed)
    sim.run()
    write_parquet(sim.rows, TABLES, args.out / "raw")
    write_parquet(sim.truth, TRUTH_TABLES, args.out / "truth")
    (args.out / "answer_key.json").write_text(json.dumps(sim.answer_key(), indent=2) + "\n")
    counts = ", ".join(f"{t}={len(r)}" for t, r in sim.rows.items())
    print(f"seed={seed} -> {args.out}\n{counts}")


if __name__ == "__main__":
    main()
