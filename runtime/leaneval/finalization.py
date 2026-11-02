"""Controller capture and official verification, independent of model conclusions."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from leaneval.artifacts import digest, export_bundle, file_digest, proof_path
from leaneval.runtime import RunLedger, RuntimeBlocked
from leaneval.verification_retry import RetryBlocked
from leaneval.verifier import accept_receipt, atomic_json, verify


# Stops whose usage is fully observed from the native stream and whose processes the
# supervisor reaped; the official verifier, not this set, decides proof acceptance.
CONTROLLED_STOP_REASONS = frozenset({
    "token_limit", "wall_limit", "api_error", "external_stop", "authentication_failure",
    "credential_access",  # the tripwire stopped the CLI; the round's files are kept, the runner halts
})


def _read_regular(root: Path, name: str) -> bytes:
    """Read beneath an anchored directory with no symlink traversal at any component."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
        raise ValueError(f"Invalid candidate path: {name}")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"Nonregular candidate file: {name}")
            return stream.read()
    finally:
        os.close(directory)


def _capture(workspace: Path, config: dict[str, Any]) -> dict[str, bytes]:
    frozen = config.get("frozen_files")
    if not isinstance(frozen, dict) or not frozen:
        raise ValueError("Controller config requires frozen file hashes")
    for name, expected in frozen.items():
        if proof_path(name):
            continue
        if hashlib.sha256(_read_regular(workspace, name)).hexdigest() != expected:
            raise ValueError(f"Modified trusted source file: {name}")
    paths = ["Submission.lean"]
    submission = workspace / "Submission"
    if submission.is_symlink():
        raise ValueError("Submission directory must not be a symlink")
    if submission.exists():
        for path in submission.rglob("*"):
            if path.is_symlink():
                raise ValueError("Submission tree contains a symlink")
            if path.is_file():
                name = path.relative_to(workspace).as_posix()
                if proof_path(name):
                    paths.append(name)
    contents = {name: _read_regular(workspace, name) for name in sorted(paths)}
    from leaneval import frontier
    frontier.enforce_scope(workspace.parent, {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()})
    from leaneval.generation import validate
    validate(workspace, contents)
    return contents


