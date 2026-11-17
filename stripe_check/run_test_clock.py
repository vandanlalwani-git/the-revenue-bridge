"""Stripe test-clock check: does the same SQL work on data that Stripe itself produced?

Creates 6 customers on 2 Stripe test clocks in a TEST-MODE account, plays 4 months of billing
(Jan-Apr 2026) by advancing the clocks, then exports Stripe's objects into the project's raw tables
(stripe_check/data/raw/*.parquet). tests/test_stripe_check.py runs sql/*.sql on that export.

The key is read from STRIPE_SECRET_KEY, or from the file named in STRIPE_KEY_FILE. It is never
printed, logged or written to the project, and a live key (sk_live_) is refused.

Usage:  STRIPE_KEY_FILE=/path/outside/repo python stripe_check/run_test_clock.py
        ... run_test_clock.py --export     re-export the last run (stripe_check/data/run.json), no clocks

Stripe-Version is pinned to 2024-06-20, the last API version where invoices, lines and charges still
carry subscription, price and invoice fields directly (generator/schema.py follows that shape).
Only the columns in generator/schema.py are exported; raw API responses are not saved.

Test-clock timestamps. On a test clock Stripe moves billing objects (customers, subscriptions,
invoices, discounts) to simulated time, but stamps charges and refunds with real wall-clock time.
The run therefore logs, for every clock advance and every action, the real-time window and the clock
time it covered, and it stops at every month-end so no window spans two months. The export puts each
payment back on the simulated timeline:
  - a successful charge at its invoice's paid_at (Stripe marks the invoice paid when the charge succeeds);
  - a refund at the clock time of the action that issued it;
  - a failed charge at its place in the advance window that produced it, kept between the invoice's
    finalisation and its resolution. Each one is checked to fall in a single calendar month. The first
    attempt on an invoice is at its finalisation (Stripe charges then), and when Stripe cancels the
    subscription for non-payment, the last failure is at the cancellation (that failure triggers it).
So every month-level figure is exact; the time within a month of a failed retry is approximate.
A live Stripe account needs none of this.
"""
from __future__ import annotations

import base64
import calendar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generator.generate import write_parquet  # noqa: E402
from generator.schema import TABLES  # noqa: E402

API = "https://api.stripe.com/v1"
VERSION = "2024-06-20"
OUT = ROOT / "stripe_check" / "data"


