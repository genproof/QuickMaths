"""Bounded loading of controller-owned proof-transfer receipts.

Large receipts receive the same complete byte/hash/schema checks as small ones.
They are never treated as proof acceptance or skipped when malformed.
"""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import re
import stat

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 100_000


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate transfer receipt key: '+key[:100])
        result[key]=value
    return result


def _identity(st):
    return st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns


def load(job, path, expected_sha):
    job,path=Path(job),Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Unsafe controller transfer receipt path')
    if path.parent.resolve()!=(job/'research-transfers').resolve():
        raise ValueError('transfer receipt must be owned by this job controller')
    if not isinstance(expected_sha,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha):
        raise ValueError('Invalid expected transfer receipt hash')
    fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_BYTES:
            raise ValueError('Missing, nonregular, or oversized controller transfer receipt')
        raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES or _identity(os.fstat(stream.fileno()))!=_identity(before):
            raise ValueError('Controller transfer receipt changed while reading')
    if _identity(path.stat(follow_symlinks=False))!=_identity(before):
        raise ValueError('Controller transfer receipt replaced while reading')
    actual=hashlib.sha256(raw).hexdigest()
    if actual!=expected_sha:raise ValueError('Controller transfer receipt digest mismatch')
    try:receipt=json.loads(raw,object_pairs_hook=_unique)
    except (UnicodeError,RecursionError) as exc:raise ValueError('Malformed transfer receipt') from exc
    if not isinstance(receipt,dict) or not isinstance(receipt.get('files'),dict):
        raise ValueError('Invalid controller transfer receipt')
    if len(receipt['files'])>MAX_FILES:raise ValueError('Too many transferred files in one receipt')
    for name in receipt['files']:
        p=PurePosixPath(name)
        if ('\x00' in name or p.is_absolute() or '..' in p.parts or p.as_posix()!=name or
                not (name=='NOTES.md' or name=='Submission.lean' or
                     name.startswith('Submission/') and name.endswith('.lean'))):
            raise ValueError('Unsafe transferred source name')
    return receipt,actual
