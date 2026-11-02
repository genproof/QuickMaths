#!/usr/bin/env python3
"""Run directories for the official verification of a proof on a benchmark commit (verify_tree.sh, vcur).

Usage:
  verify_run.py export REPO REF OUT
      write the proof files of commit REF in git repository REPO (a path or URL) to OUT: Submission.lean
      and Submission/**/*.lean only; prints the commit
  verify_run.py prepare RUN SOURCE COMMIT PROBLEM TIMEOUT [--benchmark C] [--workbench DIR]
                        [--bwrap PATH] [--envelope official|ci] [--lean-num-threads N] [--flow TEXT]
                        [--no-preflight]
      make RUN a verify_candidate.py run directory (see verify_candidate.py): a clean checkout of the
      benchmark at C whose .lake/packages is the workbench's, the verifier scripts, the organizers'
      evaluator fetched at its pinned commit, the trimmed sources of the directory SOURCE with their
      hashes, config.json. COMMIT is recorded as the source commit.
  verify_run.py receipt RUN
      after the run: write RUN/receipt.json (accepted or not, source digest, benchmark, times, limits)
      and print one line; exit 0 when accepted

Two evaluation flows, chosen by the benchmark's generated/<problem>/WorkspaceTest.lean:
  lake-comparator  lean-eval #665 (208b6b22, 2026-10-22) on: `lake test` runs the toolchain's `lake comparator`
                   (bundled leanexport, Lean checker and the toolchain's nanoda_bin) inside bubblewrap 0.12.0.
                   RUN/bwrap-bin/bwrap is the wrapper EvalTools/RunEval.lean writes (read-only bind of the
                   shared package cache) around the pinned bwrap (--bwrap, $LEH_BWRAP or
                   <workbench>/.tools/bin/bwrap; build_bwrap.sh builds it without root).
  comparator       earlier benchmarks: the workbench's comparator, landrun, lean4export and nanoda binaries.
Preflight (refused unless --no-preflight): free disk for the build outputs and the comparator's export
file (about 2 KB per proof line, written under $TMPDIR, default /tmp) on both file systems, and
vm.max_map_count for elaborating a large module closure (see preflight()).

C defaults to the current benchmark commit (CURRENT_BENCHMARK). The workbench (LEANEVAL_WORKBENCH,
default ~/projects/lean-eval-workbench; runtime/tools/build_workbench.py builds it) supplies
the benchmark repository, the built Lake packages and .tools/bin; prepare refuses when the benchmark's
lake-manifest.json at C differs from the one those packages were built for. The evaluator is cached in
LEH_VERIFY_CACHE (default ~/.cache/leh-verify) and must match its pinned SHA-256.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CURRENT_BENCHMARK = "208b6b22d396f28771dba56414b19f47cae5f2a7"
BENCHMARK_URL = "https://github.com/leanprover/lean-eval.git"
# The organizers' evaluator (Apache-2.0), fetched at a pinned commit and never vendored (NOTICE.md).
EVALUATOR = {"repository": "leanprover/lean-eval-submissions",
             "commit": "0b38814ed5606268e450df923f814e1613cf1ae7",
             "path": "scripts/evaluate_submission.py",
             "sha256": "4619d7475ceb17ec6be68a2490db6d1465a8fdff30668c7554504fda9755f7b2"}
SCRIPTS = ("verify_candidate.py", "workspace_manager.py")
BINARIES = ("comparator", "nanoda_bin", "lean4export", "landrun")  # the "comparator" flow only
BWRAP_VERSION = "0.12.0"  # pinned by lean-eval-submissions scripts/install_bwrap.sh (GHSA-pxhw-h44j-8pfx fix)
FLOW = "verify verify_candidate.py, source-only, lake test"
# The organizers' CI runner since lean-eval-submissions #2161/#2162 (2026-10-22): Namespace 16 CPU / 32 GB,
# 360 min per job. The local envelope leaves 1 GB to the OS.
CI_RUNNER = {"cpus": 16, "mem": "31G", "timeout_seconds": 21600}
# Preflight constants, measured on accepted Poincaré packages (2026-10-22/23): export 4.82 GB for 2.76M
# proof lines and 6.36 GB for 3.2M; elaborating Submission.lean of a 2,901-file module-format closure
# peaked at 70,267 memory maps (default vm.max_map_count 65530 failed with "failed to read file .olean.private").
EXPORT_BYTES_PER_LINE = 2500
DISK_RESERVE_BYTES = 5 * 1024**3
MAPS_BASE, MAPS_PER_FILE = 30_000, 14


class PrepareError(Exception):
    """The run directory cannot be made as asked; nothing is verified."""


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def default_workbench() -> Path:
    return Path(os.environ.get("LEANEVAL_WORKBENCH") or Path.home() / "projects/lean-eval-workbench")


def proof_file(rel: str) -> bool:
    """What the official flow reads: Submission.lean and Submission/**/*.lean."""
    return rel == "Submission.lean" or (rel.startswith("Submission/") and rel.endswith(".lean"))


# ---------------------------------------------------------------- sources

def export_ref(repo: str, ref: str, out: Path) -> str:
    """The proof files of commit `ref` of `repo` (path or URL) into `out`; returns the commit."""
    if out.exists():
        raise PrepareError(f"{out} exists")
    clone = None
    if not Path(repo).is_dir():
        clone = out.with_name(out.name + ".clone")
        subprocess.run(["git", "clone", "-q", "--no-checkout", repo, str(clone)], check=True)
        repo = str(clone)
    try:
        commit = git("-C", repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        listing = git("-C", repo, "ls-tree", "-r", "-z", commit, "--", "Submission.lean", "Submission")
        for entry in filter(None, listing.split("\0")):
            meta, rel = entry.split("\t", 1)
            mode, kind, blob = meta.split()
            if not proof_file(rel):
                continue
            if kind != "blob" or mode == "120000":
                raise PrepareError(f"{rel} is a {'symlink' if mode == '120000' else kind}; the verifier refuses it")
            data = subprocess.run(["git", "-C", repo, "cat-file", "blob", blob], check=True, capture_output=True).stdout
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_bytes(data)
        if not (out / "Submission.lean").is_file():
            raise PrepareError(f"{ref} ({commit[:12]}) has no Submission.lean")
        return commit
    finally:
        if clone is not None:
            shutil.rmtree(clone, ignore_errors=True)


def trim(source: Path, dest: Path) -> dict[str, str]:
    """Copy only the proof files of `source` into `dest`; refuses symlinks; returns their hashes."""
    if not (source / "Submission.lean").is_file() or (source / "Submission.lean").is_symlink():
        raise PrepareError(f"{source} has no Submission.lean")
    if (source / "Submission").is_symlink():
        raise PrepareError("Submission is a symlink; the verifier refuses it")
    hashes = {}
    candidates = [source / "Submission.lean", *sorted((source / "Submission").rglob("*"))]
    for path in candidates:
        rel = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise PrepareError(f"{rel} is a symlink; the verifier refuses it")
        if not path.is_file() or not proof_file(rel) or ".lake" in Path(rel).parts:
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest / rel)
        hashes[rel] = sha(dest / rel)
    return hashes


# ---------------------------------------------------------------- evaluator

def evaluator_file(cache: Path | None = None) -> Path:
    """The organizers' evaluator at its pinned commit, from the cache or downloaded, SHA-256 checked."""
    cache = cache or Path(os.environ.get("LEH_VERIFY_CACHE") or Path.home() / ".cache/leh-verify")
    path = cache / f"evaluator-{EVALUATOR['commit'][:12]}" / "evaluate_submission.py"
    if not path.is_file():
        url = f"https://raw.githubusercontent.com/{EVALUATOR['repository']}/{EVALUATOR['commit']}/{EVALUATOR['path']}"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".download")
        with urllib.request.urlopen(url, timeout=120) as response:
            tmp.write_bytes(response.read())
        tmp.replace(path)
    if sha(path) != EVALUATOR["sha256"]:
        raise PrepareError(f"{path}: SHA-256 differs from the pinned evaluator {EVALUATOR['sha256'][:16]}")
    return path


