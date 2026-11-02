"""lrh: controller-side scheduling rules for the agent's `./lean` calls (advisory feedback only).

Nothing here accepts a proof: the official verifier's receipt stays the only acceptance. These
rules decide how a feedback call runs (compiler threads, which lock it holds), whether it runs at
all (an identical failure is served from the step history; a call that cannot finish before the
round wall is refused) and when a running compile is stopped (its sources changed under it).
The REMOTE_PROGRAM imports this module too, so it must stay free of host-only dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Iterable

# Memory-aware parallelism: an unbounded `lake build` on a 32-core host started 32 `lean`
# processes of 4-5 GB each and OOM-killed the runner (2026-10-13). LEAN_NUM_THREADS bounds
# Lake's worker pool (the number of concurrent `lean` processes) and each process's threads.
DEFAULT_MAX_THREADS = 8
MAX_THREADS = 256
THREAD_ENV = ('LRH_LEAN_NUM_THREADS', 'LEAN_NUM_THREADS')
LAKE_JOBS_ENV = 'LRH_LAKE_JOBS'
FULL_CHECK_ENV = 'LRH_FULL_CHECK_SECONDS'

# A running compile is stopped once one of its import-closure files changed (121 compiles,
# 40 h, ran on edited files). Short calls are left alone: they finish before an abort pays off.
SUPERSEDE_MIN_SECONDS = 10.0
SUPERSEDE_POLL_SECONDS = 5.0
# A call is refused when even the fastest of the recent comparable runs would not fit into
# the round time left (+ margin): it would only be killed at the round end (17 calls, 200 min).
ROUND_MARGIN_SECONDS = 30.0
EXPECTED_SAMPLES = 3
# Identical failing compiles are served from history (826 reruns, 5 h): same action, query,
# proof files and tool identity, deterministic Lean diagnostics only.
CACHE_HISTORY = 60

NAME = r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
_COMMENTS = re.compile(r"/-.*?-/|--[^\n]*", re.DOTALL)
_IMPORT = re.compile(r"^\s*(?:public\s+|private\s+)?(?:meta\s+)?import\s+(?:all\s+)?(.*)$")
# Compiler killed by SIGKILL (the kernel OOM killer, or an external kill) rather than a Lean error.
_KILLED = re.compile(r"(?:exited with code|exit code)\s*(?:137|-9)\b|\bsignal 9\b|\bSIGKILL\b"
                     r"|^\s*Killed\s*$|\bout of memory\b|\boom[-_ ]kill", re.IGNORECASE | re.MULTILINE)


def _positive(value: Any, upper: int) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= upper else None


def lean_threads(configured: Any, cores: int | None) -> int:
    """LEAN_NUM_THREADS for agent compiles: the configured value, else min(8, cores)."""
    value = _positive(configured, MAX_THREADS)
    return value if value is not None else max(1, min(DEFAULT_MAX_THREADS, cores or 1))


def parallelism(cfg: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, int | None]:
    """Controller-side settings sent to the compiler: worker.json keys `lean_num_threads` /
    `lake_jobs` win, then the controller environment (LRH_LEAN_NUM_THREADS, LEAN_NUM_THREADS,
    LRH_LAKE_JOBS). None = default threads min(8, cores) on the compiler host, no lake jobs flag."""
    environ = os.environ if environ is None else environ
    threads = _positive(cfg.get('lean_num_threads'), MAX_THREADS)
    if threads is None:
        threads = next((v for v in (_positive(environ.get(k), MAX_THREADS) for k in THREAD_ENV)
                        if v is not None), None)
    jobs = _positive(cfg.get('lake_jobs'), MAX_THREADS)
    if jobs is None:
        jobs = _positive(environ.get(LAKE_JOBS_ENV), MAX_THREADS)
    return {'lean_num_threads': threads, 'lake_jobs': jobs}


def lake_jobs_args(jobs: Any, lake_help: str) -> list[str]:
    """`--jobs=N` only when configured and the host's lake documents a --jobs option
    (lake 4.21 has none; an unknown option would fail every build)."""
    jobs = _positive(jobs, MAX_THREADS)
    if jobs is None or not re.search(r'(?<![\w-])--jobs\b', lake_help or ''):
        return []
    return [f'--jobs={jobs}']


def full_check_seconds(cfg: dict[str, Any], default: int, environ: dict[str, str] | None = None) -> int:
    """Cap of `./lean check --full`: worker.json `full_check_seconds`, else LRH_FULL_CHECK_SECONDS,
    never below `default` (the runtime's FULL_CHECK_SECONDS)."""
    environ = os.environ if environ is None else environ
    for value in (cfg.get('full_check_seconds'), environ.get(FULL_CHECK_ENV)):
        seconds = _positive(value, 7 * 24 * 3600)
        if seconds is not None:
            return max(default, seconds)
    return default


def resource_killed(result: dict[str, Any]) -> str | None:
    """Why a compile looks killed by the host (OOM or an external SIGKILL), else None.

    Our own timeout also SIGKILLs the group but sets timed_out; that stays a timeout."""
    if result.get('timed_out') or result.get('superseded'):
        return None
    if result.get('exitcode') in (-9, 137):
        return f"compiler killed by signal 9 (exit {result.get('exitcode')})"
    for stage in result.get('stage_measurements') or []:
        if not isinstance(stage, dict) or stage.get('timed_out'):
            continue
        counters = (((stage.get('isolated_cgroup') or {}).get('observation') or {}).get('after') or {}).get('counters') or {}
        events = counters.get('memory.events') if isinstance(counters, dict) else None
        if isinstance(events, dict) and isinstance(events.get('oom_kill'), int) and events['oom_kill'] > 0:
            return f"out of memory: {events['oom_kill']} process(es) OOM-killed in the compiler cgroup"
    if result.get('exitcode') not in (0, None):
        output = (result.get('stdout') or '') + '\n' + (result.get('stderr') or '')
        match = _KILLED.search(output)
        if match:
            return 'compiler process killed (' + match.group(0).strip()[:60] + ')'
    return None


def module_imports(source: bytes) -> list[str]:
    names = []
    for line in _COMMENTS.sub('', source.decode(errors='replace')).splitlines():
        match = _IMPORT.match(line)
        if match:
            names += [t for t in match.group(1).split() if re.fullmatch(NAME, t)]
    return names


def import_closure(files: dict[str, bytes], module: str) -> set[str] | None:
    """Proof files that `module` imports transitively (itself included); None if unknown."""
    start = module.replace('.', '/') + '.lean'
    if start not in files:
        return None
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for imported in module_imports(files[name]):
            path = imported.replace('.', '/') + '.lean'
            if path in files and path not in seen:
                todo.append(path)
    return seen


def watched_module(action: str, query: str, light: bool) -> str | None:
    """The module whose import closure a compile depends on, if it may be stopped when that
    closure changes. The official full check is never stopped (it reports on its snapshot)."""
    if light:
        return 'Submission'
    if action in {'build', 'inspect', 'inspect_partial'} and query:
        module = query.split(':')[0]
        if module == 'Submission' or module.startswith('Submission.'):
            return module
    return None


class SourceWatch:
    """Polls the local workspace: did a file of the compile's closure change since the snapshot?"""

    def __init__(self, workspace: Path, names: Iterable[str], inventory: dict[str, str]):
        self.workspace = Path(workspace)
        self.names = sorted(n for n in names if n in inventory)
        self.inventory = inventory
        self.seen: dict[str, tuple[int, int, int]] = {}

    def changed(self) -> list[str]:
        changed = []
        for name in self.names:
            path = self.workspace / name
            try:
                info = os.lstat(path)
            except OSError:
                changed.append(name)
                continue
            if not stat.S_ISREG(info.st_mode):
                changed.append(name)
                continue
            key = (info.st_mtime_ns, info.st_size, info.st_ino)
            if self.seen.get(name) == key:
                continue
            try:
                current = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                changed.append(name)
                continue
            if current != self.inventory[name]:
                changed.append(name)
            else:
                self.seen[name] = key
        return changed


def check_query(action: str, query: str) -> str:
    """Normalised query of a step: `check` keeps only the mode ('' light, 'full')."""
    if action == 'check':
        return 'full' if (query or '').strip() in {'full', '--full'} else ''
    return query or ''


def _comparable(step: dict[str, Any], action: str, query: str) -> bool:
    return (step.get('action') == action and check_query(action, step.get('query') or '') == query
            and step.get('compiler_invoked') is True and not step.get('cached_from_step')
            and step.get('outcome_class') not in {'invalid_request', 'superseded', 'budget_blocked',
                                                  'environment_error', None})


def expected_seconds(history: list[dict[str, Any]], action: str, query: str) -> float | None:
    """Fastest of the last few comparable runs of this call in this round (a lower bound)."""
    samples = []
    for step in reversed(history):
        if not isinstance(step, dict) or not _comparable(step, action, query):
            continue
        elapsed = step.get('elapsed_seconds')
        if step.get('timed_out'):
            elapsed = max(float(elapsed or 0), float(step.get('lineage_compiler_allowance_seconds') or 0))
        if isinstance(elapsed, (int, float)) and elapsed > 0:
            samples.append(float(elapsed))
        if len(samples) >= EXPECTED_SAMPLES:
            break
    return min(samples) if samples else None


def round_left(job: Path, now: float | None = None) -> float | None:
    """Seconds to the round wall (round-clock.json, rewritten by the runner at each invocation)."""
    try:
        clock = json.loads((Path(job) / 'round-clock.json').read_text())
        return float(clock['deadline']) - (time.time() if now is None else now)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def round_refusal(job: Path, history: list[dict[str, Any]], action: str, query: str,
                  now: float | None = None) -> str | None:
    left = round_left(job, now)
    expected = expected_seconds(history, action, query)
    if left is None or expected is None or left >= expected + ROUND_MARGIN_SECONDS:
        return None
    shown = ' '.join(x for x in ('./lean', action, '--full' if query == 'full' and action == 'check'
                                 else query) if x)
    return (f'Refused: about {max(0, int(left // 60))} min remain in this round, and `{shown}` took '
            f'at least {int(expected // 60)} min {int(expected % 60)} s in this round; it would be '
            'killed at the round end and its result lost. Use the time for NOTES.md (HANDOFF, '
            'RESIDUALS) or a smaller module build.')


def cached_failure(history: list[dict[str, Any]], action: str, query: str,
                   inventory: dict[str, str]) -> dict[str, Any] | None:
    """The latest step in this round that compiled exactly these files for the same call and
    failed with Lean diagnostics (deterministic), if no later step of that call differs."""
    if action not in {'build', 'inspect', 'inspect_partial', 'check'} or (action == 'build' and not query):
        return None
    if action == 'check' and query == 'full':
        return None
    for step in reversed(history[-CACHE_HISTORY:]):
        if not isinstance(step, dict) or step.get('action') != action or step.get('cached_from_step'):
            continue
        if check_query(action, step.get('query') or '') != query:
            continue
        if step.get('source_files') != inventory:
            continue
        if (step.get('outcome_class') == 'lean_diagnostic' and step.get('compiler_invoked') is True
                and step.get('source_current') is True and not step.get('timed_out')
                and type(step.get('exitcode')) is int and step['exitcode'] != 0
                and step.get('diagnostics') and not step.get('resource_killed')):
            return step
        if step.get('outcome_class') in {'check_succeeded', 'lean_diagnostic', 'timeout'}:
            return None  # the latest comparable run on these files is not a reusable failure
    return None
