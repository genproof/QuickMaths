"""Capture declared residual types from already completed isolated builds.

Worker notes choose the declaration and next check. Only a real compiler probe
supplies its type; no mathematical progress or proof acceptance is inferred.
"""
from pathlib import Path
import re
from leaneval.artifacts import digest
from leaneval.contract_io import load as load_contract
from leaneval.verifier import atomic_json

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def selection(job, module, source, note=None):
    if note is None:note = load_contract(job)
    target, plan = note.get('target'), note.get('next_check')
    if (not isinstance(target,str) or not NAME.fullmatch(target)
            or not isinstance(plan,str) or not plan.strip() or len(plan)>2000
            or not isinstance(module,str) or not NAME.fullmatch(module)
            or not (module=='Submission' or module.startswith('Submission.'))):
        return None
    key=digest({'schema':1,'target':target,'module':module,'source':source})
    return {'target':target,'module':module,'key':key,'source_sha256':digest(source)}


def request(job, action, query, source):
    if action!='build' or not query or not source:return None
    item=selection(job,query,source)
    if not item:return None
    from leaneval.checkpoint_contract import safe
    path=safe(Path(job)/'research-auto-residuals'/(item['key']+'.json'))
    if path.exists():return None
    return item


def commit(job, result, requested=None):
    from leaneval.checkpoint_contract import safe,seal
    from leaneval.type_diagnostics import exact_types
    from leaneval import axiom_audit
    if result.get('source_current') is not True or result.get('checked_files_unchanged') is not True:
        return None
    source=result.get('source_files') or {}
    probe=result.get('residual_probe')
    if requested and isinstance(probe,dict):
        item=requested
        if probe.get('target')!=item['target'] or probe.get('module')!=item['module']:return None
        derived={**result,**probe,'action':'inspect','query':item['module']+':'+item['target'],
                 'scope':'type-and-axiom-diagnostic','phase':'declaration_inspection',
                 'declaration':item['target'],'invocation_id':result['step_id']}
        if len(probe.get('stdout',''))>1024*1024:return None
        derived['exact_types']=exact_types(probe.get('stdout',''),item['target'])
        derived['axiom_audit']=axiom_audit.fold(probe.get('stdout',''),[item['target']])
        if probe.get('source_exact') is not True:derived['exitcode']=None
    elif result.get('action')=='inspect' and result.get('phase')=='declaration_inspection':
        item=selection(job,result.get('checked_module'),source)
        if not item or item['target']!=result.get('declaration'):return None
        derived=result
    else:return None
    root=safe(Path(job)/'research-auto-residuals');root.mkdir(mode=0o700,exist_ok=True)
    receipt=seal(job,derived)
    record={'schema_version':1,'key':item['key'],'target':item['target'],'module':item['module'],
            'source_sha256':digest(source),'step_id':result['step_id'],'accepted':False,
            'status':'captured' if receipt else 'unavailable',
            'probe_exitcode':derived.get('exitcode'),'probe_timed_out':derived.get('timed_out',False)}
    if receipt:
        record.update(type_receipt=receipt,remaining_type=derived['exact_types'][0])
    atomic_json(safe(root/(item['key']+'.json')),record)
    if receipt:atomic_json(safe(Path(job)/'research-auto-residual.json'),record)
    return record


def current(job, note, source):
    from leaneval.checkpoint_contract import safe,load,validate
    from leaneval.frontier import policy,read
    path=safe(Path(job)/'research-auto-residual.json')
    if not path.exists():return None
    try:record=load(path)
    except (OSError,ValueError,TypeError):return None
    item=selection(job,record.get('module'),source,note=note)
    if (not item or record.get('schema_version')!=1 or record.get('status')!='captured'
            or record.get('accepted') is not False or record.get('key')!=item['key']
            or record.get('target')!=note.get('target') or record.get('source_sha256')!=digest(source)):
        return None
    value={'schema_version':1,'target':record['target'],'module':record['module'],
           'remaining_type':record.get('remaining_type'),'source_files':source,
           'source_sha256':digest(source),'step_id':record.get('step_id'),
           'invocation_id':record.get('step_id'),'kind':'compiler-type-and-diagnostic',
           'owner':policy(read(Path(job)/'config.json'))['owner'],
           'next_check':note['next_check'],'next_check_verified':False,
           'manual_remaining_type':str(note.get('remaining_type',''))[:5000],
           'capture_origin':'automatic_after_scoped_build','accepted':False}
    return value if validate(job,value,source)[0] else None
