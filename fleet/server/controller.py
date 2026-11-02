"""Persistent per-host Claude Opus proof queue; never blindly retries an uncertain job.

One controller per queue root (runner); each takes its own lease <own root>/leases/<root name>.lock, so
several queues share a host. fleet/local/fleet.py deploy writes it into the queue root with RUNTIME pinned.

Assignments live in ``<root>/manifest.json`` and may be appended while the queue runs
(the overseer does this). Each assignment runs at most once: a fresh B0 job or a
continuation seeded from a controller snapshot, then the Claude runtime supervisor,
then the official comparator+Nanoda finalization and a continuation snapshot.
Subscription usage-limit pauses are handled inside the supervisor. ``<root>/STOP``
stops before the next job (and is forwarded to the running job). Only the official
receipt establishes ``official_local_pass``; nothing is published or submitted.

Kinds: ``fresh`` (frozen template), ``continuation`` (controller snapshot), ``bundle``
(frozen template + a licensed proof bundle directory + optional overlay of repaired
files). Optional ``incremental`` gives the job an advisory cached Lean build
(``workspace/.incremental`` + ``./inc``) copied from a built official workspace or moved
from an earlier job. ``verify_policy: if_ready`` runs the official verifier only when the
model declares READY_FOR_VERIFICATION; otherwise the job is snapshotted for continuation.
A restarted controller adopts a still-running supervisor instead of abandoning its job.
``auto_continue`` (e.g. ``{"base": ID, "round": 1, "max_rounds": 200, "next": {...}}``)
appends the next round automatically when a job ends without an official pass: a
continuation from its snapshot, moving its incremental cache, with ``next`` overrides
(e.g. larger budgets). The host therefore never idles on a long repair.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import fcntl
import hashlib
import importlib
import json
import os
import re
import subprocess
import threading
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Pinned runtime directory (its .venv runs the supervisor). Deploy (local/fleet.py) and the
# feeder's runtime switch rewrite this exact line; keep it a literal absolute path.
RUNTIME = Path("/path/to/runtime")
# Host layout, relative to the home of the user running the controller unless overridden.
WB = Path(os.environ.get("LEANEVAL_WORKBENCH") or Path.home() / "projects/lean-eval-workbench")
RUNS = Path(os.environ.get("LEH_RUNS_ROOT") or Path.home() / "projects/lean-eval-runs")
LEASES = Path(os.environ.get("LEH_OWN_ROOT") or Path.home() / "leh-fleet") / "leases"  # own runners: one lease per queue root
POLICY = {"model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
# Optional allowlist of host names (comma-separated LEH_HOSTS); empty accepts the --host given.
HOSTS = {h for h in os.environ.get("LEH_HOSTS", "").split(",") if h}
ALWAYS_EXCLUDED = {"dimitrov", "pi6_sphere_three_mulEquiv_zmod_twelve"}
CONTROLLED = {"token_limit", "wall_limit", "api_error", "external_stop",
              "authentication_failure", "credential_access"}
IDLE_POLL_SECONDS = 60
MAX_CONSECUTIVE_FAILURES = 2
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    name = Path(relative)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError("Controller path escaped the queue root")
    path = root / name
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Controller-owned paths must not contain symlinks")
    return path


def halt_status(job: Path, entry: dict[str, Any]) -> str | None:
    """The status that halts this queue after `job`: the credential tripwire fired (its SECURITY-EVENT.json
    is written by the supervisor even when finalization fails later), or the account's authentication
    failed. None otherwise."""
    if (job / "SECURITY-EVENT.json").exists():
        return "halted_credential_access"
    if (entry.get("model") or {}).get("stop_reason") == "authentication_failure":
        return "halted_authentication_failure"
    return None


def validate(manifest: dict[str, Any], host: str, root: Path) -> list[dict[str, Any]]:
    if manifest.get("requested_config") != POLICY:
        raise ValueError("Exact requested model policy required")
    if manifest.get("host") != host or (HOSTS and host not in HOSTS):
        raise ValueError("Manifest is for a different host")
    excluded = set(manifest.get("excluded_problem_ids", [])) | ALWAYS_EXCLUDED
    attempts: set[str] = set()
    for a in manifest.get("assignments", []):
        for key in ("attempt_id", "problem_id"):
            if not isinstance(a.get(key), str) or not SAFE_ID.fullmatch(a[key]):
                raise ValueError(f"Unsafe or missing {key}")
        if a["attempt_id"] in attempts:
            raise ValueError("Duplicate attempt ID")
        if a["problem_id"] in excluded and not a.get("allow_excluded_rerun"):
            raise ValueError(f"Excluded problem {a['problem_id']}")
        if a.get("kind") not in {"fresh", "continuation", "bundle"}:
            raise ValueError("Assignment kind must be fresh, continuation or bundle")
        if a["kind"] == "continuation":
            if a.get("from_job"):
                if not SAFE_ID.fullmatch(a["from_job"]):
                    raise ValueError("Unsafe from_job")
            else:
                safe_path(root, a["source"])
        if a["kind"] == "bundle" and not Path(a.get("bundle_dir", "")).is_absolute():
            raise ValueError("bundle_dir must be an absolute host path")
        if a.get("overlay_dir"):
            safe_path(root, a["overlay_dir"])
        if a.get("verify_policy", "always") not in {"always", "if_ready"}:
            raise ValueError("verify_policy must be always or if_ready")
        if a.get("brief_file"):
            safe_path(root, a["brief_file"])
        for name in ("wall_seconds", "token_limit", "verification_seconds"):
            if type(a.get(name)) is not int or a[name] <= 0:
                raise ValueError("Positive explicit bounds required")
        attempts.add(a["attempt_id"])
    return list(manifest.get("assignments", []))


@contextlib.contextmanager
def lease(path: Path) -> Iterator[None]:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Lease path has symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def load_runtime() -> dict[str, Any]:
    manifest = json.loads((RUNTIME / "runtime-manifest.json").read_bytes())
    for rel, expected in manifest["files"].items():
        path = RUNTIME / rel
        if path.is_symlink() or digest(path.read_bytes()) != expected:
            raise ValueError(f"Runtime identity changed: {rel}")
    apis = {}
    for name in ("pilot", "jobs", "finalization", "verifier", "artifacts", "continuation",
                 "agent_runner"):
        module = importlib.import_module("leaneval." + name)
        if not Path(module.__file__).resolve().is_relative_to(RUNTIME):
            raise ValueError("Wrong runtime imported; launch with runtime cwd/PYTHONPATH")
        apis[name] = module
    if apis["pilot"].REQUESTED_CONFIG != POLICY:
        raise ValueError("Actual runtime model policy mismatch")
    return apis


def prepare(job: Path, root: Path, manifest: dict[str, Any], a: dict[str, Any],
            api: dict[str, Any], host: str,
            history: dict[str, Any] | None = None) -> dict[str, Any]:
    brief = None
    if a.get("brief_file"):
        brief = safe_path(root, a["brief_file"]).read_text()
    provenance = None
    if a["kind"] in {"fresh", "bundle"}:
        api["jobs"].prepare_job(job, WB, a["problem_id"], a.get("variant", "B0"),
                                a["attempt_id"], wall_seconds=a["wall_seconds"],
                                token_limit=a["token_limit"], host=host, agent_root=RUNTIME)
    else:
        if a.get("from_job"):
            parent = (history or {}).get(a["from_job"]) or {}
            if parent.get("phase") != "done" or not parent.get("snapshot"):
                raise ValueError("from_job has no finished snapshot in this queue")
            snapshot_dir = Path(parent["snapshot"]["directory"])
        else:
            snapshot_dir = safe_path(root, a["source"]) / "snapshot"
        snapshot = json.loads((snapshot_dir / "snapshot.json").read_bytes())
        if snapshot["problem_id"] != a["problem_id"] or snapshot.get("controls"):
            raise ValueError("Continuation snapshot is for a different problem")
        grants = root / "grants"
        grants.mkdir(exist_ok=True)
        grant_path = grants / (a["attempt_id"] + ".json")
        if grant_path.exists():
            raise RuntimeError("Grant already exists; never reuse an attempt")
        grant = {"schema_version": 1, "grant_id": a["attempt_id"], "attempt_id": a["attempt_id"],
                 "authorized_by": "user: Claude Opus 5.5 xhigh queue manifest "
                                  + manifest.get("campaign_id", ""),
                 "parent_snapshot_digest": snapshot["snapshot_digest"],
                 "wall_seconds": a["wall_seconds"], "token_limit": a["token_limit"],
                 "host": host, "variant": snapshot.get("variant", "B0")}
        api["verifier"].atomic_json(grant_path, grant)
        provenance = api["continuation"].seed_continuation(
            snapshot_dir, grant_path, job, WB, agent_root=RUNTIME)
    workspace = job / "workspace"
    imported: dict[str, Any] = {}
    if a["kind"] == "bundle":
        imported["bundle"] = overlay(Path(a["bundle_dir"]), workspace)
    if a.get("overlay_dir"):
        imported["overlay"] = overlay(safe_path(root, a["overlay_dir"]), workspace)
    if a.get("incremental"):
        imported["incremental"] = incremental(root, workspace, a["incremental"])
    cfg = json.loads((job / "config.json").read_bytes())
    if cfg["requested_config"] != POLICY:
        raise ValueError("Prepared config policy mismatch")
    if imported:
        cfg["imported_sources"] = imported
    cfg.update(campaign_id=manifest.get("campaign_id"), campaign_assignment=a,
               included_in_eight_attempts=False)
    api["verifier"].atomic_json(job / "config.json", cfg)
    worker = json.loads((job / "worker.json").read_bytes())
    worker["timeout_seconds"] = int(a.get("worker_timeout_seconds", 600))
    api["verifier"].atomic_json(job / "worker.json", worker)
    if brief:
        with (job / "prompt.txt").open("a") as stream:
            stream.write("\nController brief for this attempt:\n" + brief + "\n")
    return {"continuation_provenance": provenance}


PROOF_NAMES = re.compile(r"(Submission\.lean|Submission/.+\.lean|NOTES\.md)")


def overlay(source: Path, workspace: Path) -> dict[str, Any]:
    """Controller transfer of proof files into the workspace before model launch.

    Production loaded an external receiver module with source-bound receipts, which is not part
    of this snapshot; LEH_TRANSFER_RECEIVER names such a module (``apply(source, workspace)``).
    Without it, the proof files of `source` (PROOF_NAMES) are copied over the workspace and their
    hashes returned. No transfer receipt is written, so the runtime binds no transferred
    dependencies to it; acceptance stays with the official verifier.
    """
    receiver = os.environ.get("LEH_TRANSFER_RECEIVER")
    if receiver:
        import importlib.util
        spec = importlib.util.spec_from_file_location("controller_transfer_receiver", receiver)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.apply(source, workspace)
    files = {}
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source).as_posix()
        if path.is_symlink() or not path.is_file() or not PROOF_NAMES.fullmatch(rel):
            continue
        target = workspace / rel
        if any((workspace / Path(*Path(rel).parts[:i])).is_symlink() for i in range(1, len(Path(rel).parts) + 1)):
            raise ValueError("Overlay target path contains a symlink")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        target.write_bytes(data)
        files[rel] = digest(data)
    if not files:
        raise ValueError("Overlay source has no proof files")
    return {"mode": "copy", "source": str(source), "files": files, "official_verification": False}


INC_HELPER = """#!/bin/bash
# Advisory incremental Lean build against the cached official build of this proof tree.
# Usage: ./inc Submission.Some.Module [...]    (no argument: the whole Submission library)
# Syncs Submission.lean and Submission/ into .incremental, then runs lake build there.
# This is NOT the official verifier; only the controller's final receipt counts.
set -uo pipefail
W="$(cd "$(dirname "$0")" && pwd)"
I="$W/.incremental"
[ -d "$I/.lake" ] || { echo "no incremental build cache in $I"; exit 2; }
rsync -a --delete "$W/Submission/" "$I/Submission/"
cp -f "$W/Submission.lean" "$I/Submission.lean"
cd "$I"
if [ "$#" -eq 0 ]; then set -- Submission; fi
timeout "${INC_TIMEOUT:-1800}" lake build "$@" 2>&1 | grep -v '^trace:' | tail -n "${INC_TAIL:-300}"
exit "${PIPESTATUS[0]}"
"""


def incremental(root: Path, workspace: Path, spec: dict[str, Any]) -> dict[str, Any]:
    target = workspace / ".incremental"
    if spec.get("from_job"):
        previous = root / "jobs" / spec["from_job"] / "workspace/.incremental"
        if not previous.is_dir():
            raise ValueError("Earlier job has no incremental cache to move")
        previous.rename(target)
        origin = {"moved_from_job": spec["from_job"]}
    else:
        source = Path(spec["copy_from"])
        if not source.is_absolute() or not (source / ".lake/build").is_dir():
            raise ValueError("copy_from must be an absolute built Lake workspace")
        subprocess.run(["cp", "-a", str(source), str(target)], check=True)
        origin = {"copied_from": str(source)}
    for name in ("Submission", "Submission.lean"):
        path = target / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    helper = workspace / "inc"
    helper.write_text(INC_HELPER)
    helper.chmod(0o755)
    with (workspace / ".gitignore").open("a") as stream:
        stream.write(".incremental/\ninc\n")
    return origin


def _alive(pid: int, needle: str) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in {"Z", "X"} and needle in cmdline


def adopt(job: Path, root: Path, pid: int) -> tuple[int, dict[str, Any]]:
    """Wait for a supervisor started by an earlier controller, then read its result."""
    while _alive(pid, f"--job {job}"):
        if (root / "STOP").exists() and not (job / "STOP").exists():
            (job / "STOP").touch()
        time.sleep(2)
    path = job / "result.json"
    result = json.loads(path.read_bytes()) if path.exists() else {}
    code = 0 if result.get("status") == "awaiting_verifier" else (1 if result else 2)
    return code, result


def run_model(job: Path, root: Path, save: Any) -> tuple[int, dict[str, Any]]:
    env = dict(os.environ, PYTHONPATH=str(RUNTIME), PYTHONDONTWRITEBYTECODE="1")
    with (job / "model.log").open("xb") as log:
        child = subprocess.Popen(
            [str(RUNTIME / ".venv/bin/python"), "-B", "-m", "leaneval.agent_runner",
             "--job", str(job)],
            cwd=RUNTIME, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        save("model_running", supervisor_pid=child.pid, supervisor_pgid=child.pid)
        try:
            while child.poll() is None:
                if (root / "STOP").exists() and not (job / "STOP").exists():
                    (job / "STOP").touch()
                time.sleep(0.5)
        except BaseException:
            (job / "STOP").touch()
            child.wait(timeout=120)
            raise
    path = job / "result.json"
    result = json.loads(path.read_bytes()) if path.exists() else {}
    return child.returncode, result


def finish(job: Path, a: dict[str, Any], code: int, model: dict[str, Any],
           api: dict[str, Any]) -> dict[str, Any]:
    reason = model.get("stop_reason")
    controlled = reason in CONTROLLED
    if not model:
        raise RuntimeError("Supervisor produced no result; job requires reconciliation")
    if code not in (0, 1) or (code == 1 and not controlled and
                              model.get("status") != "configuration_unconfirmed"):
        raise RuntimeError(f"Uncertain supervisor outcome ({code}, {reason}); no finalization")
    requested = model.get("requested_config") or {}
    if any(requested.get(k) != v for k, v in POLICY.items()):
        raise RuntimeError("Supervisor requested policy mismatch")
    verify = a.get("verify_policy", "always") == "always" or bool(model.get("ready_marker"))
    if verify:
        result = api["finalization"].finalize_job(
            job, WB, RUNS, seconds=a["verification_seconds"], allow_controlled_stop=controlled)
        if result.get("status") not in {"rejected", "timeout", "official_local_pass"}:
            raise RuntimeError(f"Finalization infrastructure unresolved: {result.get('status')}")
    else:
        result = {"status": "not_verified_snapshot_only", "verified": False}
    snapshot = api["finalization"].snapshot_job(job, RUNS, allow_controlled_stop=controlled)
    current = api["artifacts"].load_bundle(Path(snapshot["bundle"]))
    if verify:
        original = api["artifacts"].load_bundle(Path(result["bundle"]))
        if original.files != current.files:
            raise RuntimeError("Post-verification proof snapshot differs")
    effective = model.get("effective_config") or {}
    return {"phase": "done", "model": {k: model.get(k) for k in (
                "status", "stop_reason", "observed_tokens", "usage", "claude_usage",
                "elapsed_seconds", "real_elapsed_seconds", "nudges", "pauses", "thread_id",
                "configuration_confirmed", "api_list_price_usd_estimate", "ready_marker")},
            "effective_model": effective.get("model"),
            "effective_effort": effective.get("reasoning_effort"),
            "verification": {k: result.get(k) for k in (
                "status", "verified", "artifact_digest", "bundle", "capture_directory",
                "verification_wall_seconds", "error")},
            "receipt_run_id": (result.get("receipt") or {}).get("run_id"),
            "snapshot": {"directory": snapshot["capture_directory"],
                         "snapshot_digest": snapshot["snapshot_digest"],
                         "artifact_digest": snapshot["artifact_digest"]},
            "finished_at": time.time()}


@contextlib.contextmanager
def manifest_lock(root: Path) -> Iterator[None]:
    """Shared with the coordinator's tools: serialize read-modify-write of manifest.json."""
    fd = os.open(root / "manifest.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def auto_continue(root: Path, finished: dict[str, Any], entry: dict[str, Any],
                  job: Path) -> str | None:
    if entry.get("phase") != "done" or (entry.get("verification") or {}).get(
            "status") == "official_local_pass":
        return None
    with manifest_lock(root):
        manifest = json.loads((root / "manifest.json").read_bytes())
        spec = next((x for x in manifest["assignments"]
                     if x["attempt_id"] == finished["attempt_id"]), finished)
        rule = spec.get("auto_continue")
        if not rule:
            return None
        n = int(rule.get("round", 1)) + 1
        if n > int(rule.get("max_rounds", 100)):
            return None
        base = rule.get("base") or spec["attempt_id"]
        name = f"{base}-r{n}"
        if any(x["attempt_id"] == name for x in manifest["assignments"]):
            return None
        keep = {k: v for k, v in spec.items() if k not in {
            "attempt_id", "kind", "bundle_dir", "overlay_dir", "source", "from_job",
            "incremental", "auto_continue", "origin"}}
        nxt = dict(keep, attempt_id=name, kind="continuation", from_job=spec["attempt_id"],
                   auto_continue=dict(rule, base=base, round=n),
                   origin=f"auto_continue round {n} after {spec['attempt_id']}")
        if (job / "workspace/.incremental").is_dir():
            nxt["incremental"] = {"from_job": spec["attempt_id"]}
        nxt.update(rule.get("next") or {})
        manifest["assignments"].append(nxt)
        temporary = root / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(root / "manifest.json")
        return name


