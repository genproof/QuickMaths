"""Inspect an incremental target using the controller's isolated compiler.

The model-writable incremental cache never supplies an authenticated type.
Optional inspection spends only the unused allowance of the parent check.
"""
from pathlib import Path
import base64
import hashlib
import json
import math
import time
import uuid

from leaneval.artifacts import digest
from leaneval.verifier import atomic_json

TRANSPORT_GRACE = 30
MAX_COMPILER_SECONDS = 30


def capture(job, result, modules, files, receipts, deadline):
    from leaneval import auto_residual, worker, axiom_audit
    from leaneval.checkpoint_contract import safe
    from leaneval.contract_io import load as load_contract
    from leaneval.type_diagnostics import exact_types
    if (result.get('exitcode') != 0 or result.get('timed_out') or
            not result.get('compiler_invoked') or not modules):
        return None
    remaining = deadline - time.monotonic()
    allowance = min(MAX_COMPILER_SECONDS, math.floor(remaining - TRANSPORT_GRACE - 1))
    if allowance < 1:
        return {'status':'skipped_insufficient_parent_allowance','accepted':False}
    note = load_contract(job)
    module = note.get('module') if note.get('module') in modules else modules[-1]
    inventory = {n:hashlib.sha256(data).hexdigest() for n,data in files.items()}
    if module.replace('.', '/')+'.lean' not in inventory:
        return None
    item = auto_residual.selection(job, module, inventory, note=note)
    if not item:
        return None
    root = safe(Path(job)/'research-incremental-type-probes')
    path = safe(root/(item['key']+'.json'))
    if path.exists():
        return {'status':'already_attempted','path':str(path),'accepted':False}
    if worker.proof_files(Path(job)/'workspace') != files:
        return {'status':'skipped_source_changed','accepted':False}
    # load_config rejects unsafe paths and ensures the compiler is separate from
    # the model workspace. Never point it at .incremental or import its artifacts.
    cfg = worker.load_config(Path(job)/'worker.json')
    cfg = dict(cfg, timeout_seconds=allowance)
    cfg['remote_agent'] = str(Path(__file__).resolve().parents[1])
    child = uuid.uuid4().hex
    remote_cfg = {k:cfg[k] for k in ('remote_workspace','remote_workbench',
                                    'remote_agent','timeout_seconds','problem_id')}
    request = {'action':'inspect','query':module+':'+item['target'],'check_id':child,
               'config':remote_cfg,'generation_receipts':receipts,
               'files':{n:base64.b64encode(data).decode() for n,data in files.items()}}
    # Request serialization/setup also belongs to the parent's wall allowance.
    if deadline-time.monotonic() < allowance+TRANSPORT_GRACE+0.5:
        return {'status':'skipped_insufficient_parent_allowance','accepted':False}
    begin=time.monotonic(); started_at=time.time()
    inspected=worker.request_remote(cfg,request)
    inspected.update(action='inspect',query=request['query'],step_id=child,
        source_files=inventory,source_sha256=digest(inventory),accepted=False,
        started_at=started_at,finished_at=time.time(),elapsed_seconds=time.monotonic()-begin,
        parent_incremental_step=result['step_id'],compiler_allowance_seconds=allowance)
    inspected['source_current']=(worker.proof_files(Path(job)/'workspace')==files and
        inspected.get('checked_files')==inventory and inspected.get('checked_files_unchanged') is True)
    inspected['outcome_class']=worker.classify_outcome(inspected)
    if inspected.get('phase')=='declaration_inspection':
        inspected['exact_types']=exact_types(inspected.get('stdout',''),item['target'])
        inspected['axiom_audit']=axiom_audit.fold(inspected.get('stdout',''),[item['target']])
    automatic=auto_residual.commit(job,inspected)
    if automatic:inspected['automatic_residual']=automatic
    record={'schema_version':1,'key':item['key'],'parent_step_id':result['step_id'],
        'accepted':False,'status':'captured' if automatic and automatic['status']=='captured' else 'unavailable',
        'target':item['target'],'module':module,'inspection':inspected}
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_json(path,record)
    return {'status':record['status'],'path':str(path),
        'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'step_id':child,
        'elapsed_seconds':inspected['elapsed_seconds'],'compiler_allowance_seconds':allowance,
        'source_current':inspected['source_current'],'accepted':False}
