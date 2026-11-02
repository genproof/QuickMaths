"""Read-only snapshot of one host's own runners for local/coordinator.py.

Runs ON the host: the coordinator streams this file over ssh as

  python3 -B - snapshot IDS_JSON     runners, live lineages, passes, programs, targets, account
                                     blocks, inbox outcomes of IDS_JSON (the coordinator's
                                     directive ids for this host whose outcome it has not seen)
  python3 -B - best PIDS_JSON        the best finished round of each problem on this host (fewest
                                     sorry with a passing build Submission), only for PIDS_JSON

It imports the deployed feeder (<own root>/feeder.py: LEH_OWN_ROOT, default ~/leh-fleet, or the
directory given as LEH_REMOTE_OWN_ROOT relative to the home) for its own measurements (lineage,
finished_round_job, proof_stats, best_round_job, ...) and prints one JSON object. It writes
nothing, reads no credentials and starts nothing.

Only feeder functions that exist since the 2026-10-14 P0 feeder are used, so a host that still
runs the older feeder is read correctly and reported with inbox_supported false.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

LIVE = ("running", "handoff", "paused")
PROGRAM_FINAL = ("passed", "failed", "stopped")
TARGET_KEYS = ("problem_id", "ref_min_modules", "n_solvers", "max_rounds", "title", "disabled")
ACTIVE_TACTIC = re.compile(r"active\s+tactic", re.IGNORECASE)


def parse_ts(text: Any) -> float | None:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def active_tactic(F: Any, workspace: Path | None) -> str | None:
    """The HANDOFF field `Active tactic` of NOTES.md (`- Active tactic: ...`, `**Active tactic:**`,
    `| Active tactic | ... |`), or None."""
    if workspace is None or not workspace.is_dir():
        return None
    for line in F.notes_text(workspace).splitlines()[:200]:
        match = ACTIVE_TACTIC.search(line)
        if not match:
            continue
        value = line[match.end():].strip().strip("*:|`- \t").strip()
        value = re.sub(r"\s*\|\s*$", "", value).strip("* ")
        if value:
            return value[:300]
    return None


def collect(F: Any, own: Path, ids: list[str], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    cfg = F.read_json(own / "config.json", {}) or {}
    status = F.read_json(own / "status.json", {}) or {}
    claims = F.read_json(own / "claims.json", {}) or {}
    holds = F.read_json(own / "holds.json", {}) or {}
    upgrade = getattr(F, "upgrade_keys", lambda t: t)  # a host feeder older than the rename has none
    raw_targets = {t["problem_id"]: upgrade(t) for t in (F.read_json(own / "targets.json", {}) or {}).get("targets", [])
                   if isinstance(t, dict) and t.get("problem_id")}
    targets = {pid: {k: t[k] for k in TARGET_KEYS if k in t} for pid, t in raw_targets.items()}
    roots = [Path(r) for r in cfg.get("runners", [])]
    notes = status.get("runners") or {}
    runners = {}
    for root in roots:
        wave = root.name.rsplit("-", 1)[-1]
        note = (notes.get(root.name) or {}).get("assignment")
        runners[wave] = {"root": str(root), "account": F.account_of(root, cfg), "note": note,
                         "idle": note == "idle", "stop": (root / "STOP").exists(), "held": wave in holds}
    states: dict[Path, dict[str, Any]] = {}

    def state_of(root: Path) -> dict[str, Any]:
        if root not in states:
            states[root] = F.read_json(root / "state.json", {}) or {}
        return states[root]

    lineages, passed, claim_index = [], [], {}
    for key, claim in claims.items():
        if not isinstance(claim, dict):
            continue
        pid = str(claim.get("problem_id") or key.split("~", 1)[0])
        claim_index[key] = {"problem_id": pid, "status": claim.get("status"), "host": claim.get("host"),
                            "role": claim.get("role")}
        if claim.get("status") == "passed":
            passed.append({"key": key, "problem_id": pid, "attempt": claim.get("passed_attempt"),
                           "runner": Path(claim.get("runner") or "").name.rsplit("-", 1)[-1] or None,
                           "ci_held": bool(hasattr(F, "ci_held") and F.ci_held(pid))})
        if claim.get("status") not in LIVE or not claim.get("base"):
            continue
        root = Path(claim.get("runner") or "/nonexistent")
        state = state_of(root)
        history = state.get("history") or {}
        manifest = F.read_json(root / "manifest.json", {}) or {}
        info = F.lineage(manifest.get("assignments", []), history, claim["base"])
        source = F.finished_round_job(claim, history)
        seeded = Path(claim.get("seeded_from") or "/nonexistent")
        if source is None and (seeded / "workspace").is_dir():
            source = seeded
        workspace = source / "workspace" if source else None
        stats = F.proof_stats(workspace) if workspace and workspace.is_dir() else None
        current = Path(state.get("current_job") or "/nonexistent")
        live_ws = (current / "workspace" if current.name.startswith(claim["base"])
                   and (current / "workspace").is_dir() else workspace)
        done = sum(1 for name, entry in history.items()
                   if F.round_index(claim["base"], name) is not None and (entry or {}).get("phase") == "done")
        lineages.append({
            "key": key, "problem_id": pid, "status": claim.get("status"), "role": claim.get("role"),
            "tier": F.tier_of(raw_targets.get(pid) or {}),
            "runner": root.name.rsplit("-", 1)[-1], "account": F.account_of(root, cfg), "base": claim["base"],
            "closeness": claim.get("closeness"), "round_open": bool(info["pending"]),
            "handoff_at": F.number((claim.get("handoff") or {}).get("at_ts")),
            "handoff_window": (claim.get("handoff") or {}).get("window"),
            "rounds_done": done, "source_job": str(source) if source else None,
            "source_sorry": stats["sorry"] if stats else None,
            "source_build_ok": bool(workspace and workspace.is_dir() and F.last_full_build_ok(workspace)),
            "source_complete": bool(workspace and workspace.is_dir() and F.round_complete(workspace)),
            "active_tactic": active_tactic(F, live_ws),
            "trying": [t for t, s in F.tactics(live_ws) if s == "trying"][:3] if live_ws and live_ws.is_dir() else [],
            "verifies": int(claim.get("verifies") or 0), "verify_exhausted": bool(claim.get("verify_exhausted")),
            "route_review": bool(claim.get("route_review")), "handoffs": int(claim.get("handoffs") or 0),
            "trajectory": (claim.get("trajectory") or [])[-8:]})
    programs = {}
    for path in sorted((own / "programs").glob("*.json")):
        program = F.read_json(path, {}) or {}
        lanes = program.get("lanes") or {}
        pid = str(program.get("problem_id") or path.stem)
        programs[pid] = {"status": program.get("status"), "created_at": program.get("created_at"),
                         "created_ts": parse_ts(program.get("created_at")), "lanes": len(lanes),
                         "lanes_done": sum(1 for lane in lanes.values() if (lane or {}).get("done")),
                         "reason": program.get("reason"), "route": program.get("route")}
    pending = sorted(p.stem for p in (own / "inbox").glob("*.json")) if (own / "inbox").is_dir() else []
    processed = F.read_json(own / "inbox-processed.json", {}) or {}
    results = {}
    for directive in ids:
        if directive in processed:
            results[directive] = processed[directive]
        elif (own / "inbox" / "processed" / f"{directive}.json").exists():
            results[directive] = {"status": "processed", "detail": "record pruned"}
        elif directive in pending:
            results[directive] = {"status": "pending"}
        else:
            results[directive] = {"status": "missing"}
    solved = (F.read_json(own / "leaderboard-cache.json", {}) or {}).get("our_solved") or []
    blocks = {}
    for account, block in (F.read_json(own / "account-blocks.json", {}) or {}).items():
        until = F.number((block or {}).get("until")) if isinstance(block, dict) else None
        if until and until > now:
            blocks[account] = {"until": until, "window": block.get("window")}
    return {"ok": True, "host": cfg.get("host"), "own_root": str(own), "checked_at": status.get("checked_at"),
            "checked_ts": parse_ts(status.get("checked_at")), "inbox_supported": hasattr(F, "process_inbox"),
            "runners": runners, "account_blocks": blocks, "lineages": lineages, "passed": passed,
            "programs": programs, "best": {}, "claims": claim_index, "targets": targets,
            "our_solved": sorted(solved), "inbox": {"pending": pending, "results": results},
            "alerts": (status.get("alerts") or [])[:20]}


def collect_best(F: Any, own: Path, pids: list[str]) -> dict[str, Any]:
    """Per problem, the best finished round on this host (feeder.best_round_job: fewest sorry
    among rounds whose last build Submission succeeded, ties to the latest; lane rounds
    excluded)."""
    cfg = F.read_json(own / "config.json", {}) or {}
    roots = [Path(r) for r in cfg.get("runners", [])]
    best = {}
    for pid in sorted(set(pids)):
        job = F.best_round_job(pid, roots)
        if job is None:
            continue
        stats = F.proof_stats(job / "workspace") or {}
        entry = F.job_entry(job) or {}
        best[pid] = {"job": str(job), "sorry": stats.get("sorry"), "lines": stats.get("lines"), "build_ok": True,
                     "finished_at": F.number(entry.get("finished_at")),
                     "runner": job.parent.parent.name.rsplit("-", 1)[-1],
                     "active_tactic": active_tactic(F, job / "workspace")}
    return best


def main() -> None:
    own = Path(os.environ.get("LEH_OWN_ROOT") or Path.home() / (os.environ.get("LEH_REMOTE_OWN_ROOT") or "leh-fleet"))
    os.environ["LEH_OWN_ROOT"] = str(own)
    sys.path.insert(0, str(own))
    import feeder as F  # noqa: PLC0415 - the deployed feeder on this host

    mode = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    values = [str(v) for v in json.loads(sys.argv[2])] if len(sys.argv) > 2 else []
    if mode == "best":
        print(json.dumps(collect_best(F, F.OWN, values), default=str))
    else:
        print(json.dumps(collect(F, F.OWN, values), default=str))


if __name__ == "__main__":
    main()