# ---------------------------------------------------------------- run directory

def benchmark_flow(bench: Path, problem: str) -> str:
    """'lake-comparator' when the problem's WorkspaceTest runs the toolchain's `lake comparator`."""
    text = (bench / "generated" / problem / "WorkspaceTest.lean").read_text(errors="replace")
    return "lake-comparator" if '"comparator", "--config"' in text else "comparator"


def default_bwrap(workbench: Path) -> Path:
    return Path(os.environ.get("LEH_BWRAP") or workbench / ".tools/bin/bwrap")


def bwrap_version(bwrap: Path) -> str:
    out = subprocess.run([str(bwrap), "--version"], capture_output=True, text=True, timeout=30)
    return out.stdout.strip().removeprefix("bubblewrap ").strip()


def cache_wrapper(path: Path, bwrap: Path, cache: Path, shell: str = "/bin/sh") -> None:
    """The wrapper EvalTools/RunEval.lean `withCacheSandbox` writes: bwrap with a read-only bind of the
    shared package cache inserted before the first `--`. `lake test` drops COMPARATOR_BWRAP, so the
    wrapper is found as the first `bwrap` on PATH."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{shell}\ninserted=\nfor arg do\n  shift\n"
                    "  if [ \"$arg\" = -- ] && [ -z \"$inserted\" ]; then\n"
                    f"    set -- \"$@\" --ro-bind {shlex.quote(str(cache))} {shlex.quote(str(cache))}\n"
                    "    inserted=1\n  fi\n  set -- \"$@\" \"$arg\"\ndone\n"
                    f"exec {shlex.quote(str(bwrap))} \"$@\"\n")
    path.chmod(0o755)


def proof_lines(source: Path) -> tuple[int, int]:
    """(files, lines) of the proof files under `source`."""
    files = [source / "Submission.lean", *sorted((source / "Submission").rglob("*.lean"))]
    files = [f for f in files if f.is_file()]
    return len(files), sum(f.read_bytes().count(b"\n") for f in files)


def preflight(run_parent: Path, source: Path, *, tmpdir: Path | None = None,
              map_limit: int | None = None, free=shutil.disk_usage) -> dict:
    """Host checks that cost hours when they fail late: disk for the export file and the build, and the
    memory-map limit. Returns the measurements and a list of problems (empty: go)."""
    files, lines = proof_lines(source)
    need = DISK_RESERVE_BYTES + EXPORT_BYTES_PER_LINE * lines
    tmpdir = tmpdir or Path(os.environ.get("TMPDIR") or "/tmp")
    if map_limit is None:
        maps = Path("/proc/sys/vm/max_map_count")
        map_limit = int(maps.read_text()) if maps.exists() else None
    maps_needed = MAPS_BASE + MAPS_PER_FILE * files
    report = {"proof_files": files, "proof_lines": lines, "disk_needed_bytes": need,
              "map_limit": map_limit, "maps_estimated": maps_needed, "problems": []}
    for label, path in (("run directory", run_parent), ("TMPDIR", tmpdir)):
        path = Path(path).absolute()
        while not path.exists():  # the run's parent may not exist yet
            path = path.parent
        have = free(path).free
        report[f"free_bytes_{label.split()[0].lower()}"] = have
        if have < need:
            report["problems"].append(f"{label} {path}: {have / 1e9:.1f} GB free, the run needs about "
                                      f"{need / 1e9:.1f} GB (build outputs and the comparator export)")
    if map_limit is not None and maps_needed > 0.9 * map_limit:
        report["problems"].append(f"vm.max_map_count {map_limit} is below the ~{maps_needed} maps a "
                                  f"{files}-file closure needs (raise it to 1048576, a root change)")
    return report


def manifest_pins(text: str) -> dict[str, str]:
    return {p["name"]: p["rev"] for p in json.loads(text)["packages"]}


def benchmark_checkout(run: Path, workbench: Path, commit: str) -> Path:
    """run/benchmark-clean: the benchmark at `commit`, .lake/packages -> the workbench's built packages."""
    vendor = workbench / "vendor/lean-eval"
    packages = (vendor / ".lake/packages").resolve()
    if not packages.is_dir():
        raise PrepareError(f"{vendor}/.lake/packages is missing; build the workbench first")
    bench = run / "benchmark-clean"
    git("clone", "-q", "--no-checkout", str(vendor), str(bench))
    if subprocess.run(["git", "-C", str(bench), "cat-file", "-e", f"{commit}^{{commit}}"],
                      capture_output=True).returncode:
        git("-C", str(bench), "fetch", "-q", BENCHMARK_URL, commit)
    git("-C", str(bench), "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", commit)
    built = manifest_pins(git("-C", str(vendor), "show", "HEAD:lake-manifest.json"))
    wanted = manifest_pins((bench / "lake-manifest.json").read_text())
    if built != wanted:
        diff = sorted(k for k in set(built) | set(wanted) if built.get(k) != wanted.get(k))
        raise PrepareError(f"benchmark {commit[:12]} pins other Lake packages than the workbench was built "
                           f"for ({diff}); build a workbench for that manifest")
    (bench / ".lake").mkdir()
    (bench / ".lake/packages").symlink_to(packages, target_is_directory=True)
    return bench


