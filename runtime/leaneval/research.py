"""Research rollout r1: bounded environment checks and source-bound handoffs.

This module creates advisory evidence only. It never grants proof acceptance,
changes a benchmark, or changes the authorized model/budget/verification policy.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import shutil
import subprocess
import shlex
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

REVISION = "lrh-20261011-r1"
# lrh: upstream r6 protocol (45 lines of bookkeeping: guard/generate receipts, cost plans,
# contracts) replaced by the working rules our passing rounds followed.
PROTOCOL = """
Working protocol (lrh-20261011-r1):
- Plan before editing: read the exact hole statements and search the libraries for the main
  ingredients, then write the lemma chain from library results to the holes in NOTES.md.
- Prefer the shortest route the pinned libraries support; prove only the special case the
  hole needs. Keep modules focused (a few hundred lines); adding modules is cheap.
- Compile the module you edit after each change. A third identical failure on unchanged
  source is refused: change the proof or test something else.
- Keep the HANDOFF at the top of NOTES.md current (proved / open with exact names and
  types / next step / rejected routes). Inherited notes are claims until rechecked.
- budget_blocked: write the HANDOFF and stop. environment_circuit: the environment is
  broken, not the proof; run ./lean preflight after it is repaired.
"""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def preflight(job: Path, workbench: Path) -> dict[str, Any]:
    """Check installed toolchain and a writable build overlay before model spend.

    This is an Init smoke, not a proof or full import-closure verification. Shared
    dependency compilation is left to the existing pinned verifier.
    """
    workspace = job / "workspace"
    started = time.monotonic()
    receipt: dict[str, Any] = {"revision": REVISION, "accepted": False,
                               "scope": "toolchain-and-writable-overlay",
                               "started_at": time.time(), "status": "environment_error"}
    ticket = None
    compiler_started = None
    try:
        expected = (workbench / "vendor/lean-eval/lean-toolchain").read_text().strip()
        actual = (workspace / "lean-toolchain").read_text().strip()
        if actual != expected:
            raise ValueError("workspace toolchain differs from pinned workbench")
        receipt["toolchain"] = actual
        receipt["toolchain_sha256"] = sha(workspace / "lean-toolchain")
        if shutil.disk_usage(workspace).free < 512 * 1024 * 1024:
            raise ValueError("less than 512 MiB free for build overlay")
        env = dict(os.environ)
        env["PATH"] = str(Path.home() / ".elan/bin") + os.pathsep + env.get("PATH", "")
        lean = shutil.which("lean", path=env["PATH"])
        if lean is None:
            raise ValueError("Lean executable missing")
        # elan resolves the exact toolchain using this cwd; it must already exist.
        toolchains = Path.home() / ".elan/toolchains"
        installed = toolchains / actual.replace("/", "--").replace(":", "---")
        if not (installed / "bin/lean").is_file():
            raise ValueError("pinned Lean toolchain is not installed")
        from leaneval import lineage
        ticket = lineage.compiler_reservation(job, "init-preflight/" + uuid.uuid4().hex, 30)
        compiler_started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix=".research-smoke-", dir=workspace) as tmp:
            source = Path(tmp) / "Smoke.lean"
            source.write_text("import Init\nexample : (1 : Nat) = 1 := rfl\n")
            result = subprocess.run([lean, str(source), "-o", str(Path(tmp) / "Smoke.olean")],
                                    cwd=workspace, env=env, capture_output=True,
                                    text=True, timeout=min(30, ticket['seconds']) if ticket else 30, check=False)
            receipt["exit_code"] = result.returncode
            if result.returncode:
                raise ValueError("pinned Init smoke failed: " + result.stderr[-1000:])
        receipt["status"] = "ready"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt["error"] = str(exc)
    if ticket is not None:
        lineage.compiler_finished(ticket, time.monotonic() - compiler_started)
    receipt["wall_seconds"] = round(time.monotonic() - started, 6)
    atomic(job / "research-preflight.json", receipt)
    if receipt["status"] != "ready":
        raise ValueError("research preflight environment_error: " + receipt.get("error", "unknown"))
    return receipt


def prepare(job: Path, workbench: Path, config: dict[str, Any]) -> str:
    from leaneval.preparation import Session
    with Session(job, config) as progress:
        return _prepare(job, workbench, config, progress)


def _prepare(job: Path, workbench: Path, config: dict[str, Any], progress) -> str:
    from leaneval import frontier, lineage
    progress.phase("source_admission")
    # Admission precedes environment work and any model reservation.
    frontier.admission(job, config, source_only=True)
    progress.phase("lineage_budget")
    shared_budget = lineage.prepare(job, config)
    progress.phase("init_preflight")
    preflight(job, workbench)
    progress.phase("environment_preflight")
    worker_config = job / "worker.json"
    if worker_config.is_file():
        from leaneval import worker
        cfg = worker.load_config(worker_config)
        cfg['timeout_seconds'] = min(cfg['timeout_seconds'], 180)
        result = worker.feedback(cfg, 'preflight')
        if result.get('phase') == 'environment_custom_targets' and (
            'permission denied' in (result.get('stdout', '') + result.get('stderr', '')).lower()
        ):
            repair = worker.feedback(cfg, 'repair')
            atomic(job / 'research-environment-repair.json', repair)
            if repair['outcome_class'] == 'check_succeeded':
                result = worker.feedback(cfg, 'preflight')
        atomic(job / 'research-environment.json', result)
        if result['outcome_class'] == 'budget_blocked':
            raise lineage.RuntimeBlocked(result.get('stderr', 'Lineage budget blocked'))
        if result['outcome_class'] != 'check_succeeded':
            raise ValueError('Environment admission failed before model spend: ' +
                             result.get('stderr', '')[-1500:])
    progress.phase("workspace_wrappers")
    workspace = job / "workspace"
    if (workspace / 'inc').is_file() and (workspace / '.incremental').is_dir():
        wrapper = workspace / 'inc'
        if not (job / 'incremental-wrapper-before-r6').exists():
            shutil.copyfile(wrapper, job / 'incremental-wrapper-before-r6')
        runtime = Path(__file__).resolve().parents[1]
        wrapper.write_text('#!/bin/sh\nexec env LEANEVAL_RESEARCH_JOB=' + shlex.quote(str(job)) +
            ' PYTHONPATH=' + shlex.quote(str(runtime)) + ' ' + shlex.quote(str(runtime / '.venv/bin/python')) +
            ' -B -m leaneval.incremental "$@"\n')
        wrapper.chmod(0o755)
    progress.phase("source_inventory")
    sources = [workspace / "Submission.lean"]
    if (workspace / "Submission").is_dir():
        sources.extend((workspace / "Submission").rglob("*.lean"))
    inventory = {str(p.relative_to(workspace)): sha(p) for p in sorted(sources)
                 if p.is_file() and not p.is_symlink()}
    artifact = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
    metadata = {"revision": REVISION, "run_id": config.get("run_id"),
                "attempt_id": config.get("attempt_id"), "problem_id": config.get("problem_id"),
                "campaign_id": config.get("campaign_id"), "host": config.get("host"),
                "benchmark_commit": config.get("benchmark_commit"),
                "statement_revision": config.get("statement_revision"),
                "parent": config.get("continuation_provenance"),
                "policy": config.get("requested_config"),
                "wall_seconds": config.get("wall_seconds"), "token_limit": config.get("token_limit"),
                "initial_source_digest": artifact, "source_files": inventory,
                "protocol_sha256": hashlib.sha256(PROTOCOL.encode()).hexdigest(),
                "runtime_sha256": sha(Path(__file__).with_name("agent_runner.py")),
                "research_sha256": sha(Path(__file__)), "accepted": False}
    if shared_budget is not None:
        metadata['lineage'] = shared_budget.snapshot()
    atomic(job / "research-identity.json", metadata)
    python = Path(__file__).resolve().parents[1] / ".venv/bin/python"
    module_root = Path(__file__).resolve().parents[1]
    wrapper = ("#!/bin/sh\nexec env LEANEVAL_RESEARCH_JOB=" + shlex.quote(str(job)) + " PYTHONPATH=" + shlex.quote(str(module_root))
               + " " + shlex.quote(str(python)) + " -B -m leaneval.research \"$@\"\n")
    command = workspace / "research"
    command.write_text(wrapper)
    command.chmod(0o755)
    progress.phase("continuation_material")
    parent = (config.get("continuation_provenance") or {}).get("parent_job")
    if isinstance(parent, str):
        source = Path(parent) / "workspace/research-contract.json"
        if source.is_file() and source.stat().st_size <= 32768:
            inherited_contract = source.read_text()
            (workspace / "research-contract.prev.json").write_text(inherited_contract)
    # Explicitly bounded parent handoff; the source stays on disk, never summarized as a proof.
    inherited = workspace / "NOTES.prev.md"
    brief = ""
    if inherited.is_file() and not (job / "research-inherited.json").is_file():
        text = inherited.read_text(errors="replace")
        brief = ("\nInherited handoff excerpt (unverified; read the cited sources):\n"
                 + text[:6000] + "\n")
    if shared_budget is not None:
        state = shared_budget.snapshot()
        brief += ('\nShared lineage stopping limits for all parents and continuations: '
                  + json.dumps(state['policy']['limits']) + '\nCurrent job reservation: '
                  + json.dumps(shared_budget.member(job)['reserved']) + '\n')
    return PROTOCOL + brief + contract_prompt(workspace, previous=True) + frontier.prepare(job, workbench, config, progress=progress)


def contract_prompt(workspace: Path, *, previous: bool = False) -> str:
    path = workspace / ("research-contract.prev.json" if previous else "research-contract.json")
    if not path.is_file():
        return ""
    try:
        if path.stat().st_size > 32768:
            raise ValueError("contract exceeds 32 KiB")
        value = json.loads(path.read_text())
        fields = ("target", "remaining_type", "owner", "next_check")
        if not all(isinstance(value.get(key), str) and value[key].strip() for key in fields):
            raise ValueError("contract needs target, remaining_type, owner, next_check")
        prerequisites = value.get("prerequisites", [])
        if not isinstance(prerequisites, list) or len(prerequisites) > 100:
            raise ValueError("invalid prerequisite list")
        checked = []
        for item in prerequisites:
            file = workspace / item["file"]
            if not file.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("prerequisite outside workspace")
            checked.append({"file": item["file"], "source_matches":
                            file.is_file() and sha(file) == item["sha256"]})
        return ("\nResidual contract (worker claims; hashes are source identity, not proof):\n"
                + json.dumps({**{k: value[k] for k in fields}, "prerequisites": checked}) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "\nResidual contract invalid; repair it before relying on it: " + str(exc) + "\n"


def residual_prompt(workspace: Path) -> str:
    from leaneval import frontier
    frontier_note = frontier.nudge(workspace.parent)
    notes = workspace / "NOTES.md"
    text = notes.read_text(errors="replace")[:4000] if notes.is_file() else "No handoff recorded."
    return ("\nUse a concrete residual contract for this continuation: name the missing "
            "declaration/type and owner, then perform one fresh test that could disprove "
            "your route. Update the handoff with source/check evidence.\nCurrent notes "
            "excerpt (claims, not verification):\n" + text + "\n" + contract_prompt(workspace) + frontier_note)


def generation_guard(file: Path, *, expected: str | None = None,
                     contains: list[str] | None = None, absent: list[str] | None = None,
                     inputs: list[Path] | None = None) -> dict[str, Any]:
    data = file.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8")
    errors = []
    if expected is not None and actual != expected:
        errors.append("output hash mismatch")
    if not any((expected, contains, absent)):
        errors.append("an expected hash or targeted content assertion is required")
    errors.extend("missing expected content: " + s for s in contains or [] if s not in text)
    errors.extend("unexpected content: " + s for s in absent or [] if s in text)
    return {"revision": REVISION, "file": str(file), "sha256": actual,
            "inputs": {str(p): sha(p) for p in inputs or []},
            "status": "guard_failed" if errors else "guard_passed",
            "errors": errors, "accepted": False,
            "scope": "syntax-and-byte-identity; Lean verification still required"}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == 'cost':
        from leaneval import worker
        job = Path(os.environ.get("LEANEVAL_RESEARCH_JOB", str(Path.cwd().resolve().parent)))
        result = worker.feedback(worker.load_config(job / 'worker.json'), 'cost', sys.argv[2] if len(sys.argv) > 2 else '')
        print(json.dumps(result, sort_keys=True))
        return int(result.get('exitcode') != 0)
    if len(sys.argv) > 1 and sys.argv[1] == 'generate':
        from leaneval import generation
        job = Path(os.environ.get("LEANEVAL_RESEARCH_JOB", str(Path.cwd().resolve().parent)))
        return generation.main(job / 'workspace', sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in {'capture', 'frontier', 'goals'}:
        from leaneval import frontier
        return frontier.main(Path(os.environ.get("LEANEVAL_RESEARCH_JOB", str(Path.cwd().resolve().parent))), sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    guard = commands.add_parser("guard", help="check generated/received bytes before compilation")
    guard.add_argument("file", type=Path)
    guard.add_argument("--sha256")
    guard.add_argument("--contains", action="append", default=[])
    guard.add_argument("--absent", action="append", default=[])
    guard.add_argument("--input", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        result = generation_guard(args.file, expected=args.sha256, contains=args.contains,
                                  absent=args.absent, inputs=args.input)
    except (OSError, UnicodeError) as exc:
        print(json.dumps({"status": "guard_failed", "accepted": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return int(result["status"] != "guard_passed")


if __name__ == "__main__":
    sys.exit(main())
