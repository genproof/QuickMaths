#!/usr/bin/env python3
"""vcur: re-verify published LeanEval branches on the CURRENT benchmark (operator machine, 5 min).

The production submit flow accepted only branches whose VERIFICATION.json was made on the current benchmark
commit (stage_current.BENCH, 208b6b22); the fleet still packages on the runtime's pin (0e093293). This automates
the manual "vcur" flow. The verification itself is verify (verify_candidate.py, the workspace manager,
run_verify.sh, the organizers' evaluator fetched at its pin), installed with vcur/run_lane.sh and
vcur/prep_current.py into <remote_base>/auto/ on the verify host; that host needs a workbench built by
runtime/tools/build_workbench.py (config `workbench`).

  discover  heads `claude/<problem>-private` / `claude/<problem>-own-<d8>-private` of fleet.json publish.repo
            whose VERIFICATION.json is a pass on another benchmark commit (facts cached per head in the ledger;
            skips: excluded, older than min_commit_time, credited to us, the problem already has a current
            branch, a newer candidate branch exists for the problem; force_branches overrides the last two)
  start     one at a time, only when the verify host runs no other verifier: fresh clone -> src (Submission.lean +
            Submission/**/*.lean) -> list.tsv row -> rsync -> `nohup setsid bash auto/run_lane.sh LANE CPUS MEM KEY`
            (vcur/prep_current.py + verify: run_verify.sh runs verify_candidate.py)
  poll      short ssh probes: RUN-DONE, status.json, lane log, exact-argv process match (never kills anything)
  stage     fresh clone + vcur/stage_current.py: VERIFICATION.json and a README line in one commit
  push      fast-forward only, if the remote head still equals the verified commit, source.tar.gz of the tree is
            under the cap and the metadata diff has no secret-like strings; then readback (head and tree)
  cleanup   remove runs/<key>/verification/candidate-*/.lake/build on the verify host

Every transition is saved to ~/.local/state/leh-fleet/vcur-ledger.json before the next step, so a killed run
resumes. Failures are not replayed: `failed` and `held` stay until an operator runs --requeue. Never force-pushes,
never writes outside its run directories. --dry-run (the default) only reads and prints the plan; --apply executes;
--once does one tick (the LaunchAgent).
"""
from __future__ import annotations

import argparse
import collections
import datetime
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VCUR = HERE / "vcur"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = _load("leh_publish", HERE / "publish.py")
stage_current = _load("leh_stage_current", VCUR / "stage_current.py")

BENCH = stage_current.BENCH
REPO, REMOTE, INTAKE, ENV = publish.REPO, publish.REMOTE, publish.INTAKE, publish.ENV
SSH = [*publish.SSH, "-o", "ServerAliveInterval=30"]
STATE = publish.STATE
LEDGER = STATE / "vcur-ledger.json"
CONFIG = Path(os.environ.get("LEH_VCUR_CONFIG") or VCUR / "config.json")  # template: vcur/config.example.json
VERIFY = HERE.parent.parent / "verify"  # verify
# Installed into <remote_base>/auto/ on the verify host, compared by hash before every start.
LANE_SCRIPTS = {"run_lane.sh": VCUR / "run_lane.sh", "prep_current.py": VCUR / "prep_current.py",
                **{name: VERIFY / name for name in ("verify_run.py", "verify_candidate.py", "workspace_manager.py",
                                                    "run_verify.sh")}}
DEFAULTS = {
    "host": None,  # the verify host (ssh name); required
    "remote_base": "lean-eval-vcur",  # relative to ~ on the host
    "remote_python": "python3",
    "ssh_wrapper": None,  # e.g. a bin/mh script called as WRAPPER HOST COMMAND; None = publisher ssh options
    "lane": "auto",
    "cpus": "0-15",
    "mem": "48G",
    "mem_overrides": {},
    "timeout_factor": 4,
    "timeout_floor_seconds": 3600,
    "timeout_overrides": {},
    "archive_cap_bytes": 29_500_000,
    "archive_margin_bytes": 262_144,
    "min_commit_time": None,
    "skip_credited": True,
    "exclude_problems": [],
    "exclude_branches": [],
    "exclude_commits": [],
    "force_branches": [],  # queued even if a sibling branch is current or newer
    "busy_markers": ["run_lane.sh", "verify_candidate.py"],
    "min_free_gb": 20,
    "require_mem_available": True,
    "start_grace_seconds": 900,
    "overdue_grace_seconds": 3600,
    "max_step_errors": 6,
    "cleanup_failed_builds": True,
    "poll_seconds": 120,
    "workbench": None,  # LEANEVAL_WORKBENCH on the verify host; None = its default ~/projects/lean-eval-workbench
}

QUEUED, STARTING, RUNNING, VERIFIED, STAGED, PUSHED = "queued", "starting", "running", "verified", "staged", "pushed"
DONE, FAILED, HELD, SUPERSEDED = "done", "failed", "held", "superseded"
ACTIVE = (STARTING, RUNNING, VERIFIED, STAGED, PUSHED)
TRANSITIONS = {
    QUEUED: {STARTING, HELD, SUPERSEDED},
    STARTING: {RUNNING, VERIFIED, FAILED, HELD},
    RUNNING: {VERIFIED, FAILED, HELD},
    VERIFIED: {STAGED, HELD, SUPERSEDED},
    STAGED: {PUSHED, VERIFIED, HELD, SUPERSEDED},
    PUSHED: {DONE, HELD},
    FAILED: {QUEUED},  # --requeue only
    HELD: {QUEUED},  # --requeue only
    DONE: set(),
    SUPERSEDED: set(),
}
RUN_FIELDS = ("cpus", "mem", "timeout", "started_at", "archive_bytes_before", "source_files", "run_done", "new_head",
              "tree", "archive_bytes", "secret_hits", "receipt_sha256", "verification_wall_seconds", "pushed_at",
              "held_reason", "last_error", "cleaned_bytes", "lane")
