#!/usr/bin/env python3
"""Detect credential-shaped strings before they reach GitHub.

Usage:
  python3 scripts/scan_secrets.py            # scan files tracked or staged in git
  python3 scripts/scan_secrets.py FILE...    # scan the given files

Exit 0 when nothing matches, 1 when something does. A match is reported by
file, line and pattern name only: the matched text is never printed, so the
scanner cannot itself leak what it found.

This is a pattern scan, not a guarantee. It catches the common token shapes
listed below; anything else confidential must stay out of the repository by
policy (core/permissions.md).
"""
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "github-token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),
    "github-fine-grained-token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer-header": re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._~+/-]{20,}"),
}


def find_in_text(text):
    """Return [(line_number, pattern_name)] for every match in text."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS.items():
            if pattern.search(line):
                hits.append((lineno, name))
    return hits


def git_files(root):
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    return [root / p for p in out.splitlines() if p]


def main(argv):
    root = Path(__file__).resolve().parent.parent
    files = [Path(a) for a in argv] if argv else git_files(root)
    found = 0
    for path in files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: out of scope for a text scan
        for lineno, name in find_in_text(text):
            print(f"SECRET? {path}:{lineno}: matches {name}")
            found += 1
    if found:
        print(f"scan-secrets: {found} suspicious line(s). Remove them before committing.")
        return 1
    print(f"scan-secrets: OK ({len(files)} file(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
