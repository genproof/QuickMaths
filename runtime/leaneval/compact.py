"""lrh: model-facing rendering of `./lean` results, the full-check gate and name hints.

The full feedback record (step-*.json, 100-250 KB) stays on disk for the controller. The
model sees errors with their goals, sorry locations and counts; `./lean ... --json` prints
the full record. Upstream printed the whole record, and the model wrote its own filters.
"""
from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path

MAX_ERRORS = 12
MAX_ERROR_LINES = 30
MAX_TAIL_LINES = 60
NAME_INDEX = Path(os.environ.get('LRH_NAME_INDEX', str(Path.home() / '.cache/lrh/decl-names.txt')))
# Lean 4.35: "Unknown constant `Nat.foo`"; older: "unknown identifier 'foo'".
UNKNOWN = re.compile(r"unknown (?:identifier|constant) [`']([^`']+)[`']|does not contain [`']([^`']+)[`']",
                     re.IGNORECASE)
SORRY = re.compile(r"declaration uses 'sorry'")
CODE_SORRY = re.compile(r"\bsorry\b")
COMMENTS = re.compile(r"/-.*?-/|--[^\n]*", re.DOTALL)
# A full check of a finished proof once needed 1278 s on an idle host and hit the 1800 s cap
# four times under load, so a finished proof waited two rounds for verification. Only
# `./lean check --full` runs it now (cap raisable per job, see compile_policy.full_check_seconds).
FULL_CHECK_SECONDS = 5400
NOISE = re.compile(r'^(?:trace:|[✔ℹ]|\s*$)')
# lrh P1 (2026-10-14): light `./lean check`, separate official-check lock, compiler threads,
# identical-failure cache, stale-compile abort, round-wall refusal, READY footer. Recorded in
# every feedback step as `lrh_version`.
LRH_VERSION = 'lrh-p1-20261014'


def check_mode(action: str, query: str) -> str | None:
    """'light' for `./lean check`, 'full' for `./lean check --full` (and the legacy query-less
    `./lean build`, which always ran the official lake test), None for other calls."""
    if action == 'check':
        return 'full' if (query or '').strip() in {'full', '--full'} else 'light'
    if action == 'build' and not query:
        return 'full'
    return None


def full_check(action: str, query: str) -> bool:
    """The official lake test: `./lean check --full` or a query-less `./lean build`."""
    return check_mode(action, query) == 'full'


def code_sorry(files: dict[str, bytes]) -> list[str]:
    """Proof files whose code (comments stripped) still contains `sorry`."""
    return sorted(name for name, data in files.items() if name.endswith('.lean')
                  and CODE_SORRY.search(COMMENTS.sub('', data.decode(errors='replace'))))


# Records of refused or stopped calls (gates, round wall, superseded): no compiler verdict.
NO_VERDICT = {'invalid_request', 'superseded', 'budget_blocked'}


def _whole_build(step: dict) -> bool:
    """`./lean build Submission` or a light check (it builds Submission first), with a verdict."""
    return (step.get('outcome_class') not in NO_VERDICT and (
        (step.get('action') == 'build' and step.get('query') == 'Submission')
        or (step.get('action') == 'check' and step.get('check_mode') == 'light')))


def full_check_gate(history: list[dict], source_sha256: str,
                    files: dict[str, bytes] | None = None, *, light: bool = False) -> str | None:
    """Refusal text unless the files are sorry-free and (full check only) `./lean build
    Submission` or a light check already succeeded on these exact files."""
    open_files = code_sorry(files or {})
    name = 'Check' if light else 'Full check'
    if open_files:
        shown = ', '.join(open_files[:5]) + (' ...' if len(open_files) > 5 else '')
        return (f'{name} refused: `sorry` remains in {len(open_files)} file(s) ({shown}). '
                'The check fails on any sorry; build the modules you work on '
                '(`./lean build Module.Name`) and run the check once all holes are closed.')
    if light:
        return None
    for step in reversed(history):
        if _whole_build(step):
            if (step.get('source_sha256') == source_sha256
                    and step.get('outcome_class') == 'check_succeeded'):
                return None
            break
    return ('Full check refused: first make `./lean build Submission` (or `./lean check`) succeed '
            'on the current files. The full check (official lake test, minutes to over an hour) '
            'is optional: READY_FOR_VERIFICATION after a passing `./lean check` is enough, the '
            'controller runs the official verification outside this session.')


