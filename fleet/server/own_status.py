#!/usr/bin/env python3
"""Compact status of the fleet runners (read-only): queues, jobs, model stream, claims."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

OWN = Path(os.environ.get("LEH_OWN_ROOT") or Path.home() / "leh-fleet")


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def stream_summary(events: Path) -> dict:
    n, init, models, last_text, result = 0, None, set(), "", None
    try:
        lines = events.read_text(errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        n += 1
        if e.get("type") == "system" and e.get("subtype") == "init":
            init = {k: e.get(k) for k in ("model", "permissionMode", "claude_code_version")}
        message = e.get("message") or {}
        if message.get("model"):
            models.add(message["model"])
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                last_text = block["text"]
        if e.get("type") == "result":
            result = {k: e.get(k) for k in ("subtype", "is_error", "num_turns")}
    return {"events": n, "init": init, "models": sorted(models), "last_text": last_text[-300:],
            "last_result": result, "age_s": int(time.time() - events.stat().st_mtime)}


def main() -> None:
    cfg = load(OWN / "config.json", {})
    for root in map(Path, cfg.get("runners", [])):
        state = load(root / "state.json", {})
        beat = load(root / "heartbeat.json", {})
        print(f"== {root.name}: {state.get('status')} heartbeat_age="
              f"{int(time.time() - beat.get('observed_at', 0))}s finished={len(state.get('history', {}))}")
        job = state.get("current_job")
        if job:
            job = Path(job)
            print(f"   job {job.name}")
            s = stream_summary(job / "events.jsonl")
            if s:
                print(f"   stream events={s['events']} age={s['age_s']}s init={s['init']} "
                      f"models={s['models']} result={s['last_result']}")
                if s["last_text"]:
                    print("   last text: " + s["last_text"].replace("\n", " ")[:300])
            notes = job / "workspace/NOTES.md"
            if notes.exists():
                print(f"   NOTES.md {notes.stat().st_size} bytes")
        for name, entry in list(state.get("history", {}).items())[-3:]:
            v = entry.get("verification") or {}
            print(f"   done {name}: {entry.get('phase')} {v.get('status')} "
                  f"{(entry.get('model') or {}).get('stop_reason')} {entry.get('error', '')[:120]}")
    status = load(OWN / "status.json", {})
    claims = status.get("claims", {})
    counts: dict[str, int] = {}
    for c in claims.values():
        counts[c.get("status")] = counts.get(c.get("status"), 0) + 1
    print(f"claims: {counts}; packages: {status.get('packages')}; published: "
          f"{status.get('published')}; alerts: {status.get('alerts')}; disk_free_gb: "
          f"{status.get('disk_free_gb')}; checked {status.get('checked_at')}")


if __name__ == "__main__":
    main()
