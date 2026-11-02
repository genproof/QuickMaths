"""Host side: record a lane proved outside the fleet (by hand, or by a hedge on another host). Copies
LANE_DIR (on this host; it must hold Main.lean and no code-level sorry) into a hand job
<own root>/hand-jobs/<pid>-<lane>-<ts>/workspace/Submission/Lanes/<lane>, stops the lane's running
lineage, and marks the lane done with that job. Build artifacts (.lake, *.olean, *.ilean) are skipped.

Usage: ssh HOST python3 - PID LANE LANE_DIR REASON < hand_done.py
"""
import json, os, shutil, sys, time
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 4 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, lane, src, reason = ARGS[0], ARGS[1], Path(ARGS[2]).expanduser(), ARGS[3]
if not (src / "Main.lean").is_file():
    raise SystemExit(f"{src} has no Main.lean")
path = F.OWN / "programs" / f"{pid}.json"
info = ((F.read_json(path, {}) or {}).get("lanes") or {}).get(lane)
if not info or info.get("done"):
    raise SystemExit(f"lane {lane} of {pid} is missing or already done")
job = F.OWN / "hand-jobs" / f"{pid}-{lane}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
dest = job / "workspace" / F.LANES_DIR / lane
shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".lake", "*.olean", "*.ilean"))
bad = [f.relative_to(dest).as_posix() for f in dest.rglob("*.lean") if F.code_sorry(f)]
if bad:
    shutil.rmtree(job)
    raise SystemExit(f"sorry in the hand lane files {bad}")
with F.flock(F.OWN / "feeder.lock", blocking=True):
    p = F.read_json(path, {})
    info = p["lanes"][lane]
    if info.get("done"):
        raise SystemExit(f"lane {lane} became done meanwhile; {job} is unused")
    sub = info.get("claim") or {}
    pending = []
    if sub.get("runner") and sub.get("status") == "running":
        root = Path(sub["runner"])
        state = F.read_json(root / "state.json", {}) or {}
        manifest = F.read_json(root / "manifest.json", {}) or {}
        li = F.lineage(manifest.get("assignments", []), state.get("history", {}), sub["base"])
        F.stop_lineage(root, li["pending"], state)
        pending = li["pending"]
        sub.update(status="stopped", stopped_reason=reason[:300], stopped_at=F.iso())
    info.update(done=True, done_job=str(job), done_by="operator-hand", done_at=F.iso())
    F.write_json(path, p)
    F.log(f"program {pid}: lane {lane} done by hand in {job.name} ({reason[:200]})")
print(json.dumps({"lane": lane, "done_job": str(job), "stopped": pending,
                  "files": sum(1 for _ in dest.rglob("*.lean"))}))
