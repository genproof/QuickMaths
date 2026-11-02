#!/usr/bin/env python3
"""Round features from a runner host's queues, for `leh evolve report` (read-only; stdlib, Python 3.10+).

    extract.py [--campaigns DIR] [--own DIR] [--feeder FILE] [--host NAME] > rounds.jsonl

Runs on the runner host itself (`leh evolve extract HOST` sends it there over ssh) and prints one JSON
object per round: the assignment (problem, kind, lineage, round, seeding), the controller's record of the
round (model stop reason, tokens, wall and pause time, verification status) and what the round left in its
workspace, measured with the feeder's own parsers (sorry count, proof lines, the RESIDUALS estimate, open
and proved rows, how many TACTICS rows are being tried or failed, the HANDOFF fields missing). Raw logs, NOTES.md text and
proof files never leave the host: only these numbers and short names do.

--campaigns: the queue roots' parent (default $LEH_CAMPAIGNS_ROOT or ~/projects/lean-eval-campaigns);
--own: the feeder's state directory (default $LEH_OWN_ROOT or ~/leh-fleet), for lineage roles and target
size hints; --feeder: the feeder module whose NOTES.md parsers are used (default <own>/feeder.py).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import socket
import sys
from pathlib import Path

SCHEMA = 1
ROUND = re.compile(r"^(?P<base>.+?)-r(?P<round>\d+)$")


def load_feeder(path: Path):
    sys.dont_write_bytecode = True  # -I ignores PYTHONDONTWRITEBYTECODE; leave no __pycache__ on the host
    spec = importlib.util.spec_from_file_location("leh_feeder_for_evolve", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"no feeder module at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def lineage_of(assignment: dict) -> tuple[str, int]:
    attempt = assignment["attempt_id"]
    auto = assignment.get("auto_continue") or {}
    if auto.get("base"):
        return auto["base"], int(auto.get("round") or 1)
    match = ROUND.match(attempt)
    return (match.group("base"), int(match.group("round"))) if match else (attempt, 1)


def workspace_features(feeder, workspace: Path) -> dict:
    if not (workspace / "Submission.lean").is_file():
        return {"workspace": False}
    stats = feeder.proof_stats(workspace) or {}
    table = feeder.residual_summary(workspace)
    statuses = list(table["keys"].values())
    return {"workspace": True, "sorry": stats.get("sorry"), "lines": stats.get("lines"),
            "estimate": table["estimate"], "open_rows": table["open"], "max_row": table["max"],
            "rows": table["rows"], "proved_rows": statuses.count("proved"), "refuted_rows": statuses.count("refuted"),
            "replaced_rows": statuses.count("replaced"),
            "trying_tactics": sum(1 for _, s in feeder.tactics(workspace) if s == "trying"),
            "failed_tactics": sum(1 for _, s in feeder.tactics(workspace) if s == "failed"),
            "handoff_missing": feeder.handoff_missing(workspace),
            "lanes": len(list((workspace / "Submission/Lanes").glob("*/Main.lean")))}


def pause_seconds(pauses) -> float:
    """Total usage-limit pause time of a round (each pause: started_at, ended_at or until)."""
    total = 0.0
    for pause in pauses or []:
        if isinstance(pause, dict) and isinstance(pause.get("started_at"), (int, float)):
            end = pause.get("ended_at") or pause.get("until")
            if isinstance(end, (int, float)) and end > pause["started_at"]:
                total += end - pause["started_at"]
    return round(total, 1)


def claim_roles(own: Path) -> dict[str, dict]:
    """lineage base -> {role, seeded}: the feeder's claims (the current lineage of each problem) and
    programs (architect, lanes, integrator); earlier lineages keep the role recorded in their assignment."""
    roles: dict[str, dict] = {}
    for key, claim in (read_json(own / "claims.json", {}) or {}).items():
        if isinstance(claim, dict) and claim.get("base"):
            roles[claim["base"]] = {"role": claim.get("role") or "FRESH", "claim": key,
                                    "seeded": bool(claim.get("seeded_from") or claim.get("route_review"))}
    for path in sorted((own / "programs").glob("*.json")):
        program = read_json(path, {}) or {}
        subs = [(name, sub, "LANE") for name, sub in (program.get("lanes") or {}).items()]
        subs += [(name, program.get(name), name.upper()) for name in ("architect", "integrator")]
        for name, sub, role in subs:
            if isinstance(sub, dict) and sub.get("base"):
                roles[sub["base"]] = {"role": sub.get("role") or role, "program": path.stem, "lane": name,
                                      "seeded": True}
    return roles


def rounds(campaigns: Path, own: Path, feeder, host: str):
    roles = claim_roles(own)
    targets = {t.get("problem_id"): t for t in (read_json(own / "targets.json", {}) or {}).get("targets", [])
               if isinstance(t, dict)}
    for manifest_path in sorted(campaigns.glob("*/manifest.json")):
        root = manifest_path.parent
        manifest = read_json(manifest_path, {}) or {}
        history = (read_json(root / "state.json", {}) or {}).get("history") or {}
        for order, assignment in enumerate(manifest.get("assignments") or []):
            if not isinstance(assignment, dict) or "attempt_id" not in assignment:
                continue
            attempt = assignment["attempt_id"]
            base, number = lineage_of(assignment)
            record = history.get(attempt) or {}
            model = record.get("model") or {}
            verification = record.get("verification") or {}
            target = targets.get(assignment.get("problem_id")) or {}
            label = roles.get(base) or {}
            yield {
                "schema": SCHEMA, "host": host, "runner": root.name, "order": order,
                "attempt": attempt, "problem": assignment.get("problem_id"), "lineage": base, "round": number,
                "kind": assignment.get("kind") or "fresh",
                "role": assignment.get("lineage_role") or label.get("role"),
                "program": label.get("program"), "lane": label.get("lane"),
                "seeded": bool(assignment.get("overlay_dir") or assignment.get("seed_bundle")
                               or (assignment.get("kind") == "continuation" and number == 1) or label.get("seeded")),
                "wall_budget": assignment.get("wall_seconds"),
                "phase": record.get("phase"), "finished_at": record.get("finished_at"),
                "model_status": model.get("status"), "stop_reason": model.get("stop_reason"),
                "tokens": model.get("observed_tokens"), "output_tokens": (model.get("usage") or {}).get("output_tokens"),
                "elapsed": model.get("elapsed_seconds"), "nudges": model.get("nudges"),
                "pause_seconds": pause_seconds(model.get("pauses")),
                "pauses": len(model.get("pauses") or []), "ready": model.get("ready_marker"),
                "verification": verification.get("status"), "verified": bool(verification.get("verified")),
                "verification_seconds": verification.get("verification_wall_seconds"),
                "ref_min_modules": target.get("ref_min_modules"), "n_solvers": target.get("n_solvers"),
                **workspace_features(feeder, root / "jobs" / attempt / "workspace"),
            }


# "Estimate: 2.5k …" or "Estimate: 1200 lines": the k must follow the number on the same line and not start a word.
ESTIMATE = re.compile(r"(?i:estimate):[ \t]*[~≈]?[ \t]*(\d+(?:\.\d+)?)[ \t]*([kK](?![A-Za-z]))?")
KIND = re.compile(r"Kind:\s*(new|reuse)", re.I)


def lean_lines(folder: Path) -> int:
    return sum(len(p.read_text(errors="replace").splitlines()) for p in folder.rglob("*.lean")
               if p.is_file() and p.name != "LaneNotes.lean") if folder.is_dir() else 0


def lane_records(own: Path, host: str):
    """One record per lane of every program: the architect's estimate and kind (from the lane's CONTRACT in the
    architecture base), the lines actually written, done or not, rounds, and whether the lane refuted a statement.
    Only counts and the contract's numbers leave the host, no proof text."""
    for path in sorted((own / "programs").glob("*.json")):
        program = read_json(path, {}) or {}
        base = Path(program.get("base_job") or "") / "workspace"
        for lane, info in (program.get("lanes") or {}).items():
            if not isinstance(info, dict):
                continue
            main = base / (info.get("dir") or f"Submission/Lanes/{lane}") / "Main.lean"
            contract = main.read_text(errors="replace") if main.is_file() else ""
            est = ESTIMATE.search(contract)
            kind = KIND.search(contract)
            sub = info.get("claim") or {}
            job = Path(info["done_job"]) if info.get("done_job") else None
            if job is None and sub.get("runner") and sub.get("base"):
                jobs = sorted(Path(sub["runner"]).glob(f"jobs/{sub['base']}*"), key=lambda j: j.stat().st_mtime)
                job = jobs[-1] if jobs else None
            yield {"schema": SCHEMA, "record": "lane", "host": host, "program": path.stem, "lane": lane,
                   "estimate": float(est.group(1)) * (1000 if est.group(2) else 1) if est else None,
                   "kind": kind.group(1).lower() if kind else None,
                   "lines": lean_lines(job / "workspace" / (info.get("dir") or "")) if job else 0,
                   "done": bool(info.get("done")), "rounds": sub.get("rounds"), "status": sub.get("status"),
                   "refuted": sorted(program.get("refuted") or {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    home = Path.home()
    parser.add_argument("--campaigns", type=Path,
                        default=Path(os.environ.get("LEH_CAMPAIGNS_ROOT") or home / "projects/lean-eval-campaigns"))
    parser.add_argument("--own", type=Path, default=Path(os.environ.get("LEH_OWN_ROOT") or home / "leh-fleet"))
    parser.add_argument("--feeder", type=Path)
    parser.add_argument("--host", default=socket.gethostname().split(".")[0])
    args = parser.parse_args(argv)
    feeder = load_feeder(args.feeder or args.own / "feeder.py")
    count = 0
    for record in rounds(args.campaigns, args.own, feeder, args.host):
        sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
        count += 1
    lanes = 0
    for record in lane_records(args.own, args.host):
        sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
        lanes += 1
    print(f"extract: {count} rounds from {args.campaigns}, {lanes} program lanes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
