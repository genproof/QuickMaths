#!/usr/bin/env python3
"""prep_current.py V SRC COMMIT PROBLEM TIMEOUT : make V a verify_candidate.py run dir for the current benchmark
(208b6b22), with V/source = the trimmed tree SRC (Submission.lean + Submission/**/*.lean only).

Default: verify (verify_run.py prepare): a clean benchmark checkout whose .lake/packages are the
workbench's (LEANEVAL_WORKBENCH, default ~/projects/lean-eval-workbench), verify_candidate.py and
workspace_manager.py from verify, the organizers' evaluator fetched at its pinned commit. On the
verify host these files sit next to this script in <base>/auto/ (verify_current.py installs them).

Runs on the verify host (python3.12) from run_lane.sh.
"""
import importlib.util, json, sys
from pathlib import Path
V, SRC, commit, problem, timeout = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
# lean-eval main since #665 (2026-10-22): evaluation by the toolchain's `lake comparator` in bubblewrap;
# verify picks that flow from the benchmark (verify_run.CURRENT_BENCHMARK).
BENCH = "208b6b22d396f28771dba56414b19f47cae5f2a7"


def harness():
    here = Path(__file__).resolve().parent
    path = next(p for p in (here / "verify_run.py", here.parents[2] / "verify/verify_run.py") if p.is_file())
    spec = importlib.util.spec_from_file_location("verify_run", path)
    verify_run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify_run)
    config = verify_run.prepare(Path(V), Path(SRC), commit, problem, timeout, benchmark=BENCH)
    print("prepared", problem, commit[:12], "source files", len(json.loads((Path(V) / "source-sha256.json").read_text())),
          "evaluator", config["evaluator_commit"][:12])


harness()
