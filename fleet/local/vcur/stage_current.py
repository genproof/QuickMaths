#!/usr/bin/env python3
"""stage_current.py CLONE PROBLEM [RUN] : record the official verification against the current benchmark
(208b6b22) in a branch clone and commit it on top. Works for any branch name: CLONE is a clone of the branch
(`claude/<p>-private`, `claude/<p>-own-<d8>-private`, ...) whose HEAD is the verified commit; RUN is the run dir
name under <base>/runs on the verify host (default: PROBLEM).

Sources are untouched: the verified source hashes are checked against the clone. VERIFICATION.json takes the
format of the branches already submitted; the earlier record is kept under
`earlier_verification`, and README.md gets one paragraph in front of the earlier verification paragraph. The
commit is asserted to change only README.md and VERIFICATION.json. Nothing is pushed here.

verify_current.py imports stage_clone() and passes the run bundle it fetched itself.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

# lean-eval main since #665 (2026-10-22): evaluation by the toolchain's `lake comparator` in bubblewrap;
# verify picks that flow from the benchmark (verify_run.CURRENT_BENCHMARK).
BENCH = "208b6b22d396f28771dba56414b19f47cae5f2a7"
MARKER = "Fresh source-only official local verification passed"
# The flow of runs whose config names none (runs made before the verifier recorded its flow); verify/ runs
# record their own.
FLOW = "verify_candidate.py, source-only, lake test"
UNCHANGED_KEYS = ["sources_unchanged", "protected_unchanged", "binaries_unchanged", "dependency_sources_unchanged"]
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


class StageError(Exception):
    """A precondition of the record does not hold; nothing was committed."""


def check(condition, message: str) -> None:
    if not condition:
        raise StageError(message)


def git(clone: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True, text=True,
                          input=stdin).stdout


def source_hashes(clone: Path) -> dict[str, str]:
    """Hashes of exactly what the verifier sees: Submission.lean and Submission/**/*.lean."""
    files = [clone / "Submission.lean", *sorted((clone / "Submission").rglob("*.lean"))]
    return {f.relative_to(clone).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in files if f.is_file()}


def check_bundle(problem: str, head: str, bundle: dict, bench: str = BENCH) -> None:
    """The assertions of the manual stage_current.py, as StageError."""
    status, config, hashes, receipt = (bundle["status"], bundle["config"], bundle["hashes"], bundle["receipt"])
    check(status.get("phase") == "finished" and status.get("accepted") is True,
          f"run not accepted: phase {status.get('phase')} accepted {status.get('accepted')}")
    check(receipt.get("accepted") and receipt.get("nanoda_accepted") and receipt.get("lean_kernel_accepted"),
          "receipt does not record acceptance by both kernels")
    check(receipt.get("exit_code") == 0 and not receipt.get("timed_out") and not receipt.get("illegal_sorry_axiom"),
          f"receipt exit {receipt.get('exit_code')} timed_out {receipt.get('timed_out')} "
          f"illegal {receipt.get('illegal_sorry_axiom')}")
    check(receipt.get("benchmark_commit") == bench == config.get("benchmark_commit"),
          f"benchmark {receipt.get('benchmark_commit')} / {config.get('benchmark_commit')} != {bench}")
    check(config.get("commit") == head and config.get("problem_id") == problem,
          f"run is {config.get('problem_id')}@{config.get('commit')}, clone is {problem}@{head}")
    check("source_sha256" not in receipt or receipt["source_sha256"] == hashes,
          "receipt source hashes differ from source-sha256.json")
    check(all(status.get(k) for k in UNCHANGED_KEYS),
          "inputs changed: " + ", ".join(k for k in UNCHANGED_KEYS if not status.get(k)))


def build_verification(problem: str, old: dict, bundle: dict, head: str, host: str = "verify-host",
                       bench: str = BENCH) -> dict:
    status, config, hashes, receipt = (bundle["status"], bundle["config"], bundle["hashes"], bundle["receipt"])
    return {
        "problem_id": problem,
        "statement_revision": old.get("statement_revision", 1),
        "benchmark_commit": bench,
        "status": "official_local_pass",
        "accepted": True,
        "nanoda_accepted": True,
        "lean_kernel_accepted": True,
        "official_lake_test_exit_code": 0,
        "inputs_unchanged": all(status[k] for k in UNCHANGED_KEYS),
        "receipt_sha256": status["receipt_sha256"],
        "receipt_started_at": receipt["started_at"],
        "verification_wall_seconds": round(receipt["elapsed_seconds"], 2),
        "official_log_sha256": receipt["log_sha256"],
        "source_commit": head,
        "source_sha256": hashes,
        "illegal_sorry_axiom": False,
        "verifier": {
            "flow": config.get("flow") or FLOW,
            "host": host,
            "receipt_path": status["receipt"],
            "evaluator_commit": config["evaluator_commit"],
            "evaluator_sha256": config["evaluator_sha256"],
            "manager_sha256": config["manager_sha256"],
            "binary_sha256": config["binary_sha256"],
            "lean_pool_commit": config["lean_pool_commit"],
            "evaluation_flow": config.get("evaluation_flow", "comparator"),
            **({"bwrap_version": config["bwrap_version"], "bwrap_sha256": config.get("bwrap_sha256")}
               if config.get("bwrap_version") else {}),
        },
        "earlier_verification": {k: v for k, v in old.items()
                                 if not k.endswith("files_sha256") and k != "source_sha256"},
    }


def readme_with_current(text: str, *, head: str, receipt_sha256: str, files: int, date: str,
                        bench: str = BENCH, flow: str = "comparator") -> str:
    check(MARKER in text, f"README has no '{MARKER}' paragraph")
    tool = " (the toolchain's `lake comparator` under bubblewrap 0.12.0)" if flow == "lake-comparator" else ""
    line = (f"Current verification ({date}): the official `verify_candidate.py` flow{tool} passed both Nanoda and the "
            f"Lean kernel (`lake test`, exit 0) against the current benchmark commit `{bench}` on the exact "
            f"{files} source files of this commit's parent `{head}`; receipt SHA256 `{receipt_sha256}`. "
            f"Sources, protected files, pinned tools and Lean Pool dependency sources were unchanged. "
            f"`VERIFICATION.json` holds this record and keeps the earlier one under `earlier_verification`.\n\n"
            f"Earlier: ")
    return text.replace(MARKER, line + MARKER, 1)


def stage_clone(clone: Path, problem: str, bundle: dict, *, host: str = "verify-host", bench: str = BENCH) -> dict:
    """Write VERIFICATION.json + README line and commit them on top of the clone's HEAD."""
    clone = Path(clone)
    head = git(clone, "rev-parse", "HEAD").strip()
    check(not git(clone, "status", "--porcelain").strip(), "clone has uncommitted changes")
    check_bundle(problem, head, bundle, bench)
    hashes, status, receipt = bundle["hashes"], bundle["status"], bundle["receipt"]
    check(source_hashes(clone) == hashes, f"{problem}: verified sources differ from the branch")
    old = json.loads((clone / "VERIFICATION.json").read_text())
    check(old.get("benchmark_commit") != bench, "VERIFICATION.json is already on the current benchmark")
    check(old.get("problem_id", problem) == problem, f"VERIFICATION.json is for {old.get('problem_id')}")
    new = build_verification(problem, old, bundle, head, host, bench)
    readme = clone / "README.md"
    text = readme_with_current(readme.read_text(), head=head, receipt_sha256=status["receipt_sha256"],
                               files=len(hashes), date=str(receipt["started_at"])[:10], bench=bench,
                               flow=bundle["config"].get("evaluation_flow", "comparator"))
    (clone / "VERIFICATION.json").write_text(json.dumps(new, indent=2) + "\n")
    readme.write_text(text)
    message = (f"Verify {problem} against current benchmark {bench[:8]}\n\n"
               f"Official verify_candidate.py flow (lake test, Nanoda + Lean kernel) on the unchanged sources of\n"
               f"{head[:12]}, benchmark {bench[:8]}; receipt {status['receipt_sha256'][:16]}. Sources unchanged;\n"
               f"VERIFICATION.json and README record it, the earlier record is kept.\n\n{TRAILER}\n")
    git(clone, "add", "VERIFICATION.json", "README.md")
    git(clone, "commit", "-q", "-F", "-", stdin=message)
    new_head = git(clone, "rev-parse", "HEAD").strip()
    changed = git(clone, "diff", "--name-only", head, new_head).split()
    check(sorted(changed) == ["README.md", "VERIFICATION.json"], f"commit changed {changed}")
    check(git(clone, "rev-parse", f"{new_head}^").strip() == head, "new commit is not a child of the verified one")
    return {"problem_id": problem, "head": head, "new_head": new_head,
            "receipt_sha256": status["receipt_sha256"],
            "verification_wall_seconds": round(receipt["elapsed_seconds"], 2), "files": len(hashes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clone", type=Path)
    parser.add_argument("problem")
    parser.add_argument("run", nargs="?", help="run dir name under <base>/runs (default: PROBLEM)")
    parser.add_argument("--host", required=True, help="the verify host (ssh name)")
    parser.add_argument("--base", default="lean-eval-vcur", help="run base, relative to ~ on host")
    parser.add_argument("--ssh-wrapper", help="script called as WRAPPER HOST COMMAND; "
                                              "default: the publisher's ssh options")
    args = parser.parse_args()
    if args.ssh_wrapper:
        ssh = [args.ssh_wrapper]
    else:
        spec = importlib.util.spec_from_file_location("leh_publish",
                                                      Path(__file__).resolve().parents[1] / "publish.py")
        publish = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publish)
        ssh = publish.SSH

    def remote_json(path: str):
        out = subprocess.run([*ssh, args.host, f"cat {path}"], check=True, capture_output=True, text=True).stdout
        return json.loads(out)

    run = f"{args.base}/runs/{args.run or args.problem}"
    status = remote_json(f"{run}/status.json")
    bundle = {"status": status, "config": remote_json(f"{run}/config.json"),
              "hashes": remote_json(f"{run}/source-sha256.json"), "receipt": remote_json(status["receipt"])}
    result = stage_clone(args.clone, args.problem, bundle, host=args.host)
    print(f"{args.problem}\t{result['head']}\t{result['new_head']}\t{result['receipt_sha256']}\t"
          f"{round(result['verification_wall_seconds'])}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
