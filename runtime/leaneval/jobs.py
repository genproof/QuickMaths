"""Prepare isolated local proof jobs; never launch models or publish proofs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from leaneval.artifacts import git_tree, problem_spec
from leaneval.pilot import REQUESTED_CONFIG
from leaneval.planning import profile

# Small explicit source whitelist. No recursive library reads or reference solutions.
LIBRARY_PATHS = {
    "annals_equiangular_lines_fixed_angle": (
        "Mathlib/LinearAlgebra/Projectivization/Basic.lean",
        "Mathlib/LinearAlgebra/Dimension/Finite.lean",
        "Mathlib/Analysis/InnerProductSpace/Basic.lean",
    ),
    "annals_improved_bounds_sunflower_lemma": (
        "Mathlib/Combinatorics/SetFamily/LYM.lean",
        "Mathlib/Combinatorics/SetFamily/Intersecting.lean",
        "Mathlib/Data/Finset/Card.lean",
    ),
    "annals_unit_conjecture": (
        "Mathlib/Algebra/MonoidAlgebra/Basic.lean",
        "Mathlib/Algebra/MonoidAlgebra/Defs.lean",
        "Mathlib/Algebra/Group/Units/Basic.lean",
    ),
    "dimitrov": (
        "Mathlib/NumberTheory/NumberField/Basic.lean",
        "Mathlib/NumberTheory/NumberField/Embeddings.lean",
        "Mathlib/RingTheory/IntegralClosure/IsIntegral/Basic.lean",
    ),
    "weak_goldbach": (
        "Mathlib/NumberTheory/PrimeCounting.lean",
        "Mathlib/Data/Nat/Prime/Basic.lean",
        "Mathlib/NumberTheory/ArithmeticFunction.lean",
    ),
}

AGENT_INSTRUCTIONS = """# Local assigned LeanEval task

Work only on this assigned task and make substantial proof progress now.
Edit only Submission.lean, Submission/**/*.lean, NOTES.md and research-contract.json (diagnostic metadata only).
Preserve ALL official hole statements, hypotheses and definition contracts. Never edit
trusted files, AGENTS.md, tools, lean, configuration, compiler/feedback infrastructure,
or existing mathematical declarations to weaken the task. Do not introduce sorry or axioms.
Compiler and search (all through the wrapper; never run lean/lake directly):
- ./lean build Submission.Path.Module  compiles one module and its imports (cached, seconds)
- ./lean build Submission              compiles the whole proof
- ./lean check                         readiness check (seconds): no sorry, Submission and the
                                       official statement wrapper build, standard axioms for
                                       every hole; reply READY_FOR_VERIFICATION once it passes
- ./lean check --full                  official lake test (lake comparator in bubblewrap, both kernels;
                                       minutes to over an hour); optional, the controller runs it after READY
- ./lean search REGEX                  ripgrep over Mathlib, lean-pool and the solution bank
                                       (licensed formalizations listed in <workbench>/bank/SOURCES.json)
