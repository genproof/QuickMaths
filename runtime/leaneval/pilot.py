"""Deterministic, offline requests for the first bounded local B0/B1 tranche.

This module reads explicitly selected source files only. It never runs a model,
starts a host, verifies a proof, or publishes a benchmark submission.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from leaneval.artifacts import digest, problem_spec
from leaneval.planning import benchmark

PROBLEMS = (
    "annals_equiangular_lines_fixed_angle",
    "annals_improved_bounds_sunflower_lemma",
    "annals_unit_conjecture",
    "dimitrov",
)
HOSTS = ("host-1", "host-2", "host-3", "host-4", "host-5")
REQUESTED_CONFIG = {"model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
_MAX_SOURCE_BYTES = 131072
_MAX_LIBRARY_FILES = 4


@dataclass(frozen=True)
class PilotBudget:
    """Caller-selected tranche limits including retries, tools and verification.

    Tokens are an accounting threshold: a Claude Code CLI supervisor polls stream
    usage and may overshoot; this is not hard token pre-admission enforcement.
    """

    attempt_wall_seconds: int
    attempt_tokens: int
    total_wall_seconds: int
    total_tokens: int

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.total_wall_seconds < self.attempt_wall_seconds:
            raise ValueError("Total wall limit must accommodate one attempt")
        if self.total_tokens < self.attempt_tokens:
            raise ValueError("Total token limit must accommodate one attempt")


def build_pilot_manifest(
    budget: PilotBudget, *, reference_exposure: str, pilot_id: str = "local-pilot-v1",
    host_policy: str = "rotating",
) -> dict[str, Any]:
    """Build eight paired requests; completion dependencies prohibit racing.

    Scheduling is work-conserving but must honor host exclusivity and each
    ``requires_completed`` dependency. ``paired_same_host`` keeps each pair on
    host-1 through host-4 and leaves host-5 for a separate audit; the default
    preserves rotating host assignments. The fifth host starts as a verification /
    librarian reserve; any separate audit needs its own budget and journal.
    """
    if host_policy not in {"rotating", "paired_same_host"}:
        raise ValueError("Host policy must be rotating or paired_same_host")
    if not reference_exposure.strip() or not pilot_id.strip():
        raise ValueError("Declare reference exposure and a nonempty pilot ID")
    catalog = benchmark()
    attempts = []
    for wave in range(2):
        for index, problem_id in enumerate(PROBLEMS):
            arm = "B0" if (index + wave) % 2 == 0 else "B1"
            attempt_id = f"{pilot_id}-{index + 1}-{arm}"
            previous_arm = "B1" if arm == "B0" else "B0"
            attempts.append({
                "attempt_id": attempt_id, "problem_id": problem_id, "arm": arm,
                "statement_revision": problem_spec(problem_id)["statement_revision"],
                "host": (HOSTS[index] if wave == 0 or host_policy == "paired_same_host"
                         else HOSTS[(index + 4) % len(HOSTS)]),
                "order": len(attempts) + 1,
                "requires_completed": [] if wave == 0 else [
                    f"{pilot_id}-{index + 1}-{previous_arm}"
                ],
                "requested_config": dict(REQUESTED_CONFIG), "effective_config": None,
            })
    return {
        "schema_version": 1, "pilot_id": pilot_id, "status": "prepared_not_executed",
        "scope": "local proof generation only; no benchmark submission",
        "benchmark": {"commit": catalog["benchmark_commit"], "group": catalog["group"],
                      "scope": catalog["scope"]},
        "budget": asdict(budget),
        "budget_scope": "all attempts, retries, tool compute and final verification",
        "token_enforcement": "polled accounting threshold; overshoot possible",
        "backend": "existing Claude Code subscription",
        "usd_cost": None, "usd_cost_reason": "No per-token subscription price known",
        "reference_exposure": reference_exposure,
        "requested_config": dict(REQUESTED_CONFIG), "effective_config": None,
        "hosts": list(HOSTS), "host_policy": host_policy,
        "initial_reserve": {"host": HOSTS[-1], "roles": ["verification", "librarian"],
                            "separate_audit_requires_separate_budget": True},
        "scheduler": {"one_active_attempt_per_problem": True,
                      "one_active_attempt_per_host": True, "paired_order": "interleaved"},
        "cache_policy": "isolated attempts; no cross-arm proof or feedback reuse",
        "workspace_policy": "fresh independent workspace for every attempt",
        "audit": {"host": HOSTS[-1], "problem_id": "weak_goldbach",
                  "included_in_eight_attempts": False, "requires_separate_budget": True},
        "features": {"reviewer": False, "dag": False, "external_prover_api": False},
        "attempts": attempts,
    }


def _source(workspace: Path, name: str) -> dict[str, str]:
    relative = PurePosixPath(name)
    if (relative.is_absolute() or ".." in relative.parts or str(relative) != name
            or relative.suffix != ".lean"):
        raise ValueError("Source whitelist requires normalized relative .lean paths")
    path = workspace / name
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError("Source symlinks are forbidden")
    if not path.is_file() or not path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError(f"Source missing or outside workspace: {name}")
    if path.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds the bounded brief limit")
    data = path.read_bytes()
    return {"path": name, "sha256": hashlib.sha256(data).hexdigest(),
            "text": data.decode("utf-8")}


def build_pilot_request(
    manifest: dict[str, Any], attempt_id: str, workspace: Path, *,
    library_files: tuple[str, ...] = (), effective_config: dict[str, Any] | None = None,
    feedback: dict[str, Any] | None = None, steps: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Render an exact target plus optional bounded, explicit B1 source brief.

    ``feedback`` is a full snapshot with schema_version=1, revision (positive
    integer), full_state (text), and diagnostics (list). ``steps`` reference
    saved steps by step_id, revision, status and artifact_sha256. Supplied
    effective configuration is evidence from the actual session, never inferred.
    B1 library sources must be explicitly copied/selected under this workspace;
    public Solution.lean and prior Submission proofs are excluded.
    """
    if manifest.get("schema_version") != 1:
        raise ValueError("Unknown pilot manifest schema")
    if manifest["benchmark"]["commit"] != benchmark()["benchmark_commit"]:
        raise ValueError("Stale benchmark manifest")
    attempt = next((a for a in manifest["attempts"] if a["attempt_id"] == attempt_id), None)
    if attempt is None:
        raise ValueError("Unknown attempt")
    problem = problem_spec(attempt["problem_id"])
    if problem["statement_revision"] != attempt["statement_revision"]:
        raise ValueError("Stale statement revision")
    if attempt["requested_config"] != REQUESTED_CONFIG:
        raise ValueError("Pilot requires the pinned model and reasoning effort")
    target_sources = [_source(workspace, name) for name in
                      ("Challenge.lean", "ChallengeDeps.lean", "Submission.lean")]
    if feedback is None:
        feedback = {"schema_version": 1, "revision": 1,
                    "full_state": "Initial state: no Lean feedback recorded.", "diagnostics": []}
    if (feedback.get("schema_version") != 1 or type(feedback.get("revision")) is not int
            or feedback["revision"] <= 0 or not isinstance(feedback.get("full_state"), str)
            or not isinstance(feedback.get("diagnostics"), list)):
        raise ValueError("Feedback must be a versioned full-state snapshot")
    enhanced = None
    if attempt["arm"] == "B1":
        if not library_files or len(library_files) > _MAX_LIBRARY_FILES:
            raise ValueError("B1 requires 1 to 4 explicitly selected library source files")
        if len(set(library_files)) != len(library_files):
            raise ValueError("Duplicate library source")
        for name in library_files:
            if (PurePosixPath(name).name in {"Solution.lean", "Submission.lean"}
                    or "Submission" in PurePosixPath(name).parts):
                raise ValueError("Existing proofs cannot enter the library brief")
        sources = [_source(workspace, name) for name in library_files]
        if sum(len(s["text"].encode()) for s in sources) > _MAX_SOURCE_BYTES:
            raise ValueError("Library brief exceeds bounded total size")
        for step in steps:
            required = {"step_id", "revision", "status", "artifact_sha256"}
            if (not required <= step.keys() or not isinstance(step.get("step_id"), str)
                    or not step["step_id"] or type(step.get("revision")) is not int
                    or step["revision"] <= 0 or step.get("status") not in {
                        "conjecture", "Lean-checked", "official-verified", "failed",
                    } or not isinstance(step.get("artifact_sha256"), str)
                    or len(step["artifact_sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in step["artifact_sha256"])):
                raise ValueError("Step reference is missing its version or artifact identity")
        enhanced = {"schema_version": 1, "library_sources": sources,
                    "full_feedback": feedback, "step_references": list(steps),
                    "recall_policy": "Recall exact saved steps; recheck stale revisions"}
    elif library_files or steps:
        raise ValueError("B0 cannot receive enhanced library or step-reference context")
    contract = (
        "Work locally on the frozen statement and close ALL official holes. "
        "Preserve existing declaration statements, hypotheses, "
        "definition hole contracts and trusted files. "
        "Edit only Submission.lean and Submission/**/*.lean; no sorry, axioms or weakening. "
        "Checkpoint work and all failed diagnostics within the shared tranche limits. "
        "Only a fresh pristine official lake test with real comparator AND Nanoda replay, "
        "bound to the complete artifact, can establish official local pass. "
        "Lean compilation alone is not a pass. Never claim service acceptance, publish, "
        "submit, or contact external prover APIs."
    )
    request = {
        "schema_version": 1, "manifest_sha256": digest(manifest),
        "attempt": attempt, "benchmark": manifest["benchmark"], "problem": problem,
        "budget": manifest["budget"], "reference_exposure": manifest["reference_exposure"],
        "requested_config": dict(REQUESTED_CONFIG), "effective_config": effective_config,
        "target_sources": target_sources, "feedback": feedback,
        "enhanced_brief": enhanced, "contract": contract,
    }
    request["prompt"] = contract + "\n\nFrozen request:\n" + json.dumps(
        request, ensure_ascii=False, sort_keys=True, indent=2,
    )
    return request
