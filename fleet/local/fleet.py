#!/usr/bin/env python3
"""Own runners on several hosts (runs on the operator machine). Layout: fleet.json
(LEH_FLEET_CONFIG, default fleet/fleet.json; template fleet.example.json).

  fleet.py status                    queues, current jobs, programs, alerts on every host
  fleet.py deploy HOST [--apply]     files, account check, runtime, queue roots, config, units
  fleet.py partition [--from HOST] [--apply]
                                     move one host's unclaimed targets and retries (with their
                                     latest proof files as seeds) to the other hosts, disjointly

Every host runs its own feeder (assignment, progress gate, packaging into its outbox); the
publisher collects packages from all hosts. Accounts are secret references (`file:` only for
runners); the token files must already be on each runner host, mode 600. This tool never reads,
copies or prints a token: deploy only checks that each referenced file exists and is private.

Remote layout, relative to the fleet user's home on every host: LEH_REMOTE_OWN_ROOT (default
leh-fleet) and LEH_REMOTE_CAMPAIGNS_ROOT (default projects/lean-eval-campaigns). ssh options:
LEH_SSH_CONFIG and LEH_SSH_KNOWN_HOSTS (optional; StrictHostKeyChecking and BatchMode always on).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FLEET_PATH = Path(os.environ.get("LEH_FLEET_CONFIG") or HERE / "fleet.json")
EXAMPLE = HERE / "fleet.example.json"


LEGACY_KEYS = {"rival_min_modules": "ref_min_modules", "retry_max_rival_modules": "retry_max_ref_modules",
               "program_min_rival_modules": "program_min_ref_modules"}  # renamed 2026-10-27


def upgrade_keys(value):
    """Legacy key names renamed in a fleet.json section or a target (a new name present wins)."""
    if isinstance(value, dict):
        for old, new in LEGACY_KEYS.items():
            if old in value:
                value.setdefault(new, value[old])
                del value[old]
    return value


def load_fleet(path: Path = FLEET_PATH) -> dict:
    fleet = json.loads(path.read_text()) if path.is_file() else {}
    for spec in (fleet.get("hosts") or {}).values():
        upgrade_keys(spec)
    return upgrade_keys(fleet)


FLEET = load_fleet()
# Keys of fleet.json (a host entry may override one) that deploy writes into config.json;
# docs/FEEDER-POLICY.md describes them.
POLICY_KEYS = ("defaults", "retry_max_ref_modules", "max_retries", "program_min_ref_modules",
               "max_program_runners", "min_free_gb",
               "verify_lineages", "verify_backfill", "verify_round_seconds", "max_verifies",
               "churn_rounds", "estimate_stall_ratio", "estimate_stall_min_lines",
               "quota_handoff", "quota_handoff_after_s", "adopt_paused_claims", "stop_siblings_on_pass",
               "max_active_runners", "min_mem_available_gb", "max_memory_pressure_avg60",
               "runtime_env", "inbox", "leaderboard_model", "contributors")


def ssh_options() -> list[str]:
    options = ["-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    if os.environ.get("LEH_SSH_CONFIG"):
        options = ["-F", os.environ["LEH_SSH_CONFIG"], *options]
    if os.environ.get("LEH_SSH_KNOWN_HOSTS"):
        options += ["-o", f"UserKnownHostsFile={os.environ['LEH_SSH_KNOWN_HOSTS']}"]
    return options


SSH = ["ssh", *ssh_options()]
# Remote paths are relative to the login directory (the fleet user's home) unless absolute.
OWN = os.environ.get("LEH_REMOTE_OWN_ROOT") or "leh-fleet"
CAMP = os.environ.get("LEH_REMOTE_CAMPAIGNS_ROOT") or "projects/lean-eval-campaigns"
POLICY = {"model": "claude-opus-5-5", "reasoning_effort": "xhigh"}
FILES = ["feeder.py", "run_controller.sh", "start.sh", "own_status.py",
         "smoke_account.py", "restart_from_files.py"]
UNITS = ["leh-runner@.service", "leh-feeder.service", "leh-feeder.timer"]
DEFAULT_ACCOUNT_REF = "file:~/.config/leh/accounts/{name}.token"


def sh(host: str, command: str, stdin: bytes | None = None, check: bool = True) -> str:
    out = subprocess.run([*SSH, host, command], input=stdin, capture_output=True, timeout=1800)
    if check and out.returncode:
        raise RuntimeError(f"{host}: {command[:80]}: {out.stderr.decode()[-500:]}")
    return out.stdout.decode()


def read_remote_json(host: str, path: str, default=None):
    text = sh(host, f"cat {shlex.quote(path)} 2>/dev/null || true")
    return json.loads(text) if text.strip() else default


def write_remote_json(host: str, path: str, value) -> None:
    q = shlex.quote
    sh(host, f"cat > {q(path + '.tmp')} && mv {q(path + '.tmp')} {q(path)}",
       stdin=(json.dumps(value, indent=1) + "\n").encode())


def remote_home(host: str) -> str:
    home = sh(host, 'printf %s "$HOME"').strip()
    if not home.startswith("/"):
        raise RuntimeError(f"{host}: no absolute home directory")
    return home


def absolute(home: str, path: str) -> str:
    return path if path.startswith("/") else f"{home.rstrip('/')}/{path}"


def root_of(wave: str, home: str | None = None) -> str:
    """The queue root of runner `wave`: absolute under `home`, else relative to the remote home."""
    root = f"{CAMP}/fleet-1-{wave}"
    return absolute(home, root) if home else root


def account_ref(name: str, fleet: dict | None = None) -> str:
    fleet = FLEET if fleet is None else fleet
    return (fleet.get("accounts") or {}).get(name) or DEFAULT_ACCOUNT_REF.format(name=name)


def merge_host_config(config: dict, host: str, spec: dict, runtime: str, fleet: dict | None = None,
                      home: str | None = None) -> dict:
    """The host's config.json after a deploy: existing keys stay; the host's runners and their
    account references are added; a runner keeps its runtime pin unless fleet.json names one (a new
    runner gets `runtime`); policy keys come from fleet.json, a host entry overriding them."""
    fleet = FLEET if fleet is None else fleet
    runners = spec["runners"]
    config = dict(config, host=host,
                  runners=sorted(set(config.get("runners", [])) | {root_of(w, home) for w in runners}))
    config["tokens"] = dict(config.get("tokens") or {}, **{w: account_ref(a, fleet) for w, a in runners.items()})
    pins = dict(config.get("runtimes") or {})
    for w in runners:
        if w in spec.get("runtimes", {}):
            pins[w] = spec["runtimes"][w]
        else:
            pins.setdefault(w, runtime)
    config["runtimes"] = pins
    for key in POLICY_KEYS:
        if key in spec or key in fleet:
            config[key] = spec.get(key, fleet.get(key))
    return config


def controller_text(runtime: str) -> str:
    """The queue controller (server/controller.py) with RUNTIME pinned to `runtime`."""
    text = (HERE / "server/controller.py").read_text()
    return re.sub(r'^RUNTIME = Path\("[^"]+"\)', f'RUNTIME = Path("{runtime}")', text, count=1, flags=re.M)


def check_accounts(host: str, refs: dict[str, str]) -> list[str]:
    """Problems with the accounts' token files on `host` (never reads them): file: references
    only, each an existing regular file with mode 600."""
    problems = []
    for name, ref in sorted(refs.items()):
        if not ref.startswith("file:"):
            problems.append(f"{name}: runners need a file: reference (got {ref.split(':', 1)[0]}:)")
            continue
        path = ref[5:]
        target = f'"$HOME"/{shlex.quote(path[2:])}' if path.startswith("~/") else shlex.quote(path)
        mode = sh(host, f"test -f {target} && ! test -L {target} && stat -c %a {target} || true").strip()
        if mode != "600":
            problems.append(f"{name}: {path} missing on {host}" if not mode else f"{name}: {path} has mode {mode}, not 600")
    return problems


RUNNER_NAME = re.compile(r"[A-Za-z][A-Za-z_]*[0-9]+")  # attempt names carry the number (feeder.next_base)


def deploy(host: str, apply: bool) -> None:
    spec = FLEET["hosts"][host]
    runners = spec["runners"]
    bad = sorted(w for w in runners if not RUNNER_NAME.fullmatch(w))
    if bad:
        raise SystemExit(f"runner names must be letters then digits (e.g. wave101): {bad}")
    refs = {a: account_ref(a) for a in sorted(set(runners.values()))}
    plan = [f"sync {len(FILES)} server files, briefs and systemd units to {host}:~/{OWN}",
            f"check account files {sorted(refs)} on {host} (placed there by the operator)",
            "runtime deploy (content-addressed)",
            f"queue roots {list(runners)}", "config.json (host, runners, account references, runtimes)",
            "install units, enable feeder timer"]
    print("\n".join(f"  - {p}" for p in plan))
    if not apply:
        return
    q = shlex.quote
    home = remote_home(host)
    own = absolute(home, OWN)
    problems = check_accounts(host, refs)
    if problems:
        raise SystemExit("accounts not ready:\n  " + "\n  ".join(problems))
    server = HERE / "server"
    sh(host, " && ".join(f"mkdir -p {q(own + '/' + d)}" for d in (
        "briefs/roles", "leases", "claude-state", "quota-state", "outbox", "programs", "runtimes", "seeds", "inbox")))
    rsync = ["rsync", "-a", "--exclude", "__pycache__", "-e", " ".join(SSH)]
    subprocess.run([*rsync, *[str(server / f) for f in FILES], f"{host}:{own}/"], check=True)
    subprocess.run([*rsync, f"{server / 'briefs'}/", f"{host}:{own}/briefs/"], check=True)
    subprocess.run([*rsync, f"{server / 'systemd'}/", f"{host}:{own}/systemd/"], check=True)
    sh(host, f"chmod 755 {q(own + '/run_controller.sh')} {q(own + '/start.sh')}")
    # runtime
    env = dict(os.environ, LEH_REMOTE_OWN_ROOT=OWN)
    out = subprocess.run([str(server / "deploy_runtime.sh"), host], capture_output=True, text=True, check=True, env=env)
    runtime = out.stdout.strip().splitlines()[-1].split(": ", 1)[-1].split(":", 1)[-1]
    print(f"  runtime {runtime}")
    # queue roots (controller from this repository, runtime pinned)
    controller = controller_text(runtime)
    for wave in runners:
        root = root_of(wave, home)
        if sh(host, f"test -d {q(root)} && echo yes || true").strip() == "yes":
            # an existing queue gets this repository's controller too, keeping the runtime it is pinned to
            # (a running controller keeps the code it loaded; the next start uses the new file)
            current = sh(host, f"cat {q(root + '/controller.py')} 2>/dev/null || true")
            pinned = re.search(r'^RUNTIME = Path\("([^"]+)"\)', current, re.M)
            text = controller_text(pinned.group(1) if pinned else runtime)
            if text != current:
                sh(host, f"cat > {q(root + '/controller.py.new')} && mv {q(root + '/controller.py.new')} "
                         f"{q(root + '/controller.py')}", stdin=text.encode())
                print(f"  queue root {root}: controller updated")
            continue
        sh(host, f"mkdir -p {q(root)} && echo {q(host)} > {q(root + '/HOST')} && cp {q(own + '/start.sh')} {q(root + '/start.sh')}")
        sh(host, f"cat > {q(root + '/controller.py')}", stdin=controller.encode())
        write_remote_json(host, f"{root}/manifest.json", {
            "campaign_id": f"fleet-1-{wave}", "host": host, "requested_config": POLICY,
            "excluded_problem_ids": [], "assignments": []})
        print(f"  queue root {root}")
    # config (merge into an existing one; a host keeps its own keys)
    config = merge_host_config(read_remote_json(host, f"{own}/config.json", {}) or {}, host, spec, runtime, home=home)
    write_remote_json(host, f"{own}/config.json", config)
    if sh(host, f"test -f {q(own + '/targets.json')} && echo yes || true").strip() != "yes":
        write_remote_json(host, f"{own}/targets.json", {"source": "fleet partition", "targets": []})
    # units: fill the templates for this host's user and layout, then install them
    camp = absolute(home, CAMP)
    render = " && ".join(
        f"sed -e \"s|@USER@|$(id -un)|g\" -e \"s|@GROUP@|$(id -gn)|g\" -e {q('s|@OWN@|' + own + '|g')} "
        f"-e {q('s|@CAMPAIGNS@|' + camp + '|g')} {q(own + '/systemd/' + u)} > {q(own + '/systemd/rendered-' + u)}"
        for u in UNITS)
    sh(host, render)
    sh(host, " && ".join(f"sudo -n install -m 644 {q(own + '/systemd/rendered-' + u)} /etc/systemd/system/{u}"
                         for u in UNITS)
             + " && sudo -n systemctl daemon-reload && sudo -n systemctl enable --now leh-feeder.timer")
    print(f"  {host}: deployed; feeder timer enabled")


EXPORT = r'''
import json, os, shutil, sys, tarfile, io
ws = sys.argv[1]
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as tar:
    for rel in ["Submission.lean"] + [os.path.relpath(os.path.join(d, f), ws)
                for d, _, fs in os.walk(os.path.join(ws, "Submission")) for f in fs if f.endswith(".lean")]:
        tar.add(os.path.join(ws, rel), arcname=rel)
    notes = "NOTES.md" if os.path.exists(os.path.join(ws, "NOTES.md")) else "NOTES.prev.md"
    if os.path.exists(os.path.join(ws, notes)):
        tar.add(os.path.join(ws, notes), arcname="NOTES.md")
sys.stdout.buffer.write(buf.getvalue())
'''


def partition(apply: bool, source: str | None = None) -> None:
    hosts = list(FLEET["hosts"])
    source = source or hosts[0]
    hosts = [h for h in hosts if h != source]
    slots = [h for h in hosts for _ in FLEET["hosts"][h]["runners"]]
    master = [upgrade_keys(t) for t in read_remote_json(source, f"{OWN}/targets.json", {})["targets"]]
    claims = read_remote_json(source, f"{OWN}/claims.json", {})
    solved = set((read_remote_json(source, f"{OWN}/leaderboard-cache.json", {}) or {}).get("our_solved") or [])
    retry_max = int(FLEET.get("retry_max_ref_modules") or 0)
    retries, untried = [], []
    for t in sorted(master, key=lambda t: t.get("ref_min_modules") or 10**9):
        c = claims.get(t["problem_id"]) or {}
        done = int(c.get("retries", 1 if c.get("retried") else 0))
        if (c.get("status") in ("exhausted", "failed") and not c.get("program_status")
                and done < int(FLEET.get("max_retries", 2)) and t["problem_id"] not in solved
                and (not retry_max or (t.get("ref_min_modules") or 10**9) <= retry_max)
                and c.get("runner") and c.get("base")):
            rounds = int(c.get("rounds") or 1)
            job = c["base"] if rounds <= 1 else f"{c['base']}-r{rounds}"
            retries.append((t, f"{c['runner']}/jobs/{job}"))
    for t in master:
        if not t.get("disabled") and t["problem_id"] not in claims and t["problem_id"] not in solved:
            untried.append((t, None))
    plan: dict[str, list] = {h: [] for h in hosts}
    for i, item in enumerate(retries + untried):
        plan[slots[i % len(slots)]].append(item)
    for host, items in plan.items():
        print(f"{host}: " + ", ".join(f"{t['problem_id']}{'*' if job else ''}" for t, job in items))
    print("(* = retry seeded with its latest proof files)")
    if not apply:
        return
    q = shlex.quote
    for host, items in plan.items():
        current = read_remote_json(host, f"{OWN}/targets.json", {"targets": []}) or {"targets": []}
        have = {t["problem_id"] for t in current["targets"]}
        own = absolute(remote_home(host), OWN) if any(job for _, job in items) else OWN
        for t, job in items:
            if t["problem_id"] in have:
                continue
            entry = {k: v for k, v in t.items() if k not in ("disabled", "reason")}
            if job:
                seed = f"{own}/seeds/{t['problem_id']}-{Path(job).name}"
                blob = subprocess.run([*SSH, source, f"python3 - {q(job + '/workspace')}"], input=EXPORT.encode(),
                                      capture_output=True, check=True, timeout=600).stdout
                sh(host, f"mkdir -p {q(seed)} && tar -xf - -C {q(seed)}", stdin=blob)
                entry["seed_dir"] = seed
            current["targets"].append(entry)
        write_remote_json(host, f"{OWN}/targets.json", current)
    # mark delegated on the source host under its feeder lock
    moved = {t["problem_id"]: host for host, items in plan.items() for t, _ in items}
    write_remote_json(source, "/tmp/leh-fleet-delegated.json", moved)
    sh(source, "python3 -", stdin=DELEGATE.replace("@OWN@", json.dumps(OWN)).encode())
    print(f"delegated {len(moved)} problems")


STATUS = r'''
import json, os
s = json.load(open(os.path.join(os.path.expanduser("~"), @OWN@, "status.json")))
for name, r in s["runners"].items():
    job = (r.get("current_job") or "").split("/")[-1]
    print(f"  {name}: {r.get('queue_status')} {job}")
c = s.get("claims", {})
counts = {}
for v in c.values():
    counts[v.get("status")] = counts.get(v.get("status"), 0) + 1
print("  claims:", counts, "| programs:", {k: v["status"] for k, v in (s.get("programs") or {}).items()},
      "| alerts:", s.get("alerts"), "| checked", s.get("checked_at", "")[:19])
'''


DELEGATE = """
import json, os, sys
own = os.path.join(os.path.expanduser("~"), @OWN@)
os.environ["LEH_OWN_ROOT"] = own
sys.path.insert(0, own)
import feeder as F
moved = json.load(open("/tmp/leh-fleet-delegated.json"))
with F.flock(F.OWN / "feeder.lock", blocking=True):
    c = F.read_json(F.OWN / "claims.json", {}) or {}
    for pid, host in moved.items():
        c[pid] = {"status": "delegated", "host": host, "at": F.iso(), "previous_claim": c.get(pid)}
    F.write_json(F.OWN / "claims.json", c)
    F.log("delegated " + json.dumps(moved))
"""


def status() -> None:
    for host in FLEET["hosts"]:
        print(f"== {host}")
        try:
            print(sh(host, "python3 -", stdin=STATUS.replace("@OWN@", json.dumps(OWN)).encode()), end="")
        except RuntimeError as exc:
            print(f"  unavailable: {str(exc)[-200:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("deploy"); d.add_argument("host"); d.add_argument("--apply", action="store_true")
    p = sub.add_parser("partition"); p.add_argument("--apply", action="store_true")
    p.add_argument("--from", dest="source", help="the host whose targets are moved (default: the first in fleet.json)")
    args = parser.parse_args()
    if not FLEET:
        raise SystemExit(f"no fleet config at {FLEET_PATH}; copy {EXAMPLE.name} and set LEH_FLEET_CONFIG")
    if args.cmd == "status":
        status()
    elif args.cmd == "deploy":
        deploy(args.host, args.apply)
    else:
        partition(args.apply, args.source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
