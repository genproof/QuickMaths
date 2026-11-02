"""Host side: queue a stop_lineage directive in this host's feeder inbox. With BASE, it ends the one
claim of PID whose lineage starts with BASE (a plain lineage, a hedge, a second lineage `PID~N`): the
running round gets a controlled STOP, the claim is marked `stopped`. With `-` instead of BASE, it ends
every lineage of PID on this host, including an active lane program. The feeder applies it on its
next tick (the directive expires after one hour) and records the outcome in inbox-processed.json.

Refuses a BASE that no claim of this host has, so it never reaches another host's or person's work.

Usage: ssh HOST python3 - PID BASE|- REASON < stop_lineage.py
"""
import json, os, sys, time
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 3 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

pid, base, reason = ARGS[0], (None if ARGS[1] == "-" else ARGS[1]), ARGS[2]
if not F.SAFE_ID.fullmatch(pid) or (base and not F.SAFE_ID.fullmatch(base)):
    raise SystemExit("bad PID or BASE")
claims = F.read_json(F.OWN / "claims.json", {}) or {}
if base and not any(isinstance(c, dict) and c.get("base") == base and F.claim_pid(k, c) == pid
                    for k, c in claims.items()):
    raise SystemExit(f"no claim of {pid} with base {base} on this host")
ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
did = f"op.stop.{(base or pid)[-40:]}.{ts}"
d = {"id": did, "type": "stop_lineage", "problem_id": pid, "mark": "stopped", "reason": reason,
     "expires_at": time.time() + 3600}
if base:
    d.update(base=base, what="claims")
(F.OWN / "inbox").mkdir(exist_ok=True)
tmp = F.OWN / "inbox" / f".{did}.tmp"
tmp.write_text(json.dumps(d, indent=1))
tmp.replace(F.OWN / "inbox" / f"{did}.json")
print(did)
