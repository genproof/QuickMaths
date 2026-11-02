"""Host side: move program lanes off an exhausted account. For each PID:LANE: controlled STOP of the lane's
round, wait (at most 15 min) until its runner is idle, restart the runner units named after --restart
(`sudo -n systemctl restart leh-runner@<wave>.service`; a controller reads its config.json token only
when it starts), then re-open each lane on its own files (resume_job). The feeder gives the lanes to
idle runners with quota.

Order: first point the runners at the new account in config.json "tokens" (fleet.json + fleet.py deploy,
or by hand), then run this. If the feeder then blocks the NEW account for a pause the old one hit, drop
that entry with unblock.py.

Usage: ssh HOST python3 - PID:LANE [PID:LANE ...] [--restart waveNNN ...] < switch_lanes.py
"""
import json, os, re, subprocess, sys, time
from pathlib import Path
ARGS = sys.argv[1:]
i = ARGS.index("--restart") if "--restart" in ARGS else len(ARGS)
pairs, restart = [a.split(":") for a in ARGS[:i]], ARGS[i + 1:]
if (not pairs or ARGS[0] in ("-h", "--help") or any(len(x) != 2 for x in pairs)
        or not all(re.fullmatch(r"[A-Za-z][A-Za-z_]*\d+", w) for w in restart)):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

IDLE = ("idle_waiting_for_assignments", "stopped")
roots, jobs = [], {}
with F.flock(F.OWN / "feeder.lock", blocking=True):
    programs = {pid: F.read_json(F.OWN / "programs" / f"{pid}.json", {}) or {} for pid, _ in pairs}
    for pid, lane in pairs:
        if not ((programs[pid].get("lanes") or {}).get(lane) or {}).get("claim"):
            raise SystemExit(f"lane {lane} of {pid} has no claim on this host; nothing done")
    for pid, lane in pairs:
        sub = programs[pid]["lanes"][lane]["claim"]
        root = Path(sub["runner"])
        state = F.read_json(root / "state.json", {}) or {}
        manifest = F.read_json(root / "manifest.json", {}) or {}
        li = F.lineage(manifest.get("assignments", []), state.get("history", {}), sub["base"])
        jobs[(pid, lane)] = F.current_job(sub) or F.last_round_job(sub)
        F.stop_lineage(root, li["pending"], state)
        sub["status"] = "stopped"
        roots.append(root)
    for pid, p in programs.items():
        F.write_json(F.OWN / "programs" / f"{pid}.json", p)
t0 = time.time()
while time.time() - t0 < 900 and any((F.read_json(r / "state.json", {}) or {}).get("status") not in IDLE
                                      for r in roots):
    time.sleep(5)
waited = round(time.time() - t0)
restarted = {}
for w in restart:
    done = subprocess.run(["sudo", "-n", "systemctl", "restart", f"leh-runner@{w}.service"], check=False, timeout=60)
    restarted[w] = done.returncode
with F.flock(F.OWN / "feeder.lock", blocking=True):
    out = {}
    for pid, lane in pairs:
        path = F.OWN / "programs" / f"{pid}.json"
        p = F.read_json(path, {})
        info = p["lanes"][lane]
        info.setdefault("previous_claims", []).append(info.pop("claim"))
        job = jobs[(pid, lane)]
        if job is not None:  # a lane assigned but not started yet has no files of its own: fresh start
            info["resume_job"] = str(job)
        F.write_json(path, p)
        out[f"{pid}:{lane}"] = job.name if job is not None else "fresh"
        F.log(f"program {pid}: lane {lane} re-opened by the operator on {job.name if job is not None else 'the base files'}")
print(json.dumps({"waited_s": waited, "restarted": restarted, "lanes": out}, indent=1))
