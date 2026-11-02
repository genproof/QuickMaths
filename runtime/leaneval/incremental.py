"""Budgeted execution of an existing incremental cache; diagnostic results only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from leaneval import costs, frontier, generation, lineage
from leaneval.artifacts import digest
from leaneval.planning import profile
from leaneval.verifier import atomic_json, environment, tool_identity


def capture_output(job, check_id, out, err, limit=32*1024*1024):
    """Retain bounded raw bytes outside the model-writable workspace."""
    from leaneval.checkpoint_contract import safe
    result = {'schema_version': 1, 'step_id': check_id, 'streams': {}, 'accepted': False}
    try:
        root = safe(job / 'research-incremental-checks' / check_id)
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        for name, data in [('stdout', out), ('stderr', err)]:
            retained = data[-limit:]
            path = safe(root / (name + '.log'))
            with path.open('xb') as stream:
                stream.write(retained)
            result['streams'][name] = {'path': str(path), 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'stored_bytes': len(retained),
                'stored_sha256': hashlib.sha256(retained).hexdigest(),
                'offset': len(data)-len(retained), 'complete': len(data) <= limit}
        atomic_json(root / 'output.json', result)
    except (OSError, ValueError) as exc:
        result['capture_error'] = str(exc)
    return result


def configuration(cache):
    result = {}
    for name in ('lean-toolchain', 'lakefile.lean', 'lakefile.toml', 'lake-manifest.json'):
        path = cache / name
        if path.is_symlink():
            raise ValueError('Symlink in incremental configuration')
        if path.exists():
            if not path.is_file() or path.stat().st_size > 4*1024*1024:
                raise ValueError('Invalid incremental configuration')
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def progress_refusal(job, inventory, query, requested_query, error, started_at, started):
    """A policy outcome, with no compiler dispatch or budget reservation."""
    from leaneval import worker
    from leaneval.checkpoint_contract import safe
    step=uuid.uuid4().hex
    now={name:hashlib.sha256(data).hexdigest() for name,data in worker.proof_files(job/'workspace').items()}
    result={'schema_version':4,'step_id':step,'action':'incremental','query':query,
            'requested_query':requested_query,'phase':'research_progress_gate',
            'outcome_class':'invalid_request','exitcode':2,'timed_out':False,
            'compiler_invoked':False,'request_dispatched':False,'accepted':False,
            'prefix_verified':False,'source_files':inventory,'source_sha256':digest(inventory),
            'source_current':now==inventory,'source_evidence':'current_local_snapshot_only',
            'policy_reason':error.reason,'required_next_action':str(error),'stdout':'',
            'stderr':str(error),'diagnostics':[],'started_at':started_at,'finished_at':time.time(),
            'elapsed_seconds':time.monotonic()-started}
    path=safe(job/'research-progress-refusals'/step/'result.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    result['controller_policy_record']=str(path)
    atomic_json(path,{'schema_version':1,'job':str(job.resolve()),
                     'config_sha256':hashlib.sha256((job/'config.json').read_bytes()).hexdigest(),
                     'result':result,'accepted':False})
    directory=job/'workspace/.feedback';directory.mkdir(exist_ok=True)
    atomic_json(directory/('policy-refusal-'+step+'.json'),result)
    atomic_json(directory/'latest.json',result)
    return result


def execute(job: Path, query: str):
    from leaneval import agent_runner, worker
    request_started_at, request_started = time.time(), time.monotonic()
    requested_query = query
    from leaneval.incremental_queries import normalize
    query = normalize(query)
    modules = json.loads(query)
    workspace = job / 'workspace'
    cache = workspace / '.incremental'
    if cache.is_symlink() or not (cache / '.lake').is_dir():
        raise ValueError('No existing incremental cache')
    files = worker.proof_files(workspace)
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    generation_receipts = generation.validate(workspace, files)
    frontier.enforce_scope(job, inventory)
    try:
        frontier.guard_next(job, inventory, 'incremental', query)
    except frontier.ProgressBlocked as exc:
        return progress_refusal(job, inventory, query, requested_query, exc,
                                request_started_at, request_started)
    config_bytes = (job / 'config.json').read_bytes()
    config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    cfg = json.loads(config_bytes)
    requested = (cfg.get('campaign_assignment') or {}).get('incremental_timeout_seconds', 1800)
    if type(requested) is not int or not 1 <= requested <= 86400:
        raise ValueError('Invalid controller incremental timeout')
    workbench = agent_runner.workbench_path()
    tools = tool_identity(workbench)
    configuration_before = configuration(cache)
    if (cache / 'lean-toolchain').read_text().strip() != profile()['benchmark']['toolchain']:
        raise ValueError('Incremental cache toolchain differs from the pinned benchmark')
    raw_env = environment(workbench)
    prefix = Path(subprocess.check_output(['lean', '--print-prefix'], cwd=workbench,
                                         env=raw_env, text=True, timeout=30).strip())
    tools['incremental_compiler'] = {'prefix': str(prefix),
        'toolchain': profile()['benchmark']['toolchain'],
        'binaries': {name:hashlib.sha256((prefix/'bin'/name).read_bytes()).hexdigest()
                     for name in ('lean','lake')}, 'configuration':configuration_before}
    raw_env['PATH'] = str(prefix/'bin') + os.pathsep + raw_env['PATH']
    check_id = uuid.uuid4().hex
    ticket = lineage.compiler_reservation(job, 'incremental/' + check_id, requested)
    wall_started = time.time()
    started = time.monotonic()
    result = {'accepted': False, 'prefix_verified': False, 'phase': 'incremental_build',
              'step_id': check_id, 'action': 'incremental', 'query': query, 'requested_query': requested_query,
              'source_files': inventory, 'source_sha256': digest(inventory),
              'started_at': wall_started, 'tool_identity': tools}
    directory = workspace / '.feedback'
    directory.mkdir(exist_ok=True)
    try:
        for name in ('Submission', 'Submission.lean'):
            path = cache / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        for name, data in files.items():
            target = cache / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        agent = Path(__file__).resolve().parents[1]
        state = workspace / '.incremental-state'
        state.mkdir(exist_ok=True)
        policy = agent_runner.sandbox_policy(job, agent, state)
        command = [str(agent_runner.landrun_path()), '--best-effort']
        for kind, paths in policy.items():
            for path in paths:
                command += ['--' + kind, path]
        command += ['--env', 'PATH', '--env', 'HOME', '--env', 'MATHLIB_NO_CACHE_ON_UPDATE',
                    '--', 'lake', 'build', *modules]
        env = {key: raw_env[key] for key in ('PATH', 'HOME', 'MATHLIB_NO_CACHE_ON_UPDATE') if key in raw_env}
        from leaneval import stage_cgroup
        process, stage_scope = stage_cgroup.spawn(command, cwd=cache, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        probe = costs.Probe(process.pid).start()
        timeout = ticket['seconds'] if ticket else requested
        timed_out = False
        from leaneval import inflight
        try:
            with inflight.track(process, stage_scope):  # cancelled by the broker at round end
                out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            with __import__("contextlib").suppress(OSError):
                stage_scope.kill()
            out, err = process.communicate()
        measurement = probe.finish()
        isolated = stage_scope.finish()
        measurement["isolated_cgroup"] = isolated
        measurement.update(phase='incremental_build', command='lake', exitcode=process.returncode,
                           timed_out=timed_out, reported_nodes=costs.built_nodes(out.decode(errors='replace')))
        result.update(exitcode=process.returncode, stdout=out.decode(errors='replace')[-100000:],
                      stderr=err.decode(errors='replace')[-100000:], timed_out=timed_out,
                      stage_measurements=[measurement], compiler_invoked=True,
                      native_output=capture_output(job, check_id, out, err))
        try:
            from leaneval.incremental_residual import capture
            inspection = capture(job, result, modules, files, generation_receipts, started + timeout)
            if inspection:
                result['residual_inspection'] = inspection
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            result['residual_inspection'] = {'status':'unavailable','error':str(exc),'accepted':False}
    finally:
        elapsed = time.monotonic() - started
        lineage.compiler_finished(ticket, elapsed)
    current = {name: hashlib.sha256(data).hexdigest() for name,data in worker.proof_files(workspace).items()}
    try:
        configuration_unchanged = configuration(cache) == configuration_before
    except (OSError, ValueError):
        configuration_unchanged = False
    checked_unchanged = configuration_unchanged and all(
        (cache / name).is_file() and hashlib.sha256((cache / name).read_bytes()).hexdigest() == value
        for name, value in inventory.items())
    result.update(elapsed_seconds=elapsed, source_current=current == inventory and checked_unchanged,
                  checked_files=inventory, scope='incremental-module-diagnostic',
                  checked_files_unchanged=checked_unchanged,
                  configuration_unchanged=configuration_unchanged, finished_at=time.time())
    result['outcome_class'] = worker.classify_outcome(result)
    if ticket:
        result['lineage_compiler_allowance_seconds'] = ticket['seconds']
    result['diagnostics'] = worker.diagnostics(result['stdout'], result['stderr'], result['source_sha256'])
    from leaneval.failed_routes import record
    failure = record(job, result)
    if failure:
        result['failed_route_record'] = failure
    from leaneval.checkpoint_contract import safe
    record_dir = safe(job / 'research-incremental-checks' / check_id)
    record_dir.mkdir(parents=True, exist_ok=True)
    def persist_result():
        atomic_json(record_dir / 'result.json', {
            'schema_version': 1, 'job': str(job.resolve()), 'step_id': check_id,
            'config_sha256': config_sha256, 'result': result, 'accepted': False})
    persist_result()
    advisory_started = time.monotonic()
    try:
        from leaneval.feedback_progress import observe
        advisory = observe(job, result)
        if advisory is not None:
            result['in_session_advisory'] = advisory
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result['in_session_advisory_error'] = str(exc)
    result['in_session_progress_seconds'] = round(time.monotonic() - advisory_started, 6)
    persist_result()
    atomic_json(directory / ('incremental-' + check_id + '.json'), result)
    atomic_json(directory / 'latest.json', result)
    return result


def main():
    from leaneval.feedback_broker import request
    job = Path(os.environ.get('LEANEVAL_RESEARCH_JOB', str(Path.cwd().resolve().parent)))
    result = request(job, 'incremental', json.dumps(sys.argv[1:] or ['Submission']), 86400)
    print(json.dumps(result))
    return int(result.get('exitcode') != 0 or result.get('timed_out', False))


if __name__ == '__main__':
    raise SystemExit(main())