BRANCH_RE = re.compile(r"^claude/(?P<pid>[A-Za-z0-9_]+?)(?:-own-[0-9a-f]{8})?-private$")
KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class Hold(Exception):
    """A deterministic precondition failed: park the item for the operator."""


class Supersede(Exception):
    """The branch moved on; this item is no longer needed."""


# ---------------------------------------------------------------- pure helpers

def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="seconds")


def parse_time(text: str) -> float:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def parse_branch(branch: str) -> str | None:
    match = BRANCH_RE.match(branch)
    return match.group("pid") if match else None


def run_key(problem: str, commit: str, attempt: int = 1) -> str:
    key = f"{problem}-{commit[:8]}" + (f"-r{attempt}" if attempt > 1 else "")
    if not KEY_RE.match(key):
        raise ValueError(f"bad run key {key!r}")
    return key


def lane_tail(lane: str, cpus: str, mem: str, key: str) -> list[str]:
    """argv[1:] of the lane process (`bash auto/run_lane.sh LANE CPUS MEM KEY`), matched exactly on the host."""
    return ["auto/run_lane.sh", lane, cpus, mem, key]


def compute_timeout(fleet_seconds, problem: str, cfg: dict) -> int:
    """max(factor x fleet verify seconds, floor); a per-problem override wins (property_t needed 6 h)."""
    override = cfg["timeout_overrides"].get(problem)
    if override:
        return int(override)
    seconds = float(fleet_seconds or 0)
    return max(int(math.ceil(cfg["timeout_factor"] * seconds)), int(cfg["timeout_floor_seconds"]))


def memory_for(problem: str, cfg: dict) -> str:
    return cfg["mem_overrides"].get(problem, cfg["mem"])


def parse_mem(text: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)", str(text).strip().upper())
    if not match:
        raise ValueError(f"bad memory size {text!r}")
    return int(float(match.group(1)) * 1024 ** " KMGT".index(match.group(2) or " "))


def upsert_list_row(text: str, row: list[str]) -> str:
    """list.tsv with the row keyed by row[0] replaced in place (first match) or appended; other rows untouched."""
    out, placed = [], False
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.split()[0] == row[0]:
            if not placed:
                out.append("\t".join(row))
                placed = True
            continue
        out.append(line)
    if not placed:
        out.append("\t".join(row))
    return "\n".join(out) + "\n"


def facts_of(verification, commit_time: str | None) -> dict:
    if not isinstance(verification, dict):
        return {"has_verification": False, "commit_time": commit_time}
    return {"has_verification": True, "problem_id": verification.get("problem_id"),
            "benchmark_commit": verification.get("benchmark_commit"), "status": verification.get("status"),
            "accepted": verification.get("accepted"),
            "wall_seconds": verification.get("verification_wall_seconds"), "commit_time": commit_time}


def select_branch(branch: str, commit: str, facts: dict, cfg: dict, credited=frozenset(), bench: str = BENCH) -> str:
    """'queue', 'current' or 'skip:<reason>' for one branch head."""
    problem = parse_branch(branch)
    if problem is None:
        return "skip:not_a_solution_branch"
    if not facts.get("has_verification"):
        return "skip:no_verification"
    if facts.get("benchmark_commit") == bench:
        return "current"
    if facts.get("status") != "official_local_pass" or facts.get("accepted") is not True:
        return "skip:not_a_pass"
    if facts.get("problem_id") != problem:
        return "skip:problem_mismatch"
    if (problem in cfg["exclude_problems"] or branch in cfg["exclude_branches"]
            or any(c and commit.startswith(c) for c in cfg["exclude_commits"])):
        return "skip:excluded"
    cutoff = cfg.get("min_commit_time")
    if cutoff and (not facts.get("commit_time") or parse_time(facts["commit_time"]) < parse_time(cutoff)):
        return "skip:before_cutoff"
    if cfg.get("skip_credited") and problem in credited:
        return "skip:credited"
    return "queue"


def host_wait_reason(probe: dict, mem: str, cfg: dict) -> str | None:
    """Why a new verification must not start now (None = go). At most one verifier on the host at a time."""
    busy = probe.get("procs") or []
    if busy:
        first = busy[0]
        return (f"host busy: {len(busy)} verifier process(es), e.g. pid {first['pid']} "
                f"{' '.join(first['args'])[:120]}")
    free = probe.get("free_bytes")
    if free is not None and free < cfg["min_free_gb"] * 2 ** 30:
        return f"only {free / 2 ** 30:.1f} GiB free on the host disk (< {cfg['min_free_gb']})"
    available = probe.get("mem_available")
    if cfg["require_mem_available"] and available is not None and available < parse_mem(mem):
        return f"MemAvailable {available / 2 ** 30:.0f} GiB < {mem}"
    return None


def run_outcome(item: dict, probe: dict, now: float, cfg: dict) -> tuple[str | None, str]:
    """Next state of a started item from one host probe; None = keep waiting."""
    elapsed = now - item["started_at"]
    status = probe.get("status") or {}
    done = (probe.get("run_done") or "").strip()
    if done:
        if status.get("phase") == "finished" and status.get("accepted") is True:
            return VERIFIED, f"accepted ({done})"
        return FAILED, (f"not accepted: phase {status.get('phase')} accepted {status.get('accepted')} "
                        f"{status.get('error', '')} ({done})").replace("  ", " ")
    if probe.get("lane_active"):
        if elapsed > item["timeout"] + cfg["overdue_grace_seconds"]:
            return HELD, (f"overdue: running {elapsed / 3600:.1f} h with timeout {item['timeout']} s; "
                          f"left running, inspect on the host (never pkill)")
        return (RUNNING if item["state"] == STARTING else None), f"running {elapsed / 60:.0f} min"
    for line in probe.get("log") or []:
        words = line.split()
        if words[:2] == ["prep", "failed"] or words[1:3] == ["no", "row"]:
            return FAILED, f"lane: {line.strip()}"
    if elapsed > cfg["start_grace_seconds"]:
        return HELD, f"lane not running and no RUN-DONE after {elapsed / 60:.0f} min"
    return None, f"waiting for the lane to start ({elapsed:.0f} s)"


