"""Fresh official Linux verification with a controller-owned receipt registry.

The registry and runner are trusted controller state, outside the Landlock-writable
workspace. This is local provenance, not remote attestation or a service credit.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import platform
import signal
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from leaneval.artifacts import (
    Bundle,
    digest,
    file_digest,
    git_tree,
    load_bundle,
    problem_spec,
)
from leaneval.planning import profile, resource_text

# The current official evaluation (lean-eval #665, benchmark 208b6b22): `lake test` runs the toolchain's
# `lake comparator` (leanexport, the Lean checker and the toolchain's nanoda_bin) inside bubblewrap.
# .tools/bin holds the pinned bubblewrap and landrun (the agent's sandbox); the toolchain supplies the rest.
TOOLS = ("landrun", "bwrap")
TOOLCHAIN_TOOLS = ("lake", "lean", "nanoda_bin")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def toolchain_bin() -> Path:
    """bin/ of the benchmark toolchain under ELAN_HOME (default ~/.elan), as elan names its directory."""
    name = profile()["benchmark"]["toolchain"].replace("/", "--").replace(":", "---")
    return Path(os.environ.get("ELAN_HOME") or Path.home() / ".elan") / "toolchains" / name / "bin"


def bwrap_wrapper(directory: Path, workbench: Path) -> Path:
    """`directory/bwrap`: the wrapper EvalTools/RunEval.lean (withCacheSandbox) puts around the pinned
    bubblewrap, adding a read-only bind of the shared Lake package cache, which a workspace reaches through
    its .lake/packages symlink. `lake comparator` takes `bwrap` from PATH (WorkspaceTest passes only PATH
    on), so `directory` goes first on PATH. Returns `directory`."""
    cache = (workbench / "vendor/lean-eval/.lake/packages").resolve()
    real = workbench / ".tools/bin/bwrap"
    quote = lambda path: "'" + str(path).replace("'", "'\\''") + "'"
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "bwrap"
    text = ("#!/bin/sh\ninserted=\nfor arg do\n  shift\n"
            '  if [ "$arg" = -- ] && [ -z "$inserted" ]; then\n'
            f'    set -- "$@" --ro-bind {quote(cache)} {quote(cache)}\n'
            '    inserted=1\n  fi\n  set -- "$@" "$arg"\ndone\n'
            f'exec {quote(real)} "$@"\n')
    if not wrapper.is_file() or wrapper.read_text() != text:
        temporary = directory / f".bwrap.{os.getpid()}.tmp"
        temporary.write_text(text)
        temporary.chmod(0o755)
        temporary.replace(wrapper)
    return directory


def environment(workbench: Path, wrapper: Path | None = None) -> dict[str, str]:
    """Build and test environment: the benchmark toolchain first (its lake, lean and nanoda_bin), the
    workbench tools, elan, then the inherited PATH. With `wrapper` (bwrap_wrapper), that directory comes
    first so the official `lake test` finds the cache-binding bubblewrap."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([
        *([str(wrapper)] if wrapper else []), str(toolchain_bin()),
        str(workbench / ".tools/bin"), str(workbench / ".venv/bin"),
        str(Path.home() / ".elan/bin"), str(Path.home() / ".cargo/bin"),
        str(Path.home() / ".local/bin"), env.get("PATH", ""),
    ])
    for name in ("COMPARATOR_BIN", "COMPARATOR_BWRAP", "COMPARATOR_LANDRUN", "COMPARATOR_LEAN4EXPORT",
                 "COMPARATOR_NANODA"):
        env.pop(name, None)  # the earlier comparator flow; never steer the toolchain's comparator
    env["MATHLIB_NO_CACHE_ON_UPDATE"] = "1"
    return env


