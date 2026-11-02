"""Explicit fresh proof tranches seeded from controller snapshots, never blind resumes."""
from __future__ import annotations

import argparse
import fcntl
import json
import re
from pathlib import Path
from typing import Any

from leaneval.artifacts import digest, file_digest, load_bundle
from leaneval.finalization import snapshot_job
from leaneval.jobs import prepare_job
from leaneval.verifier import atomic_json


def seed_continuation(  # noqa: PLR0915
    snapshot_dir: Path | str, grant_file: Path | str, new_job: Path | str,
    workbench: Path | str, *, agent_root: Path | None = None,
) -> dict[str, Any]:
    snapshot_dir, grant_file = Path(snapshot_dir).resolve(), Path(grant_file).resolve()
    new_job, workbench = Path(new_job).resolve(), Path(workbench).resolve()
    snapshot = json.loads((snapshot_dir / "snapshot.json").read_text())
    claimed = snapshot.pop("snapshot_digest")
    if digest(snapshot) != claimed or snapshot["schema_version"] != 1:
        raise ValueError("Controller snapshot identity mismatch")
    if not snapshot.get("quiescence"):
        raise ValueError("Continuation requires a controller snapshot with quiescence evidence")
    parent_workspace = Path(snapshot["parent_job"]) / "workspace"
    if grant_file.is_relative_to(parent_workspace) or snapshot_dir.is_relative_to(parent_workspace):
        raise ValueError("Continuation grant and snapshot must be controller-owned")
    if (new_job.exists() or new_job == Path(snapshot["parent_job"])
            or new_job.is_relative_to(parent_workspace)):
        raise ValueError("Continuation requires a fresh job directory")
    grant = json.loads(grant_file.read_text())
    if (grant.get("schema_version") != 1 or
            not isinstance(grant.get("authorized_by"), str) or not grant["authorized_by"].strip() or
            grant.get("parent_snapshot_digest") != claimed):
        raise ValueError("Explicit tranche grant does not authorize this snapshot")
    for key in ("grant_id", "attempt_id"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", grant.get(key, "")):
            raise ValueError("Grant and attempt IDs must be safe nonempty names")
    for key in ("wall_seconds", "token_limit"):
        if type(grant.get(key)) is not int or grant[key] <= 0:
            raise ValueError("Grant requires positive wall and token bounds")
    # The bundle is always beside the receipt; don't follow an arbitrary stored path.
    bundle = load_bundle(snapshot_dir / "bundle", controls=snapshot["controls"])
    if bundle.identity != snapshot["artifact_digest"]:
        raise ValueError("Parent proof snapshot changed")
    from leaneval import generation_handoff
    generation_state = generation_handoff.load(snapshot_dir, snapshot, bundle.files)
    prompt = None
    if grant.get("prompt_file"):
        prompt_path = Path(grant["prompt_file"]).resolve()
        if prompt_path.is_relative_to(parent_workspace):
            raise ValueError("Tranche instructions must be controller-owned")
        prompt = prompt_path.read_text()
    claims = snapshot_dir.parent / "tranche-grants"
    claims.mkdir(exist_ok=True, mode=0o700)
    marker = claims / (grant["grant_id"] + ".json")
    with (claims / (grant["grant_id"] + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists():
            raise ValueError("Tranche grant already consumed; no automatic retry")
        provenance = {"schema_version": 1, "grant_digest": digest(grant),
                      "grant_file_sha256": file_digest(grant_file), "grant_id": grant["grant_id"],
                      "authorized_by": grant["authorized_by"], "parent_snapshot_digest": claimed,
                      "parent_job": snapshot["parent_job"],
                      "parent_ledger_sha256": snapshot["parent_ledger_sha256"],
                      "parent_config_sha256": snapshot["parent_config_sha256"],
                      "parent_artifact_digest": bundle.identity,
                      "parent_usage": snapshot["usage"],
                      "parent_billing_status": snapshot["billing_status"],
                      "parent_unresolved_attempts": snapshot.get("unresolved_attempts", []),
                      "fresh_session_required": True, "new_job": str(new_job)}
        from leaneval import lineage
        shared_budget = lineage.continuation_ticket(snapshot, grant, new_job)
        # Consume before preparation; crash recovery requires a new explicit grant.
        atomic_json(marker, {"phase": "reserved", **provenance})
        prepare_job(new_job, workbench, snapshot["problem_id"],
                    grant.get("variant", snapshot["variant"]), grant["attempt_id"],
                    wall_seconds=grant["wall_seconds"], token_limit=grant["token_limit"],
                    host=grant.get("host"), agent_root=agent_root, controls=snapshot["controls"])
        if shared_budget is not None:
            lineage.bind(new_job, shared_budget)
            provenance['lineage_id'] = shared_budget.snapshot()['policy']['id']
            provenance['lineage_policy_digest'] = shared_budget.snapshot()['policy_digest']
            provenance['lineage_reservation'] = shared_budget.member(new_job)['reserved']
        for name, content in bundle.files.items():
            target = new_job / "workspace" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        generation_handoff.restore(new_job / 'workspace', generation_state, bundle.files, bundle.identity)
        config_path = new_job / "config.json"
        config = json.loads(config_path.read_text())
        config["continuation_provenance"] = provenance
        atomic_json(config_path, config)
        with (new_job / "prompt.txt").open("a") as stream:
            stream.write("\nContinue the exact proof files restored from this immutable snapshot. "
                         "Recheck inherited helpers with ./lean; previous billing remains "
                         "unresolved "
                         "when indicated and is not erased by this new bounded tranche.\n")
            stream.write(json.dumps(provenance, sort_keys=True) + "\n")
            if prompt is not None:
                stream.write("\nController tranche instructions:\n" + prompt + "\n")
        from leaneval import frontier
        frontier.inherit(snapshot_dir, snapshot, new_job)
        atomic_json(new_job / "continuation.json", provenance)
        atomic_json(marker, {"phase": "prepared", **provenance})
    return provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot")
    snap.add_argument("--job", type=Path, required=True)
    snap.add_argument("--runs", type=Path, required=True)
    snap.add_argument("--allow-controlled-stop", action="store_true")
    seed = commands.add_parser("seed")
    seed.add_argument("--snapshot", type=Path, required=True)
    seed.add_argument("--grant", type=Path, required=True)
    seed.add_argument("--job", type=Path, required=True)
    seed.add_argument("--workbench", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        result = snapshot_job(args.job, args.runs, allow_controlled_stop=args.allow_controlled_stop)
    else:
        result = seed_continuation(args.snapshot, args.grant, args.job, args.workbench)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
