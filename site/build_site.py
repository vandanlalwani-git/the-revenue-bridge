"""Build site/index.html: one static page driven by a month picker (PLAN §1).

Every figure comes from the SQL marts, the facts files, the frozen notes and the scorecard,
displayed with pipeline/display.py only (definitions §7). The page embeds its data as JSON and
draws its charts with plain SVG, so it needs no server and loads no scripts.

Usage: python site/build_site.py   (not "python -m site...": `site` is also a standard-library module)
"""
from __future__ import annotations

import html
import json
import re
import sys
from fractions import Fraction
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import display  # noqa: E402
from pipeline.run import build, connect  # noqa: E402

SITE = ROOT / "site"
FIRST_REPORTING_MONTH = "2025-01"


# ---- displayed forms --------------------------------------------------------------------
def usd(whole: int) -> str:
    return f"${whole:,}" if whole >= 0 else f"−${-whole:,}"


def usd_cents(dollars: Fraction) -> str:
    cents = display.round_half_away(dollars * 100)
    s = f"${abs(cents) // 100:,}.{abs(cents) % 100:02d}"
    return s if cents >= 0 else "−" + s


def pct(numer, denom) -> str | None:
    return None if not denom else f"{display.percent(numer, denom)}%"


def signal(now: Fraction, prev, kind: str, higher_is_better: bool, flat) -> dict | None:
    """Change on last month, as text plus a tone: good, bad or flat."""
    if prev is None or (kind == "pct" and not prev):
        return None
    change = Fraction(now) - Fraction(prev)
    if kind == "pct":
        size, shown = abs(change) * 100 / abs(prev), f"{display.percent(abs(change), abs(prev))}%"
    elif kind == "pts":
        size, shown = abs(change), f"{display.percent(abs(change), 100)} pts"
    else:
        size, shown = abs(change), usd(display.whole_dollars(abs(change)))
    tone = "flat" if size < flat else ("good" if (change > 0) == higher_is_better else "bad")
    if change == 0:
        return {"text": "no change vs last month", "tone": "flat"}
    sign = "+" if change > 0 else "−"
    return {"text": f"{sign}{shown} vs last month", "tone": tone}


def shown_bridge(opening: Fraction, parts: list[tuple[str, str, Fraction]], closing: Fraction) -> list[dict]:
    """A displayed bridge (§7.3) as rows: label, whole dollars, kind (total / add / less)."""
    out = display.bridge(opening, [(k, v) for k, _, v in parts], closing)
    rows = [{"label": "", "value": out["opening"], "kind": "total"}]
    rows += [{"label": label, "value": out[k], "kind": "add" if out[k] >= 0 else "less"} for k, label, _ in parts]
    rows.append({"label": "", "value": out["closing"], "kind": "total"})
    for r in rows:
        r["display"] = usd(r["value"])
    return rows


