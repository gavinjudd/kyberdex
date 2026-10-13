"""Reject common credential patterns and private files without printing values.

This is a release hygiene check, not a guarantee that every possible secret can
be detected. Runtime databases and subject artwork do not belong in this repo.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE" + r" KEY-----"),
    "GitHub token": re.compile(r"(?:gh[pousr]_)[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}"),
    "AWS access key": re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "Discord token": re.compile(r"[A-Za-z0-9_-]{23,30}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
    "Discord webhook": re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
}
PRIVATE_PARTS = {"state", "backups", "work", ".venv", "__pycache__", ".cache"}

def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    failures = []
    for name in filter(None, paths):
        path = Path(name)
        if (PRIVATE_PARTS.intersection(path.parts)
                or path.name.startswith(".env") and path.name != ".env.example"
                or path.suffix in {".sqlite", ".sqlite3", ".db", ".log", ".pem", ".key"}
                or name.startswith("assets/art/") and path.name != "README.md"):
            failures.append((name, "private file type"))
        data = (ROOT / path).read_bytes()
        if b"\0" in data:
            continue
        content = data.decode("utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                failures.append((name, label))
    for name, label in failures:
        print(f"{name}: {label}")
    if failures:
        return 1
    print(f"Publication check passed for {len([p for p in paths if p])} tracked files.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
