"""Freeze the note prompt (PLAN §5.3): record its SHA-256, the model and the CLI version.

Run once, after the dev-seed format lint passes and before any test-seed note is written or
evaluated. It refuses to overwrite an existing freeze.

Usage: python -m notes.freeze
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from notes.write_notes import NOTES, cli_version, load_config, sha256


def main():
    path = NOTES / "FREEZE.json"
    if path.exists():
        sys.exit("notes/FREEZE.json already exists. The prompt is frozen; it can't be re-frozen.")
    cfg = load_config()
    freeze = {"prompt_sha256": sha256(NOTES / "prompt.md"), "model": cfg["model"],
              "cli_version": cli_version(cfg),
              "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(freeze, indent=1) + "\n")
    print(json.dumps(freeze, indent=1))


if __name__ == "__main__":
    main()