def ready_state(history: list[dict], inventory: dict[str, str], files: dict[str, bytes]) -> str | None:
    """'checked' (a light/full check passed on exactly these files), 'built' (sorry-free and the
    latest `./lean build Submission` passed on exactly these files) or None. `history` is
    oldest-first; a full check that failed with Lean errors on these files means not ready, a
    timed-out or killed one does not (it is not a rejection)."""
    if not files or not inventory or code_sorry(files):
        return None
    for step in reversed(history):
        if not isinstance(step, dict):
            continue
        same = step.get('source_files') == inventory and step.get('source_current') is not False
        mode = step.get('check_mode') if step.get('action') == 'check' else None
        if mode == 'full' or (step.get('action') == 'build' and not step.get('query')):
            if same and step.get('outcome_class') == 'check_succeeded':
                return 'checked'
            if same and step.get('outcome_class') == 'lean_diagnostic':
                return None
            continue
        if not _whole_build(step):
            continue
        if not same or step.get('outcome_class') != 'check_succeeded':
            return None
        return 'checked' if step.get('action') == 'check' else 'built'
    return None


def ready_note(state: str | None) -> str | None:
    """Broker footer once only the official verification is left."""
    if state == 'checked':
        return ('READY: `./lean check` passed on exactly these files (no sorry, Submission and the '
                'official statement build, standard hole axioms). Reply READY_FOR_VERIFICATION now; '
                'the official verification (comparator, kernels, Nanoda) runs outside this session '
                'with the full CI budget.')
    if state == 'built':
        return ('READY: no `sorry` remains and `./lean build Submission` succeeded on exactly these '
                'files. Run `./lean check` (seconds: statement wrapper and hole axioms) and reply '
                'READY_FOR_VERIFICATION; the official verification runs outside this session with '
                'the full CI budget, so `./lean check --full` is not needed.')
    return None


def not_a_rejection(seconds: float | None, killed: str | None = None) -> str:
    """Message for a full check that did not finish on a gated (sorry-free, built) tree."""
    what = (f'was stopped by the host ({killed})' if killed else
            f'did not finish within {int(seconds or 0)} s')
    return (f'The full check {what}. This is NOT a rejection: the files are sorry-free and '
            '`./lean build Submission` succeeded on them. Do not optimise build time for it; reply '
            'READY_FOR_VERIFICATION (after a passing `./lean check`). The controller runs the '
            'official verification outside this session with the full CI budget (6 h on 16 CPUs).')