def _git_snapshot(repo: Path, contents: dict[str, bytes]) -> str:
    """Fresh controller Git objects; candidate hooks, index and config are never read."""
    repo.mkdir(mode=0o700)
    for name, data in contents.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    prefix = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
              "-c", "core.fsmonitor=false", "-c", "user.name=LeanEval controller",
              "-c", "user.email=controller@localhost", "-c", "commit.gpgsign=false"]
    for arguments in (["init", "--quiet", "--template="], ["add", "--", "."],
                      ["commit", "--quiet", "-m", "Capture candidate proof"]):
        subprocess.run([*prefix, *arguments], env=env, check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
    return subprocess.check_output([*prefix, "rev-parse", "HEAD"], env=env, text=True).strip()


def _absent(pid: int, *, group: bool = False) -> bool:
    if type(pid) is not int or pid <= 1:
        raise ValueError("Missing trustworthy child process identity")
    try:
        (os.killpg if group else os.kill)(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


_PROC_SCAN = r"""
import json, os, pathlib, sys
pid, expected_uid = sys.argv[1:]
if not pid.isdecimal() or not expected_uid.isdecimal():
    raise ValueError("invalid process scan identity")
proc = pathlib.Path("/proc") / pid
try:
    rows = (proc / "status").read_text().splitlines()
    uid = next(row for row in rows if row.startswith("Uid:")).split()[1]
    if uid != expected_uid:
        print(json.dumps({"pid": int(pid), "same_uid": False, "targets": []}))
    else:
        targets = []
        for path in [proc / "cwd", *(proc / "fd").iterdir()]:
            try:
                targets.append(os.readlink(path))
            except FileNotFoundError:
                pass
        print(json.dumps({"pid": int(pid), "same_uid": True, "targets": targets}))
except (FileNotFoundError, ProcessLookupError):
    print(json.dumps({"pid": int(pid), "disappeared": True, "targets": []}))
"""


def _privileged_process_targets(pid: str) -> list[str]:
    """Isolated fixed read-only helper for nondumpable same-user services."""
    try:
        output = subprocess.check_output(
            ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", _PROC_SCAN,
             pid, str(os.getuid())], timeout=5, stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        evidence = json.loads(output)
        targets = evidence["targets"]
        if (evidence.get("pid") != int(pid) or not isinstance(targets, list) or
                not all(isinstance(target, str) for target in targets) or
                not (evidence.get("same_uid") is True or
                     evidence.get("same_uid") is False or evidence.get("disappeared") is True)):
            raise ValueError("invalid privileged process scan response")
        if evidence.get("same_uid") is False and targets:
            raise ValueError("different-UID process returned target metadata")
        return targets
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        message = "Read-only privileged process scan could not confirm writer absence"
        raise RuntimeBlocked(message) from exc


def _proc_root() -> Path:
    return Path("/proc")


def _assert_workspace_quiet(workspace: Path) -> None:
    """Linux controller check, including compiler workers in separate sessions."""
    proc = _proc_root()
    if not (proc / "self/status").is_file():
        raise RuntimeBlocked("Linux /proc visibility required to confirm writer absence")
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            rows = (process / "status").read_text().splitlines()
            uid = next(row for row in rows if row.startswith("Uid:")).split()[1]
            if int(uid) != os.getuid():
                continue
            targets = []
            for path in [process / "cwd", *(process / "fd").iterdir()]:
                try:
                    targets.append(os.readlink(path))
                except FileNotFoundError:
                    continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            targets = _privileged_process_targets(process.name)
        except StopIteration as exc:
            raise RuntimeBlocked("Cannot establish candidate process ownership") from exc
        for target in targets:
            linked = Path(target.removesuffix(" (deleted)"))
            if linked.is_absolute() and linked.is_relative_to(workspace):
                raise RuntimeBlocked(f"Candidate workspace still referenced by PID {process.name}")


def _finished_invocation(
    job: Path, config: dict[str, Any], *, allow_controlled_stop: bool = False,
    require_quiescence: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_path = job / "ledger.sqlite3"
    if not ledger_path.is_file():
        raise RuntimeBlocked("job has no controller invocation ledger")
    ledger = RunLedger(ledger_path)
    state = ledger.snapshot()
    if state["identity"].get("config") != config:
        raise ValueError("Controller config differs from invocation identity")
    results = [event["data"] for event in ledger.events() if event["kind"] == "result"]
    if not results or not state["attempts"]:
        raise RuntimeBlocked("no terminal controller invocation result")
    result = results[-1]
    final = result["input"]
    if final["generation"] != state["generation"]:
        raise RuntimeBlocked("no normally completed current invocation")
    settled = all(a["status"] == "settled" for a in state["attempts"].values())
    normal = settled and result["accepted"] and final["result"].get("exit_code") == 0
    controlled = (allow_controlled_stop and final["ambiguous"] and
                  final["result"].get("reason") in CONTROLLED_STOP_REASONS and
                  type(final["result"].get("exit_code")) is int)
    if not normal and not controlled:
        raise RuntimeBlocked("invocation is active or has unresolved incurred usage")
    evidence: dict[str, Any] = {"billing_status": "settled" if settled else "unresolved",
                                "controlled_stop": controlled,
                                "unresolved_attempts": [
                                    {"attempt_id": name, **attempt}
                                    for name, attempt in state["attempts"].items()
                                    if attempt["status"] != "settled"
                                ]}
    if controlled or require_quiescence:
        checkpoint = ledger.resume()
        pid = checkpoint.content.get("pid") if checkpoint else None
        # Kernel probes fail closed on PID reuse and lack of process visibility.
        if not _absent(pid) or not _absent(pid, group=True):
            raise RuntimeBlocked("child PID/process group has not been confirmed absent")
        _assert_workspace_quiet(job / "workspace")
        evidence["quiescence"] = {"pid": pid, "process_group": pid, "pid_absent": True,
                                  "group_absent": True, "checked_at": time.time(),
                                  "basis": "terminal_result+kernel_absence+same_uid_proc_scan",
                                  }
    from leaneval import lineage
    usage = state["accounted"]
    if controlled and evidence.get("quiescence"):
        from leaneval.usage_resolution import resolve_local_time
        usage, resolution = resolve_local_time(job, state, final, evidence["quiescence"])
        # Existing finalizations keep their previously committed accounting.
        reference = lineage.reference(job)
        member = reference[0].member(job) if reference else None
        if member and member["closed"]:
            usage = dict(state["accounted"], tokens=member["spent"]["tokens"],
                         wall_seconds=member["spent"]["model_seconds"])
            resolution = {"status": "retained_existing_settlement",
                          "scope": "Prior immutable lineage settlement is not rewritten"}
        evidence["local_active_time_resolution"] = resolution
    lineage.settle(job, usage)
    return usage, evidence


def _snapshot_locked(
    job: Path, workspace: Path, runs: Path, config: dict[str, Any], *,
    allow_controlled_stop: bool, require_quiescence: bool,
) -> dict[str, Any]:
    usage, evidence = _finished_invocation(
        job, config, allow_controlled_stop=allow_controlled_stop,
        require_quiescence=require_quiescence,
    )
    controls = config.get("controls", False)
    if type(controls) is not bool:
        raise ValueError("controls must be a boolean")
    root = runs / "finalizations"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    capture = Path(tempfile.mkdtemp(prefix="capture-", dir=root))
    contents = _capture(workspace, config)
    commit = _git_snapshot(capture / "input", contents)
    bundle_path = capture / "bundle"
    bundle = export_bundle(capture / "input", commit, ".", config["problem_id"],
                           bundle_path, controls=controls)
    snapshot = {"schema_version": 1, "run_id": config["run_id"],
                "problem_id": config["problem_id"], "controls": controls,
                "variant": config.get("variant", "B0"), "parent_job": str(job),
                "capture_directory": str(capture), "bundle": str(bundle_path),
                "input_commit": commit, "artifact_digest": bundle.identity,
                "parent_ledger_sha256": file_digest(job / "ledger.sqlite3"),
                "parent_config_sha256": file_digest(job / "config.json"),
                "usage": usage, **evidence}
    from leaneval import frontier
    research_checkpoint = frontier.checkpoint(job, capture, bundle.manifest['files'], bundle.identity)
    snapshot['research_checkpoint_digest'] = research_checkpoint['checkpoint_digest']
    from leaneval.generation import validate
    generation_receipts = validate(workspace, contents)
    atomic_json(capture / 'generation-receipts.json', {'receipts': generation_receipts})
    snapshot['generation_receipts_digest'] = digest(generation_receipts)
    from leaneval import generation_handoff
    generation_state = generation_handoff.capture(workspace, capture, contents, generation_receipts, bundle.identity)
    if generation_state is not None:
        snapshot['generation_state_digest'] = generation_state
    from leaneval import lineage
    shared_budget = lineage.reference(job)
    if shared_budget is not None:
        snapshot['lineage'] = {'reference':shared_budget[1], 'state':shared_budget[0].snapshot()}
    snapshot["snapshot_digest"] = digest(snapshot)
    atomic_json(capture / "snapshot.json", snapshot)
    return snapshot


def snapshot_job(
    job_dir: Path | str, runs: Path | str, *, allow_controlled_stop: bool = False,
) -> dict[str, Any]:
    """Controller snapshot for a new authorized tranche; never alters parent accounting."""
    job, runs = Path(job_dir).resolve(), Path(runs).resolve()
    workspace = job / "workspace"
    if runs.is_relative_to(workspace) or workspace.is_relative_to(runs):
        raise ValueError("Snapshot storage must be outside candidate workspace")
    with (job / "supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeBlocked("invocation supervisor is still running") from exc
        config = json.loads((job / "config.json").read_text())
        return _snapshot_locked(job, workspace, runs, config,
                                allow_controlled_stop=allow_controlled_stop,
                                require_quiescence=True)


def finalize_job(
    job_dir: Path | str, workbench: Path | str, runs: Path | str, *, seconds: int = 600,
    allow_controlled_stop: bool = False,
) -> dict[str, Any]:
    job, workbench, runs = Path(job_dir).resolve(), Path(workbench).resolve(), Path(runs).resolve()
    workspace = job / "workspace"
    for trusted in (workbench, runs):
        if trusted.is_relative_to(workspace) or workspace.is_relative_to(trusted):
            raise ValueError("Trusted finalization paths must be separate from candidate workspace")
    with (job / "supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeBlocked("invocation supervisor is still running") from exc
        return _finalize_locked(job, workspace, workbench, runs, seconds, allow_controlled_stop)


def _finalize_locked(
    job: Path, workspace: Path, workbench: Path, runs: Path, seconds: int,
    allow_controlled_stop: bool,
) -> dict[str, Any]:
    config = json.loads((job / "config.json").read_text())
    usage, evidence = _finished_invocation(job, config, allow_controlled_stop=allow_controlled_stop)
    controls = config.get("controls", False)
    if type(controls) is not bool:
        raise ValueError("controls must be a boolean")
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = runs / "finalizations"
    root.mkdir(exist_ok=True, mode=0o700)
    capture = Path(tempfile.mkdtemp(prefix="capture-", dir=root))
    result: dict[str, Any] = {"run_id": config["run_id"], "problem_id": config["problem_id"],
                              "status": "capture_rejected", "verified": False,
                              "service_acceptance": False, "usage": usage,
                              "usd": usage["usd"], "capture_directory": str(capture), **evidence}
    try:
        contents = _capture(workspace, config)
        commit = _git_snapshot(capture / "input", contents)
        bundle_path = capture / "bundle"
        bundle = export_bundle(capture / "input", commit, ".", config["problem_id"],
                               bundle_path, controls=controls)
        result.update(input_commit=commit, artifact_digest=bundle.identity, bundle=str(bundle_path))
        receipt = verify(bundle_path, workbench, runs, seconds=seconds, controls=controls,
                         progress_path=job / "verification-progress.json", budget_job=job)
        # A forged receipt flag cannot pass the controller registry check.
        accepted = accept_receipt(runs, receipt, bundle)
        result.update(status="official_local_pass" if accepted else receipt["status"],
                      verified=accepted, receipt=receipt,
                      verification_wall_seconds=receipt.get("wall_seconds"))
        if not accepted and result["status"] == "official_local_pass":
            result["status"] = "unregistered_receipt_rejected"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        if isinstance(exc, RuntimeBlocked) and 'lineage' in str(exc).lower():
            result['status'] = 'budget_blocked'
        if isinstance(exc, RetryBlocked):
            result['status'] = 'retry_blocked'
            result['retry_policy'] = exc.evidence
    atomic_json(capture / "result.json", result)
    atomic_json(job / "verification.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--allow-controlled-stop", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = finalize_job(args.job, args.workbench, args.runs, seconds=args.seconds,
                              allow_controlled_stop=args.allow_controlled_stop)
    except (RuntimeBlocked, OSError, ValueError) as exc:
        parser.exit(2, f"finalization blocked: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
