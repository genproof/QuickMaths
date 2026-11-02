"""Host side: drop one account's entry from the feeder's account-blocks.json. Use it when a block is
attributed to the wrong account, e.g. right after a runner was switched to a new account while its
old account's usage-limit pause was still on record. The label is the account label accounts.py
prints (the token file name without .token). A real block comes back on the next pause.

Usage: ssh HOST python3 - ACCOUNT REASON < unblock.py
"""
import json, os, sys
from pathlib import Path
ARGS = sys.argv[1:]
if len(ARGS) != 2 or ARGS[0] in ("-h", "--help"):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

account, reason = ARGS
with F.flock(F.OWN / "feeder.lock", blocking=True):
    path = F.OWN / "account-blocks.json"
    blocks = F.read_json(path, {}) or {}
    gone = blocks.pop(account, None)
    F.write_json(path, blocks)
    F.log(f"operator: dropped account block {account} {gone} ({reason})")
print(json.dumps({"dropped": gone, "left": sorted(blocks)}))
