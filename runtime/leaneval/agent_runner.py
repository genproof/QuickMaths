"""Local Claude Code CLI supervisor. Model output is always awaiting independent verification.

One supervisor run is one ledger attempt. Inside it, subscription usage-limit pauses are resumed in the same native session
after the reported reset, and early turn ends are nudged while thresholds remain.
Tokens are polled stopping thresholds: all input (uncached + cache write + cache read)
plus output. No credential is printed or copied
into model-visible environment: the OAuth token is placed in the per-job CLI config
directory only while the supervisor runs and is removed afterwards.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leaneval.runtime import Budget, RunLedger, RuntimeBlocked, Usage
from leaneval import research, secret_refs

MODEL = "claude-opus-5-5"
EFFORT = "xhigh"
# No Sonnet fallback (user 2026-10-05): the subscription limit is shared by all models, so the
# fallback was refused every time; on a limit rotate accounts on Opus, else wait for the reset.
# The constants stay so evidence of older rounds (fallback_used) keeps its meaning.
FALLBACK_MODEL = "claude-sonnet-5-5"
FALLBACK_EFFORT = "max"
ALLOWED = {MODEL: EFFORT, FALLBACK_MODEL: FALLBACK_EFFORT}
CLI_VERSION = "2.1.289"
CLI_SHA256 = "a186b99e4a9c88366cd49df2f7dad56c61fc306ef0140b19ee64b7c42a8d1348"
TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
# landrun is the enforcement boundary; inside it the CLI must not stop to ask a human.
PERMISSION_MODE = "bypassPermissions"
MAX_NUDGES = 50
# lrh: after the handoff nudge, or after this many replies in a row without a tool call, the
# round ends instead of nudging on (80% of our nudges got "not ready, nothing changed" replies,
# 9-24M cached tokens per round; a fresh continuation round works better).
IDLE_REPLIES_END = 2
# lrh: upstream stopped nudging below 15% of the round (36 of 240 min). In our failed rounds
# that left a median 27 min (14% of wall) unused. Nudge until 5 minutes remain.
NUDGE_FLOOR = 0.15  # token share only
NUDGE_MIN_SECONDS = 300
HANDOFF_SECONDS = 900
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF = (60, 300, 900)
UNKNOWN_RESET_WAIT = 900
RESET_MARGIN = 90
MIN_LIMIT_PAUSE = 60.0
# Optional coordinator reserve, read live from <state_root>/coordinator-reserve.json
# ({"utilization": 0.9}): workers then pause at that share of the 5-hour or weekly window until
# its reset. No file = no reserve (user 2026-10-05: workers use whole windows; auto-continuation
# resumes everything after a reset).
RESERVE_FILE = "coordinator-reserve.json"
USAGE_SEMANTICS = "claude-message-invocation-session-v2"
SYSTEM_NOTE = (
    "You are running unattended inside a controller sandbox for one assigned Lean proof task. "
    "No human will answer questions or approve anything: never ask for confirmation, decide "
    "and keep working autonomously. Only the assigned workspace and /tmp are writable. "
    "Never print environment variables and never read credential or configuration files "
    "of the CLI."
)
# lrh P1: `./lean check` is the light readiness check (no sorry, warm Submission build, the
# official statement wrapper, standard hole axioms; seconds). READY when it passes; the official
# verification runs outside the session (`./lean check --full` is optional).
NUDGE = (
    "Controller note: {minutes} minutes of this round remain. Keep proving now: take the next "
    "lemma on the critical path of the current residual (the RESIDUALS table in NOTES.md) and "
    "work on it until it compiles. If every official hole is completely proved, run ./lean check "
    "(the light check: seconds) and, if it passes, reply exactly READY_FOR_VERIFICATION; the "
    "official verification runs outside this session. Do not stop to write summaries; the "
    "controller tells you when to write the handoff."
)
NUDGE_HANDOFF = (
    "Controller note: about {minutes} minutes of this round remain. Finish the current step, "
    "then update NOTES.md: the HANDOFF at the top states exactly what is proved and what is open "
    "(declaration names and types) and has the four fields Next step (declaration and file), "
    "Active tactic, Last failure (the stuck goal, its hypotheses and the error) and Do not retry; "
    "bring RESIDUALS and TACTICS up to date; then stop. If every official hole is completely "
    "proved and ./lean check (light, seconds) passes, reply exactly READY_FOR_VERIFICATION."
)


def handoff_due(left_wall: float, available_wall: float) -> bool:
    return left_wall <= min(HANDOFF_SECONDS, 0.25 * available_wall)


def nudge_text(left_wall: float, left_tokens: int, available_wall: float,
               available_tokens: int) -> str | None:
    """lrh: the nudge for an early turn end, or None when the round should end.

    Floors are 5 min / 15 min for real rounds and the upstream shares for tiny budgets.
    """
    if (left_wall <= min(NUDGE_MIN_SECONDS, NUDGE_FLOOR * available_wall)
            or left_tokens <= NUDGE_FLOOR * available_tokens):
        return None
    template = NUDGE_HANDOFF if handoff_due(left_wall, available_wall) else NUDGE
    return template.format(minutes=int(left_wall // 60))


def end_after_turn(tool_uses: int, idle_replies: int, handoff_sent: bool) -> tuple[int, bool]:
    """lrh: (idle replies in a row, end the round now) after a completed turn without READY."""
    idle_replies = 0 if tool_uses else idle_replies + 1
    return idle_replies, handoff_sent or idle_replies >= IDLE_REPLIES_END
RESUME_AFTER_LIMIT = (
    "Controller note: the previous turn was interrupted by a subscription usage-limit pause. "
    "The workspace is unchanged since then. Continue the assigned proof task from the current "
    "state; recheck any build that was in flight."
)
READY_MARKER = "READY_FOR_VERIFICATION"
_NEGATED_READY = re.compile(r"\bnot\b|n't\b|\bno\b", re.IGNORECASE)


def _is_ready(text: str) -> bool:
    """READY only when a line is exactly the marker, or the final line ends with it.

    A reply such as "...: **not READY_FOR_VERIFICATION**." followed by a report must not
    trigger the official verifier (observed 2026-10-04 on job w179-013).
    """
    lines = [line.strip().strip("*_`>#- ").rstrip(".!").strip("*_` ")
             for line in text.splitlines() if line.strip()]
    if any(line == READY_MARKER for line in lines):
        return True
    if not lines or not lines[-1].endswith(READY_MARKER):
        return False
    return not _NEGATED_READY.search(lines[-1][:-len(READY_MARKER)])


_LIMIT_TEXT = re.compile(
    r"usage limit|rate limit|limit reached|hit your limit|out of (?:extra )?usage|"
    r"5-hour limit|weekly limit|resets? (?:at|in)", re.IGNORECASE)
_AUTH_TEXT = re.compile(
    r"invalid api key|authentication|oauth token|not logged in|please run /login|"
    r"401|unauthori[sz]ed|token (?:has )?expired|revoked", re.IGNORECASE)


def _home() -> Path:
    return Path(os.environ.get("LEANEVAL_HOME", str(Path.home())))


def cli_path() -> Path:
    return Path(os.environ.get("LEANEVAL_CLAUDE_BIN",
                               str(_home() / ".local/share/lean-eval-claude" / CLI_VERSION
                                   / "claude")))


def state_root() -> Path:
    """CLI state lives outside campaign trees (telemetry walks those; keep secrets out)."""
    return Path(os.environ.get("LEANEVAL_CLAUDE_STATE",
                               str(_home() / "projects/lean-eval-claude-state")))


# The subscription token is a secret reference (leaneval.secret_refs): file:PATH or env:NAME.
DEFAULT_TOKEN_REF = "file:~/.config/leh/accounts/default.token"


def token_ref() -> str:
    """LEANEVAL_CLAUDE_TOKEN_REF, else the older LEANEVAL_CLAUDE_TOKEN_FILE path, else the default."""
    return (os.environ.get("LEANEVAL_CLAUDE_TOKEN_REF") or os.environ.get("LEANEVAL_CLAUDE_TOKEN_FILE")
            or DEFAULT_TOKEN_REF)


def token_path() -> Path | None:
    """The token file of a file: reference; None for env: references."""
    return secret_refs.path_of(token_ref(), _home())


def workbench_path() -> Path:
    return Path(os.environ.get("LEANEVAL_WORKBENCH",
                               str(_home() / "projects/lean-eval-workbench")))


def landrun_path() -> Path:
    return Path(os.environ.get("LEANEVAL_LANDRUN",
                               str(workbench_path() / ".tools/bin/landrun")))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_cli() -> Path:
    cli = cli_path()
    if not cli.is_file() or cli.is_symlink():
        raise RuntimeBlocked("pinned Claude Code binary missing")
    expected = os.environ.get("LEANEVAL_CLAUDE_SHA256", CLI_SHA256)
    if _sha256(cli) != expected:
        raise RuntimeBlocked("pinned Claude Code binary checksum mismatch")
    return cli


def token_paths() -> list[Path]:
    """The primary subscription token file, then extra accounts in the same directory named
    <primary file name>-2, -3, ...; empty for an env: reference."""
    primary = token_path()
    if primary is None:
        return []
    extra = [p for p in primary.parent.glob(primary.name + "-*") if not p.name.endswith(".tmp")]
    return [primary, *sorted(extra, key=lambda p: (len(p.name), p.name))]


def account_labels() -> list[str]:
    """Labels of the configured subscription accounts whose secret source exists."""
    paths = token_paths()
    if not paths:
        return [secret_refs.label(token_ref(), _home())]
    return [secret_refs.label(str(p)) for p in paths if p.is_file()]


def account_limits_path() -> Path:
    """This host's usage-limit blocks per "<token file>|<model>", shared by its supervisors."""
    return state_root() / "account-limits.json"


