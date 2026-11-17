"""Write one founder note per month with `claude -p` (PLAN §5.4).

Each call runs the CLI pinned in notes/config.yaml, from an empty temp folder, with a clean
environment (HOME and PATH only), no tools, no MCP servers and no skills. The prompt and that
month's facts go in on stdin, so the model sees nothing else: not the answer key, not the repo.

Test notes (default):  needs notes/FREEZE.json; the prompt, model and CLI must match it.
Dev-seed notes (--dev): for the pre-freeze format lint only; written outside notes/output.

Usage:
  python -m notes.write_notes [--months 2026-03 ...]
  python -m notes.write_notes --dev --facts <dev facts dir> --out <dev output dir> [--months ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from notes.sets import SETS, paths

ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "notes"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config() -> dict:
    cfg = yaml.safe_load((NOTES / "config.yaml").read_text())
    cfg["cli"] = Path(os.path.expanduser(cfg["cli_path"]))
    return cfg


def clean_env() -> dict:
    """HOME, PATH and the username only. The username lets the CLI find its own login in the Keychain."""
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"}
    for key in ("USER", "LOGNAME"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def cli_version(cfg) -> str:
    if not cfg["cli"].exists():
        sys.exit(f"CLI not found at {cfg['cli']}. The app may have updated it: re-pin notes/config.yaml.")
    out = subprocess.run([str(cfg["cli"]), "--version"], capture_output=True, text=True, env=clean_env(), timeout=60)
    version = out.stdout.split()[0] if out.stdout.split() else ""
    if version != cfg["expected_cli_version"]:
        sys.exit(f"CLI version {version!r} does not match the pinned {cfg['expected_cli_version']!r}.")
    return version


def check_freeze(cfg, prompt_hash: str, version: str, path: Path = None) -> None:
    path = path or NOTES / "FREEZE.json"
    if not path.exists():
        sys.exit(f"{path} is missing: freeze the prompt first (python -m notes.freeze).")
    freeze = json.loads(path.read_text())
    mismatches = [k for k, v in (("prompt_sha256", prompt_hash), ("model", cfg["model"]), ("cli_version", version))
                  if freeze.get(k) != v]
    if mismatches:
        sys.exit(f"Does not match FREEZE.json: {', '.join(mismatches)}.")


def write_note(cfg, prompt: str, facts_path: Path) -> dict:
    stdin = f"{prompt}\n\nFacts file for {facts_path.stem}:\n{facts_path.read_text()}"
    cmd = [str(cfg["cli"]), *cfg["flags"], "--model", cfg["model"]]
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="revenue-bridge-note-") as tmp:
        out = subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=tmp, env=clean_env(), timeout=600)
    try:
        reply = json.loads(out.stdout)
    except json.JSONDecodeError:
        sys.exit(f"{facts_path.stem}: the CLI did not return JSON. stderr: {out.stderr[-500:]}")
    if reply.get("is_error"):
        sys.exit(f"{facts_path.stem}: the CLI returned an error: {reply.get('result')}")
    return {"text": reply["result"].strip(), "seconds": round(time.time() - started, 1),
            "models_reported": sorted((reply.get("modelUsage") or {}).keys()),
            "num_turns": reply.get("num_turns")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", default="v1", choices=sorted(SETS), help="note set (see notes/sets.py)")
    ap.add_argument("--dev", action="store_true", help="dev-seed notes for the pre-freeze lint")
    ap.add_argument("--facts", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--months", nargs="*")
    args = ap.parse_args(argv)

    P = paths(args.set)
    args.facts, args.out = args.facts or P["facts"], args.out or P["output"]
    cfg = load_config()
    prompt_path = P["prompt"]
    prompt_hash = sha256(prompt_path)
    version = cli_version(cfg)
    if args.dev:
        if args.out.resolve() == P["output"].resolve():
            sys.exit("--dev notes must not go to the set's output folder.")
    else:
        check_freeze(cfg, prompt_hash, version, P["freeze"])

    months = args.months or sorted(p.stem for p in args.facts.glob("*.json"))
    args.out.mkdir(parents=True, exist_ok=True)
    for month in months:
        facts_path = args.facts / f"{month}.json"
        note = write_note(cfg, prompt_path.read_text(), facts_path)
        (args.out / f"{month}.md").write_text(note["text"] + "\n")
        meta = {"month": month, "prompt_sha256": prompt_hash, "facts_sha256": sha256(facts_path),
                "cli_version": version, "model_requested": cfg["model"], "models_reported": note["models_reported"],
                "num_turns": note["num_turns"], "seconds": note["seconds"], "dev_seed": args.dev,
                **({"set": args.set} if args.set != "v1" else {}),
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (args.out / f"{month}.meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        print(f"{month}: {len(note['text'].split())} words, {note['seconds']}s, {note['models_reported']}")


if __name__ == "__main__":
    main()
