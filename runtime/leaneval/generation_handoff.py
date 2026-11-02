"""Carry declared generation contracts and exact inputs across proof continuations."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from leaneval import generation
from leaneval.artifacts import digest
from leaneval.verifier import atomic_json

MAX_FILE=8*1024*1024
MAX_TOTAL=32*1024*1024
MAX_STATE=64*1024*1024
MAX_INPUTS=4096


def name(value):
    if not isinstance(value,str) or not value or '\\' in value:
        raise ValueError('Invalid generation input path')
    p=PurePosixPath(value)
    if not p.parts or p.is_absolute() or str(p)!=value or '..' in p.parts or p.parts[0] in {'.git','.lake','.tools','.generation','.feedback'}:
        raise ValueError('Unsafe generation input path')
    return value


def read(path,limit):
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Symlinked generation handoff source')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>limit:
            raise ValueError('Generation handoff source exceeds bound or is not regular')
        data=stream.read(limit+1);after=os.fstat(stream.fileno())
    keys=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if len(data)>limit or keys(before)!=keys(after) or keys(after)!=keys(path.stat()):
        raise ValueError('Generation handoff source changed')
    return data,before.st_mode&0o777


def validate(value,files,artifact):
    if (not isinstance(value,dict) or value.get('schema')!=1 or
            value.get('artifact_digest')!=artifact or value.get('accepted') is not False):
        raise ValueError('Generation handoff identity mismatch')
    receipts=value.get('receipts');inputs=value.get('inputs')
    if not isinstance(receipts,list) or not receipts or len(receipts)>MAX_INPUTS or not isinstance(inputs,dict) or len(inputs)>MAX_INPUTS:
        raise ValueError('Invalid generation handoff inventory')
    generation.validate_bytes(files,receipts)
    expected={}
    for r in receipts:
        if not re.fullmatch('[0-9a-f]{32}',r.get('id','')) or not isinstance(r.get('inputs'),dict) or not r['inputs']:
            raise ValueError('Invalid generation receipt identity')
        for path,h in r['inputs'].items():
            name(path)
            if path in expected and expected[path]!=h:raise ValueError('Conflicting generation input hashes')
            expected[path]=h
    if set(inputs)!=set(expected):raise ValueError('Generation input inventory differs from receipts')
    decoded={};total=0
    for path,item in inputs.items():
        name(path)
        if not isinstance(item,dict):raise ValueError('Invalid captured generation input')
        data=base64.b64decode(item['data'],validate=True);total+=len(data)
        mode=item.get('mode')
        if len(data)>MAX_FILE or total>MAX_TOTAL or type(mode) is not int or not 0<=mode<=0o777:
            raise ValueError('Generation input exceeds bound or has invalid mode')
        if generation.sha(data)!=expected[path] or item.get('sha256')!=expected[path]:
            raise ValueError('Generation input hash mismatch')
        decoded[path]=(data,mode)
    if any(str(parent) in inputs for path in inputs for parent in PurePosixPath(path).parents if str(parent)!='.'):
        raise ValueError('Overlapping generation input paths')
    return decoded


def build(root,files,receipts,artifact):
    if not receipts:return None
    if len(receipts)>MAX_INPUTS:raise ValueError('Too many generation receipts')
    inputs={};total=0
    for r in receipts:
        for path,h in r['inputs'].items():
            name(path)
            if path in inputs:
                if inputs[path]['sha256']!=h:raise ValueError('Conflicting generation inputs')
                continue
            if len(inputs)>=MAX_INPUTS:raise ValueError('Too many generation inputs')
            data,mode=read(root/path,MAX_FILE);total+=len(data)
            if total>MAX_TOTAL or generation.sha(data)!=h:raise ValueError('Generation input changed or exceeds handoff bound')
            inputs[path]={'sha256':h,'data':base64.b64encode(data).decode('ascii'),'mode':mode}
    value={'schema':1,'artifact_digest':artifact,'receipts':receipts,'inputs':inputs,'accepted':False}
    validate(value,files,artifact)
    if len(json.dumps(value).encode())>MAX_STATE:raise ValueError('Generation handoff state exceeds bound')
    return value


def capture(root,directory,files,receipts,artifact):
    value=build(root,files,receipts,artifact)
    if value is None:return None
    atomic_json(directory/'generation-state.json',value)
    return digest(value)


def load(directory,snapshot,files):
    expected=snapshot.get('generation_state_digest')
    if expected:
        data,_=read(directory/'generation-state.json',MAX_STATE);value=json.loads(data)
        if digest(value)!=expected:raise ValueError('Generation handoff digest mismatch')
    else:
        expected=snapshot.get('generation_receipts_digest')
        if not expected:return None
        data,_=read(directory/'generation-receipts.json',MAX_STATE)
        receipts=json.loads(data)['receipts']
        if digest(receipts)!=expected:raise ValueError('Legacy generation receipt digest mismatch')
        # A legacy snapshot has hashes but no captured inputs. Recover only
        # from exact matching bytes still available in its declared parent.
        value=build(Path(snapshot['parent_job'])/'workspace',files,receipts,snapshot['artifact_digest'])
        if value is None:return None
    validate(value,files,snapshot['artifact_digest'])
    return value


def restore(root,value,files,artifact):
    if value is None:return
    decoded=validate(value,files,artifact)
    folder=root/'.generation'
    if folder.is_symlink() or (folder.exists() and any(folder.iterdir())):
        raise ValueError('Continuation already contains generation metadata')
    for path,(data,mode) in decoded.items():
        target=root/path
        if any(p.is_symlink() for p in (target,*target.parents)):
            raise ValueError('Symlink at generation restore destination')
        if target.exists() and (not target.is_file() or read(target,MAX_FILE)[0]!=data):
            raise ValueError('Generation input conflicts with prepared workspace: '+path)
        if any(p.exists() and not p.is_dir() for p in target.parents):
            raise ValueError('Generation input parent is not a directory')
    for path,(data,mode) in decoded.items():
        target=root/path
        if not target.exists():
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as stream:stream.write(data)
            target.chmod(mode)
    folder.mkdir(exist_ok=True);index={}
    for r in value['receipts']:
        filename=r['id']+'.json';atomic_json(folder/filename,r);index[r['output']]=filename
    atomic_json(folder/'index.json',index)
    generation.validate(root,files)
