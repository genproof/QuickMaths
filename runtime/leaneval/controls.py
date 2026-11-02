"""Run one real official verification control, without a model or a submission."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from pathlib import Path

from leaneval.artifacts import export_bundle, git_tree, load_bundle
from leaneval.planning import profile
from leaneval.verifier import accept_receipt, atomic_json, verify

CASES = ("positive", "sorry", "axiom", "weakened", "missing")


def run_control(case: str, workbench: Path, root: Path, seconds: int) -> dict:
    directory = root / "controls" / (case + "-" + uuid.uuid4().hex)
    repo = directory / "input"
    repo.mkdir(parents=True)
    original = git_tree(workbench / "vendor/lean-eval", profile()["benchmark"]["commit"],
                        "generated/two_plus_two")
    submission = original["Submission.lean"].decode()
    if case == "positive":
        submission = submission.replace("  sorry", "  norm_num")
    elif case == "axiom":
        submission = submission.replace("namespace Submission", "namespace Submission\n"
                                        "axiom forbidden : (2 : Nat) + 2 = 4")
        submission = submission.replace("  sorry", "  exact forbidden")
    elif case == "weakened":
        submission = submission.replace("(2 : Nat) + 2 = 4", "True")
        submission = submission.replace("  sorry", "  trivial")
    elif case == "missing":
        submission = "import Mathlib\nnamespace Submission\nend Submission\n"
    original["Submission.lean"] = submission.encode()
    # Even a malicious input-repo build config must never enter the final workspace.
    original["lakefile.toml"] = b'name = "poisoned"\n'
    original["config.json"] = b'{"theorem_names": [], "enable_nanoda": false}\n'
    for name, data in original.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=LeanEval controls", "-c",
                 "user.email=controls@localhost", "commit", "-qm", f"Control: {case}"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                     text=True).strip()
    bundle_path = directory / "bundle"
    bundle = export_bundle(repo, commit, ".", "two_plus_two", bundle_path, controls=True)
    receipt = verify(bundle_path, workbench, root, seconds=seconds, controls=True)
    accepted = accept_receipt(root, receipt, bundle)
    success = accepted if case == "positive" else receipt["status"] == "rejected" and not accepted
    stale_rejected = None
    if case == "positive" and accepted:
        # Do not alter the successful artifact. A different committed proof gets a new identity.
        (repo / "Submission.lean").write_text(submission + "\n-- changed after verification\n")
        subprocess.run(["git", "-C", str(repo), "add", "Submission.lean"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=LeanEval controls", "-c",
                        "user.email=controls@localhost", "commit", "-qm", "Changed artifact"],
                       check=True)
        later_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                               text=True).strip()
        export_bundle(repo, later_commit, ".", "two_plus_two", directory / "changed", controls=True)
        changed = load_bundle(directory / "changed", controls=True)
        stale_rejected = not accept_receipt(root, receipt, changed)
        success = success and stale_rejected
    result = {"case": case, "control_passed": success, "verifier_status": receipt["status"],
              "run_id": receipt["run_id"], "artifact_digest": bundle.identity,
              "stale_artifact_rejected": stale_rejected, "input_commit": commit,
              "bundle": str(bundle_path), "wall_seconds": receipt["wall_seconds"]}
    atomic_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    result = run_control(args.case, args.workbench.resolve(), args.runs.resolve(), args.seconds)
    print(json.dumps(result, indent=2))
    return 0 if result["control_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
