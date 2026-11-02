#!/usr/bin/env python3
"""Refuse secrets, personal data and host-specific details before they enter the repository.

    tools/scrub.py --all [--authors]   every tracked file (CI); --authors also checks commit emails
    tools/scrub.py --history           every blob and commit message reachable from any ref (before a release)
    tools/scrub.py --staged            staged blobs (pre-commit hook, see tools/install-hooks.sh)
    tools/scrub.py PATH ...            the given files

A private denylist (one case-insensitive term per line: host names, account labels, user names,
private repository names) is read from --denylist, $LEH_SCRUB_DENYLIST or
~/.config/leh/scrub-denylist.txt when present. It never lives in the repository.

A line containing the marker `scrub: allow` is skipped (pattern tables and test fixtures only).
Findings name the rule, file and line, never the matched text. Exit status 1 when anything is found.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "scrub: allow"
MAX_BYTES = 1_000_000

LINE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9]{20,}")),  # scrub: allow
    ("langfuse_key", re.compile(r"\b(?:pk|sk)-lf-[A-Za-z0-9-]{8,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]{0,30}://[^/\s:@]{1,256}:[^/\s@]{1,256}@")),
    ("assignment", re.compile(r"(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
                              r"\b[\"']?\s*[:=]\s*[\"'][^\s\"']{8,}[\"']")),
    ("credential_path", re.compile(r"\.private/access|oauth-token|\.netrc\b|\.git-credentials|\bid_(?:rsa|ed25519|ecdsa)\b")),  # scrub: allow
    ("host_path", re.compile(r"(?<![\w.])/(?:home|Users)/[A-Za-z_][\w.-]*|\bubuntu@")),  # scrub: allow
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}")
EMAIL_OK = re.compile(r"(?i)(?:[\w.+-]+@users\.noreply\.github\.com|noreply@github\.com|noreply@anthropic\.com"  # scrub: allow
                      r"|[\w.+-]+@example\.(?:com|org|net))$")  # scrub: allow
IPV4 = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")
IPV4_OK = ("127.", "0.0.0.0", "192.0.2.", "198.51.100.", "203.0.113.", "255.255.255.")
FORBIDDEN_SUFFIXES = (".jsonl", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".token", ".olean",
                      ".tar", ".tgz", ".tar.gz", ".zip")
FORBIDDEN_NAMES = {"participants.json", "credentials.json", ".credentials.json", ".env", "id_rsa", "id_ed25519"}  # scrub: allow
AUTHOR_OK = re.compile(r"(?i)^(?:[\w.+-]+@users\.noreply\.github\.com|noreply@github\.com)$")  # scrub: allow


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int = 0

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}" if self.line else f"{self.path}: {self.rule}"


def load_denylist(path: str | None) -> list[str]:
    candidates = [path, os.environ.get("LEH_SCRUB_DENYLIST"), str(Path.home() / ".config/leh/scrub-denylist.txt")]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            terms = Path(candidate).expanduser().read_text(encoding="utf-8").splitlines()
            return [t.strip().lower() for t in terms if t.strip() and not t.lstrip().startswith("#")]
    return []


def scan_text(path: str, text: str, denylist: list[str]) -> list[Finding]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for rule, pattern in LINE_RULES:
            if pattern.search(line):
                found.append(Finding(rule, path, number))
        if any(not EMAIL_OK.fullmatch(m.group(0)) for m in EMAIL.finditer(line)):
            found.append(Finding("email", path, number))
        for m in IPV4.finditer(line):
            octets = m.group(1).split(".")
            if all(int(o) <= 255 for o in octets) and not m.group(1).startswith(IPV4_OK):
                found.append(Finding("ipv4", path, number))
                break
        lower = line.lower()
        if any(term in lower for term in denylist):
            found.append(Finding("denylist", path, number))
    return found


def scan_blob(path: str, data: bytes, denylist: list[str]) -> list[Finding]:
    name = Path(path).name.lower()
    found = []
    if len(data) > MAX_BYTES:
        found.append(Finding("large_file", path))
    if name in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES):
        found.append(Finding("forbidden_file", path))
    if any(term in path.lower() for term in denylist):
        found.append(Finding("denylist_path", path))
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return found  # refused as a whole, or binary: file rules only
    return found + scan_text(path, data.decode("utf-8", errors="replace"), denylist)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def tracked_files() -> list[str]:
    return [p for p in git("ls-files", "-z").decode().split("\0") if p]


def staged_files() -> list[str]:
    return [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0") if p]


def author_findings() -> list[Finding]:
    emails = set(git("log", "--all", "--format=%ae%n%ce").decode().split())
    return [Finding("commit_email_not_noreply", "<git log>") for e in sorted(emails) if not AUTHOR_OK.fullmatch(e)]


def history_findings(denylist: list[str]) -> list[Finding]:
    """Every distinct blob reachable from any ref, under the first path it appears at, and every commit
    message. A finding names the commit (messages) or the path (blobs); the history itself is unchanged."""
    found: list[Finding] = []
    blobs: dict[str, str] = {}
    for line in git("rev-list", "--objects", "--all").decode().splitlines():
        oid, _, path = line.partition(" ")
        if path:
            blobs.setdefault(oid, path)
    kinds = subprocess.run(["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"], check=True,
                           capture_output=True, input="\n".join(blobs).encode()).stdout.decode().split()
    for oid, kind in zip(kinds[0::2], kinds[1::2], strict=True):
        if kind == "blob":
            found += [Finding(f.rule, f"{blobs[oid]}@{oid[:12]}", f.line)
                      for f in scan_blob(blobs[oid], git("cat-file", "blob", oid), denylist)]
    for commit in git("rev-list", "--all").decode().split():
        found += [Finding(f.rule, f"<commit {commit[:12]} message>", f.line)
                  for f in scan_text("message", git("log", "-1", "--format=%B", commit).decode(errors="replace"),
                                     denylist)]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--authors", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--denylist")
    args = parser.parse_args(argv)
    denylist = load_denylist(args.denylist)
    findings: list[Finding] = []
    if args.staged:
        for path in staged_files():
            findings += scan_blob(path, git("show", f":{path}"), denylist)
    if args.all:
        for path in tracked_files():
            if Path(path).is_file():
                findings += scan_blob(path, Path(path).read_bytes(), denylist)
    for path in args.paths:
        findings += scan_blob(path, Path(path).read_bytes(), denylist)
    if args.authors:
        findings += author_findings()
    if args.history:
        findings += history_findings(denylist)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"scrub: {len(findings)} finding(s); nothing may be committed or pushed until they are removed",
              file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
