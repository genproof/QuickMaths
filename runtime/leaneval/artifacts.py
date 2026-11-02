"""Proof-only bundles from immutable Git objects, and exact content identities."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from leaneval.planning import benchmark, profile

_COMMIT = re.compile(r"[0-9a-f]{40}")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def proof_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (not path.is_absolute() and str(path) == name and ".." not in path.parts
            and (name == "Submission.lean" or (
                len(path.parts) > 1 and path.parts[0] == "Submission" and path.suffix == ".lean"
            )))


def problem_spec(problem_id: str, *, controls: bool = False) -> dict[str, Any]:
    if controls and problem_id == "two_plus_two":
        return {"id": problem_id, "title": "2 + 2 = 4", "statement_revision": 1,
                "workspace": "generated/two_plus_two", "holes": [
            {"name": "two_plus_two_eq_four", "basename": "two_plus_two_eq_four", "kind": "theorem"}
        ]}
    for problem in benchmark()["problems"]:
        if problem["id"] == problem_id:
            return problem
    raise ValueError(f"Problem {problem_id!r} is not in frozen v1 (controls require --controls)")


def git_tree(repo: Path, commit: str, subdirectory: str) -> dict[str, bytes]:
    """Read regular blobs directly; never follow a checked-out symlink or run Git hooks."""
    if not _COMMIT.fullmatch(commit):
        raise ValueError("An exact 40-character Git commit is required")
    directory = PurePosixPath(subdirectory)
    if directory.is_absolute() or ".." in directory.parts or str(directory) != subdirectory:
        raise ValueError("Expected a repository-relative directory")
    ref = commit if subdirectory == "." else f"{commit}:{subdirectory}"
    entries = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-rz", ref])
    result = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        header, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        name = raw_name.decode("utf-8")
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError(f"Git tree contains a non-regular entry: {name}")
        result[name] = subprocess.check_output([
            "git", "-C", str(repo), "cat-file", "blob", oid,
        ])
    return result


@dataclass(frozen=True)
class Bundle:
    manifest: dict[str, Any]
    files: dict[str, bytes]

    @property
    def identity(self) -> str:
        actual = {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}
        if actual != self.manifest.get("files") or not all(map(proof_path, actual)):
            raise ValueError("Bundle contents do not match the manifest")
        return digest(self.manifest)


def export_bundle(
    repo: Path, commit: str, workspace: str, problem_id: str, output: Path,
    *, controls: bool = False,
) -> Bundle:
    problem = problem_spec(problem_id, controls=controls)
    contents = {name: data for name, data in git_tree(repo, commit, workspace).items()
                if proof_path(name)}
    if "Submission.lean" not in contents:
        raise ValueError("Submission.lean is missing from the selected commit")
    manifest = {
        "schema_version": 1, "problem_id": problem_id,
        "statement_revision": problem["statement_revision"], "source_commit": commit,
        "source_workspace": workspace, "benchmark_commit": profile()["benchmark"]["commit"],
        "files": {name: hashlib.sha256(data).hexdigest()
                  for name, data in sorted(contents.items())},
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, data in contents.items():
        target = output / "proof" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return load_bundle(output, controls=controls)


def load_bundle(directory: Path, *, controls: bool = False) -> Bundle:
    if directory.is_symlink() or (directory / "manifest.json").is_symlink():
        raise ValueError("Bundle symlinks are forbidden")
    manifest = json.loads((directory / "manifest.json").read_text())
    required = {"schema_version", "problem_id", "statement_revision", "source_commit",
                "source_workspace", "benchmark_commit", "files"}
    if set(manifest) != required or manifest["schema_version"] != 1:
        raise ValueError("Unknown bundle schema")
    problem = problem_spec(manifest["problem_id"], controls=controls)
    if (manifest["statement_revision"] != problem["statement_revision"]
            or manifest["benchmark_commit"] != profile()["benchmark"]["commit"]):
        raise ValueError("Stale benchmark or statement revision")
    if not _COMMIT.fullmatch(manifest["source_commit"]):
        raise ValueError("Invalid source commit")
    expected = manifest["files"]
    if not isinstance(expected, dict) or "Submission.lean" not in expected:
        raise ValueError("Missing proof file inventory")
    contents = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Bundle symlink: {path.name}")
        if path.is_dir():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative == "manifest.json":
            continue
        if not relative.startswith("proof/") or not proof_path(relative.removeprefix("proof/")):
            raise ValueError(f"Non-proof file in bundle: {relative}")
        name = relative.removeprefix("proof/")
        data = path.read_bytes()
        if expected.get(name) != hashlib.sha256(data).hexdigest():
            raise ValueError(f"Artifact changed: {name}")
        contents[name] = data
    if set(contents) != set(expected):
        raise ValueError("Bundle is missing listed proof files")
    return Bundle(manifest, contents)
