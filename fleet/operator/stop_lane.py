"""Host side: controlled STOP of ONE program lane's lineage, without re-opening it. The lane keeps its
claim with status `stopped`, so the feeder does not reassign it; relane.py re-opens it later.

Usage: ssh HOST python3 - PID LANE REASON < stop_lane.py
"""
import json, os, sys
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 3 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, lane, reason = ARGS
with F.flock(F.OWN / "feeder.lock", blocking=True):
    path = F.OWN / "programs" / f"{pid}.json"
    p = F.read_json(path, {}) or {}
    sub = ((p.get("lanes") or {}).get(lane) or {}).get("claim")
    if not sub:
        raise SystemExit(f"lane {lane} of {pid} has no claim on this host")
    root = Path(sub["runner"])
    state = F.read_json(root / "state.json", {}) or {}
    manifest = F.read_json(root / "manifest.json", {}) or {}
    li = F.lineage(manifest.get("assignments", []), state.get("history", {}), sub["base"])
    F.stop_lineage(root, li["pending"], state)
    sub.update(status="stopped", stopped_reason=reason[:300], stopped_at=F.iso())
    F.write_json(path, p)
    F.log(f"program {pid}: lane {lane} stopped by operator ({reason[:200]})")
print(json.dumps({"lane": lane, "base": sub["base"], "pending": li["pending"]}))
