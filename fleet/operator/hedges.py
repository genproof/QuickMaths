"""Host side, read-only: progress of hedge lineages that work on one lane (hedge_lane.py): for each
BASE:LANE, the base's latest job on any queue of this host, files, code lines and sorry count in
Submission/Lanes/LANE, the age of the model stream and the agent's last message.

Usage: ssh HOST python3 - BASE:LANE [BASE:LANE ...] < hedges.py
"""
import json, os, sys, time
from pathlib import Path
ARGS = sys.argv[1:]
if not ARGS or ARGS[0] in ("-h", "--help") or any(a.count(":") != 1 for a in ARGS):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F


def last_text(events: Path, tail: int = 300_000) -> str:
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


for arg in ARGS:
    base, lane = arg.split(":")
    jobs = sorted(F.CAMPAIGNS.glob(f"*/jobs/{base}*"), key=lambda j: j.stat().st_mtime)
    if not jobs:
        print(base, "no job")
        continue
    job, scope = jobs[-1], f"{F.LANES_DIR}/{lane}"
    files = F.proof_paths(job / "workspace", scope) if (job / "workspace" / scope).is_dir() else []
    stats = F.proof_stats(job / "workspace", scope) or {"sorry": "?", "lines": "?"}
    events = job / "events.jsonl"
    age = f"{(time.time() - events.stat().st_mtime) / 60:.0f}m" if events.is_file() else "-"
    print(f"{job.name[-45:]} {lane} files={len(files)} lines={stats['lines']} sorry={stats['sorry']} stream_age={age}")
    text = last_text(events)
    if text:
        print("   >", text[:300])