def run(root: Path, host: str, api: dict[str, Any]) -> None:  # noqa: PLR0915
    root = root.absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Queue root must have no symlink ancestors")
    atomic = api["verifier"].atomic_json
    with lease(LEASES / (root.name + ".lock")), lease(root / "queue.lock"):
        path = root / "state.json"
        state = (json.loads(path.read_bytes()) if path.exists()
                 else {"host": host, "history": {}, "status": "prepared"})
        adoptable = None
        if state.get("status") in {"preparing", "model_running", "finalizing"}:
            current = state.get("assignment") or {}
            name = current.get("attempt_id")
            pid = state.get("supervisor_pid")
            if (state["status"] == "model_running" and name and pid
                    and name not in state["history"]):
                adoptable = (current, int(pid))
            elif name and name not in state["history"]:
                # Preparation/finalization cannot be resumed safely: record, never relaunch.
                state["history"][name] = {
                    "phase": "failed", "error": "controller restarted mid-job; not relaunched",
                    "problem_id": current.get("problem_id"), "finished_at": time.time()}
        stop_heartbeat = threading.Event()

        def save(phase: str, **extra: Any) -> None:
            state.update(status=phase, pid=os.getpid(), updated_at=time.time(), **extra)
            atomic(path, state)

        def heartbeat() -> None:
            while not stop_heartbeat.wait(5):
                with contextlib.suppress(OSError):
                    atomic(root / "heartbeat.json", {"pid": os.getpid(), "host": host,
                                                     "phase": state["status"],
                                                     "observed_at": time.time()})

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        failures = 0
        try:
            if adoptable:
                a, pid = adoptable
                job = root / "jobs" / a["attempt_id"]
                try:
                    save("model_running", adopted_supervisor=pid)
                    code, model = adopt(job, root, pid)
                    save("finalizing", model_status=model.get("status"),
                         model_stop_reason=model.get("stop_reason"))
                    entry = finish(job, a, code, model, api)
                    entry.update(problem_id=a["problem_id"], kind=a["kind"], adopted=True)
                    if entry["verification"]["status"] == "official_local_pass":
                        atomic(root / f"PASS-{a['problem_id']}.json", entry)
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    entry = {"phase": "failed", "problem_id": a["problem_id"],
                             "kind": a["kind"], "error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc()[-4000:],
                             "finished_at": time.time(), "adopted": True}
                state["history"][a["attempt_id"]] = entry
                halt = halt_status(job, entry)
                try:
                    if halt != "halted_credential_access":
                        entry["auto_continued_as"] = auto_continue(root, a, entry, job)
                except Exception as exc:  # noqa: BLE001
                    entry["auto_continue_error"] = f"{type(exc).__name__}: {exc}"
                save("between_jobs", current_job=None, assignment=None)
                if halt:
                    save(halt)
                    return
            while True:
                if (root / "STOP").exists():
                    save("stopped", current_job=None)
                    return
                with manifest_lock(root):
                    manifest = json.loads((root / "manifest.json").read_bytes())
                assignments = validate(manifest, host, root)
                pending = [a for a in assignments if a["attempt_id"] not in state["history"]]
                if not pending:
                    save("idle_waiting_for_assignments", current_job=None, assignment=None)
                    time.sleep(IDLE_POLL_SECONDS)
                    continue
                a = pending[0]
                job = root / "jobs" / a["attempt_id"]
                if job.exists() or job.is_symlink():
                    state["history"][a["attempt_id"]] = {
                        "phase": "failed", "problem_id": a["problem_id"],
                        "error": "attempt path already exists; never overwritten",
                        "finished_at": time.time()}
                    save("between_jobs")
                    continue
                save("preparing", current_job=str(job), assignment=a, started_at=time.time())
                try:
                    extra = prepare(job, root, manifest, a, api, host, state["history"])
                    code, model = run_model(job, root, save)
                    save("finalizing", model_status=model.get("status"),
                         model_stop_reason=model.get("stop_reason"))
                    entry = finish(job, a, code, model, api)
                    entry.update(problem_id=a["problem_id"], kind=a["kind"],
                                 continuation_provenance=extra["continuation_provenance"])
                    failures = 0
                    if entry["verification"]["status"] == "official_local_pass":
                        atomic(root / f"PASS-{a['problem_id']}.json", entry)
                except Exception as exc:  # noqa: BLE001 - recorded, queue moves on
                    failures += 1
                    entry = {"phase": "failed", "problem_id": a["problem_id"],
                             "kind": a["kind"], "error": f"{type(exc).__name__}: {exc}",
                             "traceback": traceback.format_exc()[-4000:],
                             "finished_at": time.time()}
                state["history"][a["attempt_id"]] = entry
                halt = halt_status(job, entry)
                try:
                    if halt != "halted_credential_access":  # no continuation on a possibly exposed account
                        entry["auto_continued_as"] = auto_continue(root, a, entry, job)
                except Exception as exc:  # noqa: BLE001
                    entry["auto_continue_error"] = f"{type(exc).__name__}: {exc}"
                save("between_jobs", current_job=None, assignment=None,
                     consecutive_failures=failures)
                if halt:
                    save(halt)
                    return
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    save("halted_consecutive_failures")
                    return
        except BaseException as exc:
            save("halted_controller_error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            stop_heartbeat.set()
            thread.join(timeout=6)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.root / "manifest.json").read_bytes())
    validate(manifest, args.host, args.root)
    run(args.root, args.host, load_runtime())


if __name__ == "__main__":
    main()