def light_verdict(result: dict) -> dict:
    """Updates that turn the remote light-check record (Submission build, Solution build, axiom
    probe) into one outcome. Never acceptance: the axiom audit is diagnostic only."""
    from leaneval import axiom_audit
    if result.get('exitcode') != 0 or result.get('timed_out') or result.get('superseded'):
        return {}  # the Submission build itself failed: its diagnostics speak for themselves
    names = result.get('hole_names')
    if not isinstance(names, list) or not names:
        return {'exitcode': 2, 'phase': 'request_validation',
                'stderr': 'Check could not read the hole names (config.json theorem_names); '
                          'use `./lean check --full`.'}
    solution = result.get('solution_build') or {}
    if solution.get('exitcode') != 0 or solution.get('timed_out') or solution.get('superseded'):
        return {'exitcode': solution.get('exitcode') if solution.get('exitcode') not in (0, None) else 1,
                'timed_out': bool(solution.get('timed_out')), 'superseded': bool(solution.get('superseded')),
                'phase': 'light_check_statement', 'stdout': solution.get('stdout', ''),
                'stderr': solution.get('stderr', ''),
                'required_next_action': (f"`./lean build Submission` passes, but the official statement "
                                         f"wrapper {result.get('statement_module', 'Solution')}.lean does not "
                                         'compile against it: a hole is missing, renamed or its statement '
                                         'differs from Challenge. Fix the hole declarations in Submission.')}
    probe = result.get('axiom_probe') or {}
    audit = axiom_audit.fold(probe.get('stdout', ''), names)
    audit['diagnostic_foundational'] &= probe.get('exitcode') == 0 and not probe.get('timed_out')
    updates = {'axiom_audit': audit, 'phase': 'light_check', 'stdout': probe.get('stdout', ''),
               'stderr': probe.get('stderr', '')}
    if audit['diagnostic_foundational']:
        updates['light_check_summary'] = (f'Check passed: no sorry, Submission and the official '
                                          f'statement build, standard axioms for {len(names)} hole(s).')
        return updates
    bad = {n: sorted(set(v) - axiom_audit.ALLOWED) for n, v in audit['closures'].items()
           if set(v) - axiom_audit.ALLOWED}
    missing = [n for n in names if n not in audit['closures']]
    detail = '; '.join([f"{n} uses {', '.join(v)}" for n, v in bad.items()]
                       + [f'{n}: no axiom report' for n in missing])
    # Keep compiler errors (e.g. an unknown hole name) visible; the axiom reports are summarised.
    kept = []
    for line in (probe.get('stdout') or '').splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        if not (isinstance(item, dict) and item.get('severity') == 'information'):
            kept.append(line)
    updates.update(stdout='\n'.join(kept),
                   exitcode=1, timed_out=bool(probe.get('timed_out')), phase='light_check_axioms',
                   required_next_action=('Axiom check failed: ' + (detail or 'probe failed') +
                                         '. Only propext, Classical.choice and Quot.sound are allowed '
                                         '(sorryAx means a sorry is still reachable).'))
    return updates


def render(result: dict, *, hints: bool = True) -> str:
    action, query = result.get('action', '?'), result.get('query', '')
    outcome = result.get('outcome_class', '?')
    status = 'OK' if outcome == 'check_succeeded' else 'FAILED'
    head = f"[step {str(result.get('step_id', ''))[:8]}] {action} {query}".rstrip()
    timing = result.get('end_to_end_seconds') or result.get('elapsed_seconds')
    parts = [f'{head}: {status} ({outcome})' + (f', {timing:.0f} s' if isinstance(timing, (int, float)) else '')]
    cache = result.get('build_cache') or {}
    if cache.get('eligible'):
        parts[0] += ', cache ' + ('warm' if cache.get('reused') else 'cold')
    if result.get('cache_note'):
        parts.append(str(result['cache_note']))
    if result.get('source_current') is False and outcome != 'superseded':
        parts.append('NOTE: files changed while this check ran; the result describes the older files.')
    if result.get('light_check_summary') and status == 'OK':
        parts.append(str(result['light_check_summary']))
    if action == 'search':
        out = (result.get('stdout') or '').splitlines()
        parts += out[:80] + ([f'... {len(out) - 80} more lines'] if len(out) > 80 else [])
        if result.get('stderr'):
            parts.append(result['stderr'][-2000:])
        return '\n'.join(parts) + '\n'
    diagnostics = result.get('diagnostics') or []
    errors = [d for d in diagnostics if d.get('severity') == 'error']
    sorries = [d for d in diagnostics if d.get('severity') == 'warning' and SORRY.search(d.get('message', ''))]
    warnings = [d for d in diagnostics if d.get('severity') == 'warning' and d not in sorries]
    for entry in errors[:MAX_ERRORS]:
        lines = entry.get('message', '').splitlines() or ['']
        parts.append(f"error {entry.get('file')}:{entry.get('line')}:{entry.get('column')}: {lines[0]}")
        body = lines[1:]
        if len(body) > MAX_ERROR_LINES:
            body = body[:MAX_ERROR_LINES - 5] + [f'  ... ({len(body) - MAX_ERROR_LINES + 5} lines omitted)'] + body[-4:]
        parts += ['  ' + line for line in body]
        if hints and entry.get('hint'):
            parts.append('  hint: ' + entry['hint'])
    if len(errors) > MAX_ERRORS:
        parts.append(f'... {len(errors) - MAX_ERRORS} more errors')
    if sorries:
        parts.append('sorry: ' + '; '.join(f"{d.get('file')}:{d.get('line')}" for d in sorries[:20])
                     + (f' (+{len(sorries) - 20})' if len(sorries) > 20 else ''))
    if warnings:
        parts.append(f'warnings: {len(warnings)} hidden')
    if status == 'FAILED' and not errors:
        # Infrastructure failures, timeouts and the official test harness: show the tail.
        raw = (result.get('stdout') or '') + '\n' + (result.get('stderr') or '')
        tail = [line for line in raw.splitlines() if not NOISE.match(line)
                and line.strip() != str(result.get('required_next_action', '')).strip()]
        parts += tail[-MAX_TAIL_LINES:]
    if result.get('required_next_action'):
        parts.append(str(result['required_next_action']))
    parts.append('full record: ./lean ' + ' '.join(x for x in (action, query) if x) + ' --json'
                 + (f"  (or .feedback/step-{int(result['revision']):06d}.json)"
                    if isinstance(result.get('revision'), int) else ''))
    return '\n'.join(parts) + '\n'