Output lists errors with goals, sorry locations and similar names for unknown identifiers;
append --json for the full record. Wrapper feedback is advisory; only the controller's final
official comparator AND Nanoda verification establishes a verified proof.
Do not submit, publish, push, use APIs/authentication, inspect credentials, delegate,
spawn agents, or call other models. Stay within this local task and its stopping limits.
Keep NOTES.md short: a HANDOFF at the top (what is proved, what is open with exact
declaration names and types, the next step, rejected routes and why), then the blueprint.
Complete every official hole before claiming readiness.
"""


def _write_json(path: Path, content: dict[str, Any]) -> None:
    path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=LeanEval Controller",
         "-c", "user.email=controller@localhost", "-C", str(repo), *args],
        text=True, stderr=subprocess.PIPE, env=env,
    ).strip()


def library_brief(workbench: Path, problem_id: str) -> dict[str, Any]:
    """Bounded actual library snippets with immutable source identities."""
    package = workbench / "vendor/lean-eval/.lake/packages/mathlib"
    revision = _git(package, "rev-parse", "HEAD")
    benchmark_pin = profile()["benchmark"]["commit"]
    dependencies = json.loads(_git(
        workbench / "vendor/lean-eval", "show", f"{benchmark_pin}:lake-manifest.json",
    ))
    expected = next((entry.get("rev") for entry in dependencies["packages"]
                     if entry["name"].strip("«»") == "mathlib"), None)
    if revision != expected:
        raise ValueError("Mathlib HEAD differs from frozen benchmark dependency pin")
    sources = []
    for name in LIBRARY_PATHS.get(problem_id, ()):
        path = package / name
        if not path.is_file():
            continue
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ValueError("Library source symlinks forbidden")
        # Read the immutable package blob; bind to and compare the actual workspace file.
        source = _git(package, "show", f"{revision}:{name}") + "\n"
        actual = path.read_text(encoding="utf-8")
        if source.rstrip("\n") != actual.rstrip("\n"):
            raise ValueError(f"Modified library source: {name}")
        lines = actual.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith(
            ("def ", "theorem ", "lemma ", "noncomputable def ", "structure ")
        )), 0)
        sources.append({
            "path": name, "package_revision": revision,
            "sha256": hashlib.sha256(actual.encode()).hexdigest(),
            "start_line": start + 1, "end_line": min(start + 72, len(lines)),
            "snippet": "\n".join(lines[start:start + 72]),
        })
    if not sources:
        raise ValueError(f"No whitelisted Mathlib source available for {problem_id}")
    return {"schema_version": 1, "package": "mathlib", "package_revision": revision,
            "sources": sources,
            "selection": "explicit task-specific whitelist; no reference proofs",
            "use": "Check real types with ./lean search before choosing at most two routes"}


def prepare_job(  # noqa: PLR0915
    job: Path, workbench: Path, problem_id: str, variant: str, attempt_id: str, *,
    wall_seconds: int = 1800, token_limit: int = 500000,
    host: str | None = None, agent_root: Path | None = None, controls: bool = False,
) -> dict[str, Any]:
    """Prepare one controller-owned job outside the source tree, without executing Lean."""
    if variant not in {"B0", "B1", "audit"}:
        raise ValueError("Variant must be B0, B1 or audit")
    if not attempt_id.strip():
        raise ValueError("Nonempty attempt ID required")
    for name, value in (("wall_seconds", wall_seconds), ("token_limit", token_limit)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if variant == "audit" and problem_id != "weak_goldbach":
        raise ValueError("The separate librarian audit is assigned to weak_goldbach")
    if controls and (problem_id != "two_plus_two" or variant != "B0"):
        raise ValueError("Controls mode permits only the B0 two_plus_two installation smoke")
    problem = problem_spec(problem_id, controls=controls)
    agent = (agent_root or Path(__file__).resolve().parents[1]).resolve()
    job, workbench = job.expanduser().resolve(), workbench.expanduser().resolve()
    if job.is_relative_to(agent) or job.is_relative_to(workbench):
        raise ValueError("Jobs must be outside the agent tree and trusted workbench")
    if job.exists():
        raise ValueError("Job directory already exists; never overwrite an attempt")
    benchmark_pin = profile()["benchmark"]["commit"]
    original = git_tree(workbench / "vendor/lean-eval", benchmark_pin, problem["workspace"])
    if "Submission.lean" not in original:
        raise ValueError("Pinned workspace is missing Submission.lean")
    for name in original:
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts
                or str(relative) != name):
            raise ValueError("Invalid frozen workspace path")
    brief = library_brief(workbench, problem_id) if variant in {"B1", "audit"} else None
    workspace = job / "workspace"
    workspace.mkdir(parents=True)
    for name, data in original.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    compiler = workspace / ".compiler" / problem_id
    compiler.mkdir(parents=True)
    feedback = workspace / ".feedback"
    feedback.mkdir()
    worker = {
        "transport": "local", "problem_id": problem_id,
        "local_workspace": str(workspace), "remote_workspace": str(compiler),
        "remote_workbench": str(workbench), "remote_agent": str(agent),
        "feedback_dir": str(feedback), "timeout_seconds": 300,
    }
    _write_json(job / "worker.json", worker)
    wrapper = ("#!/bin/bash\nset -euo pipefail\nexport PYTHONPATH=" + shlex.quote(str(agent))
               + "\nexec " + shlex.quote(str(agent / ".venv/bin/python"))
               + " -m leaneval.worker --config " + shlex.quote(str(job / "worker.json"))
               + ' "$@"\n')
    (workspace / "lean").write_text(wrapper)
    (workspace / "lean").chmod(0o755)
    (workspace / "AGENTS.md").write_text(AGENT_INSTRUCTIONS)
    (workspace / ".gitignore").write_text(".compiler/\n.feedback/\n.lake/\n")
    frozen = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(original.items())}
    config = {
        "schema_version": 1, "run_id": attempt_id, "attempt_id": attempt_id,
        "wall_seconds": wall_seconds, "token_limit": token_limit,
        "problem_id": problem_id, "statement_revision": problem["statement_revision"],
        "variant": variant, "host": host or socket.gethostname(),
        "benchmark_commit": benchmark_pin, "source_workspace": problem["workspace"],
        "requested_config": dict(REQUESTED_CONFIG), "effective_config": None,
        "requested_features": {"source_grounded_brief": brief is not None,
                               "versioned_full_feedback": True,
                               "step_references_and_recall": variant != "B0",
                               "checked_prefix": False,
                               "diagnostic_localization": (
                                   "compiler_locations" if variant != "B0" else "plain_feedback"
                               )},
        "enhanced_profile": "B1-v0-brief-handles-diagnostics" if variant == "B1" else None,
        "reference_exposure": "Public accepted panel; no reference solution loaded by job builder",
        "backend": "existing Claude Code subscription", "usd_cost": None,
        "token_enforcement": "polled threshold; overshoot possible",
        "scope": ("installation-smoke" if controls else (
            "separate library-route audit" if variant == "audit" else "local B0/B1 pilot"
        )),
        "controls": controls,
        "included_in_eight_attempts": variant != "audit" and not controls, "frozen_files": frozen,
        "library_brief": brief,
    }
    _write_json(job / "config.json", config)
    prompt = AGENT_INSTRUCTIONS + "\nAssigned frozen problem:\n" + json.dumps(
        problem, ensure_ascii=False, indent=2,
    )
    prompt += (f"\nAttempt {attempt_id}; variant {variant}; wall limit {wall_seconds}s; "
               f"token stopping threshold {token_limit}. "
               "Existing Claude subscription; USD unknown.\n")
    if variant == "audit":
        prompt += ("This is a separately accounted bounded library-route audit, outside the eight "
                   "pilot attempts. Investigate weak_goldbach; propose at most two routes. "
                   "Verify real types via ./lean search and small Lean helpers. Write NOTES.md "
                   "with concrete checked interfaces and blockers. Do not claim solved.\n")
    elif variant == "B0":
        prompt += ("Plan first: read the exact hole statements, search the libraries for the "
                   "main ingredients and write a lemma blueprint in NOTES.md. Then prove the "
                   "lemmas one by one, compiling the module you edit after each change, until "
                   "every hole is closed or the round ends.\n")
    else:
        prompt += ("Use the actual source brief below to choose at most two mathematical routes. "
                   "Start proving now, test the most expensive missing bridge early. Read full "
                   "versioned .feedback records; reference checked steps by ID and artifact hash "
                   "in NOTES.md. After context loss recall the exact saved step and full state; "
                   "recheck any stale revision. This feature bundle includes brief, recall and "
                   "precise compiler locations for the current whole-file source revision. "
                   "A checked prefix is unverified and must not be claimed. This is the "
                   "B1-v0-brief-handles-diagnostics subset; native checked-prefix state "
                   "remains future work.\n")
    if brief is not None:
        prompt += "\nSource-grounded Mathlib brief:\n" + json.dumps(
            brief, ensure_ascii=False, indent=2,
        )
    (job / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "add", "--all")
    _git(workspace, "commit", "--quiet", "--no-verify", "-m", "Frozen local pilot baseline")
    config["baseline_commit"] = _git(workspace, "rev-parse", "HEAD")
    _write_json(job / "config.json", config)
    return {"job": str(job), "workspace": str(workspace), "config": str(job / "config.json"),
            "worker_config": str(job / "worker.json"), "prompt": str(job / "prompt.txt"),
            "baseline_commit": config["baseline_commit"], "run_id": attempt_id,
            "problem_id": problem_id, "variant": variant, "status": "prepared_not_launched"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--workbench", required=True, type=Path)
    parser.add_argument("--problem", required=True)
    parser.add_argument("--variant", required=True, choices=("B0", "B1", "audit"))
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--wall-seconds", type=int, default=1800)
    parser.add_argument("--token-limit", type=int, default=500000)
    parser.add_argument("--host")
    parser.add_argument("--controls", action="store_true")
    args = parser.parse_args(argv)
    result = prepare_job(args.job, args.workbench, args.problem, args.variant, args.attempt_id,
                         wall_seconds=args.wall_seconds, token_limit=args.token_limit,
                         host=args.host, controls=args.controls)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