def prepare(run: Path, source: Path, commit: str, problem: str, timeout: int, *,
            benchmark: str = CURRENT_BENCHMARK, workbench: Path | None = None, lean_num_threads: int | None = None,
            flow: str = FLOW, evaluator: Path | None = None, bwrap: Path | None = None,
            envelope: str = "official", check_host: bool = True, host_report: dict | None = None) -> dict:
    """Make the run directory; returns its config. `evaluator` replaces the fetched file and
    `host_report` the preflight measurement in tests only."""
    workbench = (workbench or default_workbench()).resolve()
    if run.exists():
        raise PrepareError(f"{run} exists; use a new run directory")
    if not 1 <= timeout <= 7 * 86400:
        raise PrepareError("timeout must be 1 s .. 7 days")
    if envelope not in ("official", "ci"):
        raise PrepareError("envelope must be official or ci")
    host = host_report if host_report is not None else preflight(run.parent, source)
    if check_host and host["problems"]:
        raise PrepareError("preflight: " + "; ".join(host["problems"]) + " (--no-preflight to run anyway)")
    run.mkdir(parents=True)
    bench = benchmark_checkout(run, workbench, benchmark)
    if not (bench / "generated" / problem).is_dir():
        shutil.rmtree(run)
        raise PrepareError(f"benchmark {benchmark[:12]} has no problem {problem}")
    evaluation = benchmark_flow(bench, problem)
    extra: dict = {"evaluation_flow": evaluation}
    if evaluation == "lake-comparator":
        bwrap = Path(bwrap or default_bwrap(workbench)).resolve()
        if bwrap.name != "bwrap" or not bwrap.is_file():
            shutil.rmtree(run)
            raise PrepareError(f"no bubblewrap at {bwrap}; build it with runtime/tools/build_bwrap.sh "
                               f"or pass --bwrap")
        version = bwrap_version(bwrap)
        if version != BWRAP_VERSION:
            shutil.rmtree(run)
            raise PrepareError(f"{bwrap} is bubblewrap {version or '?'}; the organizers pin {BWRAP_VERSION}")
        cache_wrapper(run / "bwrap-bin/bwrap", bwrap, (bench / ".lake/packages").resolve())
        tools, binaries = bwrap.parent, {"bwrap": sha(bwrap)}
        extra.update(bwrap_version=version, bwrap_sha256=binaries["bwrap"], path_prefix=str(run / "bwrap-bin"))
    else:
        tools = workbench / ".tools/bin"
        missing = [n for n in BINARIES if not (tools / n).is_file()]
        if missing:
            shutil.rmtree(run)
            raise PrepareError(f"{tools} lacks {missing}; build the workbench first")
        binaries = {n: sha(tools / n) for n in BINARIES}
    for name in SCRIPTS:
        shutil.copy2(HERE / name, run / name)
    shutil.copy2(evaluator or evaluator_file(), run / "evaluate_submission.py")
    hashes = trim(source, run / "source")
    (run / "source-sha256.json").write_text(json.dumps(hashes, indent=2, sort_keys=True))
    pool = next((p for p in json.loads((bench / "lake-manifest.json").read_text())["packages"]
                 if p["name"].strip("«»") == "lean-pool"), None)
    config = {
        "commit": commit, "problem_id": problem,
        "benchmark": str(bench), "benchmark_commit": benchmark,
        "manager_sha256": sha(run / "workspace_manager.py"),
        "tools_bin": str(tools),
        "binary_sha256": binaries,
        "timeout_seconds": timeout,
        "official_private_lean_pool": pool is not None,
        "evaluator_sha256": sha(run / "evaluate_submission.py"),
        "evaluator_commit": EVALUATOR["commit"],
        "lean_pool_commit": pool["rev"] if pool else None,
        "flow": flow,
        "envelope": envelope,
        **extra,
        "preflight": {k: v for k, v in host.items() if k != "problems"},
        "preflight_problems": host["problems"],
    }
    if lean_num_threads:
        config["lean_num_threads"] = int(lean_num_threads)
    (run / "config.json").write_text(json.dumps(config, indent=2))
    return config


