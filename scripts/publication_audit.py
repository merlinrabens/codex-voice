#!/usr/bin/env python3
"""Heuristic publication check. Reports filenames and rules, never matched values."""
import argparse
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
PRIVATE_DIRS = {"recordings", "captures", "screenshots", "artifacts", "reports", "backups"}
PRIVATE_FILES = {"auth.json", "credentials.json", ".env"}
PRIVATE_SUFFIXES = {".log", ".jsonl", ".sqlite", ".sqlite3", ".db", ".dmg", ".pkg"}
RULES = {
    "personal-machine-path": re.compile(r"/(?:Users|home)/[^\s/\"']+|[A-Za-z]:\\Users\\[^\s\\]+"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider-token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16})\b"),
    "literal-bearer-token": re.compile(r"\bBearer[ \t]+[A-Za-z0-9_./+=-]{24,}"),
    "credential-url": re.compile(r"https?://[^\s/:@]+:[^\s/@]+@"),
}


def findings(root, private_terms):
    checked = 0
    problems = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            problems.add((relative.as_posix(), "symlink-needs-review"))
            continue
        if not path.is_file():
            continue
        checked += 1
        name = path.name.lower()
        if (any(part.lower() in PRIVATE_DIRS for part in relative.parts)
                or name in PRIVATE_FILES
                or (name.startswith(".env.") and name != ".env.example")
                or path.suffix.lower() in PRIVATE_SUFFIXES):
            problems.add((relative.as_posix(), "private-runtime-file"))
        if any(part in {"cua_node", "SkyComputerUseService", "SkyComputerUseClient"} for part in relative.parts):
            problems.add((relative.as_posix(), "vendored-native-runtime"))
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            problems.add((relative.as_posix(), "unreviewed-binary-or-unreadable-file"))
            continue
        for rule, pattern in RULES.items():
            if pattern.search(content):
                problems.add((relative.as_posix(), rule))
        folded = (relative.as_posix() + "\n" + content).casefold()
        if any(term.casefold() in folded for term in private_terms):
            problems.add((relative.as_posix(), "private-term"))
    return checked, sorted(problems)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-term", action="append", default=[], help="Additional literal term to reject; never printed.")
    args = parser.parse_args()
    checked, problems = findings(ROOT, [term for term in args.private_term if term])
    for filename, rule in problems:
        print(f"{filename}: {rule}")
    if not problems:
        print(f"PASS: {checked} source files checked.")
    return bool(problems)


if __name__ == "__main__":
    raise SystemExit(main())
