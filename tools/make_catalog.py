#!/usr/bin/env python3
"""Regenerate runtime/leaneval/resources/benchmark.json, the problem catalog the runtime reads, from a
checkout of the public LeanEval benchmark at an exact commit.

    tools/make_catalog.py REPO COMMIT [--out FILE] [--set v1]

REPO is a git checkout of https://github.com/leanprover/lean-eval (a blobless clone is enough); nothing is
checked out, every file is read with `git show COMMIT:path`. The catalog lists the members of the set (default
v1, manifests/sets/<set>.toml, including its amendments) that are active at COMMIT, each with its title, statement
revision, workspace, tags, holes (generated/<id>/holes.json) and mathematical source (manifests/problems/<id>.toml).
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "runtime/leaneval/resources/benchmark.json"
GROUP = "formalization-evaluation"


def show(repo: Path, commit: str, path: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"], check=True, capture_output=True,
                          text=True).stdout


def set_members(repo: Path, commit: str, name: str) -> list[str]:
    data = tomllib.loads(show(repo, commit, f"manifests/sets/{name}.toml"))
    members = [m["problem_id"] for m in data.get("members", [])]
    for amendment in data.get("amendments", []):
        members += [m["problem_id"] for m in amendment.get("additions", [])]
    return list(dict.fromkeys(members))


def catalog(repo: Path, commit: str, set_name: str = "v1") -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit("COMMIT must be a full 40-hex commit")
    index = {p["id"]: p for p in json.loads(show(repo, commit, "generated/index.json"))}
    problems = []
    for pid in sorted(set_members(repo, commit, set_name), key=str.lower):
        entry = index.get(pid)
        if not entry or entry.get("status") != "active" or entry.get("group") != GROUP:
            continue
        manifest = tomllib.loads(show(repo, commit, f"manifests/problems/{pid}.toml"))
        holes = json.loads(show(repo, commit, f"{entry['generated_path']}/holes.json"))["holes"]
        problems.append({
            "id": pid, "title": entry["title"], "statement_revision": entry["statement_revision"],
            "status": entry["status"], "workspace": entry["generated_path"], "tags": entry.get("tags", []),
            "holes": holes, "mathematical_source": manifest.get("source"),
            "url": f"https://lean-lang.org/eval/problems/{pid}/"})
    return {"schema_version": 1, "generated_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "benchmark_commit": commit, "scope": set_name, "group": GROUP,
            "sources": [f"https://github.com/leanprover/lean-eval/blob/{commit}/manifests/sets/{set_name}.toml",
                        f"https://github.com/leanprover/lean-eval/blob/{commit}/generated/index.json"],
            "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("commit")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--set", default="v1", dest="set_name")
    args = parser.parse_args(argv)
    data = catalog(args.repo, args.commit, args.set_name)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(data['problems'])} problems at {args.commit[:12]} -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
