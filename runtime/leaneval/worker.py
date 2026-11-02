"""Proof-only local/SSH feedback bridge for a prepared LeanEval workspace.

Feedback is advisory model-visible state, never authoritative acceptance. The
controller must run its independent clean-room verifier on the final proof bundle.
Configuration stays outside the writable model workspace; compiler and feedback
directories may be inside it, outside Submission.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from leaneval.artifacts import digest, proof_path
from leaneval.verifier import atomic_json
from leaneval import axiom_audit

# This program is controller-owned, carried over SSH; candidate files cannot replace it.
REMOTE_PROGRAM = r'''
import base64, contextlib, fcntl, hashlib, json, os, pathlib, re, shutil, signal, subprocess, sys, time
# lrh: SIGUSR1 = the controller saw this compile's sources change. Stop the compiler (its own
# session/cgroup), keep the partial build (build_cache.interrupted_outcome) and still report.
# Installed first, so an early signal cannot terminate the program by default action.
_running = []
_superseded = []
def _kill_running():
    for child, child_scope in list(_running):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            child_scope.kill()
def _supersede(signum, frame):
    _superseded.append(time.monotonic())
    _kill_running()
signal.signal(signal.SIGUSR1, _supersede)
request = json.load(sys.stdin)
cfg = request['config']
# Read-only Git operations must not opportunistically refresh shared repository indexes.
os.environ['GIT_OPTIONAL_LOCKS'] = '0'
sys.path.insert(0, cfg['remote_agent'])
from leaneval.artifacts import git_tree, proof_path
from leaneval.planning import profile
from leaneval.verifier import bwrap_wrapper, environment, tool_identity
from leaneval import costs, build_cache, stage_cgroup, compile_policy
workbench = pathlib.Path(cfg['remote_workbench'])
workspace = build_cache.workspace_for(pathlib.Path(cfg['remote_workspace']), request['action'], request.get('query', ''))
env = environment(workbench)
env['GIT_OPTIONAL_LOCKS'] = '0'
# lrh: bounded compiler parallelism (Lake's worker pool and each lean process), passed into
# landrun with --env; default min(8, cores) on this host.
lean_threads = compile_policy.lean_threads(cfg.get('lean_num_threads'), os.cpu_count())
env['LEAN_NUM_THREADS'] = str(lean_threads)
_lake = {}
def lake_build(*targets):
    if 'argv' not in _lake:
        flags = []
        if cfg.get('lake_jobs'):
            try:
                text = subprocess.run(['lake', '--help'], cwd=str(workspace), env=env, text=True,
                                      stdin=subprocess.DEVNULL, capture_output=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                text = ''
            flags = compile_policy.lake_jobs_args(cfg.get('lake_jobs'), text)
        _lake['argv'] = ['lake', *flags, 'build']
    return [*_lake['argv'], *targets]
started = time.monotonic()
deadline = started + cfg['timeout_seconds']
phase = 'infrastructure'
stage_measurements = []
cost_report = None
class StageFailure(Exception):
    def __init__(self, result):
        self.result = result
# lrh: the broker sends SIGTERM when the round ends; the compiler runs in its own session and
# cgroup scope, so take them down too instead of leaving an orphaned build in the cache.
def _terminate(signum, frame):
    for child, child_scope in list(_running):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            child_scope.kill()
    os._exit(128 + signum)
signal.signal(signal.SIGTERM, _terminate)
def run(argv, cwd, limit=None):
    remaining = deadline - time.monotonic()
    if limit is not None:
        remaining = min(remaining, limit)
    if remaining <= 0:
        return {'exitcode': None, 'stdout': '', 'stderr': '', 'timed_out': True}
    if _superseded:
        return {'exitcode': None, 'stdout': '', 'stderr': '', 'timed_out': False, 'superseded': True,
                'process_group_terminated': False}
    options = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    if request['action'] == 'search':
        p = subprocess.Popen(argv, **options)
        stage_scope = stage_cgroup.Scope(reason='non_compiler_action')
    else:
        p, stage_scope = stage_cgroup.spawn(argv, **options)
    _running.append((p, stage_scope))
    if _superseded:  # the signal arrived between Popen and registration
        _kill_running()
    measure = costs.Probe(getattr(p, 'pid', None)).start()
    timed_out = False
    group_terminated = False
    try:
        out, err = p.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
            group_terminated = True
        with contextlib.suppress(OSError):
            stage_scope.kill()
        out, err = p.communicate()
    finally:
        _running.remove((p, stage_scope))
    superseded = bool(_superseded) and not timed_out and p.returncode == -signal.SIGKILL
    measurement = measure.finish()
    measurement["isolated_cgroup"] = stage_scope.finish()
    measurement.update(phase=phase, command=pathlib.Path(argv[0]).name, exitcode=p.returncode,
                       timed_out=timed_out, superseded=superseded,
                       reported_nodes=costs.built_nodes(out.decode(errors='replace')))
    stage_measurements.append(measurement)
    return {'exitcode': p.returncode, 'stdout': out.decode(errors='replace'),
            'stderr': err.decode(errors='replace'), 'timed_out': timed_out, 'measurement': measurement,
            'superseded': superseded, 'process_group_terminated': group_terminated or superseded}
try:
    if request['action'] == 'search':
        packages = workbench / 'vendor/lean-eval/.lake/packages'
        # Both libraries are pinned official dependencies. Searching only Mathlib
        # hid relevant Lean Pool formalizations from the solver.
        roots = [str(packages / name) for name in ('mathlib', 'lean-pool')]
        # the solution bank (runtime/tools/bank.py): licensed formalizations recorded in bank/SOURCES.json
        try:
            bank = json.loads((workbench / 'bank/SOURCES.json').read_text()).get('sources', [])
        except (OSError, ValueError):
            bank = []
        roots += [str(workbench / 'bank' / s['name']) for s in bank
                  if isinstance(s, dict) and (workbench / 'bank' / str(s.get('name'))).is_dir()]
        result = run(['rg', '-n', '--glob', '*.lean', '--max-count', '20', '--',
                      request['query'], *roots], workbench)
        for key in ('stdout', 'stderr'):
            result[key] = result[key][:100000]
    else:
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError('Remote workspace must be a real prepared directory')
        tools = tool_identity(workbench)
        original = git_tree(workbench / 'vendor/lean-eval', profile()['benchmark']['commit'],
                            'generated/' + cfg['problem_id'])
        files = request['files']
        if request['action'] in {'preflight', 'repair'}:
            files = {name: base64.b64encode(data).decode()
                     for name, data in original.items() if proof_path(name)}
        if 'Submission.lean' not in files or not all(proof_path(n) for n in files):
            raise ValueError('Invalid proof-only overlay')
        phase = 'request_validation'
        from leaneval.generation import validate_bytes
        generation_receipts = validate_bytes(
            {name: base64.b64decode(data) for name, data in files.items()},
            request.get('generation_receipts', []))
        if request['action'] == 'cost' and request['query']:
            module = request['query']
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\']*(?:\.[A-Za-z_][A-Za-z0-9_\']*)*', module) or module.replace('.', '/') + '.lean' not in files:
                raise ValueError('Cost query requires an existing proof module')
        else:
            module = request['query'].split(':')[0] if request['action'] in {'build', 'inspect', 'inspect_partial'} and request['query'] else 'Submission'
        package_root = workbench / 'vendor/lean-eval/.lake/packages'
        # lrh: the import-graph cost plan is computed only for an explicit `cost` request;
        # upstream ran it before every build although nothing reads it there.
        try:
            if request['action'] != 'cost':
                raise ValueError('cost plan skipped for ' + request['action'])
            source_overlay = {name: data for name, data in original.items() if not proof_path(name)}
            source_overlay.update({name: base64.b64decode(data) for name, data in files.items()})
            roots = sorted(p for p in package_root.iterdir() if p.is_dir())
            lean_prefix = pathlib.Path(subprocess.check_output(['lean', '--print-prefix'], cwd=workbench, env=env, text=True).strip())
            roots.append(lean_prefix / 'src/lean')
            suffix = '.research-cost-readonly' if request['action'] == 'cost' else '.research-cost'
            analysis_dir = workspace.with_name(workspace.name + suffix)
            seeds, selection = [module], None
            if request['action'] == 'cost' and not request['query']:
                from leaneval.cost_scope import select
                seeds, selection = select({name: source_overlay[name] for name in files}, request.get('cost_policy'))
            report = costs.plan(source_overlay, roots, seeds, analysis_dir, tools, selection=selection,
                                max_total_modules=100000 if request['action'] == 'cost' else None)
            cost_report = costs.summary(report)
            cost_report['graph_path'] = str(analysis_dir / 'latest-graph.json.gz')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            cost_report = ({'complete': False, 'error': str(exc), 'accepted': False}
                           if request['action'] == 'cost' else None)
        if request['action'] == 'cost':
            print(json.dumps({'exitcode': 0, 'stdout': '', 'stderr': '', 'phase': 'cost_planning',
                              'cost_report': cost_report, 'compiler_invoked': False,
                              'checked_files': {name: hashlib.sha256(data).hexdigest() for name, data in source_overlay.items() if proof_path(name)},
                              'checked_files_unchanged': True, 'tool_identity': tools,
                              'accepted': False, 'timed_out': False}))
            raise SystemExit(0)
        # Reuse only controller-sealed scoped diagnostics. Full checks discard
        # every previous build artifact, including a valid scoped cache.
        eligible_cache = (request['action'] in {'inspect', 'inspect_partial', 'light_check'} or
                          (request['action'] == 'build' and bool(request['query'])))
        cache_identity = build_cache.identity(tools, original)
        cache_evidence = build_cache.prepare(workspace, package_root, cache_identity, eligible_cache,
                                             sources=set(files))
        cache = workspace / '.lake'
        packages = cache / 'packages'
        expected_packages = package_root
        phase = 'infrastructure'
        # Delete the complete previous proof overlay, including stale helper modules.
        for name in ('Submission.lean', 'Submission'):
            path = workspace / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        # Restore trusted template files as well: a previous candidate cannot replace the driver.
        for name, data in original.items():
            path = workspace / name
            for parent in path.relative_to(workspace).parents:
                if (workspace / parent).is_symlink():
                    raise ValueError('Symlink in trusted workspace')
            if path.is_symlink():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Update only the restored trusted template; candidate Lean has not entered it yet.
        phase = 'dependencies'
        lock = workspace / 'lake-manifest.json'
        if lock.is_symlink() or lock.is_file():
            lock.unlink()
        # lrh: a scoped build reuses the lake-manifest.json that `lake update` produced and
        # the pin checks below verified for this exact trusted template (saves ~5 s/build).
        verified_lock = build_cache.cached_lock(workspace, cache_identity) if eligible_cache else None
        if verified_lock is not None:
            lock.write_bytes(verified_lock)
            dependencies = {'exitcode': 0, 'timed_out': False, 'reused_verified_lock': True}
        else:
            dependencies = run(['lake', 'update'], workspace)
        dependencies['phase'] = phase
        if dependencies['exitcode'] != 0 or dependencies['timed_out']:
            raise StageFailure(dependencies)
        actual = json.loads(lock.read_text())
        pin = profile()['benchmark']['commit']
        upstream = workbench / 'vendor/lean-eval'
        expected = json.loads(subprocess.check_output(
            ['git', '-C', str(upstream), 'show', pin + ':lake-manifest.json']))
        revisions = {p['name']: p['rev'] for p in expected['packages']}
        for package in actual['packages']:
            if package.get('rev') != revisions.get(package['name']):
                raise ValueError('Dependency manifest differs from pinned benchmark')
            folder = expected_packages / package['name'].strip('«»')
            head = subprocess.check_output(['git', '-C', str(folder), 'rev-parse', 'HEAD'],
                                           text=True).strip()
            if head != package['rev']:
                raise ValueError('Shared dependency checkout differs from pinned benchmark')
        if eligible_cache and verified_lock is None:
            build_cache.save_lock(workspace, cache_identity, lock.read_bytes())
        phase = 'infrastructure'
        if request['action'] == 'repair':
            # Only a trusted pinned dependency may be repaired, never candidate code.
            phase = 'environment_cache_repair'
            package = expected_packages / 'proofwidgets'
            locks = pathlib.Path.home() / '.cache/lean-eval-environment-locks'
            locks.mkdir(parents=True, exist_ok=True)
            with (locks / 'proofwidgets.lock').open('a') as repair_lock:
                fcntl.flock(repair_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def clean_package():
                    dirty = subprocess.check_output(['git', '-C', str(package), 'status',
                        '--porcelain', '--untracked-files=normal'], text=True)
                    head = subprocess.check_output(['git', '-C', str(package),
                        'rev-parse', 'HEAD'], text=True).strip()
                    expected_head = next(p['rev'] for p in actual['packages']
                        if p['name'].strip('«»') == 'proofwidgets')
                    if dirty or head != expected_head:
                        raise ValueError('Refusing repair: dependency source is not clean and pinned')
                clean_package()
                prefix = subprocess.check_output(['lean', '--print-prefix'], cwd=workspace,
                                                 env=env, text=True).strip()
                env['PATH'] = prefix + '/bin:' + env['PATH']
                result = run(['lake', '-KerrorOnBuild=JS regeneration requires separate repair',
                              'build', 'ProofWidgets'], package)
                clean_package()
                result['phase'] = phase
                result['tracked_sources_unchanged'] = True
                raise StageFailure(result)
        if request['action'] == 'preflight':
            # Compile only pinned template imports, in the same write sandbox as proof
            # elaboration. Never enter candidate bytes during environment admission.
            imports = []
            for name, data in original.items():
                if name.endswith('.lean'):
                    for line in data.decode().splitlines():
                        if line.startswith('import ') and not any(
                            token.startswith(('Submission', 'Challenge', 'Solution'))
                            for token in line.split()[1:]):
                            imports.append(line)
            smoke = cache / 'ResearchEnvironment.lean'
            smoke.write_text('\n'.join(sorted(set(imports))) +
                             '\nexample : (1 : Nat) = 1 := rfl\n')
            probe_script = workbench / 'vendor/lean-eval/scripts/sandbox_engaged_probe.py'
            probe = run(['python3', str(probe_script), '--require-tools'], workspace)
            probe['phase'] = 'sandbox_probe'
            if probe['exitcode'] != 0 or probe['timed_out']:
                raise StageFailure(probe)
            phase = 'module_build'
            prefix = subprocess.check_output(['lean', '--print-prefix'], cwd=workspace,
                                             env=env, text=True).strip()
            env['PATH'] = prefix + '/bin:' + env['PATH']
            landrun = ['landrun', '--best-effort', '--ro', '/', '--rw', '/dev', '-ldd', '-add-exec',
                       '--env', 'PATH', '--env', 'HOME', '--env', 'LEAN_NUM_THREADS',
                       '--rwx', str(cache), '--rox', prefix,
                       '--rox', shutil.which('git', path=env['PATH'])]
            # Library custom targets must also be fresh under the actual readonly
            # shared-package mount: this catches ProofWidgets writes outside buildDir.
            if (expected_packages / 'proofwidgets').is_dir():
                widget = run(landrun + ['--', *lake_build('ProofWidgets')], workspace)
                widget['phase'] = 'environment_custom_targets'
                if widget['exitcode'] != 0 or widget['timed_out']:
                    raise StageFailure(widget)
            result = run(landrun + ['--', 'lake', 'env', 'lean', str(smoke),
                                   '-o', str(cache / 'ResearchEnvironment.olean')], workspace)
            result['phase'] = 'environment_imports'
            result['environment_identity'] = {
                'benchmark': pin, 'dependencies': actual,
                'toolchain': (workspace / 'lean-toolchain').read_text().strip(),
                'imports': sorted(set(imports)), 'sandbox': 'readonly-root; writable-workspace-lake'}
            raise StageFailure(result)
        for name, content in files.items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(content, validate=True))
        probe_script = workbench / 'vendor/lean-eval/scripts/sandbox_engaged_probe.py'
        # lrh: the sandbox-engaged probe checks the host kernel and pinned tools, not the
        # candidate; scoped builds reuse a pass from this boot within an hour (~9 s/build).
        # Full checks and preflight always run it.
        if eligible_cache and build_cache.probe_fresh(tools):
            probe = {'exitcode': 0, 'timed_out': False, 'reused_recent_probe': True}
        else:
            probe = run(['python3', str(probe_script), '--require-tools'], workspace)
            if probe['exitcode'] == 0 and not probe['timed_out']:
                build_cache.record_probe(tools)
        if probe['exitcode'] != 0 or probe['timed_out']:
            result = probe
            result['phase'] = 'sandbox_probe'
        elif request['action'] == 'light_check':
            # lrh: the agent's `./lean check`: warm scoped `lake build Submission`, then the trusted
            # statement wrapper (Solution) and `#print axioms` of every hole. Diagnostic only; the
            # build cache is sealed from the Submission build record (module-only scope).
            name_pattern = r'[A-Za-z_][A-Za-z0-9_\']*(?:\.[A-Za-z_][A-Za-z0-9_\']*)*'
            phase = 'module_build'
            prefix = subprocess.check_output(['lean', '--print-prefix'], cwd=workspace,
                                             env=env, text=True).strip()
            env['PATH'] = prefix + '/bin:' + env['PATH']
            landrun = ['landrun', '--best-effort', '--ro', '/', '--rw', '/dev', '-ldd', '-add-exec',
                       '--env', 'PATH', '--env', 'HOME', '--env', 'LEAN_NUM_THREADS',
                       '--rwx', str(cache), '--rox', prefix,
                       '--rox', shutil.which('git', path=env['PATH'])]
            result = run(landrun + ['--', *lake_build('Submission')], workspace)
            result.update(phase='module_build', checked_module='Submission', scope='module-only',
                          light_check=True)
            try:
                names = json.loads(original['config.json'])['theorem_names']
            except (KeyError, ValueError, TypeError):
                names = []
            if not isinstance(names, list) or len(names) > 100 or not all(
                    isinstance(n, str) and re.fullmatch(name_pattern, n) for n in names):
                names = []
            target = 'Solution' if 'Solution.lean' in original else 'Submission'
            result.update(hole_names=names, statement_module=target)
            def passed(step):
                return step.get('exitcode') == 0 and not step.get('timed_out') and not step.get('superseded')
            if passed(result) and names:
                phase = 'light_check_statement'
                solution = (run(landrun + ['--', *lake_build(target)], workspace) if target != 'Submission'
                            else {'exitcode': 0, 'stdout': '', 'stderr': '', 'timed_out': False})
                result['solution_build'] = {k: solution.get(k) for k in
                                            ('exitcode', 'stdout', 'stderr', 'timed_out', 'superseded')}
                if passed(solution):
                    phase = 'light_check_axioms'
                    probe_source = cache / ('ResearchLightCheck_' + request['check_id'] + '.lean')
                    probe_bytes = ('import ' + target + '\n' +
                                   ''.join('#print axioms ' + n + '\n' for n in names)).encode()
                    probe_source.write_bytes(probe_bytes)
                    axioms = run(landrun + ['--', 'lake', 'env', 'lean', '--json', str(probe_source)], workspace)
                    result['axiom_probe'] = {k: axioms.get(k) for k in
                                             ('exitcode', 'stdout', 'stderr', 'timed_out', 'superseded')}
                    result['axiom_probe']['probe_sha256'] = hashlib.sha256(probe_bytes).hexdigest()
        elif request['action'] in {'inspect', 'inspect_partial'} or (request['action'] == 'build' and request['query']):
            phase = 'request_validation'
            parts = request['query'].split(':')
            module = parts[0]
            name_pattern = r'[A-Za-z_][A-Za-z0-9_\']*(?:\.[A-Za-z_][A-Za-z0-9_\']*)*'
            frozen_identity = None
            if request['action'] == 'inspect' and module == 'Challenge':
                from leaneval.goal_types import identity
                frozen_identity = identity(original, pin, cfg['problem_id'])
                if len(parts) != 2 or parts[1] not in frozen_identity['declarations']:
                    raise ValueError('Frozen inspection requires an exact declared benchmark goal')
            elif not re.fullmatch(name_pattern, module) or not (
                module == 'Submission' or module.startswith('Submission.')
            ) or module.replace('.', '/') + '.lean' not in files:
                raise ValueError('Scoped check needs an existing Submission module')
            if request['action'] in {'inspect', 'inspect_partial'} and (len(parts) != 2 or not re.fullmatch(name_pattern, parts[1])):
                raise ValueError('inspect requires Module:fully.qualified.declaration')
            phase = 'module_build'
            prefix = subprocess.check_output(['lean', '--print-prefix'], cwd=workspace,
                                             env=env, text=True).strip()
            env['PATH'] = prefix + '/bin:' + env['PATH']
            landrun = ['landrun', '--best-effort', '--ro', '/', '--rw', '/dev', '-ldd', '-add-exec',
                       '--env', 'PATH', '--env', 'HOME', '--env', 'LEAN_NUM_THREADS',
                       '--rwx', str(cache), '--rox', prefix,
                       '--rox', shutil.which('git', path=env['PATH'])]
            result = run(landrun + ['--', *lake_build(module)], workspace)
            result.update(phase='module_build', checked_module=module, scope='module-only')
            candidate = request.get('residual_target')
            if (request['action']=='build' and result['exitcode']==0 and not result['timed_out']
                    and isinstance(candidate,dict) and candidate.get('module')==module
                    and re.fullmatch(name_pattern,str(candidate.get('target','')))
                    and deadline-time.monotonic()>=1):
                target=candidate['target']
                try:
                    probe_source=cache/('ResearchAutoResidual_'+request['check_id']+'.lean')
                    probe_bytes=('import '+module+'\n#check '+target+'\n#print axioms '+target+'\n').encode()
                    probe_source.write_bytes(probe_bytes)
                    phase='residual_type_probe'
                    probe=run(landrun+['--','lake','env','lean','--json',str(probe_source)],workspace,limit=5)
                    probe.update(target=target,module=module,source_exact=probe_source.read_bytes()==probe_bytes,
                                 probe_sha256=hashlib.sha256(probe_bytes).hexdigest())
                except (OSError,ValueError,subprocess.SubprocessError) as exc:
                    probe={'exitcode':None,'stdout':'','stderr':str(exc),'target':target,
                           'module':module,'source_exact':False,'timed_out':False}
                result['residual_probe']=probe
            if request['action'] == 'inspect_partial' and not result['timed_out']:
                from leaneval.partial_types import probe
                declaration = parts[1]
                original_source = base64.b64decode(files[module.replace('.', '/') + '.lean'], validate=True)
                probe_bytes, first_line = probe(original_source, declaration)
                probe_source = cache / ('ResearchPartial_' + request['check_id'] + '.lean')
                probe_source.write_bytes(probe_bytes)
                build = {k: result[k] for k in ('stdout', 'stderr', 'exitcode')}
                phase = 'partial_declaration_inspection'
                source_path = workspace / (module.replace('.', '/') + '.lean')
                # Elaborate at the original module path so private declarations
                # retain their module identity. Only this isolated compiler copy
                # changes, and it is restored before recording source evidence.
                source_path.write_bytes(probe_bytes)
                try:
                    result = run(landrun + ['--', 'lake', 'env', 'lean', '--json', str(source_path)], workspace)
                finally:
                    source_path.write_bytes(original_source)
                result.update(phase=phase, checked_module=module, declaration=declaration,
                              scope='partial-type-diagnostic', candidate_checked=False,
                              build=build, module_exitcode=build['exitcode'],
                              partial_probe_path=str(probe_source), partial_type_line=first_line,
                              partial_diagnostic_path=str(source_path),
                              partial_probe_sha256=hashlib.sha256(probe_bytes).hexdigest(),
                              partial_source_prefix_exact=probe_source.read_bytes()==probe_bytes,
                              invocation_id=request['check_id'])
            if request['action'] == 'inspect' and result['exitcode'] == 0 and not result['timed_out']:
                declaration = parts[1]
                probe_source = cache / ('ResearchContract_' + request['check_id'] + '.lean')
                probe_source.write_text('import ' + module + '\n#check ' + declaration +
                                        '\n#print axioms ' + declaration + '\n')
                build = {k: result[k] for k in ('stdout', 'stderr', 'exitcode')}
                result = run(landrun + ['--', 'lake', 'env', 'lean', '--json', str(probe_source)], workspace)
                result.update(phase='declaration_inspection', checked_module=module,
                              declaration=declaration, scope='type-and-axiom-diagnostic',
                              build=build, probe_sha256=hashlib.sha256(probe_source.read_bytes()).hexdigest(),
                              invocation_id=request['check_id'])
                if frozen_identity is not None:
                    result.update(scope='frozen-official-goal-type',
                                  frozen_goal_identity=frozen_identity,
                                  frozen_goal_current=all(
                                      (workspace / name).is_file() and
                                      not (workspace / name).is_symlink() and
                                      hashlib.sha256((workspace / name).read_bytes()).hexdigest() == value
                                      for name, value in frozen_identity['source_files'].items()))
        else:
            # the official flow: `lake comparator` in bubblewrap, the cache-binding wrapper first on PATH
            # (in the host user's cache, outside the agent's writable set; one per workbench)
            wrapper = bwrap_wrapper(pathlib.Path.home() / '.cache/lrh/evaluation-bin'
                                    / hashlib.sha256(str(workbench.resolve()).encode()).hexdigest()[:16], workbench)
            env['PATH'] = str(wrapper) + os.pathsep + env['PATH']
            result = run(['lake', 'test'], workspace)
            result['phase'] = 'official_lake_test'
        result['checked_files'] = {name: hashlib.sha256(base64.b64decode(data)).hexdigest()
                                   for name, data in files.items()}
        if result.get('frozen_goal_identity'):
            result['candidate_checked'] = False
            result['checked_files'] = result['frozen_goal_identity']['source_files']
        result['checked_files_unchanged'] = all(
            (workspace / name).is_file() and hashlib.sha256((workspace / name).read_bytes()).hexdigest() == value
            for name, value in result['checked_files'].items())
        result['tool_identity'] = tools
        result['generation_receipts'] = generation_receipts
        build_cache.finish(workspace, cache_evidence, result)
        result['build_cache'] = cache_evidence
    result['elapsed_seconds'] = round(time.monotonic() - started, 6)
    result['accepted'] = False
    result['stage_measurements'] = stage_measurements
    result['cost_report'] = cost_report
    result['compiler_parallelism'] = {'lean_num_threads': lean_threads, 'lake_build': _lake.get('argv')}
    result['superseded'] = bool(result.get('superseded')) or (bool(_superseded) and any(
        m.get('superseded') for m in stage_measurements))
    print(json.dumps(result))
except StageFailure as exc:
    result = exc.result
    result['accepted'] = False
    result['elapsed_seconds'] = round(time.monotonic() - started, 6)
    result['stage_measurements'] = stage_measurements
    result['cost_report'] = cost_report
    result['compiler_parallelism'] = {'lean_num_threads': lean_threads, 'lake_build': _lake.get('argv')}
    print(json.dumps(result))
except Exception as exc:
    print(json.dumps({'exitcode': None, 'stdout': '', 'stderr': str(exc),
                      'timed_out': False, 'accepted': False, 'phase': phase,
                      'superseded': bool(_superseded),
                      'elapsed_seconds': round(time.monotonic() - started, 6)}))
'''


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text())
    required = {'local_workspace', 'remote_workspace',
                'remote_workbench', 'remote_agent', 'feedback_dir', 'timeout_seconds', 'problem_id'}
    if cfg.get('transport', 'ssh') not in {'local', 'ssh'}:
        raise ValueError('Unknown worker transport')
    if cfg.get('transport', 'ssh') == 'ssh':
        required |= {'host', 'ip', 'ssh_key'}
    if not required <= cfg.keys():
        raise ValueError('Incomplete worker configuration')
    workspace = Path(cfg['local_workspace'])
    feedback = Path(cfg['feedback_dir'])
    if not workspace.is_absolute() or not feedback.is_absolute():
        raise ValueError('Local workspace and feedback paths must be absolute')
    if workspace.is_symlink():
        raise ValueError('Workspace symlinks are forbidden')
    workspace = workspace.resolve()
    if path.resolve().is_relative_to(workspace):
        raise ValueError('Configuration must be outside the writable workspace')
    feedback_path = feedback.resolve()
    if feedback_path == workspace or feedback_path.is_relative_to(workspace / 'Submission'):
        raise ValueError('Feedback must be outside the proof overlay')
    compiler = Path(cfg['remote_workspace'])
    if cfg.get('transport', 'ssh') == 'local' and compiler.resolve() == workspace:
        raise ValueError('Compiler workspace must differ from source workspace')
    if type(cfg['timeout_seconds']) is not int or cfg['timeout_seconds'] <= 0:
        raise ValueError('Positive integer timeout required')
    if cfg.get('transport', 'ssh') == 'ssh' and (
        not re.fullmatch(r'[A-Za-z0-9_.:-]+', cfg['ip']) or cfg['ip'].startswith('-')
        or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', cfg.get('ssh_user') or getpass.getuser())
    ):
        raise ValueError('Invalid SSH target')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', cfg['problem_id']):
        raise ValueError('Invalid problem ID')
    for key in ('remote_workspace', 'remote_workbench', 'remote_agent'):
        value = cfg[key]
        parsed = PurePosixPath(value)
        if not parsed.is_absolute() or '..' in parsed.parts or str(parsed) != value:
            raise ValueError('Remote paths must be canonical absolute paths')
    if cfg.get('transport', 'ssh') == 'ssh' and not Path(cfg['ssh_key']).is_absolute():
        raise ValueError('SSH key path must be absolute')
    return cfg


def proof_files(workspace: Path) -> dict[str, bytes]:
    if workspace.is_symlink():
        raise ValueError('Workspace symlinks are forbidden')
    candidates = [workspace / 'Submission.lean']
    directory = workspace / 'Submission'
    if directory.is_symlink():
        raise ValueError('Proof directory symlinks are forbidden')
    if directory.exists():
        candidates.extend(directory.rglob('*'))
    files = {}
    for path in candidates:
        if path.is_symlink():
            raise ValueError('Proof symlinks are forbidden')
        if path.is_dir():
            continue
        name = path.relative_to(workspace).as_posix()
        if not proof_path(name):
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('Proof files must be regular')
        files[name] = path.read_bytes()
    if 'Submission.lean' not in files:
        raise ValueError('Missing Submission.lean')
    return files


def _remote_argv(cfg: dict[str, Any]) -> list[str]:
    # argv keeps the key path intact; SSH's remote command is quoted as a single shell command.
    if cfg.get('transport', 'ssh') == 'local':
        return [sys.executable, '-I', '-c', REMOTE_PROGRAM]
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'StrictHostKeyChecking=accept-new', '-i', cfg['ssh_key'],
            (cfg.get('ssh_user') or getpass.getuser()) + '@' + cfg['ip'], shlex.join([
                cfg.get('remote_python', cfg['remote_agent'] + '/.venv/bin/python'),
                '-I', '-c', REMOTE_PROGRAM])]


class _SupersedeWatcher:
    """lrh: stops a running compile once a file of its import closure differs from the snapshot
    it compiles: SIGUSR1 to the local remote program, which kills its compiler group, keeps the
    partial build cached and still reports (outcome `superseded`)."""

    def __init__(self, process: Any, watch: Any):
        from leaneval import compile_policy
        self.process, self.watch = process, watch
        self.min_seconds = compile_policy.SUPERSEDE_MIN_SECONDS
        self.poll_seconds = compile_policy.SUPERSEDE_POLL_SECONDS
        self.files: list[str] = []
        self.after: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        started = time.monotonic()
        if self._stop.wait(self.min_seconds):
            return
        while not self._stop.is_set():
            try:
                changed = self.watch.changed()
            except (OSError, ValueError):
                changed = []
            if changed:
                # Not yet reaped (poll() is None), so the pid is still this child's.
                if self.process.poll() is None:
                    self.files, self.after = changed, round(time.monotonic() - started, 3)
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(self.process.pid, signal.SIGUSR1)
                return
            if self._stop.wait(self.poll_seconds):
                return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def request_remote(cfg: dict[str, Any], request: dict[str, Any], watch: Any = None) -> dict[str, Any]:
    argv = _remote_argv(cfg)
    from leaneval import inflight
    watcher = None
    try:
        # lrh: own session, tracked, so the broker can cancel it when the round ends; the
        # remote program kills its compiler group on SIGTERM.
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        with inflight.track(process):
            # lrh: stale-source abort only where the signal reaches the remote program itself.
            if watch is not None and cfg.get('transport', 'ssh') == 'local':
                watcher = _SupersedeWatcher(process, watch)
            try:
                stdout, stderr = process.communicate(json.dumps(request), timeout=cfg['timeout_seconds'] + 30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                return {'exitcode': None, 'stdout': stdout or '', 'stderr': stderr or '',
                        'timed_out': True, 'accepted': False, 'phase': 'ssh_timeout'}
            finally:
                if watcher is not None:
                    watcher.stop()
        stale = ({'superseded_files': watcher.files, 'superseded_after_seconds': watcher.after}
                 if watcher is not None and watcher.files else {})
        if process.returncode:
            if stale and process.returncode == -signal.SIGUSR1:
                return {'exitcode': None, 'stdout': stdout, 'stderr': stderr, 'phase': 'superseded',
                        'superseded': True, 'timed_out': False, 'accepted': False, **stale}
            return {'exitcode': process.returncode, 'stdout': stdout, 'stderr': stderr,
                    'phase': 'ssh', 'timed_out': False, 'accepted': False}
        payload = json.loads(stdout)
        payload['accepted'] = False
        payload.update(stale)
        return payload
    except (OSError, ValueError) as exc:
        return {'exitcode': None, 'stdout': '', 'stderr': str(exc), 'timed_out': False,
                'accepted': False, 'phase': 'infrastructure'}


def diagnostics(stdout: str, stderr: str, source_sha256: str) -> list[dict[str, Any]]:
    from leaneval.compiler_diagnostics import parse
    return parse(stdout, stderr, source_sha256)


def classify_outcome(result: dict[str, Any]) -> str:
    """Do not turn known infrastructure failures into proof-edit instructions."""
    phase = result.get('phase', '')
    if phase == 'request_validation':
        return 'invalid_request'
    if result.get('superseded') or phase == 'superseded':
        return 'superseded'
    output = (result.get('stdout', '') + '\n' + result.get('stderr', '')).lower()
    infrastructure = ('read-only file system', 'no space left on device',
                      'command not found', 'failed to execute',
                      'cannot allocate memory', 'permission denied')
    if phase in {'infrastructure', 'dependencies', 'sandbox_probe', 'ssh',
                 'ssh_timeout', 'environment_custom_targets', 'environment_imports', 'environment_cache_repair'}:
        if result.get('exitcode') != 0 or result.get('timed_out'):
            return 'environment_error'
    if result.get('exitcode') != 0 and any(token in output for token in infrastructure):
        return 'environment_error'
    if result.get('timed_out'):
        return 'timeout'
    if result.get('exitcode') != 0:
        # lrh: a compiler killed by the host (OOM killer, SIGKILL, exit 137) is not a Lean error.
        from leaneval.compile_policy import resource_killed
        if resource_killed(result):
            return 'environment_error'
    return 'check_succeeded' if result.get('exitcode') == 0 else 'lean_diagnostic'


def call_seconds(cfg: dict[str, Any], action: str, query: str) -> int:
    """lrh: the official full check (`./lean check --full`) gets FULL_CHECK_SECONDS or a larger
    configured cap (worker.json full_check_seconds, LRH_FULL_CHECK_SECONDS); other calls,
    including the light `./lean check`, the configured per-call cap."""
    from leaneval import compact, compile_policy
    seconds = int(cfg['timeout_seconds'])
    if compact.full_check(action, query):
        return max(seconds, compile_policy.full_check_seconds(cfg, compact.FULL_CHECK_SECONDS))
    return seconds


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def lock_names(action: str, query: str) -> tuple[str, ...]:
    """lrh: the compiler queue(s) a call holds while it runs.

    Scoped calls (module builds, `build Submission`, inspect, the light check) compile in the
    scoped build workspace (build_cache.workspace_for) and share `controller.lock`. The official
    full check compiles in the job's own compiler workspace, a different directory with its own
    `.lake` and cache seal, so it holds only `official.lock` and no longer queues the agent's
    builds behind it (109 calls waited 21.6 h). Preflight/repair rebuild that environment and
    hold both, always in this order (no deadlock). Search only reads the pinned packages. Step
    numbering and job-state updates are serialised separately and briefly (`records.lock`)."""
    from leaneval import compact
    if action == 'search':
        return ()
    if action in {'preflight', 'repair'}:
        return ('controller.lock', 'official.lock')
    if compact.full_check(action, query):
        return ('official.lock',)
    return ('controller.lock',)


def _previous_step_ids(directory: Path, steps: list[Path]) -> list[str]:
    """Step ids before the next record: from latest.json when it is the newest step and carries
    the whole chain, else by reading every step (the upstream way)."""
    if not steps:
        return []
    try:
        latest = json.loads((directory / 'latest.json').read_text())
        chain = latest.get('previous_step_ids')
        if (isinstance(chain, list) and latest.get('revision') == int(steps[-1].stem.split('-')[1])
                and len(chain) + 1 == len(steps) and isinstance(latest.get('step_id'), str)):
            return [*chain, latest['step_id']]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return [json.loads(p.read_text())['step_id'] for p in steps]


RECORD_BOUND_BYTES = 24 * 1024 * 1024
_PROGRESS_LINE = re.compile(r'^[\u2714\u26a0\u2716\u2139] \[\d+/\d+\]')


def _bound_record(result: dict[str, Any], limit: int = RECORD_BOUND_BYTES) -> dict[str, Any]:
    """lrh: keep a feedback record under feedback_records.MAX_FEEDBACK_BYTES (32 MiB). A
    certificate-heavy mandelbrot build wrote a 42 MB step (36,242 linter warnings, 16 MB of
    stdout) and every later build of the job failed reading it (2026-10-18). Only an oversized
    record is trimmed: all errors and `sorry` warnings, the first 500 other diagnostics, and the
    progress/error lines plus the first and last 1,000 lines of stdout/stderr are kept."""
    try:
        if len(json.dumps(result)) <= limit:
            return result
    except (TypeError, ValueError):
        return result
    trimmed = {}
    diagnostics = result.get('diagnostics')
    if isinstance(diagnostics, list) and len(diagnostics) > 1000:
        keep, other = [], []
        for entry in diagnostics:
            message = entry.get('message', '') if isinstance(entry, dict) else ''
            severe = isinstance(entry, dict) and (entry.get('severity') == 'error' or 'sorry' in str(message))
            (keep if severe else other).append(entry)
        result['diagnostics'] = keep[:5000] + other[:500]
        trimmed['diagnostics'] = len(diagnostics) - len(result['diagnostics'])
    for key in ('stdout', 'stderr'):
        text = result.get(key)
        if isinstance(text, str) and len(text) > 2 * 1024 * 1024:
            lines = text.splitlines()
            middle = [line for line in lines[1000:-1000] if _PROGRESS_LINE.match(line) or 'error' in line]
            omitted = max(0, len(lines) - 2000 - len(middle))
            result[key] = '\n'.join(lines[:1000] + [f'... lrh record bound: {omitted} lines omitted ...']
                                    + middle + lines[-1000:])
            trimmed[key] = len(text) - len(result[key])
    if trimmed:
        result['record_bounded'] = trimmed
    return result


def _write_step(directory: Path, result: dict[str, Any], steps: list[Path] | None = None) -> dict[str, Any]:
    """Number and store a record; the caller holds `records.lock` when it passes `steps`."""
    if steps is None:
        with _flock(directory / 'records.lock'):
            return _write_step(directory, result, sorted(directory.glob('step-*.json')))
    _bound_record(result)
    result['revision'] = max((int(p.stem.split('-')[1]) for p in steps), default=0) + 1
    result['previous_step_ids'] = _previous_step_ids(directory, steps)
    atomic_json(directory / f"step-{result['revision']:06d}.json", result)
    atomic_json(directory / 'latest.json', result)
    return result


def recent_steps(directory: Path, count: int = 60) -> list[dict[str, Any]]:
    """The newest `count` feedback records, oldest first."""
    recent = []
    for path in sorted(directory.glob('step-*.json'))[-count:]:
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            recent.append(value)
    return recent


def feedback(cfg: dict[str, Any], action: str, query: str = '', *, _controller: bool = False) -> dict[str, Any]:
    from leaneval import compile_policy
    # lrh: `./lean check` is the light check, `./lean check --full` (query 'full') the official test.
    query = compile_policy.check_query(action, query)
    job = Path(cfg['local_workspace']).parent
    if not _controller and (job / 'feedback-broker.json').exists():
        from leaneval.feedback_broker import request
        return request(job, action, query, call_seconds(cfg, action, query))
    if action in {'inspect', 'inspect_partial'} and not query:
        raise ValueError('inspect requires Module:fully.qualified.declaration')
    call_started = time.monotonic()
    feedback_root = Path(cfg['feedback_dir'])
    # Cost planning reads captured source; it never mutates the compiler workspace.
    # Its sequence/cache must not contend with a minutes-long compilation.
    directory = feedback_root / 'planning' if action == 'cost' else feedback_root
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    queue_started = time.monotonic()
    with contextlib.ExitStack() as locks:
        for name in lock_names(action, query):
            locks.enter_context(_flock(directory / name))
        queue_wait_seconds = time.monotonic() - queue_started
        return _feedback_locked(cfg, job, directory, feedback_root, action, query,
                                call_started, queue_wait_seconds)


def _feedback_locked(cfg: dict[str, Any], job: Path, directory: Path, feedback_root: Path,
                     action: str, query: str, call_started: float,
                     queue_wait_seconds: float) -> dict[str, Any]:
    from leaneval import compact, compile_policy
    history = sorted(directory.glob('step-*.json'))
    mode = compact.check_mode(action, query)
    queue = ('planning' if action == 'cost' else 'official' if mode == 'full' else 'compiler')
    breaker = directory / 'environment-circuit.json'
    if action not in {'search', 'preflight', 'repair', 'cost'} and breaker.is_file():
        circuit = json.loads(breaker.read_text())
        if circuit.get('state') == 'open':
            return {'exitcode': None, 'accepted': False, 'prefix_verified': False,
                    'phase': 'environment_circuit', 'outcome_class': 'environment_error',
                    'repair_required': True, 'timed_out': False,
                    'stdout': '', 'stderr': 'Repeated infrastructure failure. Repair the '
                    'environment, then run worker preflight to resume checks.',
                    'blocked_by': circuit.get('step_id'), 'compiler_invoked': False}
    if action == 'search':
        from leaneval.feedback_progress import search_barrier
        barrier_started_at = time.time()
        barrier_started = time.monotonic()
        blocked = search_barrier(job)
        if blocked:
            result = {'schema_version': 4, 'action': action, 'query': query, 'step_id': uuid.uuid4().hex,
                      'started_at': barrier_started_at, 'finished_at': time.time(),
                      'elapsed_seconds': round(time.monotonic() - barrier_started, 6),
                      'feedback_queue': 'compiler', 'source_evidence': 'current_local_snapshot_only',
                      'phase': 'research_progress_gate', 'outcome_class': 'invalid_request',
                      'exitcode': 2, 'timed_out': False, 'accepted': False, 'prefix_verified': False,
                      'compiler_invoked': False, 'request_dispatched': False, 'source_current': True,
                      'stdout': '', 'stderr': blocked['required_next_action'], 'diagnostics': [],
                      'queue_wait_seconds': round(queue_wait_seconds, 6),
                      'end_to_end_seconds': round(time.monotonic() - call_started, 6),
                      'lrh_version': compact.LRH_VERSION, **blocked}
            return _write_step(directory, result)
    files = proof_files(Path(cfg['local_workspace'])) if action not in {'search', 'preflight', 'repair'} else {}
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    from leaneval.generation import validate
    generation_receipts = validate(Path(cfg['local_workspace']), files) if files else []
    if files:
        from leaneval import frontier
        frontier.enforce_scope(Path(cfg['local_workspace']).parent, inventory)
        frontier.guard_next(Path(cfg['local_workspace']).parent, inventory, action, query)
    compiles = action in {'build', 'check', 'inspect', 'inspect_partial'}
    recent = recent_steps(directory) if files and compiles else []

    def refused(phase: str, message: str, **extra: Any) -> dict[str, Any]:
        now = time.time()
        result = {'schema_version': 4, 'action': action, 'query': query,
                  'step_id': uuid.uuid4().hex, 'started_at': now,
                  'finished_at': now, 'elapsed_seconds': 0.0, 'phase': phase,
                  'outcome_class': 'invalid_request', 'exitcode': 2, 'timed_out': False,
                  'accepted': False, 'prefix_verified': False, 'compiler_invoked': False,
                  'source_current': True, 'source_files': inventory,
                  'source_sha256': digest(inventory), 'stdout': '', 'stderr': message,
                  'diagnostics': [], 'required_next_action': message, 'feedback_queue': queue,
                  'lrh_version': compact.LRH_VERSION,
                  'queue_wait_seconds': round(queue_wait_seconds, 6),
                  'end_to_end_seconds': round(time.monotonic() - call_started, 6), **extra}
        if mode:
            result['check_mode'] = mode
        return _write_step(directory, result)

    if files and mode:
        # lrh: no check on a tree with sorry; no multi-minute official test on a tree whose
        # whole build does not pass (the light check builds Submission itself).
        refusal = compact.full_check_gate(recent, digest(inventory), files, light=mode == 'light')
        if refusal:
            return refused('full_check_gate', refusal)
    if files and compiles:
        cached = compile_policy.cached_failure(recent, action, query, inventory)
        if cached is not None:
            # lrh: the same files and call failed with Lean errors in this round; Lean is
            # deterministic, so repeat that record instead of recompiling (826 reruns, 5 h).
            now = time.time()
            result = {key: cached[key] for key in (
                'exitcode', 'phase', 'scope', 'checked_module', 'declaration', 'stdout', 'stderr',
                'diagnostics', 'checked_files', 'checked_files_unchanged', 'tool_identity',
                'axiom_audit', 'exact_types', 'required_next_action', 'hole_names',
                'statement_module') if key in cached}
            result.update({
                'schema_version': 4, 'action': action, 'query': query, 'step_id': uuid.uuid4().hex,
                'started_at': now, 'finished_at': now, 'elapsed_seconds': 0.0,
                'outcome_class': 'lean_diagnostic', 'timed_out': False, 'accepted': False,
                'prefix_verified': False, 'compiler_invoked': False, 'source_current': True,
                'cached_from_step': cached.get('step_id'), 'source_files': inventory,
                'source_sha256': digest(inventory), 'feedback_queue': queue,
                'lrh_version': compact.LRH_VERSION,
                'cache_note': (f"Identical to step {str(cached.get('step_id', ''))[:8]} (same files, "
                               'same call), which failed: its diagnostics are repeated without '
                               'recompiling. Change the proof before running this again.'),
                'queue_wait_seconds': round(queue_wait_seconds, 6),
                'end_to_end_seconds': round(time.monotonic() - call_started, 6)})
            if mode:
                result['check_mode'] = mode
            return _write_step(directory, result)
        refusal = compile_policy.round_refusal(job, recent, action, query)
        if refusal:
            return refused('round_wall_gate', refusal)
    # Credentials are needed only by local SSH; never send them to the remote program.
    remote_cfg = {key: cfg[key] for key in ('remote_workspace', 'remote_workbench',
                                           'remote_agent', 'timeout_seconds', 'problem_id')}
    # lrh: compiler threads (LEAN_NUM_THREADS) and the optional lake jobs flag for this host.
    remote_cfg.update(compile_policy.parallelism(cfg))
    cost_policy = None
    if action == 'cost' and not query:
        from leaneval import frontier
        job_config = frontier.read(Path(cfg['local_workspace']).parent / 'config.json')
        if (job_config.get('campaign_assignment') or {}).get('research_frontier'):
            cost_policy = frontier.policy(job_config)
    check_id = uuid.uuid4().hex
    remote_action = 'light_check' if mode == 'light' else action
    request = {'action': remote_action, 'query': '' if action == 'check' else query,
               'check_id': check_id, 'config': remote_cfg,
               'generation_receipts': generation_receipts, 'cost_policy': cost_policy,
               'files': {name: base64.b64encode(data).decode() for name, data in files.items()}}
    from leaneval import auto_residual
    # lrh: no automatic residual type probe after each successful build (5 s limit,
    # 62% timed out in our runs); an explicit `./lean inspect` still captures it.
    request['residual_target'] = None
    from leaneval import lineage
    seconds = call_seconds(cfg, action, query)
    try:
        ticket = lineage.compiler_reservation(Path(cfg['local_workspace']).parent, check_id,
                    seconds) if action not in {'cost', 'search'} else None
    except lineage.RuntimeBlocked as exc:
        result = {'phase': 'lineage_budget', 'outcome_class': 'budget_blocked',
                  'exitcode': None, 'compiler_invoked': False, 'accepted': False,
                  'action': action, 'query': query, 'step_id': check_id,
                  'source_files': inventory, 'source_sha256': digest(inventory),
                  'stdout': '', 'stderr': str(exc), 'timed_out': False}
        atomic_json(Path(cfg['local_workspace']).parent / 'lineage-budget-state.json', result)
        return result
    transport_cfg = dict(cfg)
    transport_cfg['timeout_seconds'] = remote_cfg['timeout_seconds'] = seconds
    if ticket:
        transport_cfg['timeout_seconds'] = int(ticket['seconds'])
        remote_cfg['timeout_seconds'] = int(ticket['seconds'])
    # lrh: stop a scoped compile once a file it imports changes (121 stale compiles, 40 h).
    watch = None
    module = compile_policy.watched_module(action, query, mode == 'light')
    if module and files:
        closure = compile_policy.import_closure(files, module)
        if closure:
            watch = compile_policy.SourceWatch(Path(cfg['local_workspace']), closure, inventory)
    started_at = time.time()
    started = time.monotonic()
    result = request_remote(transport_cfg, request, watch=watch)
    if ticket:
        observed = result.get('elapsed_seconds')
        if result.get('phase') == 'ssh_timeout' or not isinstance(observed, (int, float)):
            observed = None
        lineage.compiler_finished(ticket, observed)
        result['lineage_compiler_allowance_seconds'] = ticket['seconds']
    result.update({'schema_version': 4, 'action': action, 'query': query,
                   'started_at': started_at, 'finished_at': time.time(),
                   'compiler_invoked': result.get('compiler_invoked', action not in {'search', 'cost'}),
                   'step_id': check_id, 'source_sha256': digest(inventory),
                   'source_files': inventory, 'accepted': False,
                   'elapsed_seconds': round(time.monotonic() - started, 6),
                   'lrh_version': compact.LRH_VERSION})
    if mode:
        result['check_mode'] = mode
    if mode == 'light':
        # One outcome from the Submission build, the statement wrapper and the axiom probe.
        result.update(compact.light_verdict(result))
    # Strip credential paths if SSH diagnostics mention them; keep compiler output intact.
    for key in ('stdout', 'stderr'):
        result[key] = result.get(key, '') or ''
        if cfg.get('ssh_key'):
            result[key] = result[key].replace(cfg['ssh_key'], '[SSH key]')
    if action not in {'search', 'preflight', 'repair'}:
        try:
            current = {name: hashlib.sha256(data).hexdigest() for name, data in
                       sorted(proof_files(Path(cfg['local_workspace'])).items())}
            result['source_current'] = (current == inventory and
                result.get('checked_files', inventory) == inventory and
                result.get('checked_files_unchanged', True))
        except (OSError, ValueError):
            result['source_current'] = False
    if mode == 'light' and isinstance(result.get('axiom_audit'), dict):
        result['axiom_audit']['diagnostic_foundational'] &= (
            result.get('exitcode') == 0 and not result.get('timed_out')
            and result.get('source_current') is True)
    killed = compile_policy.resource_killed(result)
    if killed:
        result['resource_killed'] = killed
    result['outcome_class'] = classify_outcome(result)
    outcome = result['outcome_class']
    if outcome == 'superseded':
        changed = result.get('superseded_files') or []
        result['required_next_action'] = (
            'Stopped: ' + (', '.join(changed[:5]) or 'its source files') + ' changed while this ran'
            + (f" (after {int(result['superseded_after_seconds'])} s)"
               if isinstance(result.get('superseded_after_seconds'), (int, float)) else '')
            + '; its result would describe older files. The partial build stays cached; run it '
              'again on the current files.')
    if mode == 'full' and (outcome == 'timeout' or result.get('phase') == 'ssh_timeout'
                           or (outcome == 'environment_error' and killed)):
        # lrh: the full check passed the gate (sorry-free, Submission built on these files).
        result['not_a_rejection'] = True
        result['required_next_action'] = compact.not_a_rejection(remote_cfg['timeout_seconds'], killed)
    elif killed and outcome == 'environment_error':
        result['required_next_action'] = (
            f'The compiler was killed by the host ({killed}), most likely out of memory. This is '
            'not an error in your proof: run it again (smaller modules need less memory).')
    with _flock(directory / 'records.lock'):
        steps = sorted(directory.glob('step-*.json'))
        # A host kill (OOM) is transient: it never opens the environment circuit.
        if outcome == 'environment_error' and action != 'cost' and not killed:
            signature = {'phase': result.get('phase'), 'exitcode': result.get('exitcode'),
                         'stderr': result.get('stderr', '')[-2000:]}
            result['environment_fingerprint'] = digest(signature)
            previous = []
            for path in steps[-2:]:
                try:
                    previous.append(json.loads(path.read_text()))
                except (OSError, ValueError):
                    pass
            result['repair_required'] = any(
                p.get('environment_fingerprint') == result['environment_fingerprint']
                for p in previous)
            if result['repair_required']:
                atomic_json(breaker, {'state': 'open', 'at': time.time(),
                            'step_id': result['step_id'],
                            'fingerprint': result['environment_fingerprint'],
                            'phase': result.get('phase'), 'accepted': False})
        if action == 'preflight':
            result['repair_required'] = result['outcome_class'] != 'check_succeeded'
            atomic_json(breaker, {'state': 'open' if result['repair_required'] else 'closed',
                        'at': time.time(), 'step_id': result['step_id'], 'accepted': False,
                        'environment_identity': result.get('environment_identity')})
        if action == 'inspect' and result.get('phase') == 'declaration_inspection':
            declaration = result['declaration']
            audit = axiom_audit.fold(result.get('stdout', ''), [declaration])
            audit['diagnostic_foundational'] &= (result.get('exitcode') == 0 and
                not result.get('timed_out') and result.get('source_current', False) and
                result.get('invocation_id') == check_id)
            result['axiom_audit'] = audit
            from leaneval.type_diagnostics import exact_types
            result['exact_types'] = exact_types(result.get('stdout', ''), declaration)
        if action == 'inspect_partial' and result.get('phase') == 'partial_declaration_inspection':
            from leaneval.partial_types import select
            types, axiom_output = select(result.get('stdout', ''), result['partial_diagnostic_path'],
                                         result['partial_type_line'], result['declaration'])
            result['exact_types'] = types
            audit = axiom_audit.fold(axiom_output, [result['declaration']])
            audit['diagnostic_foundational'] = False
            result['axiom_audit'] = audit
        if action in {'inspect', 'inspect_partial'}:
            from leaneval.checkpoint_contract import seal
            try:
                type_receipt = seal(job, result)
                if type_receipt:
                    result['controller_type_receipt'] = type_receipt
            except (OSError, ValueError, TypeError) as exc:
                result['controller_type_receipt_error'] = str(exc)
        if isinstance(result.get('cost_report'), dict):
            from leaneval import costs
            measurement_history = sorted(feedback_root.glob('step-*.json')) if action == 'cost' else history
            timing_graph = result['cost_report'].get('_timing_graph')
            costs.attach_history(result['cost_report'], inventory, result.get('tool_identity', {}),
                                 [json.loads(p.read_text()) for p in measurement_history[-40:]] + [result])
            if action == 'cost' and result.get('source_current') is True:
                from leaneval.incremental_costs import attach
                attach(job, result['cost_report'], timing_graph, inventory,
                       result.get('tool_identity', {}), result['started_at'])
        result['prefix_verified'] = False
        result['diagnostics'] = diagnostics(
            result['stdout'], result['stderr'], result['source_sha256'])
        # lrh: library names similar to an unknown identifier (controller-side index).
        for entry in result['diagnostics']:
            if entry.get('severity') == 'error':
                try:
                    hint = compact.name_hint(entry.get('message', ''))
                except (OSError, ValueError):
                    hint = None
                if hint:
                    entry['hint'] = hint
        if action in {'search', 'build', 'check', 'inspect'}:
            advisory_started = time.monotonic()
            try:
                from leaneval.feedback_progress import observe
                advisory = observe(job, result)
                if advisory is not None:
                    result['in_session_advisory'] = advisory
            except (OSError, ValueError, TypeError, KeyError) as exc:
                result['in_session_advisory_error'] = str(exc)
            result['in_session_progress_seconds'] = round(time.monotonic() - advisory_started, 6)
        try:
            from leaneval.failed_routes import record
            failure_record = record(job, result)
            if failure_record:
                result['failed_route_record'] = failure_record
        except (OSError, ValueError, TypeError) as exc:
            result['failed_route_record_error'] = str(exc)
        try:
            automatic=auto_residual.commit(job,result,request.get('residual_target'))
            if automatic:
                result['automatic_residual']=automatic
        except (OSError,ValueError,TypeError,KeyError) as exc:
            result['automatic_residual_error']=str(exc)
        result['feedback_queue'] = queue
        result['queue_wait_seconds'] = round(queue_wait_seconds, 6)
        result['end_to_end_seconds'] = round(time.monotonic() - call_started, 6)
        return _write_step(directory, result, steps)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('action', choices=('build', 'check', 'search', 'preflight', 'repair', 'inspect', 'inspect_partial', 'cost'))
    parser.add_argument('query', nargs='?', default='')
    parser.add_argument('--json', action='store_true', help='print the full feedback record')
    parser.add_argument('--full', action='store_true',
                        help='with check: run the official lake test (comparator, kernels; minutes '
                             'to over an hour) instead of the light check')
    args = parser.parse_args()
    if args.action == 'search' and not args.query:
        parser.error('search requires a query')
    if args.full and args.action != 'check':
        parser.error('--full applies to check only')
    query = 'full' if args.full else args.query
    cfg = load_config(args.config)
    result = feedback(cfg, args.action, query)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        from leaneval.compact import render, round_clock
        print(render(result), end='')
        clock = round_clock(Path(cfg['local_workspace']).parent, time.time())
        if clock:
            print(clock)
        if result.get('stall_note'):  # computed by the broker (compact.stall_note)
            print(result['stall_note'])
        if result.get('ready_note'):  # computed by the broker (compact.ready_note)
            print(result['ready_note'])
    return 0 if result['exitcode'] == 0 and not result.get('timed_out') else 1


if __name__ == '__main__':
    raise SystemExit(main())