def month_key(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")


def ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------- API ----
def _key() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key and os.environ.get("STRIPE_KEY_FILE"):
        key = Path(os.environ["STRIPE_KEY_FILE"]).read_text().strip()
    if not key:
        sys.exit("Set STRIPE_SECRET_KEY or STRIPE_KEY_FILE.")
    if not key.startswith("sk_test_"):
        sys.exit("Refusing to run: this check only uses a test-mode key (sk_test_...).")
    return key


KEY = None


def _flatten(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    out = []
    for k, v in params.items():
        name = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _flatten(v, name)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                out += _flatten(x, f"{name}[{i}]") if isinstance(x, dict) else [(f"{name}[{i}]", str(x))]
        elif v is not None:
            out.append((name, str(v).lower() if isinstance(v, bool) else str(v)))
    return out


def call(method: str, path: str, params: dict | None = None) -> dict:
    global KEY
    KEY = KEY or _key()
    body = urllib.parse.urlencode(_flatten(params or {}))
    url = API + path + (("?" + body) if method == "GET" and body else "")
    req = urllib.request.Request(url, method=method, data=body.encode() if method != "GET" else None, headers={
        "Authorization": "Basic " + base64.b64encode(f"{KEY}:".encode()).decode(),
        "Stripe-Version": VERSION,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                obj = json.load(r)
            if obj.get("livemode"):
                sys.exit("Stripe returned a live-mode object; stopping.")
            return obj
        except urllib.error.HTTPError as e:
            err = json.loads(e.read() or b"{}").get("error", {})
            if e.code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Stripe {method} {path}: {e.code} {err.get('type')} {err.get('message')}") from None
    raise RuntimeError("unreachable")


def list_all(path: str, params: dict | None = None) -> list[dict]:
    rows, after = [], None
    while True:
        page = call("GET", path, {**(params or {}), "limit": 100, "starting_after": after})
        rows += page["data"]
        if not page["has_more"]:
            return rows
        after = page["data"][-1]["id"]


# ---------------------------------------------------------- scenario ----
START = ts("2026-01-01 00:00")
END = ts("2026-04-30 12:00")

CUSTOMERS = {   # key: (clock, name). Names and .example emails are made up.
    "northwind": ("A", "Northwind Labs"),
    "bluefin":   ("A", "Bluefin Analytics"),
    "kestrel":   ("A", "Kestrel Studio"),
    "orchid":    ("B", "Orchid Health"),
    "pinecrest": ("B", "Pinecrest Co"),
    "quill":     ("B", "Quill & Co"),
}

# (when, what, customer, detail) in clock time. Each line is one thing a real business would do.
STEPS = [
    ("2026-01-01 00:00", "subscribe", "northwind", {"price": "starter_m"}),
    ("2026-01-01 00:00", "subscribe", "bluefin",   {"price": "growth_y"}),
    ("2026-01-01 00:00", "subscribe", "kestrel",   {"price": "starter_m", "trial_days": 14}),
    ("2026-01-01 00:00", "subscribe", "orchid",    {"price": "scale_m", "seats": 3}),
    ("2026-01-01 00:00", "subscribe", "pinecrest", {"price": "growth_m"}),
    ("2026-01-10 09:00", "subscribe", "quill",     {"price": "starter_m", "coupon": True}),
    ("2026-01-20 12:00", "card_fails", "pinecrest", {}),      # renewal on 1 Feb will fail; left to Stripe
    ("2026-02-05 12:00", "card_fails", "quill",     {}),      # renewal on 10 Feb will fail
    ("2026-02-12 15:00", "upgrade",   "northwind", {"price": "growth_m"}),
    ("2026-02-13 10:00", "card_fixed", "quill",    {}),       # new card, open invoice paid: recovered
    ("2026-02-20 12:00", "refund",    "bluefin",   {"amount": 15000}),   # $150 service credit
    ("2026-03-05 11:00", "seats",     "orchid",    {"quantity": 5}),
    ("2026-03-10 12:00", "cancel_at_period_end", "kestrel", {}),
    ("2026-03-20 12:00", "write_off_if_open", "pinecrest", {}),  # finance writes off what Stripe left open
]

CATALOG = {   # key: (product, nickname, cents, interval)
    "starter_m": ("Starter", "Starter monthly", 2900, "month"),
    "growth_m":  ("Growth", "Growth monthly", 9900, "month"),
    "growth_y":  ("Growth", "Growth annual", 99000, "year"),
    "scale_m":   ("Scale", "Scale monthly", 39900, "month"),
    "seat_m":    ("Scale extra seat", "Scale seat monthly", 3000, "month"),
}


class Run:
    def __init__(self):
        self.tag = "revenue-bridge-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.meta = {"run": self.tag}
        self.clocks: dict[str, str] = {}
        self.cus: dict[str, str] = {}
        self.sub: dict[str, dict] = {}
        self.prices: dict[str, str] = {}
        self.coupon = None
        self.clock_time = START
        self.timeline: list[dict] = []   # real-time window -> clock time it covered

    def log(self, msg: str) -> None:
        print(msg, flush=True)

    def setup(self) -> None:
        products = {}
        for key, (product, nickname, cents, interval) in CATALOG.items():
            if product not in products:
                products[product] = call("POST", "/products", {"name": product, "metadata": self.meta})["id"]
            self.prices[key] = call("POST", "/prices", {
                "product": products[product], "nickname": nickname, "unit_amount": cents, "currency": "usd",
                "recurring": {"interval": interval}, "metadata": self.meta})["id"]
        self.coupon = call("POST", "/coupons", {"name": "Launch 20%", "percent_off": 20, "duration": "forever",
                                                "metadata": self.meta})["id"]
        for c in ("A", "B"):
            self.clocks[c] = call("POST", "/test_helpers/test_clocks",
                                  {"frozen_time": START, "name": f"{self.tag}-{c}"})["id"]
        self.log(f"catalog, coupon and clocks created ({self.tag})")

    def advance(self, t: int) -> None:
        """Advance both clocks to t, stopping at the last second of every month on the way."""
        d = datetime.fromtimestamp(self.clock_time, timezone.utc)
        y, m = d.year, d.month
        while True:
            month_end = ts(f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]} 23:59") + 59
            if month_end >= t:
                break
            if month_end > self.clock_time:
                self._advance(month_end)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        self._advance(t)

    def _advance(self, t: int) -> None:
        if t <= self.clock_time:
            return
        r0 = int(time.time())
        for clock in self.clocks.values():
            call("POST", f"/test_helpers/test_clocks/{clock}/advance", {"frozen_time": t})
        for clock in self.clocks.values():
            while (c := call("GET", f"/test_helpers/test_clocks/{clock}"))["status"] != "ready":
                if c["status"] == "internal_failure":
                    raise RuntimeError(f"test clock {clock} failed to advance")
                time.sleep(2)
        self.timeline.append({"kind": "advance", "real_from": r0, "real_to": int(time.time()) + 1,
                              "clock_from": self.clock_time, "clock_to": t})
        self.clock_time = t

    def to_clock(self, real: int, lo: int, hi: int, prefer: str) -> int:
        """Clock time for an object Stripe stamped with real time; lo/hi bound it in clock time."""
        found = []
        for w in sorted(self.timeline, key=lambda w: w["kind"] != prefer):
            if not (w["real_from"] <= real <= w["real_to"]):
                continue
            a, b = max(lo, w["clock_from"] + (w["kind"] == "advance")), min(hi, w["clock_to"])
            if a > b:
                continue
            if month_key(a) != month_key(b):
                raise RuntimeError("a payment could fall in either of two months; add a checkpoint")
            span = max(w["real_to"] - w["real_from"], 1)
            guess = w["clock_from"] + (w["clock_to"] - w["clock_from"]) * (real - w["real_from"]) // span
            found.append(min(max(guess, a), b))
        if not found or len({month_key(x) for x in found}) > 1:
            raise RuntimeError(f"cannot place real time {real} on the clock")
        return found[0]

    def card(self, who: str, token: str) -> None:
        pm = call("POST", f"/payment_methods/{token}/attach", {"customer": self.cus[who]})["id"]
        call("POST", f"/customers/{self.cus[who]}", {"invoice_settings": {"default_payment_method": pm}})

    def open_invoices(self, who: str) -> list[dict]:
        return [i for i in list_all("/invoices", {"customer": self.cus[who]}) if i["status"] == "open"]

    def do(self, what: str, who: str, d: dict) -> None:
        if what == "subscribe":
            clock, name = CUSTOMERS[who]
            slug = name.split()[0].lower()
            self.cus[who] = call("POST", "/customers", {
                "name": name, "email": f"billing@{slug}.example", "test_clock": self.clocks[clock],
                "metadata": self.meta})["id"]
            self.card(who, "pm_card_visa")
            items = [{"price": self.prices[d["price"]], "quantity": 1}]
            if d.get("seats"):
                items.append({"price": self.prices["seat_m"], "quantity": d["seats"]})
            sub = call("POST", "/subscriptions", {
                "customer": self.cus[who], "items": items, "trial_period_days": d.get("trial_days"),
                "coupon": self.coupon if d.get("coupon") else None, "metadata": self.meta})
            self.sub[who] = {"id": sub["id"], "items": {i["price"]["id"]: i["id"] for i in sub["items"]["data"]}}
        elif what == "card_fails":
            self.card(who, "pm_card_chargeCustomerFail")
        elif what == "card_fixed":
            self.card(who, "pm_card_visa")
            for inv in self.open_invoices(who):
                call("POST", f"/invoices/{inv['id']}/pay")
        elif what == "upgrade":
            call("POST", f"/subscriptions/{self.sub[who]['id']}", {
                "items": [{"id": self.sub[who]["items"][self.prices["starter_m"]], "price": self.prices[d["price"]]}],
                "proration_behavior": "always_invoice"})
        elif what == "seats":
            call("POST", f"/subscription_items/{self.sub[who]['items'][self.prices['seat_m']]}",
                 {"quantity": d["quantity"], "proration_behavior": "always_invoice"})
        elif what == "refund":
            charge = next(c for c in list_all("/charges", {"customer": self.cus[who]}) if c["status"] == "succeeded")
            call("POST", "/refunds", {"charge": charge["id"], "amount": d["amount"], "reason": "requested_by_customer"})
        elif what == "cancel_at_period_end":
            call("POST", f"/subscriptions/{self.sub[who]['id']}", {"cancel_at_period_end": True})
        elif what == "write_off_if_open":
            for inv in self.open_invoices(who):
                call("POST", f"/invoices/{inv['id']}/mark_uncollectible")
                self.log(f"    wrote off {inv['id']}")
        else:
            raise ValueError(what)

    def play(self) -> None:
        for when, what, who, d in STEPS:
            self.advance(ts(when))
            r0 = int(time.time())
            self.do(what, who, d)
            self.timeline.append({"kind": "action", "real_from": r0, "real_to": int(time.time()) + 1,
                                  "clock_from": self.clock_time, "clock_to": self.clock_time})
            self.log(f"{when}  {what:22s} {CUSTOMERS[who][1]}")
        self.advance(END)
        self.log(f"clocks at {datetime.fromtimestamp(END, timezone.utc):%Y-%m-%d %H:%M} UTC")

    # ------------------------------------------------------------ export ----
    def export(self) -> dict[str, list[dict]]:
        rows: dict[str, list[dict]] = {t: [] for t in TABLES}
        prices = {p: call("GET", f"/prices/{p}") for p in self.prices.values()}
        for p in prices.values():
            rows["prices"].append({"id": p["id"], "product": p["product"], "nickname": p["nickname"],
                                   "unit_amount": p["unit_amount"], "currency": p["currency"],
                                   "recurring_interval": p["recurring"]["interval"],
                                   "recurring_interval_count": p["recurring"]["interval_count"],
                                   "active": p["active"], "created": p["created"]})
        for prod_id in sorted({p["product"] for p in prices.values()}):
            p = call("GET", f"/products/{prod_id}")
            rows["products"].append({"id": p["id"], "name": p["name"], "created": p["created"], "active": p["active"]})
        c = call("GET", f"/coupons/{self.coupon}")
        rows["coupons"].append({k: c[k] for k in ("id", "name", "percent_off", "amount_off", "duration",
                                                  "duration_in_months", "created")})
        for cus_id in self.cus.values():
            cu = call("GET", f"/customers/{cus_id}")
            rows["customers"].append({"id": cu["id"], "created": cu["created"], "name": cu["name"],
                                      "email": cu["email"], "currency": cu["currency"]})
            for s in list_all("/subscriptions", {"customer": cus_id, "status": "all"}):
                rows["subscriptions"].append({
                    "id": s["id"], "customer": s["customer"], "status": s["status"], "created": s["created"],
                    "start_date": s["start_date"], "billing_cycle_anchor": s["billing_cycle_anchor"],
                    "current_period_start": s["current_period_start"],
                    "current_period_end": s["current_period_end"],
                    "cancel_at_period_end": s["cancel_at_period_end"], "canceled_at": s["canceled_at"],
                    "ended_at": s["ended_at"],
                    "cancellation_reason": (s.get("cancellation_details") or {}).get("reason"),
                    "trial_start": s["trial_start"], "trial_end": s["trial_end"],
                    "collection_method": s["collection_method"]})
                for it in s["items"]["data"]:
                    rows["subscription_items"].append({"id": it["id"], "subscription": s["id"],
                                                       "price": it["price"]["id"], "quantity": it["quantity"],
                                                       "created": it["created"]})
                if s.get("discount"):
                    dc = s["discount"]
                    rows["discounts"].append({"id": dc["id"], "coupon": dc["coupon"]["id"],
                                              "customer": dc["customer"], "subscription": dc["subscription"],
                                              "start": dc["start"], "end": dc["end"]})
            for inv in list_all("/invoices", {"customer": cus_id}):
                if inv["status"] == "draft":      # not billed yet: not part of an export
                    continue
                st = inv["status_transitions"]
                rows["invoices"].append({
                    "id": inv["id"], "customer": inv["customer"], "subscription": inv["subscription"],
                    "billing_reason": inv["billing_reason"], "status": inv["status"], "created": inv["created"],
                    "finalized_at": st["finalized_at"], "paid_at": st["paid_at"],
                    "marked_uncollectible_at": st["marked_uncollectible_at"],
                    "attempt_count": inv["attempt_count"], "subtotal": inv["subtotal"],
                    "total_discount_amount": sum(x["amount"] for x in inv["total_discount_amounts"]),
                    "total": inv["total"], "amount_due": inv["amount_due"], "amount_paid": inv["amount_paid"],
                    "amount_remaining": inv["amount_remaining"], "currency": inv["currency"]})
                for ln in list_all(f"/invoices/{inv['id']}/lines"):
                    rows["invoice_line_items"].append({
                        "id": ln["id"], "invoice": inv["id"], "subscription": ln["subscription"],
                        "subscription_item": ln["subscription_item"], "price": ln["price"]["id"],
                        "type": ln["type"], "proration": ln["proration"], "quantity": ln["quantity"],
                        "amount": ln["amount"],
                        "discount_amount": sum(x["amount"] for x in ln["discount_amounts"]),
                        # a subscription coupon sits on the invoice; the line only carries its amount
                        "discount": (ln["discount_amounts"][0]["discount"] if ln["discount_amounts"]
                                     else (ln["discounts"][0] if ln["discounts"] else None)),
                        "period_start": ln["period"]["start"], "period_end": ln["period"]["end"],
                        "currency": ln["currency"]})
            invoices = {i["id"]: i for i in rows["invoices"]}
            ended = {s["id"]: s["ended_at"] for s in rows["subscriptions"] if s["cancellation_reason"] == "payment_failed"}
            charges = sorted(list_all("/charges", {"customer": cus_id}), key=lambda c: (c["created"], c["id"]))
            first_try = {}
            for ch in charges:
                first_try.setdefault(ch["invoice"], ch["id"])
            last_fail = {}
            for ch in charges:
                if ch["status"] == "failed":
                    last_fail[ch["invoice"]] = ch["id"]
            for ch in charges:
                inv = invoices[ch["invoice"]]
                if ch["status"] == "succeeded":
                    assert ch["amount"] == inv["amount_paid"], ch["id"]
                    at = inv["paid_at"]
                else:
                    end = min(inv["paid_at"] or inv["marked_uncollectible_at"] or END, ended.get(inv["subscription"]) or END)
                    at = self.to_clock(ch["created"], inv["finalized_at"], end, prefer="advance")
                    if first_try[inv["id"]] == ch["id"]:
                        at = inv["finalized_at"]
                    elif last_fail[inv["id"]] == ch["id"] and inv["subscription"] in ended and not inv["paid_at"]:
                        at = end
                rows["charges"].append({"id": ch["id"], "customer": ch["customer"], "invoice": ch["invoice"],
                                        "amount": ch["amount"], "created": at, "status": ch["status"],
                                        "failure_code": ch["failure_code"], "currency": ch["currency"]})
                for re in list_all("/refunds", {"charge": ch["id"]}):
                    rows["refunds"].append({"id": re["id"], "charge": ch["id"], "amount": re["amount"],
                                            "created": self.to_clock(re["created"], at, END, prefer="action"),
                                            "reason": re["reason"], "status": re["status"],
                                            "currency": re["currency"]})
        return rows


def main() -> None:
    run = Run()
    if "--export" in sys.argv:
        saved = json.loads((OUT / "run.json").read_text())
        run.tag, run.timeline, run.cus = saved["run"], saved["timeline"], saved["customers"]
        run.prices, run.coupon = saved["prices"], saved["coupon"]
    else:
        run.setup()
        run.play()
    rows = run.export()
    write_parquet(rows, TABLES, OUT / "raw")
    (OUT / "run.json").write_text(json.dumps({
        "stripe_api_version": VERSION, "run": run.tag, "clock_start": START, "clock_end": END,
        "steps": [list(s[:3]) for s in STEPS], "timeline": run.timeline,
        "customers": run.cus, "prices": run.prices, "coupon": run.coupon,
        "rows": {t: len(r) for t, r in rows.items()}}, indent=1) + "\n")
    run.log("exported: " + ", ".join(f"{t}={len(r)}" for t, r in rows.items()))


if __name__ == "__main__":
    main()
