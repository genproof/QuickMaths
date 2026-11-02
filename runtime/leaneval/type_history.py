"""Retain authentic historical type observations without making them current."""
from pathlib import Path
import json
import hashlib
import os
import re
from leaneval.artifacts import digest
from leaneval.checkpoint_contract import safe, evidence_kind

LIMIT = 8
SCAN_LIMIT = 4096
READ_LIMIT = 16
MAX_BYTES = 256 * 1024
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")
HEX = re.compile('[a-f0-9]{64}')


def checked(value):
    if not isinstance(value,dict):return None
    fields={k:v for k,v in value.items() if k!='receipt_digest'}
    try:
        if value.get('receipt_digest')!=digest(fields):return None
        if (type(value.get('schema_version')) is not int or value['schema_version']!=1 or value.get('accepted') is not False
                or not isinstance(value.get('job'),str) or not Path(value['job']).is_absolute()
                or not isinstance(value.get('step_id'),str) or not re.fullmatch('[a-f0-9]{32}',value['step_id'])
                or not isinstance(value.get('module'),str) or not NAME.fullmatch(value['module'])
                or not (value['module']=='Submission' or value['module'].startswith('Submission.'))
                or not isinstance(value.get('target'),str) or not NAME.fullmatch(value['target'])
                or not isinstance(value.get('remaining_type'),str) or not 0<len(value['remaining_type'])<=100000
                or not isinstance(value.get('source_sha256'),str) or not HEX.fullmatch(value['source_sha256'])):
            return None
        if value.get('kind')=='controller-inspected-type':
            extra={'kind':'controller-inspected-type'}
        elif value.get('kind')=='controller-inspected-partial-type':
            extra=evidence_kind({**value,'kind':'partial-compiler-type'})
        else:return None
        if set(fields)!={'schema_version','job','step_id','module','target','remaining_type','source_sha256','accepted',*extra}:
            return None
    except (TypeError,ValueError):return None
    return value


def local_records(job):
    records=[]; invalid=0; truncated=False
    directory=safe(Path(job)/'research-type-receipts')
    if directory.is_dir():
        entries=[]
        with os.scandir(directory) as stream:
            for n,entry in enumerate(stream):
                if n>=SCAN_LIMIT:truncated=True;break
                if entry.name.endswith('.json') and entry.is_file(follow_symlinks=False):
                    entries.append((entry.stat(follow_symlinks=False).st_mtime,entry.path))
        for _,name in sorted(entries,reverse=True)[:READ_LIMIT]:
            try:
                path=safe(Path(name))
                if path.stat().st_size>MAX_BYTES:raise ValueError('Oversized type receipt')
                value=checked(json.loads(path.read_bytes()))
                if (not value or value['job']!=str(job) or path.name!=value['step_id']+'.json'):
                    raise ValueError('Unbound type receipt')
                records.append(value)
            except (OSError,ValueError,TypeError):invalid+=1
    return records, invalid, truncated


def bound_parent(job, inherited):
    """Only the direct parent in a validated controller checkpoint is eligible."""
    name=inherited.get('parent_job')
    if not isinstance(name,str) or not name:
        return None, {'status':'not_declared'}
    try:
        parent=safe(Path(name))
        if parent==Path(job) or str(parent.resolve())!=name:
            raise ValueError('Parent must be a different canonical job')
        def config(path):
            path=safe(path/'config.json');before=path.stat()
            if not path.is_file() or before.st_size>32*1024*1024:
                raise ValueError('Missing or oversized job configuration')
            raw=path.read_bytes();after=path.stat()
            identity=lambda s:(s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            if path.is_symlink() or identity(before)!=identity(after) or len(raw)!=before.st_size:
                raise ValueError('Job configuration changed while reading')
            data=json.loads(raw)
            if not isinstance(data,dict):raise ValueError('Invalid job configuration')
            return data,hashlib.sha256(raw).hexdigest()
        child,child_sha=config(Path(job));prior,parent_sha=config(parent)
        problem=inherited.get('problem_id');revision=inherited.get('statement_revision')
        if (not isinstance(problem,str) or not problem or
                child.get('problem_id')!=problem or prior.get('problem_id')!=problem or
                child.get('statement_revision')!=revision or prior.get('statement_revision')!=revision):
            raise ValueError('Parent and child task identity differ from checkpoint')
        return parent, {'status':'bound','parent_job':name,
            'parent_config_sha256':parent_sha,'child_config_sha256':child_sha}
    except (OSError,ValueError,TypeError):
        return None, {'status':'unavailable','parent_job':name}


def collect(job,inherited):
    records,invalid,truncated=local_records(job)
    parent_history={'status':'not_requested'}
    if inherited:
        try:
            valid=digest({k:v for k,v in inherited.items() if k!='checkpoint_digest'})==inherited.get('checkpoint_digest')
        except (TypeError,ValueError):valid=False
        if valid:
            history=inherited.get('historical_types') or {}
            candidates=history.get('receipts',[]) if isinstance(history,dict) else []
            # Old checkpoints already carry authenticated current type receipts.
            current=inherited.get('type_receipts') or {}
            if isinstance(current,dict):
                for value in list(current.values())[:LIMIT]:
                    if checked(value) and value['source_sha256']==digest(inherited.get('source_files',{})):
                        records.append(value)
                    else:invalid+=1
            if isinstance(candidates,list):
                for value in candidates[:LIMIT]:
                    if checked(value):records.append(value)
                    else:invalid+=1
            parent,parent_history=bound_parent(job,inherited)
            if parent is not None:
                try:
                    extra,bad,cut=local_records(parent)
                    records.extend(extra);invalid+=bad;truncated|=cut
                    parent_history.update(status='read',records_found=len(extra))
                except (OSError,ValueError,TypeError):
                    parent_history['status']='unavailable'
        else:invalid+=1
    unique={}
    for value in records:
        unique.setdefault((value['job'],value['step_id']),value)
    return {'schema_version':1,'receipts':list(unique.values())[:LIMIT],
            'retained_limit':LIMIT,'scan_truncated':truncated,
            'invalid_records_ignored':invalid,'parent_history':parent_history,'accepted':False}


def brief(history,source,exclude_steps=()):
    rows=[]
    for receipt in history.get('receipts',[])[:LIMIT]:
        value=checked(receipt)
        if not value or value['step_id'] in exclude_steps:continue
        text=value['remaining_type'];truncated=len(text)>2000
        rows.append({'target':value['target'],'module':value['module'],
            'recorded_type':text[:2000], 'type_truncated':truncated,
            'type_sha256':hashlib.sha256(text.encode()).hexdigest(), 'receipt_job':value['job'],'step_id':value['step_id'],
            'receipt_digest':value['receipt_digest'],'source_sha256':value['source_sha256'],
            'source_matches_current':value['source_sha256']==digest(source),
            'partial_elaboration':value['kind']=='controller-inspected-partial-type',
            'current_contract':False,'accepted':False})
        if len(rows)>=3:break
    if not rows:return ''
    return ('\nHistorical compiler type observations (not current residual contracts):\n'+
        json.dumps(rows,ensure_ascii=False)+
        '\nUse these to recover previous declarations and checks. A source change requires a fresh '
        'type check before relying on applicability; historical types do not establish current '
        'axioms, proof progress, or task acceptance. Full records remain in controller type receipts '
        'or research-inherited.json historical_types.\n')