def process_start_ticks(pid: int) -> str | None:
    """Capture Linux process identity once; unavailable identity stays unknown."""
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        value = fields[19]
        return value if int(value) > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def execute(
    argv: list[str], cwd: Path, env: dict[str, str], log: Path, deadline: float,
) -> int:
    """Bounded process-group execution with durable, source-bound phase observations."""
    from leaneval import costs, stage_cgroup
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Verification wall budget exhausted")
    with log.open("wb") as stream:
        process, stage_scope = stage_cgroup.spawn(
            argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        start_ticks = process_start_ticks(process.pid)
        measure = costs.Probe(process.pid).start()
        progress_path = env.get("LEANEVAL_VERIFICATION_PROGRESS")
        started = time.monotonic()
        status = "running"
        code = None
        def progress(state: str, exit_code: int | None = None) -> None:
            if progress_path:
                atomic_json(Path(progress_path), {
                    "schema_version": 2, "phase": log.name,
                    "verifier_job_id": env.get("LEANEVAL_VERIFICATION_JOB_ID"),
                    "candidate_digest": env.get("LEANEVAL_VERIFICATION_CANDIDATE"),
                    "status": state, "accepted": False, "pid": process.pid,
                    "process_start_ticks": start_ticks,
                    "heartbeat_at": time.time(), "elapsed_seconds": time.monotonic() - started,
                    "log": str(log), "log_bytes": log.stat().st_size,
                    "sampled_peak_tree_rss_bytes": measure.peak if measure.samples else None,
                    "exit_code": exit_code})
        def event(state: str, exit_code: int | None = None) -> None:
            with (log.parent / "lifecycle.jsonl").open("a") as events:
                events.write(json.dumps({"at": time.time(), "phase": log.name, "status": state,
                    "pid": process.pid, "process_start_ticks": start_ticks,
                    "verifier_job_id": env.get("LEANEVAL_VERIFICATION_JOB_ID"),
                    "candidate_digest": env.get("LEANEVAL_VERIFICATION_CANDIDATE"),
                    "exit_code": exit_code}) + "\n")
        try:
            event("started")
            progress("running")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, time.monotonic() - started)
                try:
                    code = process.wait(timeout=min(10, remaining))
                    status = "completed"
                    progress(status, code)
                    return code
                except subprocess.TimeoutExpired:
                    progress("running")
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                stage_scope.kill()
            process.wait()
            code = process.returncode
            status = "interrupted"
            progress(status, code)
            raise
        finally:
            measurement = measure.finish()
            measurement["isolated_cgroup"] = stage_scope.finish()
            measurement.update(phase=log.name, status=status, exitcode=code)
            measurement['reported_nodes'] = costs.built_nodes(log.read_text(errors='replace'))
            atomic_json(log.with_suffix('.metrics.json'), measurement)
            event(status, code)



def bwrap_version(path: Path) -> str | None:
    try:
        return subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def tool_identity(workbench: Path) -> dict[str, Any]:
    """The trusted evaluation inputs, checked: the pins, the clean benchmark checkout at its commit, the pinned
    bubblewrap and the benchmark toolchain's lake, lean and nanoda_bin. Raises ValueError otherwise."""
    upstream = workbench / "vendor/lean-eval"
    pins = json.loads((workbench / "pins.json").read_text())
    if pins != json.loads(resource_text("verifier-pins.json")):
        raise ValueError("Workbench verifier pins differ from the trusted runner pins")
    if pins["benchmark"]["rev"] != profile()["benchmark"]["commit"]:
        raise ValueError("Workbench and frozen benchmark pins differ")
    if pins.get("evaluation") != "lake-comparator":
        raise ValueError("Unsupported evaluation flow in verifier pins")
    actual = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if actual != pins["benchmark"]["rev"]:
        raise ValueError("Incorrect verifier pin: benchmark")
    if subprocess.check_output(["git", "-C", str(upstream), "diff", "HEAD", "--name-only"], text=True).strip():
        raise ValueError("Modified trusted source: benchmark")
    if (upstream / "lean-toolchain").read_text().strip() != profile()["benchmark"]["toolchain"]:
        raise ValueError("Benchmark toolchain differs from the frozen profile")
    version = bwrap_version(workbench / ".tools/bin/bwrap")
    if version != "bubblewrap " + pins["bwrap"]["version"]:
        raise ValueError(f"Workbench bubblewrap must be {pins['bwrap']['version']} (found {version})")
    toolchain = toolchain_bin()
    missing = [name for name in TOOLCHAIN_TOOLS if not (toolchain / name).is_file()]
    if missing:
        raise ValueError(f"Benchmark toolchain at {toolchain} lacks {missing}")
    return {
        "pins": pins,
        "binaries": {**{name: file_digest(workbench / ".tools/bin" / name) for name in TOOLS},
                     **{name: file_digest(toolchain / name) for name in TOOLCHAIN_TOOLS}},
        "runner": {name: file_digest(Path(__file__).with_name(name))
                   for name in ("verifier.py", "artifacts.py")},
    }


