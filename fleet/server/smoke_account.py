"""Smoke-test one subscription account with the runtime's own credential install and pinned CLI.

Usage (from a deployed runtime dir, PYTHONPATH=.): smoke_account.py SECRET_REF
(file:~/.config/leh/accounts/<name>.token or env:NAME). Prints only status and rate-limit
windows; deletes the temp config dir."""
import json, os, shutil, subprocess, sys, tempfile, time, datetime
from pathlib import Path
import leaneval.agent_runner as cr

token_ref = sys.argv[1]
own = Path(os.environ.get("LEH_OWN_ROOT") or Path.home() / "leh-fleet")
own.mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix="smoke-", dir=own))
try:
    os.chmod(base, 0o700)
    cfg, home = base / "cfg", base / "home"
    cfg.mkdir(mode=0o700); home.mkdir(mode=0o700)
    cr.install_credentials(cfg, cr._read_token(token_ref))
    env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "TERM": "dumb"}
    cmd = [str(cr.cli_path()), "-p", "Reply with exactly: OK", "--model", "claude-opus-5-5",
           "--output-format", "stream-json", "--verbose", "--max-turns", "1"]
    out = subprocess.run(cmd, cwd=home, env=env, capture_output=True, text=True, timeout=180)
    t = lambda x: datetime.datetime.utcfromtimestamp(x).strftime("%m-%d %H:%M") if x else None
    print("exit", out.returncode)
    for line in out.stdout.splitlines():
        try: ev = json.loads(line)
        except ValueError: continue
        if ev.get("type") == "rate_limit_event":
            info = ev["rate_limit_info"]
            print("rate_limit", info.get("status"), {k: (v.get("utilization"), t(v.get("resetsAt")))
                  for k, v in (info.get("unifiedWindows") or {}).items()})
        if ev.get("type") == "result":
            print("result is_error", ev.get("is_error"), "api_error", ev.get("api_error_status"),
                  ev.get("api_error_code"), "text:", str(ev.get("result"))[:160])
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            print("init model", ev.get("model"))
finally:
    shutil.rmtree(base, ignore_errors=True)
    print("temp removed:", not base.exists())
