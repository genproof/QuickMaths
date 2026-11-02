"""Host side: start a new lane program of PID seeded with the proof files of SEED_JOB (a job directory:
absolute, or `fleet-1-<runner>/<job>` under the campaigns root). The architect starts on the next idle
runner (once the seed job has a result.json). A STOPPED program file of PID is archived first under
programs-archive/; an active one is refused.

Usage: ssh HOST python3 - PID SEED_JOB < seed_program.py
"""
import os, shutil, sys
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 2 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, seed = ARGS
with F.flock(F.OWN / "feeder.lock", blocking=True):
    path = F.OWN / "programs" / f"{pid}.json"
    if path.exists():
        old = F.read_json(path, {}) or {}
        if old.get("status") != "stopped":
            raise SystemExit(f"program {pid} is {old.get('status')!r}; stop it first")
        archive = F.OWN / "programs-archive"
        archive.mkdir(exist_ok=True)
        shutil.move(str(path), str(archive / f"{pid}-{F.iso()[:19].replace(':', '')}.json"))
    print(F.create_program(pid, seed))
