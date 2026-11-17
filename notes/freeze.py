"""Freeze the note prompt (PLAN §5.3): record its SHA-256, the model and the CLI version.

Run once, after the dev-seed format lint passes and before any test-seed note is written or
evaluated. It refuses to overwrite an existing freeze.

Usage: python -m notes.freeze
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import argparse

from notes.sets import SETS, paths
from notes.write_notes import cli_version, load_config, sha256


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", default="v1", choices=sorted(SETS))
    P = paths(ap.parse_args(argv).set)
    path = P["freeze"]
    if path.exists():
        sys.exit(f"{path} already exists. The prompt is frozen; it can't be re-frozen.")
    cfg = load_config()
    freeze = {"prompt_sha256": sha256(P["prompt"]), "model": cfg["model"],
              "cli_version": cli_version(cfg),
              "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(freeze, indent=1) + "\n")
    print(json.dumps(freeze, indent=1))


if __name__ == "__main__":
    main()
