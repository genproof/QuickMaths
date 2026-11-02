"""Host side, read-only: the live lineages of this host (claims running, handed off or paused) with
runner, account, current round, sorry count, proof lines, role and the `Next step` line of the
round's NOTES.md HANDOFF.

Usage: ssh HOST python3 - < lineages.py      (or: run.py HOST lineages.py)
"""
import os, sys
from pathlib import Path
if sys.argv[1:]:
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

cfg = F.read_json(F.OWN / "config.json", {}) or {}
claims = F.read_json(F.OWN / "claims.json", {}) or {}
for key, c in sorted(claims.items()):
    if not isinstance(c, dict) or c.get("status") not in ("running", "handoff", "paused") or not c.get("base"):
        continue
    root = Path(c["runner"])
    job = F.current_job(c) or F.last_round_job(c)
    ws = job / "workspace" if job else None
    stats = F.proof_stats(ws) if ws and ws.is_dir() else None
    rnd = F.round_index(c["base"], job.name) if job else None
    print(f"{key:34s} {F.wave_of(root)} {F.account_of(root, cfg)} {c['status']:8s} base={c['base'][-45:]} "
          f"r{rnd or '?'} sorry={stats['sorry'] if stats else '?'} lines={stats['lines'] if stats else '?'} "
          f"role={c.get('role')}")
    for line in (F.notes_text(ws) if ws else "").splitlines():
        text = line.strip().lstrip("-* ").strip()
        if text.lower().startswith("next step"):
            print("    next:", text[len("next step"):].lstrip(" :*")[:160])
            break