def registry(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(root / "receipts.sqlite3")
    connection.execute("""CREATE TABLE IF NOT EXISTS receipts (
        run_id TEXT PRIMARY KEY, artifact TEXT NOT NULL, payload TEXT NOT NULL)""")
    return connection


def accept_receipt(root: Path, receipt: dict[str, Any], bundle: Bundle) -> bool:
    """Never trust a worker-supplied pass flag, even if its content hash is correct."""
    try:
        identity = bundle.identity
    except (ValueError, TypeError):
        return False
    with contextlib.closing(registry(root)) as connection:
        row = connection.execute(
            "SELECT artifact,payload FROM receipts WHERE run_id = ?", (receipt.get("run_id"),)
        ).fetchone()
    return bool(row and row[0] == identity and json.loads(row[1]) == receipt
                and receipt.get("status") == "official_local_pass")


def tree_matches(root: Path, files: dict[str, bytes]) -> bool:
    if root.is_symlink():
        return False
    observed = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == ".lake":
            continue
        if path.is_symlink():
            return False
        if path.is_file():
            observed.add(relative.as_posix())
    return observed == set(files) and all(
        (root / name).read_bytes() == content for name, content in files.items()
    )


def prepare(
    workbench: Path, workspace: Path, bundle: Bundle, env: dict[str, str],
    job: Path, deadline: float,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    pin = profile()["benchmark"]["commit"]
    upstream = workbench / "vendor/lean-eval"
    original = git_tree(upstream, pin, "generated/" + bundle.manifest["problem_id"])
    if original["lean-toolchain"].decode().strip() != profile()["benchmark"]["toolchain"]:
        raise ValueError("Workspace toolchain differs from the frozen profile")
    for name, content in original.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (workspace / ".lake").mkdir()
    (workspace / ".lake/packages").symlink_to(upstream / ".lake/packages", target_is_directory=True)
    code = execute(["lake", "update"], workspace, env, job / "dependencies.log", deadline)
    if code:
        raise RuntimeError(f"Dependency preparation failed ({code})")
    dependencies = json.loads((workspace / "lake-manifest.json").read_text())
    upstream_manifest = json.loads(subprocess.check_output([
        "git", "-C", str(upstream), "show", f"{pin}:lake-manifest.json",
    ]))
    expected = {p["name"]: p["rev"] for p in upstream_manifest["packages"]}
    for package in dependencies["packages"]:
        if package.get("rev") != expected.get(package["name"]):
            raise ValueError(f"Dependency pin mismatch: {package['name']}")
        package_dir = upstream / ".lake/packages" / package["name"].strip("«»")
        head = subprocess.check_output(["git", "-C", str(package_dir), "rev-parse", "HEAD"],
                                       text=True).strip()
        if head != package["rev"]:
            raise ValueError(f"Dependency checkout changed: {package['name']}")
    original.update(bundle.files)
    for name, content in bundle.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    original["lake-manifest.json"] = (workspace / "lake-manifest.json").read_bytes()
    return original, dependencies


def verify(
    bundle_dir: Path, workbench: Path, run_root: Path, *, seconds: int,
    controls: bool = False, progress_path: Path | None = None, budget_job: Path | None = None,
) -> dict[str, Any]:
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("Verification requires a positive wall budget")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise ValueError("Official checking requires x86_64/aarch64 Linux with Landlock")
    abi = ctypes.CDLL(None, use_errno=True).syscall(444, 0, 0, 1)
    if abi <= 0:
        raise ValueError("Landlock is unavailable; checking cannot proceed")
    bundle = load_bundle(bundle_dir, controls=controls)
    workbench, run_root = workbench.resolve(), run_root.resolve()
    started = time.monotonic()
    deadline = started + seconds
    run_id = uuid.uuid4().hex
    from leaneval import lineage
    ticket = lineage.compiler_reservation(budget_job, 'verify/'+run_id, seconds) if budget_job is not None else None
    if ticket:
        deadline = min(deadline, started + ticket['seconds'])
    # Check after reservation so a larger requested budget cannot bypass the
    # policy when the shared compiler budget grants a smaller actual allowance.
    if budget_job is not None:
        from leaneval.verification_retry import assess
        try:
            retry = assess(budget_job, bundle.manifest, tool_identity(workbench), run_root,
                           ticket['seconds'] if ticket else seconds)
            atomic_json(budget_job / 'verification-retry.json', retry)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            lineage.compiler_finished(ticket, 0)
            if hasattr(exc, 'evidence'):
                atomic_json(budget_job / 'verification-retry.json', exc.evidence)
            raise
    job = run_root / run_id
    job.mkdir(parents=True, mode=0o700)
    workspace = job / bundle.manifest["problem_id"]
    receipt: dict[str, Any] = {
        "schema_version": 1, "run_id": run_id, "artifact_digest": bundle.identity,
        "artifact": bundle.manifest, "status": "infra_failed", "landlock_abi": abi,
        "checks": {}, "logs": {}, "service_acceptance": False,
    }
    env = environment(workbench, bwrap_wrapper(job / "bwrap-bin", workbench))
    env["LEANEVAL_VERIFICATION_JOB_ID"] = run_id
    env["LEANEVAL_VERIFICATION_CANDIDATE"] = bundle.identity
    receipt['delivery'] = {'local_verification': 'evaluating', 'hosted': 'not_submitted'}
    try:
        import io
        import tarfile
        archive_path = job / 'proof-inputs.tar.gz'
        with tarfile.open(archive_path, 'w:gz') as archive:
            for name, data in sorted(bundle.files.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(data))
        receipt['delivery']['archive'] = {'status': 'created', 'sha256': file_digest(archive_path),
            'bytes': archive_path.stat().st_size, 'proof_source_bytes': sum(len(v) for v in bundle.files.values()),
            'scope': 'Captured proof files only; not a hosted submission package'}
    except (OSError, ValueError) as exc:
        receipt['delivery']['archive'] = {'status': 'archive_failed', 'error': str(exc)}
    if progress_path is not None:
        env["LEANEVAL_VERIFICATION_PROGRESS"] = str(progress_path)
    try:
        tools = tool_identity(workbench)
        receipt["verifier"] = tools
        originals, dependencies = prepare(workbench, workspace, bundle, env, job, deadline)
        receipt["dependencies"] = dependencies
        receipt["template_digest"] = digest({
            name: hashlib.sha256(data).hexdigest()
            for name, data in originals.items()
        })
        config = json.loads((workspace / "config.json").read_text())
        receipt["required_holes"] = config["theorem_names"]
        spec = problem_spec(bundle.manifest["problem_id"], controls=controls)
        receipt["semantic_review_required"] = any(h["kind"] == "def" for h in spec["holes"])
        probe = execute([
            "python3", str(workbench / "vendor/lean-eval/scripts/sandbox_engaged_probe.py"),
            "--require-tools",
        ], workspace, env, job / "sandbox.log", deadline)
        receipt["checks"]["sandbox"] = probe
        if probe:
            raise RuntimeError("Official sandbox probe failed")
        code = execute(["lake", "test"], workspace, env, job / "official.log", deadline)
        receipt["checks"]["official_lake_test"] = code
        log = (job / "official.log").read_text(errors="replace")
        kernels = all(marker in log for marker in (
            "nanoda kernel accepts the solution", "Lean default kernel accepts the solution",
            "Your solution is okay!",
        ))
        receipt["checks"]["both_kernels_reported"] = kernels
        unchanged = (tree_matches(workspace, originals)
                     and load_bundle(bundle_dir, controls=controls).identity == bundle.identity
                     and tool_identity(workbench) == tools)
        receipt["checks"]["inputs_unchanged"] = unchanged
        receipt["status"] = (
            "official_local_pass" if code == 0 and kernels and unchanged else "rejected"
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        receipt["error"] = str(exc)
        if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
            receipt["status"] = "timeout"
    receipt['delivery']['local_verification'] = receipt['status']
    receipt['stage_measurements'] = [json.loads(p.read_text()) for p in sorted(job.glob('*.metrics.json'))]
    timeline = job / 'lifecycle.jsonl'
    receipt['lifecycle'] = {'path': str(timeline), 'sha256': file_digest(timeline) if timeline.exists() else None}
    receipt["wall_seconds"] = round(time.monotonic() - started, 6)
    lineage.compiler_finished(ticket, receipt['wall_seconds'])
    if ticket:
        receipt['lineage_compiler_allowance_seconds'] = ticket['seconds']
    receipt["logs"] = {p.name: file_digest(p) for p in job.glob("*.log")}
    atomic_json(job / "receipt.json", receipt)
    with contextlib.closing(registry(run_root)) as connection:
        connection.execute("INSERT INTO receipts VALUES (?, ?, ?)",
                           (run_id, bundle.identity, json.dumps(receipt, sort_keys=True)))
        connection.commit()
    return receipt
