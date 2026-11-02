"""Host side: end the running lineage of PID AFTER its current round. No STOP: the running round
finishes normally, only its successor is cancelled (auto_continue off, unstarted rounds dropped).
Then either
  --program   turn the claim into a lane program seeded with the running round's job (status
              waiting_seed; the architect starts once that job has written result.json), or
  default     mark the claim `stopped` (no retry, no fresh start of PID on this host).

Usage: ssh HOST python3 - PID REASON [--program] < endround.py
"""
import json, os, sys
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) not in (2, 3) or ARGS[0] in ("-h", "--help") or ARGS[2:] not in ([], ["--program"]):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, reason, as_program = ARGS[0], ARGS[1], ARGS[2:] == ["--program"]
with F.flock(F.OWN / "feeder.lock", blocking=True):
    claims = F.read_json(F.OWN / "claims.json", {}) or {}
    c = claims.get(pid) or {}
    if c.get("status") != "running":
        raise SystemExit(f"claim {pid} is {c.get('status')!r}, not running")
    root = Path(c["runner"])
    state = F.read_json(root / "state.json", {}) or {}
    current = Path(state.get("current_job") or "").name
    with F.manifest_lock(root):
        manifest = json.loads((root / "manifest.json").read_text())
        info = F.lineage(manifest["assignments"], state.get("history", {}), c["base"])
        if current not in info["pending"]:
            raise SystemExit(f"the runner's current job {current!r} is not a pending round of {c['base']}")
        kept = []
        for a in manifest["assignments"]:
            if a["attempt_id"] in info["pending"] and a["attempt_id"] != current \
                    and a["attempt_id"] not in state.get("history", {}):
                continue  # never started: drop it
            if a["attempt_id"] == current:
                a["auto_continue"] = None
            kept.append(a)
        manifest["assignments"] = kept
        tmp = root / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2) + "\n")
        tmp.replace(root / "manifest.json")
    job = root / "jobs" / current
    if as_program:
        path = F.start_program(pid, job, claims)
        F.log(f"operator: {pid} lineage on {root.name} ends after {current}; program {path.name} seeded with it ({reason})")
    else:
        c.update(status="stopped", previous_status="running", stopped_at=F.iso(), stopped_reason=reason,
                 last_job=str(job))
        F.log(f"operator: {pid} lineage on {root.name} ends after {current}; claim stopped ({reason})")
    F.write_json(F.OWN / "claims.json", claims)
print(json.dumps({"pid": pid, "runner": root.name, "current": current,
                  "dropped": [n for n in info["pending"] if n != current], "program": as_program}))
