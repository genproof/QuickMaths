"""Host side, read-only: per runner of this host: wave, account label, queue status, a usage-limit
pause of its current round (feeder.runner_quota), and the latest rate-limit event of its last three
jobs (status, limit type, reset time, per-window utilization); then the account blocks the feeder
keeps (account-blocks.json). With --json, one JSON document instead of text.

It prints account labels (the secret reference's file or variable name) and the CLI's rate-limit
fields only. It never opens a token file or prints a secret reference's target.

Usage: ssh HOST python3 - [--json] < accounts.py
"""
import json, os, sys, time
from pathlib import Path
ARGS = sys.argv[1:]
if ARGS not in ([], ["--json"]):
    raise SystemExit(__doc__)
sys.path.insert(0, os.environ.get("LEH_OWN_ROOT") or str(Path.home() / "leh-fleet"))
import feeder as F

RATE_FIELDS = ("status", "rateLimitType", "resetsAt")


def rate_limit(root: Path, tail: int = 2_000_000) -> tuple[float | None, dict | None]:
    """(mtime of the events file, whitelisted rate_limit_info) of the newest event in the last 3 jobs."""
    for events in sorted(root.glob("jobs/*/events.jsonl"), key=lambda p: p.stat().st_mtime)[-3:][::-1]:
        with events.open("rb") as stream:
            stream.seek(max(0, events.stat().st_size - tail))
            data = stream.read().decode("utf-8", "replace")
        found = None
        for line in data.splitlines():
            if '"rate_limit_event"' not in line:
                continue
            try:
                info = json.loads(line).get("rate_limit_info") or {}
            except (ValueError, AttributeError):
                continue
            windows = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
            found = {k: info.get(k) for k in RATE_FIELDS}
            found["windows"] = {str(k): {"utilization": w.get("utilization"), "resetsAt": w.get("resetsAt")}
                                for k, w in windows.items() if isinstance(w, dict)}
        if found:
            return events.stat().st_mtime, found
    return None, None


def at(ts) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ts)) if isinstance(ts, (int, float)) and ts else "-"


now = time.time()
cfg = F.read_json(F.OWN / "config.json", {}) or {}
out = []
for name in cfg.get("runners") or []:
    root = Path(name)
    state = F.read_json(root / "state.json", {}) or {}
    seen, info = rate_limit(root)
    quota = F.runner_quota(root, state, now)
    job = Path(state.get("current_job") or "").name if state.get("status") == "model_running" else ""
    out.append({"runner": F.wave_of(root), "account": F.account_of(root, cfg),
                "status": "STOP" if (root / "STOP").exists() else state.get("status"), "job": job[-55:],
                "paused": quota["paused"], "window": quota["window"], "until": quota["until"] or None,
                "rate_limit_seen_at": seen, "rate_limit": info})
blocks = {a: {k: b.get(k) for k in ("until", "window", "runner", "observed_at")}
          for a, b in (F.read_json(F.OWN / "account-blocks.json", {}) or {}).items() if isinstance(b, dict)}
if ARGS == ["--json"]:
    print(json.dumps({"host": F.HOST, "at": now, "runners": out, "account_blocks": blocks}, indent=1))
    raise SystemExit(0)
for r in out:
    rl = r["rate_limit"] or {}
    windows = " ".join(f"{k}={w['utilization']}@{at(w['resetsAt'])}" for k, w in (rl.get("windows") or {}).items())
    pause = f"PAUSED {r['window']} until {at(r['until'])}" if r["paused"] else ""
    print(f"{r['runner']} {r['account']} {r['status']} {pause} rate={rl.get('status')} {rl.get('rateLimitType') or ''} "
          f"resets={at(rl.get('resetsAt'))} {windows} {r['job']}".rstrip())
for account, b in sorted(blocks.items()):
    print(f"block {account}: {b.get('window')} until {at(b.get('until'))} (seen on {b.get('runner')})")
