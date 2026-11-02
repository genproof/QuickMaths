"""Host side, read-only: open RESIDUALS rows with their line estimates, parsed exactly as the
feeder's progress gate parses them (feeder.residual_rows).

  PID          every unfinished lane of the program PID: the lane's own open rows (keys that name
               `Lanes.<lane>.`), their summed estimate, and how many other open rows its notes carry
  NOTES ...    the open rows of each given NOTES.md file (absolute paths, or a job directory)

Usage: ssh HOST python3 - PID < residuals.py
       ssh HOST python3 - /path/to/job/workspace/NOTES.md [...] < residuals.py
"""
import os, sys
from pathlib import Path
ARGS = sys.argv[1:]
if not ARGS or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F


def open_rows(text: str) -> list[tuple[str, int | None]]:
    return [(r["key"], r["est"]) for r in F.residual_rows(text) if r["status"] == "open"]


if "/" in ARGS[0]:
    for arg in ARGS:
        path = Path(arg)
        path = path / "workspace" / "NOTES.md" if path.is_dir() else path
        if not path.is_file():
            print(arg, "missing")
            continue
        rows = open_rows(path.read_text(errors="replace"))
        print(f"{arg[-70:]} open={len(rows)} est={sum(e for _, e in rows if e)} {rows}")
    raise SystemExit(0)

pid = ARGS[0]
p = F.read_json(F.OWN / "programs" / f"{pid}.json")
if not p:
    raise SystemExit(f"no program {pid} on this host")
print("program", p.get("status"), p.get("route") or "")
for name, lane in (p.get("lanes") or {}).items():
    if lane.get("done"):
        continue
    c = lane.get("claim") or {}
    if not c.get("runner"):
        print(f"  {name:16s} unassigned")
        continue
    jobs = sorted(Path(c["runner"]).glob(f"jobs/{c['base']}*"), key=lambda j: j.stat().st_mtime)
    rows = open_rows(F.notes_text(jobs[-1] / "workspace")) if jobs else []
    own = [(k.rsplit(".", 1)[-1][:30], e) for k, e in rows if f"Lanes.{name}." in k]
    print(f"  {name:16s} r{c.get('rounds')} {str(c.get('status')):8s} est={sum(e for _, e in own if e):6d} "
          f"{own} other_open={len(rows) - len(own)}")