# ---- loading the marts ----------------------------------------------------------------------
def rows(con, sql: str, *params) -> list[dict]:
    cur = con.execute(sql, list(params))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def month_data(con) -> dict:
    by_month = lambda sql: {r["month"]: r for r in rows(con, sql)}
    bridge, retention = by_month("SELECT * FROM mrr_bridge"), by_month("SELECT * FROM retention_month")
    recon, ecl, revenue = by_month("SELECT * FROM reconciliation_month"), by_month("SELECT * FROM ecl_month"), \
        by_month("SELECT * FROM revenue_month")
    dunning = by_month("SELECT month, count(*) AS customers, sum(arr_cents) AS arr FROM customer_mrr WHERE past_due GROUP BY 1")
    drivers = rows(con, "SELECT * FROM mrr_bridge_drivers ORDER BY month, movement, arr_delta_cents DESC")
    rates = rows(con, "SELECT * FROM ecl_rates ORDER BY month, stage")
    balances = rows(con, "SELECT month, bucket, sum(balance_cents) AS cents FROM receivables_month_end GROUP BY ALL")
    months = [m for (m,) in con.execute("SELECT month FROM calendar WHERE month >= ? ORDER BY 1",
                                        [FIRST_REPORTING_MONTH]).fetchall()]
    mrr = display.arr_cents_to_mrr_dollars
    dollars = display.cents_to_dollars
    all_months = sorted(bridge)
    out = {}
    for m in months:
        b, r, rc, e, rv = bridge[m], retention[m], recon[m], ecl[m], revenue[m]
        d = dunning.get(m, {"customers": 0, "arr": 0})
        opening, closing = b["opening_arr_cents"], b["closing_arr_cents"]
        kpi = {
            "closing_mrr": usd(display.whole_dollars(mrr(closing))),
            "mom_growth": pct(closing - opening, opening),
            "logo_churn": pct(r["churned_customers"], r["opening_customers"]),
            "gross_mrr_churn": pct(r["churn_arr_cents"] + r["contraction_arr_cents"], r["opening_arr_cents"]),
            "nrr": pct(r["nrr_arr_cents"], r["nrr_base_arr_cents"]),
            "grr": pct(r["grr_arr_cents"], r["nrr_base_arr_cents"]),
            "at_risk_mrr": usd(display.whole_dollars(mrr(d["arr"] or 0))),
            "at_risk_customers": d["customers"],
            "paying_customers": r["closing_customers"],
        }
        steps = display.mrr_bridge_row(b)
        bridge_steps = [{"key": "opening", "label": "Opening", "value": steps["opening"], "kind": "total"},
                        {"key": "new", "label": "New", "value": steps["new"], "kind": "add"},
                        {"key": "expansion", "label": "Expansion", "value": steps["expansion"], "kind": "add"},
                        {"key": "reactivation", "label": "Reactivation", "value": steps["reactivation"], "kind": "add"},
                        {"key": "contraction", "label": "Contraction", "value": steps["contraction"], "kind": "less"},
                        {"key": "churn_voluntary", "label": "Churn, voluntary", "value": steps["churn_voluntary"], "kind": "less"},
                        {"key": "churn_involuntary", "label": "Churn, involuntary", "value": steps["churn_involuntary"], "kind": "less"},
                        {"key": "closing", "label": "Closing", "value": steps["closing"], "kind": "total"}]
        for s in bridge_steps:
            s["display"] = usd(s["value"])
        # Drivers inside expansion and contraction, displayed so they add up to the bridge step.
        split = {}
        for movement in ("expansion", "contraction"):
            ds = [x for x in drivers if x["month"] == m and x["movement"] == movement]
            if not ds:
                continue
            # Item deltas are signed, like the bridge step, and sum to it exactly in cents.
            shown = display.bridge(Fraction(0), [(x["driver"], mrr(x["arr_delta_cents"])) for x in ds],
                                   Fraction(steps[movement]))
            split[movement] = [{"driver": x["driver"].replace("_", " ").capitalize(), "display": usd(abs(shown[x["driver"]])),
                                "customers": x["customers"]} for x in ds]
        counts = {k: b[c] for k, c in [("new", "new_customers"), ("reactivated", "reactivated_customers"),
                                        ("expanded", "expanded_customers"), ("contracted", "contracted_customers"),
                                        ("churned_voluntary", "churned_voluntary_customers"),
                                        ("churned_involuntary", "churned_involuntary_customers"),
                                        ("blind_spot", "blind_spot_customers")]}

        den = Fraction(rc["r1_denominator"] * 100)
        f = lambda col: Fraction(rc[col]) / den
        r1 = shown_bridge(f("r1_closing_mrr"), [
            ("timing", "Timing: closing MRR above the month's average", -f("r1_timing")),
            ("daycount", "Day count and proration", f("r1_daycount_proration")),
            ("once", "One-off coupons (in revenue, never in MRR)", -f("r1_once_coupons")),
            ("refunds", "Refunds for delivered service", -f("r1_refunds_delivered"))], f("r1_net_revenue"))
        r1[0]["label"], r1[-1]["label"] = "Closing MRR", "Net revenue"
        r2 = shown_bridge(dollars(rc["net_revenue_cents"]), [
            ("refunds", "Refunds for delivered service, added back", dollars(rc["refunds_delivered_cents"])),
            ("cl", "Increase in contract liability", dollars(rc["cl_change_cents"])),
            ("uwo", "Unearned part of write-offs", dollars(rc["unearned_written_off_cents"])),
            ("ur", "Unearned part of refunds", dollars(rc["refunds_unearned_cents"]))], dollars(rc["billings_cents"]))
        r2[0]["label"], r2[-1]["label"] = "Net revenue", "Billings"
        r3 = shown_bridge(dollars(rc["billings_cents"]), [
            ("ar", "Increase in receivables", -dollars(rc["ar_change_cents"])),
            ("ewo", "Earned part of write-offs", -dollars(rc["earned_written_off_cents"])),
            ("uwo", "Unearned part of write-offs", -dollars(rc["unearned_written_off_cents"])),
            ("ref", "Refunds paid", -dollars(rc["refunds_total_cents"]))], dollars(rc["net_cash_cents"]))
        r3[0]["label"], r3[-1]["label"] = "Billings", "Net cash collected"
        cl = shown_bridge(dollars(rc["cl_open_cents"]), [
            ("bill", "Billings", dollars(rc["billings_cents"])),
            ("rev", "Revenue recognised", -dollars(rc["gross_recognised_cents"])),
            ("uwo", "Unearned part of write-offs", -dollars(rc["unearned_written_off_cents"])),
            ("ur", "Unearned part of refunds", -dollars(rc["refunds_unearned_cents"]))], dollars(rc["cl_close_cents"]))
        cl[0]["label"], cl[-1]["label"] = "Opening", "Closing"
        ar = shown_bridge(dollars(rc["ar_open_cents"]), [
            ("bill", "Billings", dollars(rc["billings_cents"])),
            ("coll", "Cash collected", -dollars(rc["collections_cents"])),
            ("ewo", "Earned part of write-offs", -dollars(rc["earned_written_off_cents"])),
            ("uwo", "Unearned part of write-offs", -dollars(rc["unearned_written_off_cents"]))], dollars(rc["ar_close_cents"]))
        ar[0]["label"], ar[-1]["label"] = "Opening", "Closing"
        al = shown_bridge(dollars(rc["allowance_open_cents"]), [
            ("bde", "Bad-debt expense", dollars(rc["bad_debt_expense_cents"])),
            ("used", "Used on write-offs", -dollars(rc["earned_written_off_cents"]))], dollars(rc["allowance_close_cents"]))
        al[0]["label"], al[-1]["label"] = "Opening", "Closing"
        unexplained = {
            "r1": usd_cents(Fraction(rc["r1_unexplained"]) / den), "r2": usd_cents(dollars(rc["r2_unexplained_cents"])),
            "r3": usd_cents(dollars(rc["r3_unexplained_cents"])), "cl": usd_cents(dollars(rc["cl_unexplained_cents"])),
            "ar": usd_cents(dollars(rc["ar_unexplained_cents"])), "al": usd_cents(dollars(rc["ecl_unexplained_cents"]))}
        bal = {x["bucket"]: x["cents"] for x in balances if x["month"] == m}
        bucket_names = {"current": "Not yet failed", "failed_1": "Failed once", "failed_2": "Failed twice",
                        "failed_3": "Failed three times"}
        bucket_days = {"current": "card not yet declined", "failed_1": "days 0–3 of retries",
                       "failed_2": "days 3–7", "failed_3": "days 7–14, last retry next"}
        buckets = []
        for x in (x for x in rates if x["month"] == m):
            cents = bal.get(x["bucket"], 0)
            # Same rounding as 50_ledger.sql: each bucket's expected loss, then summed into the allowance.
            loss = (display.round_half_away(Fraction(cents * x["rate_loss_cents"], x["rate_observed_cents"]))
                    if x["rate_observed_cents"] else 0)
            buckets.append({"bucket": bucket_names[x["bucket"]], "days": bucket_days[x["bucket"]],
                            "balance": usd(display.whole_dollars(dollars(cents))), "balance_cents": cents,
                            "rate": pct(x["rate_loss_cents"], x["rate_observed_cents"]) or "0.0%",
                            "floored": x["rate_from"] != x["bucket"], "rate_from": bucket_names[x["rate_from"]],
                            "expected_loss": usd_cents(dollars(loss)), "expected_loss_cents": loss,
                            "history": (f"{x['lost_invoices']} of {x['observed_invoices']} never paid"
                                        if x["observed_invoices"] else "no history yet")})
        allowance = {"open": usd_cents(dollars(e["allowance_open_cents"])), "close": usd_cents(dollars(e["allowance_close_cents"])),
                     "close_cents": e["allowance_close_cents"],
                     "write_offs": usd_cents(dollars(e["earned_written_off_cents"])),
                     "change": usd_cents(dollars(e["allowance_close_cents"] - e["allowance_open_cents"])),
                     "expense": usd_cents(dollars(e["bad_debt_expense_cents"]))}
        kpi["net_revenue"] = usd(display.whole_dollars(dollars(rv["net_revenue_cents"])))
        kpi["net_cash"] = usd(display.whole_dollars(dollars(rv["net_cash_cents"])))
        kpi["billings"] = usd(display.whole_dollars(dollars(rv["billings_cents"])))
        # Each KPI's change on the month before (December 2024 for January 2025), coloured by whether it is
        # good news, bad news or flat. Flat: under 1% for money, under 0.3 pts for churn, 0.5 pts for NRR, $50 at risk.
        p_ = all_months[all_months.index(m) - 1]
        pb, pr, pre = bridge[p_], retention[p_], revenue[p_]
        pd_ = dunning.get(p_, {"customers": 0, "arr": 0})
        rate = lambda n, dn: Fraction(n) * 100 / dn
        kpi["signals"] = {
            "mrr": signal(Fraction(closing), Fraction(pb["closing_arr_cents"]), "pct", True, 1),
            "net_revenue": signal(Fraction(rv["net_revenue_cents"]), Fraction(pre["net_revenue_cents"]), "pct", True, 1),
            "net_cash": signal(Fraction(rv["net_cash_cents"]), Fraction(pre["net_cash_cents"]), "pct", True, 1),
            "logo_churn": signal(rate(r["churned_customers"], r["opening_customers"]),
                                 rate(pr["churned_customers"], pr["opening_customers"]), "pts", False, Fraction(3, 10)),
            "nrr": signal(rate(r["nrr_arr_cents"], r["nrr_base_arr_cents"]),
                          rate(pr["nrr_arr_cents"], pr["nrr_base_arr_cents"]) if pr["nrr_base_arr_cents"] else None,
                          "pts", True, Fraction(1, 2)),
            "at_risk": signal(mrr(d["arr"] or 0), mrr(pd_["arr"] or 0), "usd", False, 50),
        }
        # Raw numbers for the trend lines only; every figure printed on the page is a display string above.
        series = {"mrr": float(mrr(closing)), "net_revenue": rv["net_revenue_cents"] / 100,
                  "billings": rv["billings_cents"] / 100, "net_cash": rv["net_cash_cents"] / 100,
                  "logo_churn": float(Fraction(r["churned_customers"], r["opening_customers"]) * 100),
                  "gross_mrr_churn": float(Fraction(r["churn_arr_cents"] + r["contraction_arr_cents"], r["opening_arr_cents"]) * 100),
                  "nrr": float(Fraction(r["nrr_arr_cents"], r["nrr_base_arr_cents"]) * 100),
                  "grr": float(Fraction(r["grr_arr_cents"], r["nrr_base_arr_cents"]) * 100),
                  "at_risk": float(mrr(d["arr"] or 0))}
        out[m] = {"kpi": kpi, "series": series, "bridge": bridge_steps, "drivers": split, "counts": counts,
                  "recon": {"r1": r1, "r2": r2, "r3": r3}, "roll": {"cl": cl, "ar": ar, "al": al},
                  "unexplained": unexplained, "buckets": buckets, "allowance": allowance,
                  "bad_debt": usd(display.whole_dollars(dollars(e["bad_debt_expense_cents"]))),
                  "revenue": {"billings": usd(display.whole_dollars(dollars(rv["billings_cents"]))),
                              "net_revenue": usd(display.whole_dollars(dollars(rv["net_revenue_cents"]))),
                              "net_cash": usd(display.whole_dollars(dollars(rv["net_cash_cents"])))}}
    return {"months": months, "per_month": out}


