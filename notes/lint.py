"""Pre-freeze format lint on dev-seed notes (PLAN §5.3): length, citation syntax, digits only.

It never reads the answer key and never runs on the test seed's notes.

Usage: python -m notes.lint --facts <dev facts dir> --notes <dev output dir>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from notes.checks import format_problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--facts", type=Path, required=True)
    ap.add_argument("--notes", type=Path, required=True)
    args = ap.parse_args(argv)
    if (args.notes.resolve() == (Path(__file__).parent / "output").resolve()):
        sys.exit("The lint is for dev-seed notes only.")
    failed = 0
    for note_path in sorted(args.notes.glob("*.md")):
        facts = json.loads((args.facts / f"{note_path.stem}.json").read_text())
        problems = format_problems(note_path.read_text(), {f["id"] for f in facts["facts"]})
        failed += bool(problems)
        print(f"{note_path.stem}: {'ok' if not problems else '; '.join(problems)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
