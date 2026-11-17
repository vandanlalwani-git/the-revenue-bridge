"""Day 7: the static page (site/build_site.py)."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]


def load_builder():
    # Loaded by path: the folder is called site/, which is also a standard-library module.
    spec = importlib.util.spec_from_file_location("build_site", ROOT_DIR / "site" / "build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "index.html"
    load_builder().main(out)
    return out.read_text()


@pytest.fixture(scope="module")
def data(page):
    raw = re.search(r"const DATA = (.*?);\nconst \$", page, re.S).group(1)
    return json.loads(raw.replace("<\\/", "</"))


def money(s: str) -> int:
    return int(s.replace("−", "-").replace("$", "").replace(",", ""))


def test_placeholders_filled_and_no_scripts_loaded(page):
    assert "/*__DATA__*/" not in page and "<!--__DEFINITIONS__-->" not in page
    assert "<script src" not in page


def test_eighteen_months_and_every_unexplained_line_is_zero(data):
    assert data["months"][0] == "2025-01" and data["months"][-1] == "2026-06" and len(data["months"]) == 18
    assert data["default_month"] == "2026-06"
    for m, d in data["per_month"].items():
        assert set(d["unexplained"].values()) == {"$0.00"}, m


def test_displayed_bridges_add_up(data):
    for m, d in data["per_month"].items():
        tables = [d["bridge"], *d["recon"].values(), *d["roll"].values()]
        for rows in tables:
            assert rows[0]["value"] + sum(r["value"] for r in rows[1:-1]) == rows[-1]["value"], m
        for movement, drivers in d["drivers"].items():
            step = next(s for s in d["bridge"] if s["key"] == movement)
            assert sum(money(x["display"]) for x in drivers) == abs(step["value"]), (m, movement)


def test_page_agrees_with_the_facts_files(data):
    for m, d in data["per_month"].items():
        facts = {f["label"]: f["display"] for f in json.loads((ROOT_DIR / "notes" / "facts" / f"{m}.json").read_text())["facts"]}
        assert money(d["kpi"]["closing_mrr"]) == money(facts["Closing MRR"]), m
        assert d["kpi"]["logo_churn"] == facts["Logo churn (customers lost / paying at prior month-end)"], m
        r1 = {r["label"]: r["value"] for r in d["recon"]["r1"]}
        assert r1["Net revenue"] == money(facts["Net revenue"]), m


def test_every_month_shows_its_update_with_each_figure_resolved(data):
    # v2 updates: a headline and 4-6 bullets; every cited figure is hoverable with its fact, and no [F#] tag shows.
    assert sorted(data["notes"]) == data["months"]
    for m, n in data["notes"].items():
        text = (ROOT_DIR / "notes" / "v2" / "output" / f"{m}.md").read_text()
        facts = {f["id"]: f for f in json.loads((ROOT_DIR / "notes" / "v2" / "facts" / f"{m}.json").read_text())["facts"]}
        assert 4 <= len(n["bullets"]) <= 6 and all(b["topic"] and b["means"] for b in n["bullets"]), m
        shown = n["headline"] + "".join(b["what"] + b["means"] for b in n["bullets"])
        assert not re.search(r"\[F\d+\]", shown) and "cus_" not in shown, m
        tips = re.findall(r'class="fig" tabindex="0" data-tip="([^"]*)">([^<]*)</span>', shown)
        cited = [facts[i] for i in re.findall(r"\[(F\d+)\]", text)]
        from notes.checks import EXEMPT, numbers_with_citations
        assert len(tips) == len(numbers_with_citations(text, EXEMPT)), m      # one hoverable figure per number
        labels = {f"{f['label']}: {f['display']}" for f in cited}
        assert all(t.replace("&amp;", "&").replace("&#x27;", "'") in labels for t, _ in tips), m


def test_kpi_signals_and_provision_matrix(data):
    for m, d in data["per_month"].items():
        assert all(s is None or s["tone"] in ("good", "bad", "flat") for s in d["kpi"]["signals"].values()), m
        assert sum(b["expected_loss_cents"] for b in d["buckets"]) == d["allowance"]["close_cents"], m
    assert data["per_month"]["2025-01"]["kpi"]["signals"]["mrr"] is not None     # compared with Dec 2024


def test_cohort_summary(data):
    cs = data["cohorts"]["cohorts"]
    assert len(cs) == 30 and cs[0]["cohort"] == "2024-01"
    assert all(c["logo"][0] <= 100 for c in cs)
    assert len(data["cohorts"]["average"]["logo"]) == 30 and data["cohorts"]["average"]["logo"][0] <= 100


def test_scorecard_events_and_definitions_are_on_the_page(page, data):
    for name, folder in (("v1", ROOT_DIR / "notes"), ("v2", ROOT_DIR / "notes" / "v2")):
        s = data["scorecard"][name]
        assert s["summary"]["notes"] == 18, name
        assert s["freeze"]["prompt_sha256"] == json.loads((folder / "FREEZE.json").read_text())["prompt_sha256"], name
        assert [e["id"] for e in s["events"]] == [f"E{i}" for i in range(1, 9)], name
        card = json.loads((folder / "scorecard.json").read_text())
        assert sum(len(e["signals"]) for e in s["events"]) == len(card["signals"]), name
        caught = sum(e["caught"] for e in s["events"])
        assert f"{caught}/{sum(e['of'] for e in s['events'])}" == card["summary"]["primary_signals_caught"], name
    assert page.count("<details>") == 9