def cohorts(con) -> dict:
    """Per cohort: logo and revenue retention by months since first paid day, plus the
    all-cohort average curve (weighted by customers, or by month-0 MRR for revenue)."""
    data = rows(con, "SELECT * FROM cohort_retention ORDER BY cohort, k")
    grid, totals = {}, {}
    for x in data:
        c = grid.setdefault(x["cohort"], {"size": x["cohort_customers"], "logo": [], "revenue": [], "months": []})
        c["logo"].append(float(display.percent(x["retained_customers"], x["cohort_customers"])))
        c["revenue"].append(float(display.percent(x["arr_cents"], x["m0_arr_cents"])) if x["m0_arr_cents"] else None)
        c["months"].append(x["month"])
        t = totals.setdefault(x["k"], [0, 0, 0, 0])
        t[0] += x["retained_customers"]; t[1] += x["cohort_customers"]
        t[2] += x["arr_cents"]; t[3] += x["m0_arr_cents"]
    average = {"logo": [], "revenue": [], "cohorts": []}
    for k in sorted(totals):
        r, n, a, a0 = totals[k]
        average["logo"].append(float(display.percent(r, n)))
        average["revenue"].append(float(display.percent(a, a0)) if a0 else None)
        average["cohorts"].append(sum(1 for c in grid.values() if len(c["logo"]) > k))
    return {"cohorts": [{"cohort": k, **v} for k, v in grid.items()], "average": average}


