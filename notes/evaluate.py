"""Score the 18 founder notes against the facts and the answer key (PLAN §5.5).

Refuses to run unless notes/FREEZE.json exists and every note was written with the frozen
prompt, model and CLI version, from the current facts files.

Per note:
- Grounding. Every number must be followed by a fact ID and equal that fact's displayed form
  (definitions §7). A number that matches a different fact is "miscited". A number that matches
  no fact in the file is "ungrounded", which is hallucination type (a).
- Word-numbers and format, with the same checks as the pre-freeze lint (notes/checks.py).
- Anomaly language ("spike", "surge", "sharp", "concentrated", "unusual" ...) must cite, in the
  same sentence, a trailing-average fact that crosses a threshold: ratio at least 1.5x or at most
  0.67x, a percent change of at least 50% either way, or a rate change of at least 1.0 pts.
- Causal claims ("because", "due to", "driven by" ...) are listed for manual review. Hallucination
  type (b), a cause the cited facts don't show, comes only from notes/review.json.
  An accurate mention of a movement that was not planted is never penalised.

Per planted signal:
- Metrics layer: the threshold checks in tests/test_planted_events.py and tests/test_bridge.py.
- Narrative layer: whether that month's note mentions the signal, by fact-ID and keyword rules.

Usage: python -m notes.evaluate
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from notes.checks import CITATION, format_problems, numbers_with_citations, word_numbers, words
from notes.sets import SETS, paths
from notes.write_notes import ROOT, sha256

P = paths("v1")             # set by main(); v1 unless --set says otherwise

ANOMALY = re.compile(r"\b(spik\w*|surg\w*|sharp\w*|unusual\w*|concentrat\w*|jump\w*|soar\w*|plung\w*|"
                     r"collaps\w*|outsized|exceptional\w*|dramatic\w*)\b", re.I)
CAUSAL = re.compile(r"\b(because|due to|driven by|as a result|caused by|thanks to|owing to|on the back of|"
                    r"reflect\w*|after (the|our|a))\b", re.I)


# ---- loading, with the freeze check --------------------------------------------------
def load_notes() -> dict:
    freeze_path = P["freeze"]
    if not freeze_path.exists():
        sys.exit("notes/FREEZE.json is missing: refusing to evaluate.")
    freeze = json.loads(freeze_path.read_text())
    if sha256(P["prompt"]) != freeze["prompt_sha256"]:
        sys.exit(f"{P['prompt']} no longer matches its FREEZE.json: refusing to evaluate.")
    notes = {}
    for meta_path in sorted(P["output"].glob("*.meta.json")):
        meta = json.loads(meta_path.read_text())
        month = meta["month"]
        facts_path = P["facts"] / f"{month}.json"
        bad = [k for k, want in (("prompt_sha256", freeze["prompt_sha256"]), ("model_requested", freeze["model"]),
                                 ("cli_version", freeze["cli_version"]), ("facts_sha256", sha256(facts_path)))
               if meta.get(k) != want]
        if bad or meta.get("dev_seed"):
            sys.exit(f"{month}: note does not match the freeze or the facts ({', '.join(bad) or 'dev seed'}).")
        notes[month] = {"text": (P["output"] / f"{month}.md").read_text().strip(),
                        "facts": json.loads(facts_path.read_text())["facts"], "meta": meta}
    if len(notes) != 18:
        sys.exit(f"expected 18 notes, found {len(notes)}.")
    return notes


# ---- grounding ------------------------------------------------------------------------
def norm(s: str) -> str:
    s = s.lower().replace("times", "×").replace("$", "").replace(",", "").replace(" ", "")
    return s.lstrip("-")                       # "lost $549" and "-$549" are the same figure


def forms(fact: dict) -> set[str]:
    out = {norm(fact["display"])}
    if "display_k" in fact:
        out.add(norm(fact["display_k"]))
    return out


def grounding(text: str, facts: list[dict]) -> list[dict]:
    by_id = {f["id"]: f for f in facts}
    numeric = [f for f in facts if not isinstance(f["value"], str)]
    results = []
    for token, fid in numbers_with_citations(text, P["exempt_re"]):
        t = norm(token)
        if fid is None:
            status = "uncited"
        elif fid in by_id and t in forms(by_id[fid]):
            status = "grounded"
        elif any(t in forms(f) for f in numeric):
            status = "miscited"
        else:
            status = "ungrounded"
        results.append({"number": token, "cites": fid, "status": status})
    return results


# ---- anomaly language and causal claims -------------------------------------------------
def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z$0-9])", text) if s.strip()]


def crosses(fact: dict, facts: list[dict]) -> bool:
    """Does the trailing-average fact, or one of its siblings for the same metric, cross a threshold?"""
    if fact["group"] != "vs_trailing_3m":
        return False
    metric = fact["label"].split(":")[0]
    for f in facts:
        if f["group"] != "vs_trailing_3m" or f["label"].split(":")[0] != metric:
            continue
        v = f["value"]
        if f["label"].endswith("times its trailing average") and (v >= 1.5 or v <= 0.67):
            return True
        if f["label"].endswith("change vs trailing average, percent") and abs(v) >= 50:
            return True
        if f["display"].endswith("pts") and abs(v) >= 1.0:
            return True
    return False


def anomaly_claims(text: str, facts: list[dict]) -> list[dict]:
    by_id = {f["id"]: f for f in facts}
    out = []
    for s in sentences(text):
        found = sorted({m.group(0).lower() for m in ANOMALY.finditer(s)})
        if found:
            cited = [by_id[i] for i in CITATION.findall(s) if i in by_id]
            out.append({"sentence": s, "words": found, "supported": any(crosses(f, facts) for f in cited)})
    return out


def causal_claims(text: str) -> list[str]:
    return [s for s in sentences(text) if CAUSAL.search(s)]


# ---- narrative layer: does the note mention the signal? ----------------------------------
def cited(note) -> list[dict]:
    by_id = {f["id"]: f for f in note["facts"]}
    return [by_id[i] for i in CITATION.findall(note["text"]) if i in by_id]


def cites_label(note, pattern: str) -> bool:
    return any(re.search(pattern, f["label"], re.I) for f in cited(note))


def says(note, pattern: str) -> bool:
    return re.search(pattern, note["text"], re.I) is not None


def sentence_with(note, *patterns: str) -> bool:
    return any(all(re.search(p, s, re.I) for p in patterns) for s in sentences(note["text"]))


def cites_value(note, value: str) -> bool:
    return any(f["value"] == value for f in cited(note)) or value in note["text"]


def customer_name(customer_id: str) -> str:
    import duckdb
    return duckdb.connect().execute(f"SELECT name FROM '{ROOT / 'data' / 'raw' / 'customers.parquet'}' WHERE id = ?",
                                    [customer_id]).fetchone()[0]


def narrative_rules(key: dict) -> dict:
    """(event, month, signal) -> rule(note). Each rule names what counts as a mention."""
    e2 = next(e for e in key["events"] if e["id"] == "E2")["details"]["customer"]
    e2_name = customer_name(e2)
    e6 = next(e for e in key["events"] if e["id"] == "E6")["details"]["customer"]
    starter_vol = lambda n: (cites_label(n, r"^Starter (customers|MRR) churned, voluntary")
                             or sentence_with(n, r"\bStarter\b", r"churn"))
    return {
        ("E1", "2025-09", 0): lambda n: cites_label(n, r"from price change") or says(n, r"price (change|rise|increase)"),
        ("E1", "2025-09", 1): starter_vol,
        ("E1", "2025-10", 2): starter_vol,
        ("E2", "2025-11", 0): lambda n: cites_value(n, e2) or cites_value(n, e2_name) or says(n, r"\b(single|largest) (customer|account|logo)"),
        ("E2", "2025-11", 1): lambda n: cites_label(n, r"^Gross MRR churn") and cites_label(n, r"^Logo churn"),
        ("E3", "2026-04", 0): lambda n: cites_label(n, r"from discount ended") or says(n, r"discount"),
        ("E3", "2026-04", 1): lambda n: sentence_with(n, r"2026-01|January", r"churn|left|lost|retention"),
        ("E3", "2026-04", 2): lambda n: cites_label(n, r"^Cohort 2026-01: (logo retention|change in logo retention|still paying)"),
        ("E4", "2026-02", 0): lambda n: cites_label(n, r"first failed payment|entering dunning"),
        ("E4", "2026-02", 1): lambda n: cites_label(n, r"involuntar"),
        ("E4", "2026-02", 2): lambda n: cites_label(n, r"in dunning at month-end"),
        ("E4", "2026-03", 3): lambda n: cites_label(n, r"involuntar"),
        ("E4", "2026-02", 4): lambda n: cites_label(n, r"^At-risk MRR"),
        ("E4", "2026-02", 5): lambda n: cites_label(n, r"^Bad-debt expense"),
        ("E4", "2026-03", 6): lambda n: cites_label(n, r"^At-risk MRR"),
        ("E5", "2026-03", 0): lambda n: (cites_label(n, r"^(Billings|Net cash collected|Cash collected)")
                                         and cites_label(n, r"^Net revenue")),
        ("E5", "2026-03", 1): lambda n: cites_label(n, r"contract liability") or says(n, r"deferred revenue|contract liability"),
        ("E5", "2026-03", 2): lambda n: cites_label(n, r"from interval switch") or says(n, r"\bannual\b"),
        ("E5", "2026-03", 3): lambda n: cites_label(n, r"^Logo churn"),
        ("E6", "2026-04", 0): lambda n: cites_label(n, r"refund") and cites_label(n, r"^Net revenue|^Net cash"),
        ("E6", "2026-04", 1): lambda n: cites_label(n, r"refund") and cites_label(n, r"^Closing MRR"),
        ("E7", "2026-05", 0): lambda n: cites_label(n, r"^Reactivat") or says(n, r"reactivat|win-?back|returning"),
    }


# ---- metrics layer: the threshold checks already in the test suite ------------------------
METRIC_TESTS = {
    "E1": ["test_e1_price_rise", "test_e1_price_change_expansion_in_september_only", "test_e1_starter_voluntary_churn"],
    "E2": ["test_e2_one_logo_dominates_churn", "test_e2_one_logo_is_most_of_november_churn",
           "test_e2_gross_mrr_churn_far_above_logo_churn"],
    "E3": ["test_e3_promo_cohort_cliff_and_discount_roll_off", "test_e3_cliff_at_m3_and_revenue_retention_above_logo",
           "test_e3_discount_roll_off_is_april_expansion_from_january_cohort"],
    "E4": ["test_e4_card_expiry", "test_e4_counts_at_the_metrics_layer", "test_e4_bad_debt_expense_rises"],
    "E5": ["test_e5_annual_push_billings", "test_e5_interval_switch_contraction_in_march",
           "test_e5_cash_far_above_revenue_deferred_up_mrr_down"],
    "E6": ["test_e6_large_refund_customer_stays", "test_e6_service_credit_is_all_delivered"],
    "E7": ["test_e7_winback_reactivation_wave", "test_e7_reactivation_wave_in_may"],
    "E8": ["test_e8_control_month_is_quiet"],
}


def metrics_layer() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "r.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}",
                        "-k", "e1 or e2 or e3 or e4 or e5 or e6 or e7 or e8", "tests"],
                       cwd=ROOT, capture_output=True, text=True)
        cases = {c.get("name"): not any(ch.tag in ("failure", "error") for ch in c)
                 for c in ET.parse(xml).getroot().iter("testcase")}
    out = {}
    for event, names in METRIC_TESTS.items():
        found = {n: cases[n] for n in names if n in cases}
        missing = [n for n in names if n not in cases]
        if missing:
            sys.exit(f"metrics layer: tests not found: {missing}")
        out[event] = {"passed": all(found.values()), "tests": found}
    return out


# ---- the scorecard ------------------------------------------------------------------------
def evaluate() -> dict:
    notes = load_notes()
    key = json.loads((ROOT / "data" / "answer_key.json").read_text())
    review_path = P["review"]
    review = json.loads(review_path.read_text()) if review_path.exists() else {}
    review_months = review.get("months", {})

    months = {}
    for month, note in notes.items():
        g = grounding(note["text"], note["facts"])
        ids = {f["id"] for f in note["facts"]}
        reviewed = review_months.get(month, {}).get("unsupported_causes", [])
        months[month] = {
            "words": words(note["text"]),
            "format_problems": format_problems(note["text"], ids, P["min_words"], P["max_words"], P["bullets"], P["exempt_re"]),
            "numbers": len(g),
            "grounded": sum(r["status"] == "grounded" for r in g),
            "miscited": [r for r in g if r["status"] == "miscited"],
            "uncited": [r for r in g if r["status"] == "uncited"],
            "ungrounded": [r for r in g if r["status"] == "ungrounded"],
            "word_numbers": word_numbers(note["text"]),
            "anomaly_claims": anomaly_claims(note["text"], note["facts"]),
            "causal_claims_for_review": causal_claims(note["text"]),
            "unsupported_causes_from_review": reviewed,
        }

    rules = narrative_rules(key)
    metrics = metrics_layer()
    signals = []
    for event in key["events"]:
        for i, s in enumerate(event["signals"]):
            if event["id"] == "E8":
                caught = all(c["supported"] for c in months[s["month"]]["anomaly_claims"])
                rule_name = "no unsupported anomaly language"
            else:
                caught = rules[(event["id"], s["month"], i)](notes[s["month"]])
                rule_name = "fact-ID and keyword rule"
            signals.append({"event": event["id"], "month": s["month"], "role": s.get("role", "primary"),
                            "signal": s["signal"], "metrics_layer": metrics[event["id"]]["passed"],
                            "narrative_caught": caught, "rule": rule_name})

    total = lambda k: sum(len(m[k]) if isinstance(m[k], list) else m[k] for m in months.values())
    primary = [s for s in signals if s["role"] == "primary"]
    summary = {
        "notes": len(months),
        "numbers": total("numbers"), "grounded": total("grounded"),
        "miscited": total("miscited"), "uncited": total("uncited"),
        "hallucinations_ungrounded_numbers": total("ungrounded"),
        "hallucinations_unsupported_causes": total("unsupported_causes_from_review"),
        "causal_claims_awaiting_review": 0 if review else total("causal_claims_for_review"),
        "word_numbers": total("word_numbers"),
        "format_problems": total("format_problems"),
        "unsupported_anomaly_claims": sum(not c["supported"] for m in months.values() for c in m["anomaly_claims"]),
        "primary_signals_caught": f"{sum(s['narrative_caught'] for s in primary)}/{len(primary)}",
        "supporting_signals_caught": f"{sum(s['narrative_caught'] for s in signals if s['role'] == 'supporting')}"
                                     f"/{sum(s['role'] == 'supporting' for s in signals)}",
        "metrics_layer_events_passing": f"{sum(m['passed'] for m in metrics.values())}/{len(metrics)}",
        "manual_review": review.get("reviewer", "not recorded"),
    }
    return {"summary": summary, "signals": signals, "months": months, "metrics_layer": metrics,
            "review_comments": review.get("comments", [])}


def markdown(card: dict) -> str:
    s = card["summary"]
    lines = ["# Notes scorecard", "", "Generated by `python -m notes.evaluate` from the frozen prompt's 18 notes.", "",
             "## Summary", "", "| Measure | Result |", "|---|---|"]
    lines += [f"| {k.replace('_', ' ')} | {v} |" for k, v in s.items()]
    lines += ["", "## Planted signals", "", "| Event | Month | Role | Signal | Metrics layer | Note caught it |",
              "|---|---|---|---|---|---|"]
    for x in card["signals"]:
        lines.append(f"| {x['event']} | {x['month']} | {x['role']} | {x['signal']} | "
                     f"{'pass' if x['metrics_layer'] else 'FAIL'} | {'yes' if x['narrative_caught'] else 'no'} |")
    lines += ["", "## Per note", "", "| Month | Words | Numbers | Grounded | Miscited | Uncited | Ungrounded | "
              "Word-numbers | Unsupported anomaly claims | Causal claims to review |", "|---|---|---|---|---|---|---|---|---|---|"]
    for m, r in card["months"].items():
        lines.append(f"| {m} | {r['words']} | {r['numbers']} | {r['grounded']} | {len(r['miscited'])} | {len(r['uncited'])} | "
                     f"{len(r['ungrounded'])} | {len(r['word_numbers'])} | "
                     f"{sum(not c['supported'] for c in r['anomaly_claims'])} | {len(r['causal_claims_for_review'])} |")
    issues = [(m, kind, x) for m, r in card["months"].items()
              for kind in ("ungrounded", "miscited", "uncited") for x in r[kind]]
    if issues:
        lines += ["", "## Number issues", "", "| Month | Kind | Number | Cited |", "|---|---|---|---|"]
        lines += [f"| {m} | {kind} | {x['number']} | {x['cites'] or '-'} |" for m, kind, x in issues]
    claims = [(m, c) for m, r in card["months"].items() for c in r["anomaly_claims"] if not c["supported"]]
    if claims:
        lines += ["", "## Unsupported anomaly language", ""]
        lines += [f"- **{m}** ({', '.join(c['words'])}): {c['sentence']}" for m, c in claims]
    causal = [(m, c) for m, r in card["months"].items() for c in r["causal_claims_for_review"]]
    if causal:
        lines += ["", "## Causal claims for manual review", ""]
        lines += [f"- **{m}**: {c}" for m, c in causal]
    if card["review_comments"]:
        lines += ["", "## Manual review comments", ""] + [f"- {c}" for c in card["review_comments"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    global P
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", default="v1", choices=sorted(SETS))
    P = paths(ap.parse_args(argv).set)
    card = evaluate()
    P["scorecard_json"].write_text(json.dumps(card, indent=1, ensure_ascii=False) + "\n")
    P["scorecard_md"].write_text(markdown(card))
    print(json.dumps(card["summary"], indent=1))


if __name__ == "__main__":
    main()
