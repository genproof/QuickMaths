"""A durable, explicitly budgeted pre-model dependency check, at most two calls.

A timeout can authorize another native check only after the worker has sealed
an interrupted cache. A saved running intent is uncertain, never permission to
resubmit. No result here grants theorem acceptance.
"""
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time

from leaneval.artifacts import digest
from leaneval.verifier import atomic_json, tool_identity


def budget(dep, slice_seconds):
    total = dep.get('compile_total_timeout_seconds', slice_seconds)
    if type(total) is not int or not slice_seconds <= total <= 1200:
        raise ValueError('consumer total compile timeout must be between its slice and 1200 seconds')
    return total


def resumable(result, source_sha):
    cache = result.get('build_cache') or {}
    return (result.get('outcome_class') == 'timeout' and result.get('timed_out') is True
            and result.get('exitcode') == -9 and result.get('source_current') is True
            and result.get('source_sha256') == source_sha
            and cache.get('sealed') is True
            and cache.get('sealed_outcome') == 'interrupted_module_build'
            and cache.get('requires_native_rebuild') is True
            and type(cache.get('progress_nodes')) is int and cache['progress_nodes'] > 0)


def environment_identity(cfg):
    return {"runtime":cfg["remote_agent"], "workbench":cfg["remote_workbench"],
            "tools":tool_identity(Path(cfg["remote_workbench"]))}


def run(job, cfg, action, query, slice_seconds, total_seconds, *, feedback=None, clock=time.monotonic):
    from leaneval import worker
    from leaneval.checkpoint_contract import safe
    began = clock()
    job = Path(job)
    if type(slice_seconds) is not int or not 1 <= slice_seconds <= 600:
        raise ValueError('invalid dependency check slice')
    budget({'compile_total_timeout_seconds': total_seconds}, slice_seconds)
    source = {n:hashlib.sha256(v).hexdigest() for n,v in worker.proof_files(job/'workspace').items()}
    source_sha = digest(source)
    config_sha = hashlib.sha256((job/'config.json').read_bytes()).hexdigest()
    identity = {'job':str(job.resolve()), 'config_sha256':config_sha, 'source_sha256':source_sha,
                'action':action, 'query':query, 'slice_seconds':slice_seconds, 'total_seconds':total_seconds,
                'environment':environment_identity(cfg)}
    key = digest(identity)
    root = safe(job/'research-dependency-batches');root.mkdir(exist_ok=True,mode=0o700)
    path = safe(root/(key+'.json')); lock_path = safe(root/(key+'.lock'))
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None, {'status':'busy','path':str(path),'accepted':False}
        if path.exists():
            if path.stat().st_size > 16*1024*1024:raise ValueError('oversized dependency batch')
            record=json.loads(path.read_bytes())
            if record.get('identity') != identity:raise ValueError('dependency batch identity mismatch')
        else:
            record={'schema_version':1,'identity':identity,'started_at':began,
                    'status':'ready','attempts':[],'accepted':False,
                    'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    'clock_kind':'monotonic', 'created_at':time.time()}
        def last_result():
            return next((a['result'] for a in reversed(record['attempts']) if 'result' in a),None)
        def view():
            return {'status':record['status'],'path':str(path),'source_sha256':source_sha,
                    'started_at':record['started_at'],'total_seconds':total_seconds,
                    'attempt_count':len(record['attempts']),'accepted':False}
        if record.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip() or clock() < record['started_at']:
            record['status']='budget_exhausted';atomic_json(path,record)
        if record['status'] in {'running','complete','stopped','budget_exhausted','source_changed'}:
            # A running marker survives a controller crash. Its remote process
            # might still run; even a dead caller is not proof of termination.
            return last_result(), view()
        if record['status'] not in {'ready','retry_ready'}:raise ValueError('invalid dependency batch state')
        caller = worker.feedback if feedback is None else feedback
        while len(record['attempts']) < 2:
            remaining = math.floor(total_seconds - max(0,clock()-record['started_at']))
            allowance = min(slice_seconds, cfg['timeout_seconds'], remaining)
            if allowance < 1:
                record['status']='budget_exhausted';atomic_json(path,record);return last_result(),view()
            current={n:hashlib.sha256(v).hexdigest() for n,v in worker.proof_files(job/'workspace').items()}
            if current != source:
                record['status']='source_changed';atomic_json(path,record);return last_result(),view()
            record['status']='running'
            record['attempts'].append({'started_at':clock(),'allowance_seconds':allowance})
            atomic_json(path,record)
            # Let exceptions retain the uncertain running intent. They never
            # authorize a fresh attempt or reset this source's total budget.
            result=caller({**cfg,'timeout_seconds':allowance},action,query,_controller=True)
            record['attempts'][-1].update(finished_at=clock(),result=result)
            if (result.get('outcome_class') == 'check_succeeded' and result.get('exitcode') == 0
                    and result.get('source_current') is True and result.get('source_sha256') == source_sha):record['status']='complete'
            elif len(record['attempts']) < 2 and resumable(result,source_sha):record['status']='retry_ready'
            else:record['status']='stopped'
            atomic_json(path,record)
            if record['status']!='retry_ready':return result,view()
        return last_result(),view()
