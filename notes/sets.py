"""The two note sets. Each has its own prompt, freeze, facts, notes, scorecard and review.

v1: the original paragraph notes. Frozen 2026-11-13 and scored; kept unchanged as the published record.
v2: the founder-update format Vandan asked for after reviewing the site on 2026-11-17: a headline, then
    4-6 bullets, each with a "What it means" line, and customers named rather than shown as IDs.
    v2 was written after v1's results were seen. Its prompt changes format only and is not tuned to the
    signals v1 missed; it gets its own dev-seed lint and freeze, and the same evaluator and answer key.
"""
from __future__ import annotations

from pathlib import Path

NOTES = Path(__file__).resolve().parent

SETS = {
    "v1": {"dir": NOTES, "customer_label": "id", "min_words": 120, "max_words": 200, "bullets": False, "exempt": "v1"},
    "v2": {"dir": NOTES / "v2", "customer_label": "name", "min_words": 150, "max_words": 250, "bullets": True, "exempt": "v2"},
}


def paths(name: str) -> dict:
    s = SETS[name]
    d = s["dir"]
    from notes import checks
    return {**s, "exempt_re": checks.EXEMPT_V1 if s["exempt"] == "v1" else checks.EXEMPT, "prompt": d / "prompt.md", "freeze": d / "FREEZE.json", "facts": d / "facts",
            "output": d / "output", "scorecard_json": d / "scorecard.json", "scorecard_md": d / "scorecard.md",
            "review": d / "review.json"}
