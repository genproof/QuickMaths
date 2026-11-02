"""Host side, read-only: write a lane program as a gzip tar to stdout: the base (architect) proof files,
each lane's directory from its done job or its latest round, and each unfinished lane's own NOTES.md
from that round. Input of adopt_prog.py (move the program to another host) and hedge_lane.py (a
second attempt at one lane on another host).

Layout (job layout, so the feeder reads each part as a job):
  base/workspace/{Submission.lean,Submission/**,NOTES.md}
  <lane>/workspace/Submission/Lanes/<lane>/...    the lane directory
  <lane>/workspace/NOTES.md                        open lanes only: the lane round's own notes
  MANIFEST.json                                    problem_id, base_job, lanes {done, job}

Usage: ssh HOST python3 - PID < export_prog.py > export.tgz
"""
import io, json, os, sys, tarfile, time
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 1 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid = ARGS[0]
p = F.read_json(F.OWN / "programs" / f"{pid}.json")
if not p or not p.get("base_job"):
    raise SystemExit(f"no program {pid} with an architecture on this host")
base = Path(p["base_job"])
out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz")


def add(path: Path, arc: str) -> None:
    out.add(str(path), arcname=arc, recursive=False)


man = {"problem_id": pid, "base_job": str(base), "lanes": {}, "exported_at": F.iso(), "host": F.HOST}
for f in sorted((base / "workspace").rglob("*")):
    rel = f.relative_to(base / "workspace").as_posix()
    if f.is_file() and not f.is_symlink() and F.PROOF_FILE.fullmatch(rel) and not rel.startswith(".lake/"):
        add(f, f"base/workspace/{rel}")
for lane, info in (p.get("lanes") or {}).items():
    job = Path(info["done_job"]) if info.get("done") else (F.last_round_job(info["claim"]) if info.get("claim") else None)
    if job is None and info.get("resume_job"):
        job = Path(info["resume_job"])  # re-opened, not assigned yet: its files are the resume job's
    man["lanes"][lane] = {"done": bool(info.get("done")), "job": str(job) if job else None}
    if not job or not (job / "workspace" / info["dir"]).is_dir():
        continue
    d = job / "workspace" / info["dir"]
    for f in sorted(d.rglob("*")):
        if f.is_file() and not f.is_symlink() and not any(x.startswith(".") for x in f.relative_to(d).parts):
            add(f, f"{lane}/workspace/{info['dir']}/{f.relative_to(d).as_posix()}")
    notes = next((job / "workspace" / n for n in ("NOTES.md", "NOTES.prev.md") if (job / "workspace" / n).is_file()), None)
    if notes and not info.get("done"):
        add(notes, f"{lane}/workspace/NOTES.md")
data = json.dumps(man, indent=1).encode()
ti = tarfile.TarInfo("MANIFEST.json")
ti.size, ti.mtime = len(data), time.time()
out.addfile(ti, io.BytesIO(data))
out.close()
