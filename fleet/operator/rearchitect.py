"""Host side: re-architect a lane program NOW, with every lane's work. Use it when a lane shows that an
interface is false or badly cut while other lanes still run (the feeder re-architects by itself only
after a lane ENDS without a proof). It
  1. gives every running lane's round a controlled STOP and waits (at most 15 min) for those runners;
  2. merges the base proof files and each lane's directory (done job, else its last round) into
     seeds/<pid>-rearch-<ts>/workspace, writes REASON under `## OPERATOR RE-ARCHITECTURE` in its NOTES.md
     and appends each unfinished lane's own notes (headings demoted, as the feeder does for splits);
  3. sends the program back to the architect on those files (ARCHITECT+REARCHITECT+ARCHITECT_FIX with
     REASON). Lanes whose statements the new architecture keeps and that are proved stay done.

TRIGGER_LANE is the lane that showed the problem. REASON is text, or @FILE for a text file on this host;
name the false statement and the corrected interface. --expect-route NAME refuses unless the program's
`route` field is NAME (a guard when several programs of one problem were tried).

Usage: ssh HOST python3 - PID TRIGGER_LANE REASON [--expect-route NAME] < rearchitect.py
"""
import json, os, shutil, sys, time
from pathlib import Path
ARGS = sys.argv[1:]
route = None
if ARGS[3:4] == ["--expect-route"] and len(ARGS) == 5:
    route, ARGS = ARGS[4], ARGS[:3]
if len(ARGS) != 3 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

IDLE = ("idle_waiting_for_assignments", "stopped")
pid, trigger, reason = ARGS
reason = Path(reason[1:]).read_text().strip() if reason.startswith("@") else reason.strip()
if not reason:
    raise SystemExit("REASON is empty")
path = F.OWN / "programs" / f"{pid}.json"
with F.flock(F.OWN / "feeder.lock", blocking=True):
    p = F.read_json(path, {}) or {}
    if p.get("status") != "lanes" or trigger not in (p.get("lanes") or {}):
        raise SystemExit(f"program {pid} is {p.get('status')!r} with lanes {sorted(p.get('lanes') or {})}; nothing done")
    if route is not None and (p.get("route") or "") != route:
        raise SystemExit(f"program {pid} is on route {p.get('route')!r}, not {route!r}; nothing done")
    roots = []
    for lane, info in p["lanes"].items():
        sub = info.get("claim") or {}
        if info.get("done") or sub.get("status") != "running":
            continue
        root = Path(sub["runner"])
        state = F.read_json(root / "state.json", {}) or {}
        manifest = F.read_json(root / "manifest.json", {}) or {}
        li = F.lineage(manifest.get("assignments", []), state.get("history", {}), sub["base"])
        F.stop_lineage(root, li["pending"], state)
        sub["status"] = "stopped"
        roots.append(root)
    F.write_json(path, p)
t0 = time.time()
while time.time() - t0 < 900 and any((F.read_json(r / "state.json", {}) or {}).get("status") not in IDLE
                                      for r in roots):
    time.sleep(5)
with F.flock(F.OWN / "feeder.lock", blocking=True):
    p = F.read_json(path, {})
    seed = F.OWN / "seeds" / f"{pid}-rearch-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    ws = seed / "workspace"
    base_ws = Path(p["base_job"]) / "workspace"
    for f in sorted(base_ws.rglob("*")):
        rel = f.relative_to(base_ws).as_posix()
        if f.is_file() and not f.is_symlink() and F.PROOF_FILE.fullmatch(rel) and not rel.startswith(".lake/"):
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, ws / rel)
    notes = ws / "NOTES.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text((notes.read_text(errors="replace").rstrip() if notes.is_file() else "")
                     + f"\n\n## OPERATOR RE-ARCHITECTURE (trigger: lane {trigger})\n\n{reason}\n")
    jobs = {}
    for lane, info in p["lanes"].items():
        sub = info.get("claim") or {}
        job = Path(info["done_job"]) if info.get("done") else (F.last_round_job(sub) if sub else None)
        if job is None and info.get("resume_job"):
            job = Path(info["resume_job"])
        if not job or not (job / "workspace" / info["dir"]).is_dir():
            continue
        jobs[lane] = str(job)
        dst = ws / info["dir"]
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(job / "workspace" / info["dir"], dst, ignore=shutil.ignore_patterns(".*"))
        if not info.get("done"):
            F.append_lane_notes(ws, lane, job)
    (seed / "result.json").write_text("{}\n")
    F.write_json(seed / "SOURCE.json", {"base_job": p["base_job"], "lanes": jobs, "at": F.iso()})
    p.update(status="architect", architect=None, rearchitected=int(p.get("rearchitected", 0)) + 1,
             rearchitect={"lane": trigger, "job": jobs.get(trigger), "at": F.iso()},
             architect_fix={"job": str(seed), "reason": reason[:4000], "at": F.iso()},
             previous_lanes=p["lanes"])
    F.write_json(path, p)
    F.log(f"program {pid}: operator re-architecture with all lanes' work ({seed.name}); lanes {sorted(jobs)}")
print(json.dumps({"seed": str(seed), "lanes": jobs, "waited_s": round(time.time() - t0)}, indent=1))
