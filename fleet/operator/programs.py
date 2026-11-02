"""Host side, read-only: one line per lane program on this host: status, creation time, end reason,
and each role (architect, integrator, split architect, lanes) with its runner and status.

Usage: ssh HOST python3 - [PID ...] < programs.py
"""
import os, sys
from pathlib import Path
if sys.argv[1:2] in (["-h"], ["--help"]):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pids = sys.argv[1:]
for path in sorted((F.OWN / "programs").glob("*.json")):
    p = F.read_json(path, {}) or {}
    if pids and p.get("problem_id") not in pids:
        continue
    roles = []
    for name, sub in (("architect", p.get("architect")), ("integrator", p.get("integrator")),
                      ("split", (p.get("split") or {}).get("sub"))):
        if sub:
            roles.append(f"{name}:{F.wave_of(Path(sub['runner']))}:{sub.get('status')}")
    for name, lane in (p.get("lanes") or {}).items():
        sub = lane.get("claim") or {}
        where = F.wave_of(Path(sub["runner"])) if sub.get("runner") else "-"
        roles.append(f"{name}:{'done' if lane.get('done') else where}:{sub.get('status')}")
    print(path.stem, p.get("status"), str(p.get("created_at", ""))[:16], str(p.get("reason") or "")[:80],
          "|", " ".join(roles))
