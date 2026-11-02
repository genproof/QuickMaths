#!/usr/bin/env python3
"""The solution bank: licensed Lean formalizations at exact commits, searchable by every agent.

Usage: python3.12 bank.py [--workbench DIR] add NAME URL COMMIT [--license SPDX] [--note TEXT]
       python3.12 bank.py [--workbench DIR] list | check | remove NAME

The bank lives in the workbench (default $LEANEVAL_WORKBENCH, else ~/projects/lean-eval-workbench) as
bank/<NAME>/, one git checkout per source at its commit, and bank/SOURCES.json, the record of each source
(url, commit, licence, Lean file count, note). The agents' sandbox mounts the workbench read-only, and
`./lean search QUERY` searches the bank next to Mathlib and lean-pool, so a lane or an architect finds
earlier formalizations of the step it works on (the ARCHITECT contract's `Reuse:` field names them).

Only sources under a licence of the profile's source policy (resources/profile.toml
[source_policy].vendored_code_licenses, Apache-2.0 and MIT by default) are added. The licence is read
from the checkout's LICENSE/COPYING file; --license names it when the file is not recognised, and must
still be one the policy allows. Copied code keeps its licence header and is recorded in NOTES.md, as the
agent brief says. `check` verifies every checkout is at its commit and unmodified (the worker searches
whatever is there, so check after any manual change); `remove` deletes one source.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1] / "leaneval" / "resources"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
COMMIT = re.compile(r"[0-9a-f]{40}")
RESERVED = {"sources.json", "sources.tmp", ".lock"}
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING", "LICENSE-APACHE", "LICENSE-MIT")
SIGNATURES = (("Apache-2.0", re.compile(r"Apache License,?\s+Version 2\.0", re.I)),
              ("MIT", re.compile(r"Permission is hereby granted, free of charge, to any person", re.I)))


class Refused(Exception):
    pass


def allowed_licenses() -> list[str]:
    profile = tomllib.loads((RESOURCES / "profile.toml").read_text())
    return list(profile.get("source_policy", {}).get("vendored_code_licenses", ["Apache-2.0", "MIT"]))


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def license_file(folder: Path) -> Path | None:
    return next((folder / name for name in LICENSE_FILES if (folder / name).is_file()), None)


def detect_license(folder: Path) -> str | None:
    for name in LICENSE_FILES:
        path = folder / name
        if path.is_file():
            text = path.read_text(errors="replace")[:20000]
            for spdx, pattern in SIGNATURES:
                if pattern.search(text):
                    return spdx
    return None


class Bank:
    def __init__(self, workbench: Path):
        self.root = workbench / "bank"
        self.record = self.root / "SOURCES.json"

    def sources(self) -> list[dict]:
        if not self.record.is_file():
            return []
        return json.loads(self.record.read_text()).get("sources", [])

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def save(self, sources: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.record.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema_version": 1, "sources": sorted(sources, key=lambda s: s["name"])},
                                  indent=2) + "\n")
        tmp.replace(self.record)

    def add(self, name: str, url: str, commit: str, license_: str | None, note: str) -> dict:
        if not NAME.fullmatch(name) or name.lower() in RESERVED:
            raise Refused(f"source name {name!r}: letters, digits, '_', '.', '-' (not a reserved name)")
        if not COMMIT.fullmatch(commit):
            raise Refused("COMMIT must be a full 40-hex commit")
        with self.lock():
            return self._add(name, url, commit, license_, note)

    def _add(self, name: str, url: str, commit: str, license_: str | None, note: str) -> dict:
        if any(s["name"] == name for s in self.sources()):
            raise Refused(f"{name} is already in the bank; remove it first")
        folder = self.root / name
        if folder.exists():
            raise Refused(f"{folder} exists but is not recorded; move it away")
        self.root.mkdir(parents=True, exist_ok=True)
        partial = self.root / f".{name}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        try:
            git("clone", "--quiet", "--no-checkout", "--", url, str(partial))
            git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit, cwd=partial)
            if git("rev-parse", "HEAD", cwd=partial) != commit:
                raise Refused(f"{url} has no commit {commit}")
            if license_file(partial) is None:
                raise Refused("the source has no LICENSE or COPYING file at that commit")
            found = detect_license(partial)
            spdx = license_ or found
            if found and license_ and found != license_:
                raise Refused(f"the checkout's licence file is {found}, not {license_}")
            if spdx not in allowed_licenses():
                raise Refused(f"licence {spdx or 'not recognised'}: the source policy allows {allowed_licenses()}; "
                              "pass --license only for a licence file this tool does not recognise")
            shutil.rmtree(partial / ".git" / "hooks", ignore_errors=True)
            partial.rename(folder)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        entry = {"name": name, "url": url, "commit": commit, "license": spdx, "license_detected": bool(found),
                 "lean_files": sum(1 for _ in folder.rglob("*.lean")), "note": note,
                 "added_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.save([*self.sources(), entry])
        return entry

    def check(self) -> list[str]:
        problems = []
        for source in self.sources():
            folder = self.root / source["name"]
            if not (folder / ".git").exists():
                problems.append(f"{source['name']}: checkout missing")
                continue
            head = git("rev-parse", "HEAD", cwd=folder)
            if head != source["commit"]:
                problems.append(f"{source['name']}: HEAD {head[:12]} is not {source['commit'][:12]}")
            if git("status", "--porcelain", cwd=folder):
                problems.append(f"{source['name']}: modified files")
        return problems

    def remove(self, name: str) -> None:
        with self.lock():
            self._remove(name)

    def _remove(self, name: str) -> None:
        sources = self.sources()
        if not any(s["name"] == name for s in sources):
            raise Refused(f"{name} is not in the bank")
        shutil.rmtree(self.root / name, ignore_errors=True)
        self.save([s for s in sources if s["name"] != name])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workbench", type=Path,
                        default=Path(os.environ.get("LEANEVAL_WORKBENCH") or Path.home() / "projects/lean-eval-workbench"))
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("url")
    add.add_argument("commit")
    add.add_argument("--license", dest="license_")
    add.add_argument("--note", default="")
    sub.add_parser("list")
    sub.add_parser("check")
    remove = sub.add_parser("remove")
    remove.add_argument("name")
    args = parser.parse_args(argv)
    bank = Bank(args.workbench.expanduser().absolute())
    try:
        if args.action == "add":
            entry = bank.add(args.name, args.url, args.commit, args.license_, args.note)
            print(f"added {entry['name']} at {entry['commit'][:12]} ({entry['license']}, {entry['lean_files']} Lean files)")
        elif args.action == "list":
            for s in bank.sources():
                print(f"{s['name']:24s} {s['commit'][:12]} {s['license']:10s} {s['lean_files']:6d} files  {s['url']}"
                      + (f"  # {s['note']}" if s.get("note") else ""))
        elif args.action == "check":
            problems = bank.check()
            print("\n".join(problems) if problems else f"ok: {len(bank.sources())} source(s) at their commits")
            return 1 if problems else 0
        else:
            bank.remove(args.name)
            print(f"removed {args.name}")
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"failed: {' '.join(map(str, exc.cmd))}: {(exc.stderr or '').strip()[-300:]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