def push_decision(remote_head: str | None, verified_commit: str, new_head: str, archive_bytes, cap: int,
                  secret_hits) -> str:
    """'push', 'already_pushed' or 'hold:<reason>'. Push only as a fast-forward of the verified commit."""
    if remote_head is not None and remote_head == new_head:
        return "already_pushed"
    if remote_head is None:
        return "hold:branch_missing"
    if remote_head != verified_commit:
        return "hold:remote_moved"
    if archive_bytes is None or archive_bytes >= cap:
        return "hold:archive_too_big"
    if secret_hits:
        return "hold:secret_scan"
    return "push"


# Pattern table (tools/scrub.py skips lines marked `scrub: allow`).
SECRET_PATTERNS = [
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9]{20,}")),  # scrub: allow
    ("langfuse_key", re.compile(r"\b(?:pk|sk)-lf-[A-Za-z0-9-]{8,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}|\bauthorization\s*:")),
    ("credential_path", re.compile(r"\.ssh/|oauth[-_]token|\.config/(?:leh/accounts|lean-eval)|\.netrc"  # scrub: allow
                                   r"|\.git-credentials|known_hosts|id_(?:rsa|ed25519|ecdsa)\b")),  # scrub: allow
    ("assignment", re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token"
                              r"|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',]{8,}")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("ipv4", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
]


def secret_scan(diff: str) -> list[str]:
    """Secret-like strings on the added lines of a diff, as 'pattern (line N)' -- never the matched text."""
    hits = []
    for number, line in enumerate(diff.splitlines(), 1):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        hits += [f"{name} (diff line {number})" for name, pattern in SECRET_PATTERNS if pattern.search(line)]
    return hits


def new_item(problem: str, branch: str, commit: str, fleet_seconds, now: float) -> dict:
    return {"id": f"{problem}@{commit[:12]}", "problem_id": problem, "branch": branch, "commit": commit,
            "fleet_seconds": int(round(float(fleet_seconds or 0))), "attempt": 1,
            "key": run_key(problem, commit), "state": QUEUED, "queued_at": iso(now), "errors": 0,
            "history": [{"at": iso(now), "from": None, "to": QUEUED, "note": "discovered"}]}


def load_config(path: Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path is not None and path.exists():
        extra = json.loads(path.read_text())
        unknown = sorted(set(extra) - set(DEFAULTS))
        if unknown:
            raise ValueError(f"unknown config keys in {path}: {unknown}")
        cfg.update(extra)
    return cfg


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_script_hashes() -> dict[str, str]:
    return {name: sha256(path) for name, path in LANE_SCRIPTS.items()}


def lane_env(cfg: dict) -> dict[str, str]:
    """Variables run_lane.sh gets on the verify host: the workbench."""
    env = {}
    if cfg.get("workbench"):
        env["LEANEVAL_WORKBENCH"] = cfg["workbench"]
    for name, value in env.items():
        if not isinstance(value, str) or not value or value.startswith("~") or ".." in value.split("/") \
                or any(c in value for c in "\n\0'\"$`\\"):
            raise ValueError(f"vcur config: {name} must be a plain path (relative to ~ on the host, or absolute)")
    return env


def copy_sources(clone: Path, src: Path) -> int:
    """src = exactly the files the verifier reads: Submission.lean and Submission/**/*.lean."""
    if not (clone / "Submission.lean").is_file():
        raise Hold("branch has no Submission.lean")
    files = [clone / "Submission.lean", *sorted((clone / "Submission").rglob("*.lean"))]
    for f in files:
        dest = src / f.relative_to(clone)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    return len(files)


# ---------------------------------------------------------------- ledger

class Ledger:
    """JSON ledger: `seen` caches facts per branch head, `items` holds one record per (branch, commit)."""

    def __init__(self, path: Path, persist: bool = True, clock=time.time):
        self.path, self.persist, self.clock = Path(path), persist, clock
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {"version": 1, "benchmark_commit": BENCH, "seen": {}, "items": {}}
        if self.data.get("benchmark_commit") != BENCH:
            raise RuntimeError(f"{self.path} is for benchmark {self.data.get('benchmark_commit')}, not {BENCH}")

    @property
    def items(self) -> dict:
        return self.data["items"]

    def save(self) -> None:
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def add(self, item: dict) -> bool:
        if item["id"] in self.items:
            return False
        self.items[item["id"]] = item
        self.save()
        return True

    def find(self, branch: str, commit: str) -> dict | None:
        return next((i for i in self.items.values() if i["branch"] == branch and i["commit"] == commit), None)

    def queued(self) -> list[dict]:
        return [i for i in self.items.values() if i["state"] == QUEUED]

    def active(self) -> dict | None:
        active = [i for i in self.items.values() if i["state"] in ACTIVE]
        if len(active) > 1:
            raise RuntimeError(f"more than one active item: {[i['id'] for i in active]}")
        return active[0] if active else None

    def move(self, item: dict, state: str, note: str = "", **fields) -> None:
        current = item["state"]
        if state not in TRANSITIONS[current]:
            raise ValueError(f"illegal transition {current} -> {state} for {item['id']}")
        item.update(fields)
        item["state"] = state
        item["updated_at"] = iso(self.clock())
        item.setdefault("history", []).append({"at": item["updated_at"], "from": current, "to": state,
                                               "note": note[:500]})
        self.save()

    def requeue(self, item_id: str) -> dict:
        item = self.items.get(item_id)
        if item is None:
            raise KeyError(f"no item {item_id}")
        if item["state"] not in (FAILED, HELD):
            raise ValueError(f"{item_id} is {item['state']}; only failed or held items are requeued")
        for name in RUN_FIELDS:
            item.pop(name, None)
        attempt = item.get("attempt", 1) + 1
        self.move(item, QUEUED, note="requeued by operator", attempt=attempt,
                  key=run_key(item["problem_id"], item["commit"], attempt), errors=0)
        return item


# ---------------------------------------------------------------- I/O boundaries

def run(args, *, cwd=None, stdin: str | None = None, timeout: int = 300) -> str:
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True, env=ENV, cwd=cwd,
                          input=stdin, timeout=timeout).stdout