# ---------------------------------------------------------------- receipt

def run_done_fields(text: str) -> dict[str, str]:
    """`rc 0 wall 540s peak 9177800704 oom 0 scope systemd mem 64G cpus 0-15` as a dict."""
    words = text.split()
    return {words[i]: words[i + 1] for i in range(0, len(words) - 1, 2)}


def receipt(run: Path) -> dict:
    config = json.loads((run / "config.json").read_text())
    status = json.loads((run / "status.json").read_text()) if (run / "status.json").is_file() else {}
    hashes = json.loads((run / "source-sha256.json").read_text())
    done = (run / "RUN-DONE").read_text().strip() if (run / "RUN-DONE").is_file() else ""
    official = json.loads(Path(status["receipt"]).read_text()) if status.get("receipt") else {}
    record = {
        "problem_id": config["problem_id"],
        "accepted": status.get("phase") == "finished" and status.get("accepted") is True,
        "phase": status.get("phase"), "error": status.get("error"),
        "envelope": config.get("envelope") or ("ci" if config.get("lean_num_threads") else "official"),
        "evaluation_flow": config.get("evaluation_flow", "comparator"),
        "bwrap_version": config.get("bwrap_version"),
        "benchmark_commit": config["benchmark_commit"],
        "source_commit": config["commit"],
        "source_files": len(hashes),
        "source_digest": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "official_receipt": status.get("receipt"), "official_receipt_sha256": status.get("receipt_sha256"),
        "nanoda_accepted": official.get("nanoda_accepted"), "lean_kernel_accepted": official.get("lean_kernel_accepted"),
        "illegal_axiom": status.get("illegal_axiom"), "exit_code": status.get("exit_code"),
        "lake_test_seconds": official.get("elapsed_seconds"), "total_seconds": status.get("elapsed_seconds"),
        "timeout_seconds": config["timeout_seconds"], "lean_num_threads": config.get("lean_num_threads"),
        "run": run_done_fields(done),
        "evaluator_commit": config["evaluator_commit"], "binary_sha256": config["binary_sha256"],
        "written_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    (run / "receipt.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("export")
    p.add_argument("repo")
    p.add_argument("ref")
    p.add_argument("out", type=Path)
    p = sub.add_parser("prepare")
    p.add_argument("run", type=Path)
    p.add_argument("source", type=Path)
    p.add_argument("commit")
    p.add_argument("problem")
    p.add_argument("timeout", type=int)
    p.add_argument("--benchmark", default=CURRENT_BENCHMARK)
    p.add_argument("--workbench", type=Path)
    p.add_argument("--lean-num-threads", type=int)
    p.add_argument("--flow", default=FLOW)
    p.add_argument("--bwrap", type=Path)
    p.add_argument("--envelope", default="official", choices=("official", "ci"))
    p.add_argument("--no-preflight", action="store_true")
    p = sub.add_parser("preflight")
    p.add_argument("run_parent", type=Path)
    p.add_argument("source", type=Path)
    p = sub.add_parser("receipt")
    p.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "export":
            print(export_ref(args.repo, args.ref, args.out))
        elif args.action == "prepare":
            config = prepare(args.run, args.source, args.commit, args.problem, args.timeout,
                             benchmark=args.benchmark, workbench=args.workbench,
                             lean_num_threads=args.lean_num_threads, flow=args.flow, bwrap=args.bwrap,
                             envelope=args.envelope, check_host=not args.no_preflight)
            print("prepared", args.problem, args.commit[:12], "benchmark", config["benchmark_commit"][:12],
                  "flow", config["evaluation_flow"],
                  "source files", len(json.loads((args.run / "source-sha256.json").read_text())))
            for problem in config["preflight_problems"]:
                print("WARNING (preflight skipped):", problem, file=sys.stderr)
        elif args.action == "preflight":
            report = preflight(args.run_parent, args.source)
            print(json.dumps(report, indent=2))
            return 1 if report["problems"] else 0
        else:
            r = receipt(args.run)
            print(f"{'ACCEPTED' if r['accepted'] else 'REJECTED'} {r['problem_id']} ({r['envelope']}) "
                  f"benchmark {r['benchmark_commit'][:12]} source {r['source_commit'][:12]} "
                  f"digest {r['source_digest'][:16]} lake test {r['lake_test_seconds']} s "
                  f"{' '.join(f'{k} {v}' for k, v in r['run'].items())}; {args.run / 'receipt.json'}")
            return 0 if r["accepted"] else 1
    except (PrepareError, subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        print(f"verify_run {args.action}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