_NAMES: dict | None = None


def _load_names() -> dict | None:
    global _NAMES
    if _NAMES is None:
        names = {}
        try:
            with NAME_INDEX.open(encoding='utf-8') as stream:
                for line in stream:
                    full = line.strip()
                    if full:
                        names.setdefault(full.rsplit('.', 1)[-1], []).append(full)
        except OSError:
            names = {}
        _NAMES = names
    return _NAMES or None


def name_hint(message: str, limit: int = 6) -> str | None:
    match = UNKNOWN.search(message)
    if not match:
        return None
    wanted = match.group(1) or match.group(2)
    names = _load_names()
    if not names:
        return None
    last = wanted.rsplit('.', 1)[-1]
    found = list(names.get(last, []))
    if len(found) < limit:
        pool = [k for k in names if k[:1] == last[:1] and abs(len(k) - len(last)) <= 4]
        close = difflib.get_close_matches(last, pool, n=limit, cutoff=0.8)
        for key in close:
            found += [n for n in names[key] if n not in found]
    namespace = wanted.rsplit('.', 1)[0] + '.' if '.' in wanted else ''
    found = sorted((n for n in found if n != wanted),
                   key=lambda n: (not (namespace and n.startswith(namespace)), len(n)))[:limit]
    return ('existing names like ' + repr(last) + ': ' + ', '.join(found)) if found else None


DECL = re.compile(r'^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|nonrec|partial|unsafe)\s+)*'
                  r'(?:theorem|lemma|def|abbrev|instance|structure|class|inductive)\s+'
                  r'([^\s(:{\[⦃]+)')
NAMESPACE = re.compile(r'^\s*namespace\s+(\S+)')
END = re.compile(r'^\s*end\s+(\S+)')


