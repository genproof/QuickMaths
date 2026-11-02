"""Host side, read-only: progress of every lane of the lane programs on this host: the lane's latest
job, its files, code lines and sorry count inside the lane directory, its status, the age of the
model stream and, for an open lane, the agent's last message.

Usage: ssh HOST python3 - [PID ...] < lanes.py
"""
import json, os, sys, time
from pathlib import Path
if sys.argv[1:2] in (["-h"], ["--help"]):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F


def last_text(events: Path, tail: int = 400_000) -> str:
    """The agent's last non-empty text message in the tail of a job's events.jsonl."""
    if not events.is_file():
        return ""
    with events.open("rb") as stream:
        stream.seek(max(0, events.stat().st_size - tail))
        data = stream.read().decode("utf-8", "replace")
    last = ""
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "assistant":
            for part in (e.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text", "").strip():
                    last = part["text"].strip().replace("\n", " ")
    return last


pids = sys.argv[1:]
for path in sorted((F.OWN / "programs").glob("*.json")):
    p = F.read_json(path, {}) or {}
    if pids and p.get("problem_id") not in pids:
        continue
    print("##", p.get("problem_id"), p.get("status"))
    for name, lane in (p.get("lanes") or {}).items():
        c = lane.get("claim") or {}
        if lane.get("done") and lane.get("done_job"):
            job = Path(lane["done_job"])
        elif c.get("runner"):
            jobs = sorted(Path(c["runner"]).glob(f"jobs/{c['base']}*"), key=lambda j: j.stat().st_mtime)
            job = jobs[-1] if jobs else None
        else:
            print(f"  {name}: {'DONE' if lane.get('done') else 'unassigned'}")
            continue
        if job is None:
            print(f"  {name}: no job yet")
            continue
        files = F.proof_paths(job / "workspace", lane["dir"]) if (job / "workspace" / lane["dir"]).is_dir() else []
        stats = F.proof_stats(job / "workspace", lane["dir"]) or {"sorry": "?", "lines": "?"}
        events = job / "events.jsonl"
        age = f"{(time.time() - events.stat().st_mtime) / 60:.0f}m" if events.is_file() else "-"
        print(f"  {name}: {'DONE ' if lane.get('done') else ''}{job.name[-40:]} files={len(files)} "
              f"lines={stats['lines']} sorry={stats['sorry']} status={c.get('status')} stream_age={age}")
        text = "" if lane.get("done") else last_text(events)
        if text:
            print("     >", text[:280])
