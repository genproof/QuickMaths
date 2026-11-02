"""Host side (the hedge host, not the program's host): start a route hedge for ONE lane of a lane program
that runs on another host. From the program's export (export_prog.py) it builds a seed in
seeds/<pid>-hedge-<lane>-<ts>/ (base proof files, every lane's directory, NOTES.md with an OPERATOR
HEDGE section naming ROUTE, then the lane's own notes) and queues a PARALLEL start_lineage directive in
this host's feeder inbox for RUNNER (preferred; another idle runner with quota may take it).

The hedge edits only Submission/Lanes/<lane>/, never replies READY_FOR_VERIFICATION, and does not touch
the program: whichever closes the lane first is taken, by copying its lane directory into the program
host and running hand_done.py there. ROUTE must name a route that differs from the lane's current one
(identical duplicates won 1 time in 17); it is text, or @FILE for a text file on this host.

EXPORT: the tarball, absolute or relative to the own root (e.g. uploads/export.tgz from run.py --upload).
Refuses when a program of the problem is active on this host (the feeder would reject the start).

Usage: ssh HOST python3 - PID LANE RUNNER EXPORT ROUTE [--rounds N] < hedge_lane.py
"""
import json, os, shutil, sys, tarfile, time
from pathlib import Path
ARGS = sys.argv[1:]
rounds = 3
if ARGS[5:6] == ["--rounds"] and len(ARGS) == 7 and ARGS[6].isdigit():
    rounds, ARGS = int(ARGS[6]), ARGS[:5]
if len(ARGS) != 5 or ARGS[0] in ("-h", "--help") or not 1 <= rounds <= 50:
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, lane, runner, export, route = ARGS
export = Path(export) if Path(export).is_absolute() else F.OWN / export
if route.startswith("@"):
    route = Path(route[1:]).read_text().strip()
if not route.strip():
    raise SystemExit("ROUTE is empty: name the different route this hedge takes")
if F.program_active(pid):
    raise SystemExit(f"a program of {pid} is active on this host; run the hedge on another host")
ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
seed = F.OWN / "seeds" / f"{pid}-hedge-{lane}-{ts}"
raw, part = seed.with_name(seed.name + ".export"), seed.with_name(seed.name + ".partial")
raw.mkdir(parents=True)
with tarfile.open(export) as tar:
    members = tar.getmembers()
    bad = [m.name for m in members if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts]
    if bad:
        raise SystemExit(f"refusing export with links or unsafe paths: {bad[:5]}")
    tar.extractall(raw, members=members,
                       **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
man = json.loads((raw / "MANIFEST.json").read_text())
if man.get("problem_id") != pid or lane not in man.get("lanes", {}):
    raise SystemExit(f"the export is of {man.get('problem_id')} with lanes {sorted(man.get('lanes', {}))}")
base = raw / "base" / "workspace"
for f in sorted(base.rglob("*")):
    rel = f.relative_to(base).as_posix()
    if f.is_file() and not f.is_symlink() and F.PROOF_FILE.fullmatch(rel):
        (part / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, part / rel)
for name in man["lanes"]:
    src = raw / name / "workspace" / F.LANES_DIR / name
    if src.is_dir():
        dst = part / F.LANES_DIR / name
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".*"))
origin = man.get("host") or "the program host"
notes = part / "NOTES.md"
notes.write_text((notes.read_text(errors="replace") if notes.is_file() else "") + f"""

## OPERATOR HEDGE ({ts}): parallel attempt at lane {lane} ONLY
The lane program on {origin} keeps running all lanes, including {lane}. This lineage is a hedge on a
DIFFERENT route for {lane}; whichever closes the lane first is taken (the operator copies the lane dir).
Route of this hedge: {route}
- Edit ONLY files under {F.LANES_DIR}/{lane}/ (new files there are fine). Do not touch other lanes,
  Interfaces.lean, Reduction.lean or shared libraries, and never change the lane's main statement.
- Goal: make {F.LANES_DIR}/{lane} sorry-free (its residuals in its own files).
- Never reply READY_FOR_VERIFICATION: the other lanes are still open; the operator integrates.
- Keep `./lean build Submission.Lanes.{lane}.Main` green at round end.
""")
lane_notes = (raw / lane / "workspace" / "NOTES.md").is_file()
if lane_notes:
    F.append_lane_notes(part, lane, raw / lane)
part.rename(seed)
shutil.rmtree(raw, ignore_errors=True)
did = f"op.hedge.{pid}.{lane}.{ts}"
d = {"id": did, "type": "start_lineage", "problem_id": pid, "role": "PARALLEL", "seed_dir": str(seed),
     "prefer_runner": runner, "max_rounds": rounds,
     "fields": {"parallel_of": f"the lane program's lane {lane} on {origin} (same residuals, its own route; "
                               f"see NOTES '## LANE {lane}'). This hedge's route: {route[:1500]}. "
                               f"Work ONLY in {F.LANES_DIR}/{lane}/ - see NOTES '## OPERATOR HEDGE'."},
     "reason": f"operator route hedge for {pid} lane {lane}", "target": {"problem_id": pid},
     "expires_at": time.time() + 3 * 3600}
(F.OWN / "inbox").mkdir(exist_ok=True)
tmp = F.OWN / "inbox" / f".{did}.tmp"
tmp.write_text(json.dumps(d, indent=1))
tmp.replace(F.OWN / "inbox" / f"{did}.json")
print(json.dumps({"seed": str(seed), "directive": did, "lane_notes": lane_notes, "max_rounds": rounds}))
