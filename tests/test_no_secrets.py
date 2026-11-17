"""Nothing that would be committed may contain an API key, a token or the author's personal email."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = re.compile(rb"(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}|whsec_[0-9A-Za-z]{16,}|gh[pousr]_[0-9A-Za-z]{30,}"
                      rb"|github_pat_\w{20,}|@gmail\.com|/Users/[a-z]+/")


def test_no_secrets_in_committable_files():
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.split("\n")
    hits = [f for f in files if f and (ROOT / f).is_file() and PATTERNS.search((ROOT / f).read_bytes())]
    assert hits == []