def git_out(clone: Path, *args: str) -> str:
    return run(["git", "-C", clone, *args]).strip()


class Hub:
    """GitHub side: git over the existing credential helper, gh api for single files and commit metadata."""

    def __init__(self, remote: str = REMOTE, repo: str = REPO, intake: str = INTAKE):
        self.remote, self.repo, self.intake = remote, repo, intake

    def heads(self) -> dict[str, str]:
        heads = {}
        for line in run(["git", "ls-remote", "--heads", self.remote], timeout=120).splitlines():
            commit, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/claude/"):
                heads[ref[len("refs/heads/"):]] = commit
        return heads

    def head(self, branch: str) -> str | None:
        for line in run(["git", "ls-remote", self.remote, f"refs/heads/{branch}"], timeout=120).splitlines():
            commit, _, ref = line.partition("\t")
            if ref == f"refs/heads/{branch}":
                return commit
        return None

    def file_json(self, commit: str, path: str):
        try:
            text = run(["gh", "api", f"repos/{self.repo}/contents/{path}?ref={commit}",
                        "-H", "Accept: application/vnd.github.raw+json"])
        except subprocess.CalledProcessError as exc:
            if "404" in (exc.stderr or "") or "Not Found" in (exc.stderr or ""):
                return None
            raise
        try:
            return json.loads(text)
        except ValueError:
            return None

    def commit_info(self, commit: str) -> dict:
        data = json.loads(run(["gh", "api", f"repos/{self.repo}/commits/{commit}"]))
        return {"time": data["commit"]["committer"]["date"], "tree": data["commit"]["tree"]["sha"]}

    def credited(self) -> set[str]:
        if not publish.RESULTS_ACCOUNT:
            return set()
        results = json.loads(run(["gh", "api", f"repos/{self.intake}/contents/results/{publish.RESULTS_ACCOUNT}.json",
                                  "-H", "Accept: application/vnd.github.raw+json"]))
        return {r["problem_id"] for r in results.get("results", [])}

    def clone(self, branch: str, dest: Path) -> None:
        run(["git", "clone", "-q", "--single-branch", "--branch", branch, self.remote, dest], timeout=1800)

    def push(self, clone: Path, new_head: str, branch: str) -> None:
        """Plain push of an exact commit: git refuses anything but a fast-forward. Never --force."""
        run(["git", "-C", clone, "push", "-q", self.remote, f"{new_head}:refs/heads/{branch}"], timeout=1800)


PROBE_PROGRAM = r'''
import hashlib, json, os, shutil, sys
home = os.path.expanduser("~")
base = os.path.join(home, sys.argv[1])
key, markers, tail, scripts = sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4]), json.loads(sys.argv[5])
run = os.path.join(base, "runs", key)
skip = {os.getpid(), os.getppid()}


def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


procs = []
for pid in (os.listdir("/proc") if os.path.isdir("/proc") else []):
    if not pid.isdigit() or int(pid) in skip:
        continue
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        continue
    if not any(os.path.basename(a) in markers for a in args):
        continue
    try:
        cwd = os.readlink("/proc/%s/cwd" % pid)
    except OSError:
        cwd = None
    procs.append({"pid": int(pid), "args": args, "cwd": cwd})
ours = [p["pid"] for p in procs
        if (os.path.basename(p["args"][0]) == "bash" and p["args"][1:] == tail)
        or (p["cwd"] == os.path.realpath(run) and any(os.path.basename(a) == "verify_candidate.py" for a in p["args"]))]
mem = None
for line in (read("/proc/meminfo") or "").splitlines():
    if line.startswith("MemAvailable:"):
        mem = int(line.split()[1]) * 1024
status = None
text = read(os.path.join(run, "status.json"))
if text:
    try:
        status = json.loads(text)
    except ValueError:
        status = {"phase": "unreadable"}
log = []
for line in (read(os.path.join(base, "lane-%s.log" % tail[1])) or "").splitlines():
    t = line.split()
    if (t[:3] == ["prep", "failed", key] or (len(t) > 3 and t[1:3] == ["no", "row"] and t[3] == key)
            or (len(t) > 2 and t[1] in ("start", "done") and t[2] == key)):
        log.append(line)
print(json.dumps({
    "procs": procs, "ours": ours, "lane_active": bool(ours), "run_exists": os.path.isdir(run),
    "run_done": read(os.path.join(run, "RUN-DONE")), "status": status, "log": log[-20:],
    "free_bytes": shutil.disk_usage(base if os.path.isdir(base) else home).free, "mem_available": mem,
    "scripts": {n: digest(os.path.join(base, "auto", n)) for n in scripts},
}))
'''

