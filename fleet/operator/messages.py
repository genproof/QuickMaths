"""Host side, read-only: the agent's last N text messages (default 3) of the latest job of a lineage
BASE on any queue of this host, or of a job directory given by its absolute path.

Usage: ssh HOST python3 - BASE|JOB_DIR [N] < messages.py
"""
import json, os, sys
from pathlib import Path
ARGS = sys.argv[1:]
if not 1 <= len(ARGS) <= 2 or ARGS[0] in ("-h", "--help") or (len(ARGS) == 2 and not ARGS[1].isdigit()):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

target, n = ARGS[0], int(ARGS[1]) if len(ARGS) == 2 else 3
if target.startswith("/"):
    job = Path(target)
else:
    jobs = sorted(F.CAMPAIGNS.glob(f"*/jobs/{target}*"), key=lambda j: j.stat().st_mtime)
    if not jobs:
        raise SystemExit(f"no job of {target} on this host")
    job = jobs[-1]
events = job / "events.jsonl"
if not events.is_file():
    raise SystemExit(f"{job.name}: no events.jsonl")
msgs = []
with events.open(errors="replace") as stream:
    for line in stream:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "assistant":
            for part in (e.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text", "").strip():
                    msgs.append(part["text"].strip().replace("\n", " "))
print("==", job.name)
for m in msgs[-n:]:
    print("-", m[:700])
