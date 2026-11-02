"""Host side: re-open ONE lane of an active program on the files of JOB (a job directory whose workspace
holds the lane directory, usually the lane's last round). The feeder starts the lane on the next idle
runner with those files and the job's notes. This is how a HELD lane (its round died with its
supervisor) or a lane stopped by stop_lane.py goes back to work. Refuses a running or done lane.

Usage: ssh HOST python3 - PID LANE JOB REASON < relane.py
"""
import json, os, sys
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 4 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, lane, job, reason = ARGS[0], ARGS[1], Path(ARGS[2]), ARGS[3]
with F.flock(F.OWN / "feeder.lock", blocking=True):
    path = F.OWN / "programs" / f"{pid}.json"
    p = F.read_json(path, {}) or {}
    if p.get("status") != "lanes":
        raise SystemExit(f"program {pid} is {p.get('status')!r}, not in lanes")
    info = p["lanes"][lane]
    if info.get("done"):
        raise SystemExit(f"lane {lane} is already done")
    if not (job / "workspace" / info["dir"]).is_dir():
        raise SystemExit(f"{job} has no {info['dir']}")
    sub = info.get("claim")
    if sub and sub.get("status") == "running":
        raise SystemExit(f"lane {lane} is still running ({sub['base']}); stop_lane.py first")
    if info.pop("claim", None):
        info.setdefault("previous_claims", []).append(sub)
    info["resume_job"] = str(job)
    F.write_json(path, p)
    F.log(f"program {pid}: lane {lane} re-opened by operator on {job.name} ({reason})")
print(json.dumps({lane: info["resume_job"]}))