def read_account_limits(now: float) -> dict[str, float]:
    try:
        data = json.loads(account_limits_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > now}


def block_account(name: str, model: str, until: float) -> None:
    data = read_account_limits(time.time())
    key = f"{name}|{model}"
    data[key] = max(until, data.get(key, 0.0))
    path = account_limits_path()
    temporary = path.with_name(f".account-limits.{os.getpid()}.tmp")
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data))
        temporary.replace(path)


def _read_token(ref: str | Path | None = None) -> str:
    """The subscription token of `ref` (a secret reference or a token file), default token_ref()."""
    try:
        token = secret_refs.resolve(str(ref) if ref is not None else token_ref(), _home())
    except secret_refs.SecretError as exc:
        raise RuntimeBlocked(f"subscription token: {exc}") from exc
    if not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{20,}", token):
        raise RuntimeBlocked("subscription token has an unexpected format")
    return token


@dataclass
class TranscriptEvidence:
    """Tail only this job's own session transcript for native model/effort evidence."""

    root: Path
    session_id: str
    path: Path | None = None
    offset: int = 0
    pending: bytes = b""
    efforts: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    pairs: set[tuple[str, str]] = field(default_factory=set)
    entries: int = 0
    usage_updates: list = field(default_factory=list)

    def poll(self) -> None:
        self.usage_updates = []
        if not re.fullmatch(r"[0-9a-f-]{36}", self.session_id):
            raise ValueError("unsafe session ID")
        if self.path is None:
            matches = list((self.root / "projects").glob(f"*/{self.session_id}.jsonl"))
            if len(matches) > 1:
                raise ValueError("ambiguous transcript path for session")
            if not matches:
                return
            self.path = matches[0]
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        self.pending += chunk
        while b"\n" in self.pending:
            raw, self.pending = self.pending.split(b"\n", 1)
            if not raw.strip():
                continue
            event = json.loads(raw)
            if event.get("type") != "assistant" or event.get("isSidechain"):
                continue
            if event.get("sessionId") != self.session_id:
                raise ValueError("transcript session identity mismatch")
            message = event.get("message") or {}
            if message.get("id") and isinstance(message.get("usage"), dict):
                self.usage_updates.append((message["id"], message["usage"]))
            model = (event.get("message") or {}).get("model")
            if not isinstance(model, str) or model == "<synthetic>":
                continue
            self.entries += 1
            self.models.add(model)
            for key in ("effort", "perTurnEffort"):
                if isinstance(event.get(key), str):
                    self.efforts.add(event[key])
                    self.pairs.add((model, event[key]))


