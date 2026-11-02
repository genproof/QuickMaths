"""Host side: adopt a lane program exported by export_prog.py as an active program on this host.
Done lanes stay done on their exported files; unfinished lanes continue on their exported files and
notes (resume_job), so the feeder starts each on the next idle runner of this host.

EXPORT is the tarball (absolute, or relative to the own root, e.g. uploads/export.tgz from
`run.py --upload`), extracted into seeds/<pid>-adopt-<ts>/, or an already extracted directory.
Refuses when a program of the problem is active here. Stop the program on its old host first
(stop_lineage.py PID - REASON there, or the coordinator), so the two never run at once.

Usage: ssh HOST python3 - EXPORT REASON < adopt_prog.py
"""
import json, os, shutil, sys, tarfile, time
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 2 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F


def extract(tgz: Path) -> Path:
    """Unpack an export into seeds/<pid>-adopt-<ts>/ (no links, no paths outside the directory)."""
    part = F.OWN / "seeds" / f".adopt-{os.getpid()}.partial"
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    with tarfile.open(tgz) as tar:
        members = tar.getmembers()
        bad = [m.name for m in members if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts]
        if bad:
            raise SystemExit(f"refusing export with links or unsafe paths: {bad[:5]}")
        tar.extractall(part, members=members,
                       **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    pid = json.loads((part / "MANIFEST.json").read_text())["problem_id"]
    seed = F.OWN / "seeds" / f"{pid}-adopt-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    part.rename(seed)
    return seed


src, reason = Path(ARGS[0]), ARGS[1]
src = src if src.is_absolute() else F.OWN / src
seed = src if src.is_dir() else extract(src)
man = json.loads((seed / "MANIFEST.json").read_text())
pid = man["problem_id"]
with F.flock(F.OWN / "feeder.lock", blocking=True):
    if F.program_active(pid):
        raise SystemExit(f"a program of {pid} is already active on this host")
    lanes = {}
    for lane, info in man["lanes"].items():
        entry = {"dir": f"{F.LANES_DIR}/{lane}", "done": bool(info["done"])}
        if (seed / lane / "workspace" / F.LANES_DIR / lane).is_dir():
            entry["done_job" if info["done"] else "resume_job"] = str(seed / lane)
        elif info["done"]:
            raise SystemExit(f"done lane {lane} has no files in the export")
        lanes[lane] = entry
    path = F.OWN / "programs" / f"{pid}.json"
    if path.exists():  # a finished program of the problem: keep it next to the others
        archive = F.OWN / "programs-archive"
        archive.mkdir(exist_ok=True)
        shutil.move(str(path), str(archive / f"{pid}-{F.iso()[:19].replace(':', '')}.json"))
    F.write_json(path, {"problem_id": pid, "status": "lanes", "seed_job": str(seed / "base"),
                        "base_job": str(seed / "base"), "created_at": F.iso(), "lanes": lanes,
                        "adopted_from": man, "adopted_reason": reason})
    claims = F.read_json(F.OWN / "claims.json", {}) or {}
    claims[pid] = {"status": "program", "program": str(path), "previous_claim": claims.get(pid)}
    F.write_json(F.OWN / "claims.json", claims)
    F.log(f"program {pid} adopted from {seed} ({reason}); lanes {sorted(lanes)}")
print(json.dumps({"seed": str(seed), "lanes": lanes}, indent=1))