# ---- notes, scorecard, definitions -------------------------------------------------------------
def figures(line: str, facts: dict) -> str:
    """Escape a line and turn each cited number into a hoverable figure; the [F#] tags themselves are not shown."""
    from notes.checks import CITATION, NUMBER
    out, pos = [], 0
    for m in CITATION.finditer(line):
        seg = line[pos:m.start()]
        nums = list(NUMBER.finditer(seg))
        f = facts[m.group(1)]
        tip = html.escape(f"{f['label']}: {f['display']}", quote=True)
        if nums:
            n = nums[-1]
            out.append(html.escape(seg[:n.start()]))
            out.append(f'<span class="fig" tabindex="0" data-tip="{tip}">{html.escape(n.group(0).strip())}</span>')
            out.append(html.escape(seg[n.end():].rstrip()))
        else:
            out.append(html.escape(seg.rstrip()))
        pos = m.end()
    out.append(html.escape(line[pos:]))
    return "".join(out)


def notes(months: list[str]) -> dict:
    """The v2 founder updates (headline + bullets), with their facts behind each figure."""
    from notes.sets import paths
    P = paths("v2")
    out = {}
    for m in months:
        text = (P["output"] / f"{m}.md").read_text().strip()
        facts = {f["id"]: f for f in json.loads((P["facts"] / f"{m}.json").read_text())["facts"]}
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        headline = figures(lines[0].removeprefix("Headline:").strip(), facts)
        bullets = []
        for line in lines[1:]:
            body = line.removeprefix("- ").strip()
            topic, _, rest = body.partition(":")
            what, _, means = rest.partition("What it means:")
            cap = lambda t: t[:1].upper() + t[1:]           # display only: start each sentence with a capital
            bullets.append({"topic": html.escape(topic.strip()), "what": figures(cap(what.strip()), facts),
                            "means": figures(cap(means.strip()), facts)})
        out[m] = {"headline": headline, "bullets": bullets}
    return out


