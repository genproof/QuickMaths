"""Controller-owned provenance for disposable scoped diagnostic build caches.

Full verification never consumes this cache. Lake still validates source traces.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import time

SEAL_ROOT = Path.home()/'.cache/lean-eval-scoped-build-seals'
WORK_ROOT = Path.home()/'.cache/lean-eval-scoped-workspaces'


def workspace_for(original, action, query):
    original = checked(original)
    # lrh: the light `./lean check` (remote action light_check) reuses the scoped Submission build.
    if action not in {'inspect', 'inspect_partial', 'light_check'} and not (action == 'build' and query):
        return original
    root = checked(WORK_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = scoped(original)
    if not workspace.exists():
        adopt_previous_round(original, workspace) or seed_from_sibling(original, workspace)
    workspace.mkdir(exist_ok=True, mode=0o700)
    return workspace


def scoped(original):
    return checked(checked(WORK_ROOT)/hashlib.sha256(str(original).encode()).hexdigest())


ROUND = re.compile(r'(?P<base>.+?)(?:-r(?P<n>[0-9]+))?')


def previous_rounds(original):
    """Workspaces of the earlier rounds of this job's lineage, latest first:
    <root>/jobs/<base>-r<n>/<rest> -> <base>-r<n-1>, ..., <base>."""
    parts = Path(original).parts
    if 'jobs' not in parts[:-1]:
        return []
    i = len(parts) - 1 - parts[::-1].index('jobs')
    if i + 1 >= len(parts):
        return []
    match = ROUND.fullmatch(parts[i + 1])
    n = int(match['n'] or 1)
    names = [f"{match['base']}-r{k}" if k > 1 else match['base'] for k in range(n - 1, 0, -1)]
    return [Path(*parts[:i + 1], name, *parts[i + 2:]) for name in names]


def adopt_previous_round(original, workspace):
    """lrh: a continuation round starts on its lineage's last sealed scoped build instead of an
    empty cache (2026-10-16: 89 of 96 lane rounds began with a cold Main build, 57 of them hit
    the 30-min cap). The earlier round has finished, so its build directory and seal move here
    by rename: inodes and mtimes are kept, so prepare() verifies the same seal and Lake's traces
    still rebuild whatever the new sources change. Any doubt leaves the new round cold."""
    try:
        for previous in previous_rounds(original):
            old = scoped(previous)
            state = state_path(old)
            build = old/'.lake'/'build'
            if not (state.is_file() and build.is_dir()) or build.is_symlink():
                continue
            if state.stat().st_size > 16384:
                return False
            prior = json.loads(state.read_bytes())
            if sealed_tree(build, prior) != prior['tree']:
                return False  # changed outside a completed build: never carried forward
            staging = checked(workspace.with_name(workspace.name + '.adopt'))
            if staging.exists():
                return False
            staging.mkdir(mode=0o700)
            (staging/'.lake').mkdir(mode=0o700)
            os.rename(build, staging/'.lake'/'build')
            try:
                os.rename(staging, workspace)  # fails if the round already created its workspace
            except OSError:
                os.rename(staging/'.lake'/'build', build)  # give the build back, seal unchanged
                shutil.rmtree(staging, ignore_errors=True)
                return False
            os.replace(state, state_path(workspace))
            lock = lock_path(old)
            if lock.is_file():
                os.replace(lock, lock_path(workspace))
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False


JOB = re.compile(r'[^-]+-[0-9]+-w[0-9]+-[0-9]+-(?P<pid>[A-Za-z0-9_]+)(?:-.*)?')
SEED_MAX_BYTES = 12*1024**3
SEED_RESERVE_BYTES = 20*1024**3  # free disk kept after a seed copy


def sibling_jobs(original):
    """Other jobs of the same problem on this host (any runner of the campaign root), as the
    `original` path each would use: <campaigns>/<runner>/jobs/<name>/<rest>."""
    parts = Path(original).parts
    if 'jobs' not in parts[:-1]:
        return []
    i = len(parts) - 1 - parts[::-1].index('jobs')
    if i + 1 >= len(parts) or i < 2:
        return []
    match = JOB.fullmatch(parts[i + 1])
    if not match:
        return []
    campaigns, rest = Path(*parts[:i - 1]), parts[i + 2:]
    found = []
    for job in campaigns.glob('*/jobs/*'):
        other = JOB.fullmatch(job.name)
        if other and other['pid'] == match['pid'] and job.name != parts[i + 1]:
            found.append(Path(job, *rest))
    return found


def seed_from_sibling(original, workspace):
    """lrh: the first round of a new lane (or a split/architect round) has no earlier round to
    adopt, and on mandelbrot every new lane spent 40+ min building the shared library cold
    (2026-10-17). Copy the largest sealed scoped build of another job of the same problem: the
    source seal is verified before and after the copy, the copy (new inodes, mtimes kept) gets
    its own seal with the source's identity and outcome, so prepare() still rejects it under a
    different trusted environment and Lake's traces rebuild every module whose sources differ.
    Any doubt leaves the round cold."""
    try:
        found = []
        for candidate in sibling_jobs(original):
            old = scoped(candidate)
            state, build = state_path(old), old/'.lake'/'build'
            if not (state.is_file() and build.is_dir()) or build.is_symlink():
                continue
            if state.stat().st_size > 16384:
                continue
            prior = json.loads(state.read_bytes())
            if prior.get('outcome') in ('check_succeeded', 'lean_diagnostic', 'interrupted_module_build'):
                found.append((int(prior['tree']['files']), float(prior.get('at', 0)), str(old), state, prior))
        # the largest sealed build covers most of the shared library (2026-10-17: the newest one was a
        # 2.3k-file lane build while a 4.8k-file interrupted architect build sat unused)
        for _, _, old, state, prior in sorted(found, reverse=True)[:3]:
            old = Path(old)
            if seed_copy(old, state, prior, workspace):
                return True
        return False
    except (OSError, ValueError, KeyError, TypeError):
        return False


def seed_copy(old, state, prior, workspace):
    try:
        build = old/'.lake'/'build'
        if sealed_tree(build, prior) != prior['tree'] or prior['tree']['bytes'] > SEED_MAX_BYTES:
            return False
        if shutil.disk_usage(workspace.parent).free < 2*prior['tree']['bytes'] + SEED_RESERVE_BYTES:
            return False
        staging = checked(workspace.with_name(workspace.name + '.seed'))
        if staging.exists():
            return False
        staging.mkdir(mode=0o700)
        try:
            (staging/'.lake').mkdir(mode=0o700)
            shutil.copytree(build, staging/'.lake'/'build', symlinks=True, copy_function=shutil.copy2)
            if (not state.is_file() or json.loads(state.read_bytes()) != prior
                    or sealed_tree(build, prior) != prior['tree']):
                raise ValueError('source build changed during the copy')
            measured = sealed_tree(staging/'.lake'/'build', prior)
            if (measured['files'], measured['bytes']) != (prior['tree']['files'], prior['tree']['bytes']):
                raise ValueError('incomplete copy')
            os.rename(staging, workspace)  # fails if the round already created its workspace
        except (OSError, ValueError):
            shutil.rmtree(staging, ignore_errors=True)
            return False
        seal = state_path(workspace)
        tmp = checked(seal.with_suffix('.tmp'))
        tmp.write_text(json.dumps({'identity': prior['identity'], 'tree': tree(workspace/'.lake'/'build'),
                                   'tree_schema': SEAL_SCHEMA,
                                   'outcome': prior['outcome'], 'at': time.time(),
                                   'seeded_from': str(old)}, sort_keys=True)+'\n')
        os.replace(tmp, seal)
        lock = lock_path(old)
        if lock.is_file():
            shutil.copy2(lock, lock_path(workspace))
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def checked(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Unsafe cache path')
    return path


SEAL_SCHEMA = 2


def tree(path, seconds=8, max_bytes=16*1024**3, max_files=100000, skip_setup=True):
    """Stat seal (inode, size, mtime_ns) of a controller-owned cache.

    lrh (seal schema 2): Lake's per-module `ir/*.setup.json` files are left out. Lake writes
    one before each module compile and never reads it afterwards; host disk cleaners delete
    old ones (one runner host, 2026-10-18: a 5-minute cleaner made 21 sealed mandelbrot builds look
    "changed outside a completed build" in a day, each followed by a cold rebuild over the
    30-min cap). Seals without `tree_schema` are verified with them included.

    lrh: upstream hashed every artifact's bytes, which took ~8 s per build and fell back
    to a full rebuild once the bound was exceeded on large projects. The cache lives
    outside the model sandbox and only feeds advisory diagnostics, so detecting
    modification outside a completed build by metadata is sufficient.
    """
    checked(path)
    started = time.monotonic()
    rows = {}
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            checked(Path(directory)/name)
        for name in sorted(files):
            if skip_setup and name.endswith('.setup.json'):
                continue
            item = Path(directory)/name
            stat = item.lstat()
            if not item.is_file() or item.is_symlink() or stat.st_nlink != 1:
                raise ValueError('Nonregular or hard-linked cache artifact')
            total += stat.st_size
            if len(rows) >= max_files or total > max_bytes or time.monotonic()-started > seconds:
                raise ValueError('Cache sealing bound exceeded')
            rows[str(item.relative_to(path))] = f'{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}'
    return {'digest': digest(rows), 'files': len(rows), 'bytes': total}


def sealed_tree(build, prior):
    """The stat seal of `build` computed the way `prior` was sealed."""
    return tree(build, skip_setup=prior.get('tree_schema') == SEAL_SCHEMA)


def identity(tools, template, sources=None):
    # lrh: module paths are no longer part of the identity. Upstream dropped the whole
    # cache whenever a module file was added or removed (27% of scoped builds, 3-5x
    # slower). Lake traces still recompile changed files; prepare() removes the
    # artifacts of deleted modules so they cannot satisfy a stale import.
    return digest({'schema': 2, 'tools': tools, 'template':
                   {k: hashlib.sha256(v).hexdigest() for k, v in template.items()}})


def prune_removed_modules(build, sources):
    """Delete build artifacts of Submission modules whose source file no longer exists."""
    present = {name[:-len('.lean')] for name in sources if name.endswith('.lean')}
    removed = 0
    for top in ('lib/lean', 'ir'):
        base = build/top
        for item in sorted(base.glob('Submission/**/*')) + sorted(base.glob('Submission.*')):
            if item.is_symlink() or not item.is_file():
                continue
            relative = item.relative_to(base)
            module = str(relative.parent/relative.name.split('.', 1)[0])
            if module not in present:
                item.unlink()
                removed += 1
    return removed


def state_path(workspace):
    root = checked(SEAL_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return checked(root/(hashlib.sha256(str(workspace).encode()).hexdigest()+'.json'))


def prepare(workspace, package_root, cache_identity, eligible, sources=None):
    workspace, package_root = checked(workspace), checked(package_root)
    cache = checked(workspace/'.lake')
    build = checked(cache/'build')
    state = state_path(workspace)
    reused = False
    reason = 'full_or_environment_check'
    measured = None
    if eligible and state.is_file() and build.is_dir():
        try:
            if state.stat().st_size > 16384: raise ValueError('Oversized cache seal')
            prior = json.loads(state.read_bytes())
            if prior['identity'] != cache_identity:
                raise ValueError('Source paths or trusted environment changed')
            measured = sealed_tree(build, prior)
            if measured != prior['tree']:
                raise ValueError('Cache artifact content changed outside completed build')
            reused = True
            reason = 'verified_previous_scoped_build'
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
    elif eligible:
        reason = 'no_sealed_scoped_build'
    if state.exists(): state.unlink()
    if cache.exists():
        if not cache.is_dir(): raise ValueError('Lake cache is not a directory')
        for item in cache.iterdir():
            if reused and item.name == 'build': continue
            if item.is_symlink() or item.is_file(): item.unlink()
            elif item.is_dir(): shutil.rmtree(item)
            else: raise ValueError('Unsupported cache entry')
    else: cache.mkdir()
    (cache/'packages').symlink_to(package_root, target_is_directory=True)
    pruned = prune_removed_modules(build, sources) if reused and sources is not None else 0
    return {'schema_version': 1, 'identity': cache_identity, 'eligible': eligible,
            'reused': reused, 'reason': reason, 'previous_tree': measured,
            'previous_outcome': prior.get('outcome', 'check_succeeded') if reused else None,
            'pruned_removed_module_artifacts': pruned, 'accepted': False}


def lock_path(workspace):
    return checked(state_path(workspace).with_suffix('.lake-manifest.json'))


def cached_lock(workspace, cache_identity):
    """Previously verified lake-manifest.json bytes for this exact trusted template."""
    path = lock_path(workspace)
    try:
        if path.stat().st_size > 1024*1024: return None
        value = json.loads(path.read_bytes())
        if value.get('identity') == cache_identity and isinstance(value.get('manifest'), str):
            return value['manifest'].encode()
    except (OSError, ValueError, AttributeError):
        pass
    return None


def save_lock(workspace, cache_identity, manifest_bytes):
    path = lock_path(workspace)
    tmp = checked(path.with_suffix('.tmp'))
    tmp.write_text(json.dumps({'identity': cache_identity, 'manifest': manifest_bytes.decode(),
                               'at': time.time()}, sort_keys=True))
    os.replace(tmp, path)


PROBE_TTL_SECONDS = 3600


def probe_fresh(tools):
    """True if the sandbox-engaged probe passed recently for these tools on this boot."""
    path = checked(SEAL_ROOT/'sandbox-probe.json')
    try:
        value = json.loads(path.read_bytes())
        return (value.get('tools') == digest(tools) and value.get('boot') == _boot_id()
                and time.time() - float(value.get('at', 0)) < PROBE_TTL_SECONDS)
    except (OSError, ValueError, TypeError):
        return False


def record_probe(tools):
    root = checked(SEAL_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = checked(root/'sandbox-probe.json')
    tmp = checked(path.with_suffix('.tmp'))
    tmp.write_text(json.dumps({'tools': digest(tools), 'boot': _boot_id(), 'at': time.time()}))
    os.replace(tmp, path)


def _boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return 'unknown'


def completed_outcome(result):
    """Cache provenance only: an ordinary Lean error is never proof success.

    Lake's source/dependency traces still decide which artifacts can be reused.
    Signals, timeouts, environment failures and inspection probes do not qualify
    for the new failed-build path.
    """
    if (type(result.get('exitcode')) is not int or result.get('timed_out')
            or result.get('checked_files_unchanged') is not True):
        return None
    if result['exitcode'] == 0:
        return 'check_succeeded'
    if (result['exitcode'] != 1 or result.get('phase') != 'module_build'
            or result.get('scope') != 'module-only'):
        return None
    output = result.get('stdout', '') + '\n' + result.get('stderr', '')
    if any(term in output.lower() for term in (
            'read-only file system', 'no space left on device', 'command not found',
            'failed to execute', 'cannot allocate memory', 'permission denied')):
        return None
    paths = result.get('checked_files')
    if not isinstance(paths, dict):
        return None
    # A generic "build failed" message cannot establish a Lean diagnostic.
    for name in re.findall(r'^error: (Submission(?:/[^\n:]+)?\.lean):[0-9]+:[0-9]+:',
                           output, re.MULTILINE):
        if name in paths:
            return 'lean_diagnostic'
    return None


def interrupted_outcome(result):
    """Retain disposable bytes after a killed build, never a successful check.

    A subsequent request must run Lake again. Its dependency traces decide what
    needs rebuilding; the interrupted target has no diagnostic/type authority.
    lrh: a build stopped because its sources changed (`superseded`) is kept too, even before
    Lake reported a node: dropping the cache would turn the next build cold (11-13 min for
    Submission) while Lake's traces already rebuild whatever the kill left incomplete.
    """
    superseded = result.get('superseded') is True and result.get('timed_out') is not True
    if ((result.get('timed_out') is not True and not superseded) or result.get('exitcode') != -9
            or result.get('process_group_terminated') is not True
            or result.get('checked_files_unchanged') is not True
            or result.get('phase') != 'module_build' or result.get('scope') != 'module-only'):
        return None
    output = result.get('stdout', '') + '\n' + result.get('stderr', '')
    if any(term in output.lower() for term in (
            'read-only file system', 'no space left on device', 'command not found',
            'failed to execute', 'cannot allocate memory', 'permission denied')):
        return None
    measurement = result.get('measurement') or {}
    if measurement.get('phase') != 'module_build':return None
    if superseded:
        return 'interrupted_module_build'
    nodes = measurement.get('reported_nodes') or []
    if not any(isinstance(row,dict) and isinstance(row.get('target'),str)
               and re.fullmatch(r"Submission(?:\.[A-Za-z_][A-Za-z0-9_']*)*",row['target'])
               for row in nodes):
        return None
    return 'interrupted_module_build'


def finish(workspace, evidence, result):
    outcome = completed_outcome(result) or interrupted_outcome(result)
    if not evidence['eligible'] or outcome is None:
        evidence['sealed'] = False
        return
    try:
        build = checked(workspace/'.lake'/'build')
        if not build.is_dir(): raise ValueError('No completed build directory')
        measured = tree(build)
        state = state_path(workspace)
        tmp = state.with_suffix('.tmp')
        checked(tmp)
        tmp.write_text(json.dumps({'identity': evidence['identity'], 'tree': measured,
                                   'tree_schema': SEAL_SCHEMA, 'outcome': outcome,
                                   'at': time.time()}, sort_keys=True)+'\n')
        os.replace(tmp, state)
        evidence.update(sealed=True, current_tree=measured, sealed_outcome=outcome)
        if outcome == 'interrupted_module_build':
            evidence.update(build_completed=False, requires_native_rebuild=True,
                            progress_nodes=len(result['measurement'].get('reported_nodes') or []))
    except (OSError, ValueError) as exc:
        evidence.update(sealed=False, seal_error=str(exc))