def build_name_index(roots: list[Path], output: Path = NAME_INDEX) -> int:
    """Best-effort `Namespace.name` list from sources (namespace/end tracking only)."""
    names = set()
    for root in roots:
        for path in root.rglob('*.lean'):
            if '.lake' in path.relative_to(root).parts:  # nested build dirs, not the root
                continue
            stack: list[str] = []
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            for line in text.splitlines():
                if m := NAMESPACE.match(line):
                    stack.append(m.group(1))
                elif (m := END.match(line)) and stack and stack[-1] == m.group(1):
                    stack.pop()
                elif m := DECL.match(line):
                    name = m.group(1)
                    if name.startswith('_root_.'):
                        names.add(name[len('_root_.'):])
                    else:
                        names.add('.'.join(stack + [name]))
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix('.tmp')
    tmp.write_text('\n'.join(sorted(names)) + '\n', encoding='utf-8')
    os.replace(tmp, output)
    return len(names)


if __name__ == '__main__':
    import sys
    count = build_name_index([Path(p) for p in sys.argv[1:]])
    print(json.dumps({'names': count, 'index': str(NAME_INDEX)}))


def round_clock(job: Path, now: float) -> str | None:
    """`./lean` footer with the round time left (round-clock.json, written by the runner at
    every invocation start); a handoff reminder near the end."""
    try:
        clock = json.loads((job / 'round-clock.json').read_text())
        left = float(clock['deadline']) - now
        handoff = float(clock.get('handoff_seconds', 900))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    minutes = max(0, int(left // 60))
    if left <= handoff:
        return (f'Round time left: {minutes} min. Update NOTES.md now: the HANDOFF fields (Next step, '
                'Active tactic, Last failure, Do not retry), RESIDUALS and TACTICS.')
    return f'Round time left: {minutes} min.'


# In-round stall signal (IDS counts open placeholders; AdaEvolve switches tactics on stagnation):
# the set of declarations that still contain `sorry`, and since when none of them was closed.
DECL = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|nonrec|partial)\s+)*"
                  r"(?:theorem|lemma|def|instance|abbrev|structure|class)\s+([^\s:(\[{]+)")
STALL_MINUTES = 60
STALL_REPEAT_MINUTES = 20


def frontier(files: dict[str, bytes]) -> list[str]:
    """`file:declaration` for every declaration whose code contains `sorry` (same rule as the
    queue's progress gate)."""
    found = set()
    for name, data in files.items():
        if not name.endswith('.lean'):
            continue
        decl = None
        for line in COMMENTS.sub('', data.decode(errors='replace')).splitlines():
            match = DECL.match(line)
            if match:
                decl = match.group(1)
            if CODE_SORRY.search(line):
                found.add(f'{name}:{decl}')
    return sorted(found)


def stall_note(job: Path, files: dict[str, bytes], now: float) -> str | None:
    """`./lean` footer when no `sorry` declaration has been closed for STALL_MINUTES.

    frontier-clock.json keeps every open declaration seen since the last closure; closing or
    renaming any of them restarts the clock, adding new pieces (a decomposition) does not."""
    path = job / 'frontier-clock.json'
    current = frontier(files)
    try:
        state = json.loads(path.read_text())
        seen = set(state['frontier'])
        since = float(state['since'])
        shown = float(state.get('shown', 0))
    except (OSError, ValueError, KeyError, TypeError):
        seen, since, shown = set(current), now, 0.0
    if seen - set(current):
        seen, since, shown = set(current), now, 0.0
    else:
        seen |= set(current)
    note = None
    stalled = now - since
    if current and stalled >= STALL_MINUTES * 60 and now - shown >= STALL_REPEAT_MINUTES * 60:
        shown = now
        note = (f'No sorry closed for {int(stalled // 60)} min ({len(current)} open). Stuck-goal '
                'protocol: write the goal and the failed approaches under STUCK in NOTES.md, derive '
                '2-3 fresh decompositions from the goal statement alone, test each on a degenerate case '
                'and take one; if the whole route is stuck, mark its tactic failed in TACTICS and switch.')
    tmp = path.with_name(f'.{path.name}.tmp')
    try:
        tmp.write_text(json.dumps({'frontier': sorted(seen), 'since': since, 'shown': shown}))
        os.replace(tmp, path)
    except OSError:
        pass
    return note