EVENT_NAMES = {"E1": "Starter price rise", "E2": "Largest customer churns", "E3": "Promo cohort hits its discount cliff",
               "E4": "Card-expiry wave of failed payments", "E5": "Annual-prepay push", "E6": "Large refund, customer stays",
               "E7": "Win-back campaign", "E8": "Control month: nothing planted"}


def scorecard() -> dict:
    """v2 (shown on the page) and v1 (the original frozen run), side by side."""
    from notes.sets import paths
    out = {}
    for name in ("v2", "v1"):
        P = paths(name)
        card = json.loads(P["scorecard_json"].read_text())
        events = []
        for eid, title in EVENT_NAMES.items():
            sig = [x for x in card["signals"] if x["event"] == eid]
            primary = [x for x in sig if x["role"] == "primary"]
            events.append({"id": eid, "name": title, "months": sorted({x["month"] for x in sig}),
                           "in_data": all(x["metrics_layer"] for x in sig),
                           "caught": sum(x["narrative_caught"] for x in primary), "of": len(primary),
                           "signals": [{"month": x["month"], "role": x["role"], "signal": x["signal"],
                                        "caught": x["narrative_caught"]} for x in sig]})
        out[name] = {"summary": card["summary"], "events": events, "comments": card.get("review_comments", []),
                     "freeze": json.loads(P["freeze"].read_text())}
    return out