from .claude_accounting import StreamAccount, SEMANTICS


def _existing(paths: list[Path]) -> list[str]:
    seen: list[str] = []
    for path in paths:
        text = str(path)
        if path.exists() and text not in seen:
            seen.append(text)
    return seen


def sandbox_policy(job: Path, agent: Path, config_dir: Path,
                   sandbox_home: Path | None = None) -> dict[str, list[str]]:
    home = _home()
    workbench = workbench_path()
    source_venv = (agent / ".venv").resolve()
    rox = _existing([Path(p) for p in ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
                                        "/libx32", "/etc", "/opt", "/snap")]
                    + [home / ".elan", home / ".cargo", cli_path().parent, workbench,
                       agent.resolve(), source_venv.parent, source_venv])
    return {"rox": rox,
            # /etc/resolv.conf points into /run on these hosts; DNS needs the target.
            "ro": _existing([Path("/proc"), Path("/sys"), Path("/dev"), job,
                             Path("/run/systemd/resolve"), Path("/run/resolvconf")]),
            "rw": _existing([Path("/dev/null"), Path("/dev/shm"), config_dir]),
            "rwx": _existing([job / "workspace", Path("/tmp")]
                             + ([sandbox_home] if sandbox_home else []))}


def build_command(job: Path, session_id: str, *, resume: bool, agent: Path,
                  config_dir: Path, env_keys: list[str], model: str = MODEL,
                  effort: str = EFFORT, sandbox_home: Path | None = None) -> list[str]:
    policy = sandbox_policy(job, agent, config_dir, sandbox_home)
    # Same mode as the official comparator: enforce the kernel's best Landlock ABI (v4 here).
    sandbox = [str(landrun_path()), "--best-effort"]
    for flag in ("rox", "ro", "rw", "rwx"):
        for path in policy[flag]:
            sandbox += [f"--{flag}", path]
    sandbox.append("--unrestricted-network")
    for name in sorted(env_keys):
        sandbox += ["--env", name]
    claude = [str(cli_path()), "-p", "--output-format", "stream-json", "--verbose",
              "--model", model, "--effort", effort, "--permission-mode", PERMISSION_MODE,
              "--allowedTools", ",".join(TOOLS), "--permission-prompts", "none",
              "--strict-mcp-config", "--setting-sources", "user", "--disable-slash-commands",
              "--tools", ",".join(TOOLS), "--append-system-prompt", SYSTEM_NOTE]
    claude += ["--resume", session_id] if resume else ["--session-id", session_id]
    return [*sandbox, "--", *claude]


