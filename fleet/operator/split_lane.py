"""Host side: split ONE running lane of a program while the other lanes keep working. The lane's round
gets a controlled STOP; once its runner is idle (at most 15 min), program["split"] = {lane, from_job,
reason} asks the feeder for an ARCHITECT+SPLIT round on the next idle runner. That architect sees the
lane's files and its own notes and cuts it into smaller lanes without changing any other lane's
statement; a rejected split gets one fix round, then the lane continues on its own files.

Use it for a lane that is over the line cap or stuck on one row; REASON says which rows and why.

Usage: ssh HOST python3 - PID LANE REASON < split_lane.py
"""
import json, os, sys, time
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 3 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

IDLE = ("idle_waiting_for_assignments", "stopped")
pid, lane, reason = ARGS
path = F.OWN / "programs" / f"{pid}.json"
with F.flock(F.OWN / "feeder.lock", blocking=True):
    p = F.read_json(path, {}) or {}
    if p.get("status") != "lanes" or p.get("split"):
        raise SystemExit(f"program {pid} is {p.get('status')!r} with split {p.get('split')!r}; nothing done")
    info = (p.get("lanes") or {}).get(lane)
    if not info or info.get("done") or (info.get("claim") or {}).get("status") != "running":
        raise SystemExit(f"lane {lane} is not a running lane of {pid}")
    sub = info["claim"]
    root = Path(sub["runner"])
    state = F.read_json(root / "state.json", {}) or {}
    manifest = F.read_json(root / "manifest.json", {}) or {}
    li = F.lineage(manifest.get("assignments", []), state.get("history", {}), sub["base"])
    job = F.current_job(sub) or F.last_round_job(sub)
    if job is None:
        raise SystemExit(f"lane {lane} has no round with files yet; nothing to split")
    F.stop_lineage(root, li["pending"], state)
    sub["status"] = "stopped"
    F.write_json(path, p)
t0 = time.time()
while time.time() - t0 < 900 and (F.read_json(root / "state.json", {}) or {}).get("status") not in IDLE:
    time.sleep(5)
with F.flock(F.OWN / "feeder.lock", blocking=True):
    p = F.read_json(path, {})
    p["split"] = {"lane": lane, "from_job": str(job), "reason": reason, "requested_at": F.iso()}
    F.write_json(path, p)
    F.log(f"program {pid}: operator split of lane {lane} requested on {job.name} ({reason[:200]})")
print(json.dumps({"lane": lane, "from_job": job.name, "waited_s": round(time.time() - t0),
                  "runner_idle": (F.read_json(root / "state.json", {}) or {}).get("status") in IDLE}))