def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: m.group(1) if not m.group(2).startswith("http")
               else f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    return s


def markdown(md: str) -> str:
    """Enough Markdown for docs/metric_definitions.md: headings, paragraphs, lists, tables, code, rules.
    Each ## section becomes a collapsible block."""
    lines, out, i, open_section = md.split("\n"), [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            out.append("<pre>" + html.escape("\n".join(lines[i + 1:j])) + "</pre>")
            i = j + 1
        elif line.startswith("## "):
            if open_section:
                out.append("</div></details>")
            out.append(f"<details><summary>{inline(line[3:])}</summary><div class=\"doc-body\">")
            open_section, i = True, i + 1
        elif line.startswith("# "):
            i += 1                                           # the page supplies its own heading
        elif re.match(r"^#{3,4} ", line):
            out.append(f"<h4>{inline(line.lstrip('#').strip())}</h4>")
            i += 1
        elif line.strip() == "---":
            i += 1
        elif line.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in lines[i:j] if not re.match(r"^\|[\s:|-]+\|$", r)]
            head, body = cells[0], cells[1:]
            out.append('<div class="scroll"><table><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                                                          for r in body) + "</tbody></table></div>")
            i = j
        elif re.match(r"^\s*([-*]|\d+\.) ", line):
            j, items = i, []
            while j < len(lines) and (re.match(r"^\s*([-*]|\d+\.) ", lines[j]) or
                                      (lines[j].startswith("   ") and lines[j].strip())):
                m = re.match(r"^(\s*)([-*]|\d+\.) (.*)", lines[j])
                if m:
                    items.append((len(m.group(1)), m.group(3)))
                else:
                    items[-1] = (items[-1][0], items[-1][1] + " " + lines[j].strip())
                j += 1
            ordered = bool(re.match(r"^\s*\d+\.", line))
            tag = "ol" if ordered else "ul"
            html_items, depth = [f"<{tag}>"], 0
            for indent, text in items:
                level = 1 if indent >= 2 else 0
                if level > depth:
                    html_items.append("<ul>")
                elif level < depth:
                    html_items.append("</ul>")
                depth = level
                html_items.append(f"<li>{inline(text)}</li>")
            if depth:
                html_items.append("</ul>")
            html_items.append(f"</{tag}>")
            out.append("".join(html_items))
            i = j
        elif line.strip():
            j = i
            while j < len(lines) and lines[j].strip() and not re.match(r"^(#|\||```|\s*([-*]|\d+\.) |---)", lines[j]):
                j += 1
            out.append("<p>" + inline(" ".join(l.strip() for l in lines[i:j])) + "</p>")
            i = j
        else:
            i += 1
    if open_section:
        out.append("</div></details>")
    return "\n".join(out)


def links() -> dict:
    cfg = yaml.safe_load((SITE / "config.yaml").read_text()) or {}
    repo = cfg.get("repo_url")
    return {"repo": repo, "writeup": f"{repo}/blob/main/docs/writeup.md" if repo else None,
            "notes": f"{repo}/tree/main/notes/output" if repo else None, "video": cfg.get("video_url")}


def main(out: Path | None = None) -> Path:
    con = connect(ROOT / "data")
    build(con)
    md = month_data(con)
    data = {**md, "default_month": "2026-06", "cohorts": cohorts(con), "notes": notes(md["months"]),
            "scorecard": scorecard(), "links": links()}
    page = (SITE / "template.html").read_text()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = page.replace("/*__DATA__*/null", payload)
    page = page.replace("<!--__DEFINITIONS__-->", markdown((ROOT / "docs" / "metric_definitions.md").read_text()))
    reviewer = json.loads((ROOT / "notes" / "v2" / "review.json").read_text())["reviewer"]
    status = "confirmed by the author" if "confirmed by Vandan" in reviewer else "first read by Claude; awaiting the author's confirmation"
    page = page.replace("<!--__REVIEW_STATUS__-->", status)
    out = out or SITE / "index.html"
    out.write_text(page)
    print(f"wrote {out} ({len(page) // 1024} KB)")
    return out


if __name__ == "__main__":
    main()