BUNDLE_PROGRAM = r'''
import json, os, sys
base = os.path.realpath(os.path.join(os.path.expanduser("~"), sys.argv[1]))
run = os.path.join(base, "runs", sys.argv[2])


def load(path):
    with open(path) as f:
        return json.load(f)


status = load(os.path.join(run, "status.json"))
receipt = os.path.realpath(status["receipt"])
if not receipt.startswith(os.path.join(run, "receipts") + os.sep):
    raise SystemExit("receipt outside the run dir: " + receipt)
with open(os.path.join(run, "RUN-DONE")) as f:
    run_done = f.read().strip()
print(json.dumps({"status": status, "config": load(os.path.join(run, "config.json")),
                  "hashes": load(os.path.join(run, "source-sha256.json")), "receipt": load(receipt),
                  "run_done": run_done}))
'''

CLEANUP_PROGRAM = r'''
import glob, json, os, re, shutil, sys
key = sys.argv[2]
if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", key):
    raise SystemExit("bad key " + key)
base = os.path.realpath(os.path.join(os.path.expanduser("~"), sys.argv[1]))
root = os.path.join(base, "runs", key, "verification")
removed, total = [], 0
for path in sorted(glob.glob(os.path.join(root, "candidate-*", ".lake", "build"))):
    if os.path.islink(path) or not os.path.isdir(path) or os.path.realpath(path) != path:
        continue
    if not path.startswith(root + os.sep):
        continue
    for d, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    shutil.rmtree(path)
    removed.append(path)
print(json.dumps({"removed": removed, "bytes": total}))
'''


class Host:
    """The verify host (config `host`): short ssh calls only, every remote program reads its args from argv."""

    def __init__(self, cfg: dict):
        if not cfg.get("host"):
            raise ValueError("vcur config needs `host` (the verify host's ssh name)")
        self.name, self.base, self.python = cfg["host"], cfg["remote_base"], cfg["remote_python"]
        self.ssh = [cfg["ssh_wrapper"]] if cfg.get("ssh_wrapper") else list(SSH)
        self.env = lane_env(cfg)
        if self.base.startswith("~") or ".." in self.base.split("/"):
            raise ValueError("remote_base is relative to ~ on the host (or absolute), without ~ or ..")

    def path(self, *parts: str) -> str:
        return "/".join([self.base, *parts])

    def run(self, command: str, stdin: str | None = None, timeout: int = 300) -> str:
        return run([*self.ssh, self.name, command], stdin=stdin, timeout=timeout)

    def program(self, program: str, *args: str, timeout: int = 300):
        return json.loads(self.run(shlex.join([self.python, "-", *map(str, args)]), stdin=program,
                                   timeout=timeout))

    def rsync(self, sources: list, dest: str, delete: bool = False) -> None:
        run(["rsync", "-a", *(["--delete"] if delete else []), "-e", shlex.join(self.ssh),
             *map(str, sources), f"{self.name}:{dest}"], timeout=1800)

    def probe(self, key: str, tail: list[str], markers: list[str]) -> dict:
        # tail travels as one JSON argument, so this probe's own argv never looks like a lane process
        return self.program(PROBE_PROGRAM, self.base, key, json.dumps(markers), json.dumps(tail),
                            json.dumps(sorted(LANE_SCRIPTS)))

    def bundle(self, key: str) -> dict:
        return self.program(BUNDLE_PROGRAM, self.base, key)

    def cleanup(self, key: str) -> dict:
        return self.program(CLEANUP_PROGRAM, self.base, key, timeout=1800)

    def update_list(self, row: list[str]) -> None:
        current = self.run(f"cat {shlex.quote(self.path('list.tsv'))} 2>/dev/null || true")
        tmp, final = shlex.quote(self.path(".list.tsv.vcur")), shlex.quote(self.path("list.tsv"))
        self.run(f"cat > {tmp} && mv {tmp} {final}", stdin=upsert_list_row(current, row))

    def install_scripts(self, have: dict) -> bool:
        if have == local_script_hashes():
            return False
        self.run(f"mkdir -p {shlex.quote(self.path('auto'))}")
        self.rsync(list(LANE_SCRIPTS.values()), self.path("auto") + "/")
        return True

    def upload_src(self, src: Path, key: str) -> None:
        self.run(f"mkdir -p {shlex.quote(self.path('srcs'))}")
        self.rsync([f"{src}/"], self.path("srcs", key) + "/", delete=True)

    def start_lane(self, lane: str, cpus: str, mem: str, key: str) -> None:
        # a simple command with its own redirections and `&`: the detached lane keeps no fd of this ssh session
        # paths in self.env are relative to ~ like remote_base (lane_env refuses quotes and `~`)
        env = " ".join(f'{name}="{value}"' if value.startswith("/") else f'{name}="$HOME/{value}"'
                       for name, value in sorted(self.env.items()))
        self.run(f"cd {shlex.quote(self.base)} || exit 1; VCUR_BASE=\"$PWD\" {env + ' ' if env else ''}nohup setsid bash "
                 f"{shlex.join(lane_tail(lane, cpus, mem, key))} > /dev/null 2>&1 < /dev/null &", timeout=60)


# ---------------------------------------------------------------- state machine

