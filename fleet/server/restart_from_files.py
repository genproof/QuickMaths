"""Operator: fresh 1-round attempt (new lineage budget) seeded with the proof files of a
finished job via the controller's overlay_dir. Runs under the feeder lock.

Why: a continuation inherits its lineage's total model-time budget (max_rounds x wall,
fixed at round 1), so a lineage started with max_rounds 1 cannot get another full round.
Usage: python3 -B restart_from_files.py [--rounds N] PROBLEM fleet-1-waveNNN SOURCE_JOB [...]
       (triples; SOURCE_JOB is a finished job under that root's jobs/, or "<other root>/<job>")
"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

CAMP = F.CAMPAIGNS
ARGS = sys.argv[1:]
ROUNDS = 1
if ARGS[:1] == ["--rounds"]:
    ROUNDS, ARGS = int(ARGS[1]), ARGS[2:]
if not ARGS or len(ARGS) % 3:
    raise SystemExit(__doc__)
PLAN = [tuple(ARGS[i:i + 3]) for i in range(0, len(ARGS), 3)]  # (problem, root, source job)

def main():
    cfg = F.read_json(F.OWN / "config.json") or {}
    defaults = dict(F.DEFAULTS, **(cfg.get("defaults") or {}))
    targets = {t["problem_id"]: t for t in (F.read_json(F.OWN / "targets.json", {}) or {})["targets"]}
    with F.flock(F.OWN / "feeder.lock", blocking=True):
        claims = F.read_json(F.OWN / "claims.json", {}) or {}
        for pid, rootname, src_job in PLAN:
            root = CAMP / rootname
            # SOURCE_JOB may name another runner's job as "<root>/<job>"; files are copied into this root.
            src_root, _, src_job = src_job.rpartition("/")
            job = (CAMP / src_root if src_root else root) / "jobs" / src_job
            state = json.loads((job / "result.json").read_text())
            assert state.get("status") in ("awaiting_verifier", "interrupted_unknown_usage"), state.get("status")
            overlay = F.seed_overlay(root, pid, job)
            manifest = json.loads((root / "manifest.json").read_text())
            base = F.next_base(root, manifest["assignments"], pid)
            target = dict(targets[pid], kind="fresh", max_rounds=ROUNDS,
                          overlay_dir=overlay)
            target.pop("disabled", None)
            assignment = F.make_assignment(target, base, F.sync_brief(root, base, pid), defaults)
            F.append_assignment(root, assignment)
            claims[pid] = {"status": "running", "runner": str(root), "base": base, "claimed_at": F.iso(),
                           "kind": "fresh+overlay", "enqueued_by": "operator",
                           "seeded_from": src_job, "previous_claim": claims.get(pid)}
            F.write_json(F.OWN / "claims.json", claims)
            F.log(f"restart {pid} on {root.name} as {base} with overlay {overlay}")
            print(base, json.dumps(assignment)[:400])

main()
