"""Host side: re-open a STOPPED lane program. Each unfinished lane continues on its last round's files
and notes (lanes[...].resume_job), done lanes stay done; the feeder assigns the lanes to idle runners.
With --undo-rearchitect, a re-architecture that an infrastructure kill triggered (not a real lane
failure) is dropped: the architect and rearchitect fields go, the re-architecture count goes down.

Usage: ssh HOST python3 - PID REASON [--undo-rearchitect] < resume_prog.py
"""
import json, os, sys
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) not in (2, 3) or ARGS[0] in ("-h", "--help") or ARGS[2:] not in ([], ["--undo-rearchitect"]):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, reason, undo = ARGS[0], ARGS[1], ARGS[2:] == ["--undo-rearchitect"]
with F.flock(F.OWN / "feeder.lock", blocking=True):
    path = F.OWN / "programs" / f"{pid}.json"
    p = F.read_json(path, {}) or {}
    if p.get("status") != "stopped":
        raise SystemExit(f"program {pid} is {p.get('status')!r}, not stopped")
    reopened = {}
    for lane, info in p["lanes"].items():
        if info.get("done"):
            continue
        sub = info.pop("claim", None)
        if sub:
            job = F.last_round_job(sub)
            if job and (job / "workspace" / info["dir"]).is_dir():
                info["resume_job"] = str(job)
            info.setdefault("previous_claims", []).append(sub)
        reopened[lane] = info.get("resume_job")
    if undo and p.get("rearchitect"):
        p["undone_rearchitect"] = {"rearchitect": p.pop("rearchitect"), "architect": p.pop("architect", None)}
        p["rearchitected"] = max(0, int(p.get("rearchitected", 1)) - 1)
    p.update(status="lanes", reopened_at=F.iso(), reopened_reason=reason,
             previous_stop={"reason": p.pop("reason", None), "stopped_at": p.pop("stopped_at", None)})
    F.write_json(path, p)
    claims = F.read_json(F.OWN / "claims.json", {}) or {}
    claims[pid] = dict(claims.get(pid) or {}, status="program", program=str(path))
    F.write_json(F.OWN / "claims.json", claims)
    F.log(f"program {pid}: re-opened by operator ({reason}); lanes {sorted(reopened)}")
print(json.dumps(reopened, indent=1))