class Runner:
    def __init__(self, cfg: dict, ledger: Ledger, host: Host, hub: Hub, *, apply: bool,
                 workdir: Path = STATE / "vcur", log_path: Path | None = None, out=print, clock=time.time,
                 verbose: bool = False):
        self.cfg, self.ledger, self.host, self.hub = cfg, ledger, host, hub
        self.apply, self.workdir, self.log_path, self.out, self.clock = apply, Path(workdir), log_path, out, clock
        self.verbose = verbose
        self.errors = 0
        self.decisions: dict[str, str] = {}
        self._credited = None

    def say(self, message: str) -> None:
        line = f"{iso(self.clock())} {message}" if self.apply else message
        self.out(line)
        if self.apply and self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as stream:
                stream.write(line + "\n")

    def credited(self) -> set[str]:
        if self._credited is None:
            self._credited = self.hub.credited()
        return self._credited

    # -- one tick
    def tick(self) -> int:
        self.errors, self._credited = 0, None
        try:
            heads = self.hub.heads()
        except Exception as exc:  # noqa: BLE001 - network; the next tick retries
            self.say(f"ERROR listing branches: {type(exc).__name__}: {exc}")
            return 1
        fresh = self.discover(heads)
        active = self.ledger.active()
        if active is not None:
            self.guard(active, self.advance, active, heads)
        if self.ledger.active() is None:
            queue = self.ledger.queued() + ([] if self.apply else fresh)
            if queue:
                item = min(queue, key=lambda i: (i.get("fleet_seconds") or 0, i["queued_at"], i["id"]))
                self.guard(item, self.start, item, heads)
            elif not self.apply or self.verbose:
                self.say("queue empty")
        return 1 if self.errors else 0

    def guard(self, item: dict, step, *args) -> None:
        try:
            step(*args)
            if self.apply and item.get("errors") and item["id"] in self.ledger.items:
                item["errors"] = 0
                self.ledger.save()
        except (Hold, stage_current.StageError) as exc:
            self.park(item, HELD, str(exc))
        except Supersede as exc:
            self.park(item, SUPERSEDED, str(exc))
        except Exception as exc:  # noqa: BLE001 - transient (ssh, gh, git): the next tick retries this step
            self.errors += 1
            note = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                note += " | " + exc.stderr.strip()[-300:]
            self.say(f"ERROR {item['id']} ({item['state']}): {note}")
            if self.apply and item["id"] in self.ledger.items:
                item["errors"] = item.get("errors", 0) + 1
                item["last_error"] = note[:500]
                if item["errors"] >= self.cfg["max_step_errors"] and HELD in TRANSITIONS[item["state"]]:
                    self.park(item, HELD, f"{item['errors']} consecutive errors; last: {note}")
                else:
                    self.ledger.save()

    def park(self, item: dict, state: str, reason: str) -> None:
        if not self.apply or item["id"] not in self.ledger.items:
            self.say(f"would mark {item['id']} {state}: {reason}")
            return
        self.ledger.move(item, state, note=reason, **({"held_reason": reason} if state == HELD else {}))
        self.say(f"{state.upper()} {item['id']}: {reason}")

    # -- discovery
    def discover(self, heads: dict[str, str]) -> list[dict]:
        seen = self.ledger.data["seen"]
        decisions, candidates, dirty = {}, [], False
        for branch, commit in sorted(heads.items()):
            problem = parse_branch(branch)
            if problem is None:
                continue
            cached = seen.get(branch)
            try:
                if cached and cached.get("commit") == commit:
                    facts = cached["facts"]
                else:
                    facts = facts_of(self.hub.file_json(commit, "VERIFICATION.json"),
                                     self.hub.commit_info(commit)["time"])
                    seen[branch] = {"commit": commit, "facts": facts, "checked_at": iso(self.clock())}
                    dirty = True
                decision = select_branch(branch, commit, facts, self.cfg)
                if decision == "queue" and self.cfg["skip_credited"]:
                    decision = select_branch(branch, commit, facts, self.cfg, credited=self.credited())
            except Exception as exc:  # noqa: BLE001 - network; the next tick retries this branch
                self.errors += 1
                self.say(f"ERROR reading {branch}@{commit[:12]}: {type(exc).__name__}: {exc}")
                decisions[branch] = "error"
                continue
            decisions[branch] = decision
            if decision == "queue":
                candidates.append((problem, branch, commit, facts))
        # Submission needs one current branch per problem: a problem that already has one (e.g. a re-verified
        # `-own-` package) needs no second record, and of several candidates only the newest is verified.
        # force_branches overrides both rules.
        force = set(self.cfg["force_branches"])
        current = {parse_branch(b) for b, d in decisions.items() if d == "current"}
        newest = {}
        for cand in candidates:
            best = newest.get(cand[0])
            if cand[1] in force:
                continue
            if best is None or (cand[3].get("commit_time") or "", cand[1]) > (best[3].get("commit_time") or "", best[1]):
                newest[cand[0]] = cand
        fresh = []
        for problem, branch, commit, facts in candidates:
            if branch not in force and problem in current:
                decisions[branch] = "skip:problem_has_current_branch"
            elif branch not in force and newest[problem][1] != branch:
                decisions[branch] = "skip:newer_branch_for_problem"
            elif self.ledger.find(branch, commit) is None:
                fresh.append(new_item(problem, branch, commit, facts.get("wall_seconds"), self.clock()))
        self.decisions = decisions
        for item in self.ledger.queued():
            if heads.get(item["branch"]) != item["commit"]:
                self.park(item, SUPERSEDED, f"{item['branch']} moved to {str(heads.get(item['branch']))[:12]} "
                                            "before the run started")
        if self.apply:
            for item in fresh:
                self.ledger.add(item)
                self.say(f"queued {item['id']} ({item['branch']}, fleet verify {item['fleet_seconds']} s)")
            if dirty:
                self.ledger.save()
        else:
            counts = collections.Counter(decisions.values())
            self.say(f"{len(decisions)} solution branches: "
                     + ", ".join(f"{name} {n}" for name, n in sorted(counts.items())))
            boring = ("current", "skip:credited", "skip:before_cutoff")
            for branch, decision in sorted(decisions.items()):
                if self.verbose or decision not in boring:
                    self.say(f"  {decision:30} {branch} {heads[branch][:12]}")
            for item in fresh:
                self.say(f"would queue {item['id']} ({item['branch']}, fleet verify {item['fleet_seconds']} s)")
        return fresh

    # -- start one verification
    def start(self, item: dict, heads: dict[str, str]) -> None:
        problem, branch, commit, key = item["problem_id"], item["branch"], item["commit"], item["key"]
        if heads.get(branch) != commit:
            raise Supersede(f"{branch} moved to {str(heads.get(branch))[:12]} before the run started")
        cpus, mem = self.cfg["cpus"], memory_for(problem, self.cfg)
        tail = lane_tail(self.cfg["lane"], cpus, mem, key)
        probe = self.host.probe(key, tail, self.cfg["busy_markers"])
        if probe.get("run_exists") or probe.get("lane_active"):
            raise Hold(f"runs/{key} already exists on {self.host.name}; inspect it before --requeue")
        wait = host_wait_reason(probe, mem, self.cfg)
        if wait:
            self.say(f"{'waiting' if self.apply else 'would wait'}: {item['id']} not started, {wait}")
            return
        timeout = compute_timeout(item.get("fleet_seconds"), problem, self.cfg)
        if not self.apply:
            self.say(f"would start {item['id']} as {key} on {self.host.name}: cpus {cpus} mem {mem} "
                     f"timeout {timeout} s")
            return
        work = self.workdir / key
        clone, src = work / "clone", work / "src"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        self.hub.clone(branch, clone)
        head = git_out(clone, "rev-parse", "HEAD")
        if head != commit:
            raise RuntimeError(f"clone of {branch} is at {head[:12]}, not {commit[:12]}")
        old = json.loads((clone / "VERIFICATION.json").read_text())
        if old.get("benchmark_commit") == BENCH:
            raise Supersede("VERIFICATION.json is already on the current benchmark")
        if old.get("problem_id") != problem:
            raise Hold(f"VERIFICATION.json is for {old.get('problem_id')}")
        size = publish.archive_size(clone)
        cap, margin = self.cfg["archive_cap_bytes"], self.cfg["archive_margin_bytes"]
        if size + margin >= cap:
            raise Hold(f"source.tar.gz is already {size} bytes (+{margin} for the record) >= {cap}; "
                       "shrink it (local/repack_numerals.py) instead of verifying")
        files = copy_sources(clone, src)
        fleet = int(round(float(old.get("verification_wall_seconds") or 0)))
        timeout = compute_timeout(fleet, problem, self.cfg)
        if self.host.install_scripts(probe.get("scripts") or {}):
            if self.host.probe(key, tail, self.cfg["busy_markers"]).get("scripts") != local_script_hashes():
                raise RuntimeError("lane scripts on the host differ from vcur/ after install")
        self.host.update_list([key, commit, "0", "0", str(fleet), problem, str(timeout)])
        self.host.upload_src(src, key)
        plan = f"{key} on {self.host.name}: cpus {cpus} mem {mem} timeout {timeout} s, {files} source files"
        self.ledger.move(item, STARTING, note=plan, cpus=cpus, mem=mem, timeout=timeout, fleet_seconds=fleet,
                         lane=self.cfg["lane"], started_at=self.clock(), archive_bytes_before=size,
                         source_files=files)
        self.host.start_lane(self.cfg["lane"], cpus, mem, key)
        self.say(f"started {item['id']} as {plan}")

    # -- the active item
    def advance(self, item: dict, heads: dict[str, str]) -> None:
        steps = {STARTING: self.poll, RUNNING: self.poll, VERIFIED: self.stage, STAGED: self.push,
                 PUSHED: self.finish}
        while item["state"] in steps:
            before = item["state"]
            steps[before](item)
            if not self.apply or item["state"] in (before, RUNNING):
                return

    def poll(self, item: dict) -> None:
        tail = lane_tail(item.get("lane", self.cfg["lane"]), item["cpus"], item["mem"], item["key"])
        probe = self.host.probe(item["key"], tail, self.cfg["busy_markers"])
        target, note = run_outcome(item, probe, self.clock(), self.cfg)
        if target is None:
            self.say(f"{item['id']}: {note}")
        elif target == HELD:
            raise Hold(note)
        elif not self.apply:
            self.say(f"would mark {item['id']} {target}: {note}")
        elif target == FAILED:
            cleaned = self.host.cleanup(item["key"]) if self.cfg["cleanup_failed_builds"] else {"bytes": 0}
            self.ledger.move(item, FAILED, note=note, run_done=probe.get("run_done"),
                             cleaned_bytes=cleaned["bytes"])
            self.say(f"FAILED {item['id']}: {note} (not replayed; --requeue after review)")
        else:
            self.ledger.move(item, target, note=note, run_done=probe.get("run_done"))
            self.say(f"{item['id']}: {note}")

    def branch_moved(self, item: dict, remote_head: str | None) -> None:
        if remote_head is None:
            raise Hold(f"{item['branch']} no longer exists")
        facts = facts_of(self.hub.file_json(remote_head, "VERIFICATION.json"), None)
        if facts.get("benchmark_commit") == BENCH:
            raise Supersede(f"{item['branch']} moved to {remote_head[:12]}, which already has a current record")
        raise Hold(f"{item['branch']} moved from {item['commit'][:12]} to {remote_head[:12]} during "
                   "verification; nothing pushed")

    def stage(self, item: dict) -> None:
        if not self.apply:
            self.say(f"would stage {item['id']}: VERIFICATION.json + README in one commit on {item['commit'][:12]}")
            return
        clone = self.workdir / item["key"] / "clone"
        shutil.rmtree(clone, ignore_errors=True)
        clone.parent.mkdir(parents=True, exist_ok=True)
        self.hub.clone(item["branch"], clone)
        head = git_out(clone, "rev-parse", "HEAD")
        if head != item["commit"]:
            self.branch_moved(item, head)
        bundle = self.host.bundle(item["key"])
        result = stage_current.stage_clone(clone, item["problem_id"], bundle, host=self.host.name)
        new_head = result["new_head"]
        hits = secret_scan(run(["git", "-C", clone, "diff", item["commit"], new_head]))
        size = publish.archive_size(clone)
        tree = git_out(clone, "rev-parse", f"{new_head}^{{tree}}")
        self.ledger.move(item, STAGED, note=f"{new_head[:12]} receipt {result['receipt_sha256'][:16]}",
                         new_head=new_head, tree=tree, archive_bytes=size, secret_hits=hits,
                         receipt_sha256=result["receipt_sha256"],
                         verification_wall_seconds=result["verification_wall_seconds"],
                         run_done=bundle.get("run_done"))
        self.say(f"staged {item['id']}: {new_head[:12]}, source.tar.gz {size} bytes, secret hits {len(hits)}")

    def push(self, item: dict) -> None:
        branch, commit, new_head = item["branch"], item["commit"], item["new_head"]
        remote = self.hub.head(branch)
        clone = self.workdir / item["key"] / "clone"
        if remote == commit and not self._clone_at(clone, new_head):
            if self.apply:
                self.ledger.move(item, VERIFIED, note="staged clone is gone; staging again")
            else:
                self.say(f"would stage {item['id']} again (staged clone is gone)")
            return
        decision = push_decision(remote, commit, new_head, item.get("archive_bytes"),
                                 self.cfg["archive_cap_bytes"], item.get("secret_hits"))
        if decision == "already_pushed":
            if self.apply:
                self.ledger.move(item, PUSHED, note="remote is already at the staged commit")
            else:
                self.say(f"would read back {item['id']} (remote already at {new_head[:12]})")
            return
        if decision in ("hold:remote_moved", "hold:branch_missing"):
            self.branch_moved(item, remote)
        if decision == "hold:archive_too_big":
            raise Hold(f"source.tar.gz would be {item.get('archive_bytes')} bytes >= {self.cfg['archive_cap_bytes']}")
        if decision == "hold:secret_scan":
            raise Hold(f"secret-like strings in the metadata diff: {', '.join(item['secret_hits'])}")
        if not self.apply:
            self.say(f"would push {branch}: {commit[:12]} -> {new_head[:12]} (fast-forward)")
            return
        self.hub.push(clone, new_head, branch)
        self.ledger.move(item, PUSHED, note=f"pushed {commit[:12]} -> {new_head[:12]}", pushed_at=iso(self.clock()))
        self.say(f"pushed {branch}: {commit[:12]} -> {new_head[:12]}")

    def finish(self, item: dict) -> None:
        if not self.apply:
            self.say(f"would read back {item['id']} and remove its .lake/build on {self.host.name}")
            return
        remote = self.hub.head(item["branch"])
        if remote != item["new_head"]:
            raise Hold(f"readback: {item['branch']} is at {str(remote)[:12]}, pushed {item['new_head'][:12]}")
        if self.hub.commit_info(item["new_head"])["tree"] != item["tree"]:
            raise Hold("readback: the remote tree differs from the staged tree")
        cleaned = self.host.cleanup(item["key"])
        self.ledger.move(item, DONE, note=f"read back; removed {len(cleaned['removed'])} build dir(s)",
                         readback_at=iso(self.clock()), cleaned_bytes=cleaned["bytes"])
        shutil.rmtree(self.workdir / item["key"], ignore_errors=True)
        self.say(f"done {item['problem_id']}\t{item['commit']}\t{item['new_head']}\t{item['receipt_sha256']}\t"
                 f"{round(item.get('verification_wall_seconds') or 0)}s; freed {cleaned['bytes'] / 2 ** 30:.1f} GiB")

    @staticmethod
    def _clone_at(clone: Path, commit: str) -> bool:
        try:
            return (clone / ".git").exists() and git_out(clone, "rev-parse", "HEAD") == commit
        except subprocess.CalledProcessError:
            return False