def build_env(config_dir: Path, sandbox_home: Path) -> dict[str, str]:
    """Fresh environment: nothing (e.g. ANTHROPIC_* from host shells) is inherited."""
    home = _home()
    workbench = workbench_path()
    path = os.pathsep.join([
        str(workbench / ".tools/bin"),
        str(home / ".elan/bin"), str(home / ".cargo/bin"), str(home / ".local/bin"),
        "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
    ])
    xdg = config_dir / "xdg"
    env = {
        # A private empty HOME: host dotfiles (which may hold other credentials) stay unread.
        "HOME": str(sandbox_home), "ELAN_HOME": os.environ.get("ELAN_HOME", str(home / ".elan")),
        "PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "USER": os.environ.get("USER") or getpass.getuser(),
        "LOGNAME": os.environ.get("LOGNAME") or getpass.getuser(),
        "SHELL": "/bin/bash", "TMPDIR": "/tmp", "TERM": "dumb",
        "CLAUDE_CONFIG_DIR": str(config_dir), "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
        "BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "1200000",
        "XDG_CACHE_HOME": str(xdg / "cache"), "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_STATE_HOME": str(xdg / "state"), "XDG_DATA_HOME": str(xdg / "data"),
        "MATHLIB_NO_CACHE_ON_UPDATE": "1", "GIT_OPTIONAL_LOCKS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("LEANEVAL_FAKE_SCENARIO", "LEANEVAL_FAKE_STATE"):
        if name in os.environ:  # test fixtures only; never set in production
            env[name] = os.environ[name]
    return env


# Tripwire (docs/SECURITY.md): the CLI must read its credentials file, so the agent's shell, which runs in
# the same Landlock domain, can read it too. A tool call whose command or path reaches for the credentials,
# the CLI's configuration directory or a process environment ends the round: the CLI is stopped, the
# credentials file removed and the round ends with stop reason `credential_access`, which halts the
# runner (and the feeder stops every runner of that account) until an operator rotates the account. It is
# detection, not prevention: the call has already run when the alarm fires, so treat the token as exposed.
CREDENTIAL_TEXT = re.compile(r"\.credentials\b|claude[_-]config[_-]dir|claude-home/(?!xdg/)|/proc/[^\s'\"|;&]*/environ"
                             r"|\.config/leh\b|setup-token|oauth", re.IGNORECASE)


def credential_access(block: Any, config_dir: Path) -> str | None:
    """The tool name when a tool_use block reaches for the round's credentials, else None. Only what
    selects files or runs commands is read (a Bash command; the paths of Read, Grep, Glob, Edit, Write),
    never the text an agent writes into its files."""
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return None
    name, data = block.get("name"), block.get("input") or {}
    if not isinstance(data, dict):
        return None
    if name == "Bash":
        text = str(data.get("command") or "")
    else:
        text = " ".join(str(data.get(key) or "") for key in ("file_path", "path", "notebook_path")
                        + (("pattern",) if name == "Glob" else ()) + (("glob",) if name == "Grep" else ()))
    # the configuration directory itself, but not its XDG cache (`lake` and Mathlib may print paths there)
    for root in {str(config_dir), str(config_dir.resolve())}:
        if any(not rest.startswith("/xdg/") for rest in text.split(root)[1:]):
            return str(name)
    return str(name) if CREDENTIAL_TEXT.search(text) else None


def install_credentials(config_dir: Path, token: str) -> Path:
    """Subscription token as the CLI's own credentials file; never in the environment."""
    path = config_dir / ".credentials.json"
    value = {"claudeAiOauth": {"accessToken": token, "refreshToken": None,
                               "expiresAt": int((time.time() + 300 * 86400) * 1000),
                               "scopes": ["user:inference"], "subscriptionType": "pro"}}
    temporary = config_dir / ".credentials.json.tmp"
    with suppress(FileNotFoundError):
        temporary.unlink()
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
    temporary.replace(path)
    return path


def remove_credentials(config_dir: Path) -> None:
    for name in (".credentials.json", ".credentials.json.tmp"):
        with suppress(FileNotFoundError):
            (config_dir / name).unlink()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def reserve_threshold() -> float | None:
    """The live coordinator-reserve share (0 < x <= 1), or None when the switch is off."""
    try:
        value = json.loads((state_root() / RESERVE_FILE).read_text()).get("utilization")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        return None
    return float(value)


def reserve_until(info: dict[str, Any], now: float, threshold: float | None) -> float | None:
    """Latest future reset among native windows at or above the coordinator reserve."""
    if threshold is None:
        return None
    values = []
    for window in (info.get("unifiedWindows") or {}).values():
        try:
            used = float((window or {}).get("utilization") or 0)
            reset = float((window or {}).get("resetsAt") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if used >= threshold and reset > now:
            values.append(reset)
    return max(values) if values else None


def rate_limit_share() -> Path:
    """This host's latest native rate-limit observation, shared by its supervisors."""
    return state_root() / "rate-limit.json"


def publish_rate_limit(info: dict[str, Any]) -> None:
    path = rate_limit_share()
    temporary = path.with_name(f".rate-limit.{os.getpid()}.tmp")
    with suppress(OSError, TypeError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(dict(info, observed_at=time.time())))
        temporary.replace(path)


def shared_reserve_until(now: float) -> float | None:
    threshold = reserve_threshold()
    if threshold is None:
        return None
    try:
        info = json.loads(rate_limit_share().read_text())
    except (OSError, ValueError):
        return None
    return reserve_until(info, now, threshold) if isinstance(info, dict) else None


def _proc_rows() -> dict[int, tuple[int, int]]:
    rows = {}
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
        if not entry.name.isdigit():
            continue
        with suppress(OSError, IndexError, ValueError):
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            rows[int(entry.name)] = (int(fields[1]), int(fields[2]))
    return rows


def _descendants(root: int) -> set[int]:
    rows = _proc_rows()
    known = {root}
    while True:
        added = {pid for pid, (ppid, _) in rows.items() if ppid in known} - known
        if not added:
            return known - {root}
        known |= added


def _workspace_holders(workspace: Path) -> set[int]:
    holders = set()
    own = os.getuid()
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        with suppress(OSError):
            if entry.stat().st_uid != own:
                continue
            links = [entry / "cwd", *list((entry / "fd").iterdir())]
            for link in links:
                with suppress(OSError):
                    if Path(os.readlink(link)).is_relative_to(workspace):
                        holders.add(int(entry.name))
                        break
    return holders


def _stop(process: subprocess.Popen[bytes], tracked: set[int], workspace: Path) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    for pid in tracked:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    _reap(process.pid, tracked, workspace)


def _reap(leader: int, tracked: set[int], workspace: Path) -> None:
    """Descendants (builds, shells) may outlive or escape the CLI's process group."""
    with suppress(ProcessLookupError):
        os.killpg(leader, signal.SIGKILL)
    for _ in range(3):
        victims = set(tracked) | _workspace_holders(workspace)
        for pid in victims:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if not victims:
            return
        time.sleep(1)


def run_job(job_dir: Path | str, *, resume: bool = False) -> dict[str, Any]:
    """Run one explicitly authorized tranche; never blindly retries uncertain work.

    ``STOP`` is read externally. Tokens are observed stopping thresholds, not a strict
    prebounded limit. A resume of an earlier supervisor requires a settled, accounted
    prior attempt and explicit opt-in.
    """
    job = Path(job_dir).resolve()
    with (job / "supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeBlocked("another supervisor owns this job") from exc
        from leaneval.feedback_broker import serve
        with serve(job):
            return _run_locked(job, resume=resume)


def requested_config() -> dict[str, Any]:
    return {"model": MODEL, "reasoning_effort": EFFORT, "cli": "claude-code",
            "cli_version": CLI_VERSION, "cli_sha256": CLI_SHA256,
            "permission_mode": PERMISSION_MODE, "allowed_tools": list(TOOLS),
            "permission_prompts": "none", "credentials": "per-job config file, removed after",
            "sandbox": "landrun-workspace-write",
            "network_access": True, "isolated_config_dir": True, "tools": list(TOOLS),
            "mcp": "none", "billing": "claude_subscription", "usd": None,
            "token_counting": "input+cache_write+cache_read+output",
            "rate_limit_policy": ("on a usage limit resume the same session on the next "
                                  "subscription account (Opus only), else wait for the "
                                  "earliest native reset"),
            "subscription_accounts": account_labels(),
            "coordinator_reserve_switch": RESERVE_FILE,
            "max_nudges": MAX_NUDGES}


PREVIOUS_NOTES = "NOTES.prev.md"


def carry_previous_notes(config: dict[str, Any], workspace: Path) -> str:
    """Copy the parent round's NOTES.md (snapshots keep only .lean files; user 2026-10-05).

    Returns the prompt addition announcing the copy, or "" when there is nothing to carry.
    """
    parent = (config.get("continuation_provenance") or {}).get("parent_job")
    if not isinstance(parent, str):
        return ""
    source = Path(parent) / "workspace" / "NOTES.md"
    target = workspace / PREVIOUS_NOTES
    with suppress(OSError):
        if source.is_file() and not target.exists():
            shutil.copyfile(source, target)
    if not target.is_file():
        return ""
    return (f"\n\nController note: the previous round's NOTES.md is in {PREVIOUS_NOTES} "
            "(read-only reference, not carried further). Read its HANDOFF section first "
            "instead of re-reading the whole library, and keep a short HANDOFF section at the "
            "top of your own NOTES.md for the next round.\n")


def _run_locked(job: Path, *, resume: bool) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    config = json.loads((job / "config.json").read_text())
    prompt = (job / "prompt.txt").read_text()
    workspace = job / "workspace"
    if not workspace.is_dir():
        raise ValueError("job requires workspace directory")
    prompt += carry_previous_notes(config, workspace)
    if not isinstance(config.get("run_id"), str) or not config["run_id"]:
        raise ValueError("config requires run_id")
    agent = Path(__file__).resolve().parents[1]
    _verify_cli()
    if not landrun_path().is_file():
        raise RuntimeBlocked("landrun sandbox binary missing")
    token = _read_token()
    accounts = [(secret_refs.label(token_ref(), _home()), token)]
    for extra in token_paths()[1:]:
        with suppress(RuntimeBlocked, OSError):
            accounts.append((secret_refs.label(str(extra)), _read_token(extra)))
    prompt += research.prepare(job, workbench_path(), config)
    budget = Budget(config["token_limit"], config["wall_seconds"])
    assignment = config.get("campaign_assignment") or {}
    max_nudges = int(assignment.get("max_nudges", MAX_NUDGES))
    # Per-assignment preferred subscription accounts (account labels), then the rest.
    order = [n for n in assignment.get("account_order") or [] if isinstance(n, str)]
    accounts.sort(key=lambda a: order.index(a[0]) if a[0] in order else len(order))
    # Per-assignment context economy: auto-compact near this many tokens (user 2026-10-05).
    window = assignment.get("auto_compact_window")
    window = window if type(window) is int and 50_000 <= window <= 1_000_000 else None
    requested = requested_config()
    requested["max_nudges"] = max_nudges
    requested["auto_compact_window"] = window
    requested["account_order"] = [a[0] for a in accounts]
    identity = {"run_id": config["run_id"], "config": config, "requested_config": requested}
    ledger = RunLedger(job / "ledger.sqlite3", budget=budget, identity=identity,
                       cancellation_file=job / "STOP")
    state = ledger.snapshot()
    previous = ledger.resume()
    session: str | None = None
    if state["attempts"]:
        if not resume:
            raise RuntimeBlocked("job already launched; explicit safe resume is required")
        if (not previous or not previous.content.get("safe_resume") or
                state["accounted"]["unresolved"] or
                any(a["status"] != "settled" for a in state["attempts"].values())):
            raise RuntimeBlocked("interrupted or unknown invocation cannot be blindly resumed")
        session = previous.content.get("thread_id")
        if not session:
            raise RuntimeBlocked("no persisted session ID to resume")
    elif resume:
        raise RuntimeBlocked("cannot resume a job that has never run")
    from leaneval import lineage
    available_tokens, available_wall = lineage.model_allowance(job, config, state["accounted"])
    if available_tokens <= 0 or available_wall <= 0:
        raise RuntimeBlocked("run budget exhausted")
    attempt_id = f"invocation-{len(state['attempts']) + 1}"
    # Validate a resume accounting checkpoint before reserving new work.
    account = (StreamAccount.restore(previous.content.get("account_state"),
                                    previous.content.get("session_usage"))
               if session else StreamAccount())
    reservation = ledger.reserve(attempt_id, tokens=available_tokens, wall_seconds=available_wall)
    state = state_root() / hashlib.sha256(str(job).encode()).hexdigest()[:24]
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    (state / "JOB").write_text(str(job) + "\n")
    config_dir = state / "claude-home"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    for sub in ("cache", "config", "state", "data"):
        (config_dir / "xdg" / sub).mkdir(parents=True, exist_ok=True)
    sandbox_home = state / "sandbox-home"
    sandbox_home.mkdir(mode=0o700, exist_ok=True)
    env = build_env(config_dir, sandbox_home)
    if window:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
    install_credentials(config_dir, token)
    session_id = session or str(uuid.uuid4())
    transcript = TranscriptEvidence(config_dir, session_id)
    real_started = time.monotonic()
    active = 0.0
    tokens = 0
    reason: str | None = None
    returncode: int | None = None
    usage_invalid = False
    process: subprocess.Popen[bytes] | None = None
    init: dict[str, Any] | None = None
    served_models: set[str] = set()
    invocations: list[dict[str, Any]] = []
    pauses: list[dict[str, Any]] = []
    rate_limit: dict[str, Any] = {}
    rate_limit_group: str | None = None
    quota_decision: dict[str, Any] = {}
    from leaneval import quota
    quota_ledger = quota.current()
    last_result: dict[str, Any] | None = None
    nudges = 0
    turn_tools = 0
    credential_alarm: dict[str, Any] | None = None
    idle_replies = 0
    handoff_sent = False
    transient = 0
    cost_estimate = float((previous.content if previous else {}).get("cost_counter_usd") or 0)
    cost_counter_invalid = False
    ready = False
    tracked: set[int] = set()
    phase = "starting"

    def effective() -> dict[str, Any] | None:
        if init is None:
            return None
        efforts = sorted(transcript.efforts)
        models = sorted(served_models | transcript.models)
        return {"model": models[0] if len(models) == 1 else (models or None),
                "reasoning_effort": efforts[0] if len(efforts) == 1 else (efforts or None),
                "model_effort_pairs": sorted(map(list, transcript.pairs)),
                "fallback_used": FALLBACK_MODEL in models,
                "init_model": init.get("model"), "cwd": init.get("cwd"),
                "permission_mode": init.get("permissionMode"),
                "cli_version": init.get("claude_code_version"),
                "tools": init.get("tools"), "api_key_source": init.get("apiKeySource"),
                "session_id": init.get("session_id"),
                "transcript_assistant_entries": transcript.entries,
                "sandbox_policy": {"type": "landrun", "network": "unrestricted",
                                   "writable": [str(workspace), "/tmp", str(config_dir)]},
                "approval_policy": "bypassPermissions inside landrun"}

    def confirmed() -> bool:
        value = effective()
        pairs = transcript.pairs
        return bool(value and pairs and all(ALLOWED.get(m) == e for m, e in pairs)
                    and (MODEL, EFFORT) in pairs
                    and value["init_model"] in ALLOWED and value["cwd"] == str(workspace)
                    and value["permission_mode"] == PERMISSION_MODE
                    and value["cli_version"] == CLI_VERSION
                    and value["session_id"] == session_id)

    def mismatch() -> bool:
        value = effective()
        if value is None:
            return False
        bad_model = any(m not in ALLOWED for m in served_models | transcript.models)
        bad_effort = any(ALLOWED.get(m) != e for m, e in transcript.pairs)
        return (bad_model or bad_effort or value["init_model"] not in ALLOWED
                or value["cwd"] != str(workspace)
                or value["permission_mode"] != PERMISSION_MODE
                or value["session_id"] != session_id)

    def checkpoint(safe: bool = False) -> None:
        account_state = account.snapshot() if safe else None
        content = {"attempt_id": attempt_id, "thread_id": session_id,
                   "pid": process.pid if process else None, "phase": phase,
                   "elapsed_seconds": active + (time.monotonic() - invocation_started
                                                if process and process.poll() is None else 0),
                   "real_elapsed_seconds": time.monotonic() - real_started,
                   "tokens": tokens, "usage": account.usage_fields(),
                   "session_usage": account.session_usage(), "account_state": account_state,
                   "claude_usage": account.raw, "completed_turns": len(account.messages),
                   "cost_counter_usd": cost_estimate,
                   "cost_counter_invalid": cost_counter_invalid,
                   "cost_semantics": "claude-session-cumulative-estimate-v2",
                   "requested_config": requested, "effective_config": effective(),
                   "transcript_path": str(transcript.path) if transcript.path else None,
                   "rate_limit": rate_limit, "quota_decision": quota_decision, "pauses": pauses, "nudges": nudges,
                   "invocations": invocations, "usage_semantics": USAGE_SEMANTICS,
                   "safe_resume": safe and account_state is not None}
        if not ledger.cancelled():
            ledger.checkpoint(content)
        _write_json(job / "checkpoint.json", content)

    def observe(event: dict[str, Any]) -> None:
        nonlocal init, last_result, cost_estimate, cost_counter_invalid, ready, rate_limit_group
        nonlocal turn_tools, credential_alarm
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init = event
        elif kind == "assistant":
            message = event.get("message") or {}
            if event.get("parent_tool_use_id"):
                raise ValueError("unexpected subagent output")
            content = message.get("content")
            if isinstance(content, list):
                turn_tools += sum(1 for block in content
                                  if isinstance(block, dict) and block.get("type") == "tool_use")
                for block in content:
                    tool = credential_access(block, config_dir)
                    if tool and credential_alarm is None:
                        credential_alarm = {"tool": tool, "at": time.time()}
            if isinstance(message.get("model"), str) and message["model"] != "<synthetic>":
                served_models.add(message["model"])
            if message.get("id") and isinstance(message.get("usage"), dict):
                account.message(message["id"], message["usage"])
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            rate_limit.clear()
            rate_limit.update(info, observed_at=time.time())
            publish_rate_limit(info)
            rate_limit_group = quota.group_for(account_name)
            quota_ledger.observe(rate_limit_group, info, origin=socket.gethostname()+'/'+account_name, margin=RESET_MARGIN)
        elif kind == "result":
            if event.get("session_id", session_id) != session_id:
                raise ValueError("result session identity mismatch")
            last_result = event
            account.result(event.get("usage"), event.get("modelUsage"))
            if isinstance(event.get("total_cost_usd"), (int, float)):
                current_cost = float(event["total_cost_usd"])
                if (not math.isfinite(current_cost) or current_cost < cost_estimate
                        or event.get("session_id", session_id) != session_id):
                    cost_counter_invalid = True
                else:
                    # Pinned Claude reports the whole resumed conversation, not
                    # a new invoice per result. Replayed results add zero.
                    cost_estimate = current_cost
            text = event.get("result") if isinstance(event.get("result"), str) else ""
            ready = _is_ready(text)

    def wait(seconds: float, why: str) -> bool:
        """Pause without accounting active time; return False on external STOP."""
        nonlocal phase
        phase = why
        deadline = time.time() + max(0.0, seconds)
        pauses.append({"reason": why, "started_at": time.time(), "until": deadline})
        checkpoint()
        while time.time() < deadline:
            if ledger.cancelled():
                pauses[-1]["ended_at"] = time.time()
                return False
            time.sleep(min(5.0, max(0.05, deadline - time.time())))
        pauses[-1]["ended_at"] = time.time()
        return True

    def limit_reset() -> float | None:
        values = []
        windows = rate_limit.get("unifiedWindows") or {}
        if rate_limit.get("status") == "rejected" and rate_limit.get("resetsAt"):
            values.append(float(rate_limit["resetsAt"]))
        for window in windows.values():
            if (window or {}).get("utilization", 0) >= 1 and window.get("resetsAt"):
                values.append(float(window["resetsAt"]))
        return max(values) if values else None

    invocation_started = time.monotonic()
    next_prompt = prompt
    if session is None:
        # lrh: state the real budget (briefs said "4 hours" while lineage gave 9-31 min).
        next_prompt += (f"\nRound budget: {int(available_wall // 60)} minutes of active model "
                        "time, builds included. Use all of it; if your turn ends early the "
                        "controller says how much time is left, and near the end it asks for "
                        "the handoff.\n")
    resume_next = session is not None
    local_blocks: dict[str, float] = {}

    def choose(now: float) -> tuple[str, str, str, str] | None:
        nonlocal quota_decision
        quota_ledger.import_legacy(account_limits_path())
        shared = read_account_limits(now)
        legacy = {name:max([0.0] + [until for key,until in [*shared.items(),*local_blocks.items()]
                    if key.split('|',1)[0] == name]) for name,_ in accounts}
        quota_decision = quota.selection([n for n,_ in accounts], now=now,
            reserve=reserve_threshold(), legacy=legacy, ledger=quota_ledger)
        name = quota_decision['choice']
        return next(((n,secret,MODEL,EFFORT) for n,secret in accounts if n == name),None)

    def soonest_unblock() -> float:
        choose(time.time())
        return quota_decision['wake_at']

    try:
        with (job / "stderr.log").open("ab") as errors, (job / "events.jsonl").open("ab") as events:
            while True:
                if credential_alarm is not None:
                    reason = "credential_access"
                    break
                if ledger.cancelled():
                    reason = "external_stop"
                    break
                remaining_wall = available_wall - active
                if remaining_wall <= 0:
                    reason = "wall_limit"
                    break
                if tokens >= available_tokens:
                    reason = "token_limit"
                    break
                pick = choose(time.time())
                if pick is None:
                    # Every eligible quota group is blocked; preserve the existing reset wait.
                    if not wait(max(MIN_LIMIT_PAUSE, soonest_unblock() - time.time()),
                                "usage_limit_pause"):
                        reason = "external_stop"
                        break
                    continue
                account_name, account_token, model, effort = pick
                install_credentials(config_dir, account_token)
                account.begin_invocation()
                last_result = None
                turn_tools = 0
                rate_limit_before = dict(rate_limit) if rate_limit_group == quota.group_for(account_name) else {}
                rate_limit.clear()
                phase = "model_running"
                invocation_started = time.monotonic()
                # lrh: `./lean` prints the round time left from this (rate-limit pauses are
                # not charged to the budget, so it is rewritten at every invocation start).
                with suppress(OSError):
                    _write_json(job / "round-clock.json", {
                        "deadline": time.time() + max(0.0, available_wall - active),
                        "handoff_seconds": min(HANDOFF_SECONDS, 0.25 * available_wall)})
                command = build_command(job, session_id, resume=resume_next, agent=agent,
                                        config_dir=config_dir, env_keys=list(env),
                                        model=model, effort=effort, sandbox_home=sandbox_home)
                process = subprocess.Popen(command, cwd=workspace, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=errors, start_new_session=True)
                assert process.stdin is not None and process.stdout is not None
                with suppress(BrokenPipeError):
                    process.stdin.write(next_prompt.encode())
                    process.stdin.close()
                record = {"index": len(invocations) + 1, "pid": process.pid,
                          "resume": resume_next, "started_at": time.time(),
                          "requested_model": model, "requested_effort": effort,
                          "account": account_name}
                invocations.append(record)
                checkpoint()
                os.set_blocking(process.stdout.fileno(), False)
                pending = b""
                stopped_here = False
                reserved_at: float | None = None
                reserve_checked: float | None = None
                last_poll = 0.0
                last_checkpoint = time.monotonic()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    eof = False
                    while not eof:
                        now = time.monotonic()
                        if now - last_poll >= 1.0:
                            last_poll = now
                            with suppress(OSError):
                                tracked |= _descendants(process.pid)
                            try:
                                transcript.poll()
                                for message_id, message_usage in transcript.usage_updates:
                                    account.message(message_id, message_usage)
                            except (ValueError, KeyError, TypeError, OSError):
                                usage_invalid = True
                                reason = reason or "invalid_transcript_evidence"
                            if mismatch():
                                reason = "configuration_mismatch"
                        elapsed = active + (now - invocation_started)
                        live = account.tokens
                        if live > tokens:
                            tokens = live
                            ledger.observe_usage(attempt_id, Usage(tokens, elapsed))
                            checkpoint()
                            last_checkpoint = now
                        elif now - last_checkpoint >= 60:
                            # Providers that report usage only at turn end still get fresh
                            # elapsed/effective-config evidence for monitoring.
                            checkpoint()
                            last_checkpoint = now
                        if credential_alarm is not None:
                            reason = "credential_access"
                            remove_credentials(config_dir)
                        elif ledger.cancelled():
                            reason = "external_stop"
                        elif elapsed >= available_wall:
                            reason = "wall_limit"
                        elif tokens >= available_tokens:
                            reason = "token_limit"
                        if reason and not stopped_here:
                            _stop(process, tracked, workspace)
                            stopped_here = True
                        seen = rate_limit.get("observed_at")
                        if not stopped_here and seen and seen != reserve_checked:
                            reserve_checked = seen  # the live switch is read once per event
                            reserved_at = reserve_until(rate_limit, time.time(),
                                                        reserve_threshold())
                            if reserved_at is not None:
                                _stop(process, tracked, workspace)
                                stopped_here = True
                        ready_keys = selector.select(timeout=0.2)
                        for key, _ in ready_keys:
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                eof = True
                                break
                            events.write(chunk)
                            events.flush()
                            os.fsync(events.fileno())
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                if not line.strip():
                                    continue
                                try:
                                    observe(json.loads(line))
                                except (ValueError, KeyError, TypeError):
                                    usage_invalid = True
                        if process.poll() is not None and not ready_keys:
                            continue
                    if pending.strip():
                        try:
                            observe(json.loads(pending))
                        except (ValueError, KeyError, TypeError):
                            # A line cut by our own reserve stop is not invalid evidence.
                            usage_invalid = usage_invalid or reserved_at is None
                returncode = process.wait(timeout=15)
                _reap(process.pid, tracked, workspace)
                tracked.clear()
                active += time.monotonic() - invocation_started
                account.close_invocation(has_result=last_result is not None)
                try:
                    transcript.poll()
                    for message_id, message_usage in transcript.usage_updates:
                        account.message(message_id, message_usage)
                except (ValueError, KeyError, TypeError, OSError):
                    usage_invalid = True
                    reason = reason or "invalid_transcript_evidence"
                tokens = max(tokens, account.tokens)
                ledger.observe_usage(attempt_id, Usage(tokens, active))
                text = (last_result or {}).get("result")
                text = text if isinstance(text, str) else ""
                record.update(exit_code=returncode, ended_at=time.time(),
                              result_subtype=(last_result or {}).get("subtype"),
                              is_error=(last_result or {}).get("is_error"),
                              api_error_status=(last_result or {}).get("api_error_status"),
                              result_excerpt=text[:500], rate_limit=dict(rate_limit))
                if credential_alarm is not None:  # also when the alarm came with the stream's last line
                    reason = "credential_access"
                if reason:
                    break
                if reserved_at is not None and last_result is None:
                    record["outcome"] = "coordinator_reserve"
                    next_prompt, resume_next = RESUME_AFTER_LIMIT, True
                    if not wait(max(MIN_LIMIT_PAUSE, reserved_at + RESET_MARGIN - time.time()),
                                "coordinator_reserve_pause"):
                        reason = "external_stop"
                        break
                    continue
                if not rate_limit and rate_limit_before:
                    rate_limit.update(rate_limit_before)
                errored = bool((last_result or {}).get("is_error")) or returncode != 0
                limited = (rate_limit.get("status") == "rejected" or
                           (errored and (_LIMIT_TEXT.search(text) or
                                         (last_result or {}).get("api_error_status") == 429)))
                if limited:
                    reset = limit_reset()
                    until = (reset + RESET_MARGIN) if reset else time.time() + UNKNOWN_RESET_WAIT
                    record["outcome"] = "usage_limit"
                    next_prompt, resume_next = RESUME_AFTER_LIMIT, True
                    # Continue only with an eligible account outside the blocked quota group.
                    local_blocks[f"{account_name}|{model}"] = until
                    block_account(account_name, model, until)
                    group = quota.group_for(account_name)
                    if quota_ledger.blocked_until(group,time.time()) <= time.time():
                        quota_ledger.cooldown(group,until,origin=socket.gethostname()+'/'+account_name,
                            basis='api_429_cooldown' if (last_result or {}).get('api_error_status') == 429
                            else 'cli_limit_error_cooldown')
                    record['quota_group'] = group
                    following = choose(time.time())
                    if following is not None:
                        record["switch_to"] = following[2]
                        record["switch_account_to"] = following[0]
                    continue
                if errored:
                    stderr_tail = ""
                    with suppress(OSError):
                        stderr_tail = (job / "stderr.log").read_bytes()[-4000:].decode(
                            errors="replace")
                    if _AUTH_TEXT.search(text) or _AUTH_TEXT.search(stderr_tail):
                        record["outcome"] = "authentication_failure"
                        reason = "authentication_failure"
                        break
                    if init is None or transient >= MAX_TRANSIENT_RETRIES:
                        record["outcome"] = "api_error"
                        reason = "api_error"
                        break
                    record["outcome"] = "transient_error"
                    if not wait(TRANSIENT_BACKOFF[transient], "transient_error_backoff"):
                        reason = "external_stop"
                        break
                    transient += 1
                    next_prompt, resume_next = RESUME_AFTER_LIMIT, True
                    continue
                record["outcome"] = "turn_completed"
                record["tool_uses"] = turn_tools
                if ready:
                    break
                idle_replies, end_now = end_after_turn(turn_tools, idle_replies, handoff_sent)
                if end_now:
                    record["round_end"] = "handoff_answered" if handoff_sent else "idle_replies"
                    break
                left_wall = available_wall - active
                left_tokens = available_tokens - tokens
                text = nudge_text(left_wall, left_tokens, available_wall, available_tokens)
                if nudges < max_nudges and text is not None:
                    nudges += 1
                    handoff_sent = handoff_due(left_wall, available_wall)
                    next_prompt = text
                    resume_next = True
                    continue
                break
    except BaseException as exc:
        reason = f"supervisor_error:{type(exc).__name__}"
        if process is not None and process.poll() is None:
            _stop(process, tracked, workspace)
            returncode = process.returncode
        remove_credentials(config_dir)
        ledger.mark_ambiguous(attempt_id, reason)
        phase = "supervisor_error"
        checkpoint()
        raise
    remove_credentials(config_dir)
    phase = "terminated"
    elapsed = active
    termination = {"attempt_id": attempt_id, "pid": process.pid if process else None,
                   "process_group": process.pid if process else None,
                   "exit_code": returncode, "wait_completed": True,
                   "stop_reason": reason, "generation": reservation.generation,
                   "invocations": len(invocations), "pauses": len(pauses)}
    _write_json(job / "termination.json", termination)
    ledger.append_event("process_terminated", termination)
    completed = len(account.messages)
    ambiguous = reason is not None or returncode != 0 or completed == 0 or usage_invalid
    configuration_confirmed = confirmed() and reason != "configuration_mismatch"
    accepted = ledger.finish(attempt_id, event_id=f"{attempt_id}-result",
                             usage=Usage(tokens, elapsed),
                             result={"exit_code": returncode, "reason": reason},
                             generation=reservation.generation,
                             checkpoint_hash=reservation.checkpoint_hash, ambiguous=ambiguous)
    status = ("awaiting_verifier" if accepted and configuration_confirmed else
              "configuration_unconfirmed" if accepted else "interrupted_unknown_usage")
    checkpoint(safe=accepted and configuration_confirmed)
    summary = {"run_id": config["run_id"], "status": status, "attempt_id": attempt_id,
               "thread_id": session_id, "exit_code": returncode, "stop_reason": reason,
               "observed_tokens": tokens, "usage": account.usage_fields(),
               "claude_usage": account.raw, "session_usage": account.session_usage(),
               "elapsed_seconds": elapsed,
               "real_elapsed_seconds": time.monotonic() - real_started, "usd": None,
               "api_list_price_usd_estimate": (None if cost_counter_invalid else round(cost_estimate, 6)),
               "cost_counter_invalid": cost_counter_invalid,
               "cost_semantics": "claude-session-cumulative-estimate-v2",
               "requested_config": requested, "effective_config": effective(),
               "configuration_confirmed": configuration_confirmed,
               "transcript_path": str(transcript.path) if transcript.path else None,
               "invocations": invocations, "pauses": pauses, "nudges": nudges,
               "ready_marker": ready, "rate_limit": rate_limit,
               "cli_state_directory": str(state),
               "usage_semantics": USAGE_SEMANTICS, "usage_invalid": usage_invalid,
               "security_event": credential_alarm and {**credential_alarm, "kind": "credential_access",
                                                       "account": (invocations[-1] if invocations else {}).get("account")},
               "verified": False}
    if credential_alarm is not None:
        _write_json(job / "SECURITY-EVENT.json", summary["security_event"])
    _write_json(job / "result.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_job(args.job, resume=args.resume)
    except (RuntimeBlocked, ValueError, OSError) as exc:
        parser.exit(2, f"supervisor blocked: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "awaiting_verifier" else 1


if __name__ == "__main__":
    raise SystemExit(main())
