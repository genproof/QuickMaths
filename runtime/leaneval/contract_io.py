"""Bound and preserve model-authored contract metadata independently of proofs."""
from pathlib import Path
import hashlib
import json
import os
import stat
import tempfile

MAX_READ = 32 * 1024 * 1024
MAX_FIELD = 200_000
MAX_BRIEF = 512_000
FIELDS = ('schema_version','target','module','remaining_type','owner','next_check',
          'source_sha256','step_id','invocation_id','kind','candidate_checked','module_exitcode',
          'partial_probe_sha256','controller_type_receipt','type_receipt_job','accepted',
          'blocked_by','edit_scope','prerequisites','axiom_audit','source_files')


def safe(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('symlink or non-absolute metadata path')
    return path


def _archive(directory, raw, digest):
    directory = safe(directory)
    target = safe(directory/('research-contract-'+digest+'.json'))
    directory.mkdir(parents=True,exist_ok=True)
    if target.exists():
        if target.read_bytes()!=raw: raise ValueError('Contract archive digest collision')
    else:
        fd,name=tempfile.mkstemp(prefix='.contract-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw)
            os.replace(name,target)
        finally:
            if os.path.exists(name):os.unlink(name)
    return str(target)


def read(job, archive=None):
    path = Path(job)/'workspace/research-contract.json'
    info = {'path':str(path),'status':'missing','accepted':False}
    raw = None
    try:
        safe(path)
        fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            st=os.fstat(stream.fileno())
            info['bytes']=st.st_size
            if not stat.S_ISREG(st.st_mode):raise ValueError('not a regular metadata file')
            if st.st_size>MAX_READ:raise ValueError('metadata exceeds bounded read limit')
            raw=stream.read(MAX_READ+1)
            if len(raw)>MAX_READ:raw=None;raise ValueError('metadata grew beyond bounded read limit')
    except FileNotFoundError:
        return {},info
    except (OSError,ValueError) as exc:
        info.update(status='unavailable',error=str(exc))
        return {'metadata_error':info['error']},info
    info.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    if archive is not None:info['archive']=_archive(archive,raw,info['sha256'])
    try:
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('contract is not an object')
    except (ValueError,UnicodeError,RecursionError) as exc:
        info.update(status='invalid_json',error=str(exc)[:1000])
        return {'metadata_error':info['error']},info
    compact={};omitted=[];used=0
    for key in FIELDS:
        if key not in value:continue
        item=value[key]
        size=len(json.dumps(item,ensure_ascii=False).encode())
        # The exact source inventory is used only for comparison and removed from
        # compact typed checkpoints. Every other field has its own small bound.
        if key!='source_files' and (size>MAX_FIELD or used+size>MAX_BRIEF):
            omitted.append(key);continue
        compact[key]=item
        if key!='source_files':used+=size
    omitted.extend(k for k in value if k not in FIELDS)
    info.update(status='read',omitted_fields=sorted(omitted)[:100],omitted_field_count=len(omitted))
    return compact,info


def load(job):
    return read(job)[0]