def print_status(ledger: Ledger, out=print) -> None:
    items = sorted(ledger.items.values(), key=lambda i: i.get("updated_at") or i["queued_at"])
    if not items:
        out("ledger is empty")
    for item in items:
        last = (item.get("history") or [{}])[-1]
        out(f"{item['state']:10} {item['id']:60} {item['key']:58} {item.get('updated_at') or item['queued_at']} "
            f"{last.get('note', '')[:120]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan (the default)")
    mode.add_argument("--apply", action="store_true", help="execute: start, stage, push, clean up")
    parser.add_argument("--once", action="store_true", help="one tick, then exit (LaunchAgent)")
    parser.add_argument("--status", action="store_true", help="print the ledger and exit (no network)")
    parser.add_argument("--requeue", metavar="ITEM", help="put a failed/held item back in the queue (new run key)")
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--ledger", default=str(LEDGER))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    ledger_path = Path(args.ledger)
    if args.status:
        print_status(Ledger(ledger_path, persist=False))
        return 0
    if not REPO:
        parser.error(f"no publish.repo in {publish.FLEET}; see fleet.example.json")
    if not args.apply:
        ledger = Ledger(ledger_path, persist=False)
        if args.requeue:
            item = ledger.items.get(args.requeue)
            print(f"would requeue {args.requeue} ({item['state'] if item else 'unknown item'}); add --apply")
            return 0
        print("DRY RUN: read-only; --apply executes")
        return Runner(cfg, ledger, Host(cfg), Hub(), apply=False, verbose=args.verbose).tick()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path.parent / "vcur.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another vcur run holds the lock")
            return 0
        ledger = Ledger(ledger_path)
        if args.requeue:
            item = ledger.requeue(args.requeue)
            print(f"requeued {item['id']} as {item['key']}")
            return 0
        runner = Runner(cfg, ledger, Host(cfg), Hub(), apply=True, log_path=ledger_path.parent / "vcur.log",
                        verbose=args.verbose)
        while True:
            rc = runner.tick()
            if args.once or (ledger.active() is None and not ledger.queued()):
                return rc
            time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    sys.exit(main())
