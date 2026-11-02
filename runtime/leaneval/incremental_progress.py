"""Validate diagnostic progress against an immutable controller-side result."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

FIELDS=('step_id','action','query','source_files','source_sha256','source_current',
        'checked_files','checked_files_unchanged','configuration_unchanged',
        'compiler_invoked','exitcode','timed_out','outcome_class','started_at','finished_at',
        'tool_identity','native_output','diagnostics','exact_types')


def read(path,limit):
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('symlinked incremental evidence')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        a=os.fstat(f.fileno())
        if not stat.S_ISREG(a.st_mode) or a.st_size>limit:raise ValueError('invalid incremental evidence file')
        data=f.read(limit+1);b=os.fstat(f.fileno())
    identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if len(data)>limit or identity(a)!=identity(b) or identity(b)!=identity(path.stat()):
        raise ValueError('incremental evidence changed during read')
    return data


def validate(job,feedback):
    try:
        step=feedback.get('step_id')
        if not isinstance(step,str) or not re.fullmatch('[0-9a-f]{32}',step):
            raise ValueError('missing incremental invocation identity')
        raw=read(job/'research-incremental-checks'/step/'result.json',32*1024*1024)
        record=json.loads(raw)
        if (not isinstance(record,dict) or record.get('schema_version')!=1 or
                record.get('job')!=str(job.resolve()) or record.get('step_id')!=step or record.get('accepted') is not False):
            raise ValueError('incremental controller identity mismatch')
        if record.get('config_sha256')!=hashlib.sha256(read(job/'config.json',4*1024*1024)).hexdigest():
            raise ValueError('incremental configuration changed')
        actual=record.get('result')
        if not isinstance(actual,dict) or any(actual.get(k)!=feedback.get(k) for k in FIELDS):
            raise ValueError('incremental feedback differs from controller result')
        if (actual.get('action')!='incremental' or actual.get('compiler_invoked') is not True or
                actual.get('source_current') is not True or actual.get('checked_files_unchanged') is not True or
                actual.get('configuration_unchanged') is not True or actual.get('timed_out') is not False or
                type(actual.get('exitcode')) is not int or actual['exitcode'] not in (0,1) or
                actual.get('outcome_class') not in {'check_succeeded','lean_diagnostic'}):
            raise ValueError('not a completed source-bound incremental diagnostic')
        a,b=actual.get('started_at'),actual.get('finished_at')
        if any(type(x) not in (int,float) or not math.isfinite(x) for x in (a,b)) or not 0<a<=b:
            raise ValueError('invalid incremental timestamps')
        compiler=(actual.get('tool_identity') or {}).get('incremental_compiler') or {}
        binaries=compiler.get('binaries') or {};config=compiler.get('configuration') or {}
        if (not isinstance(compiler.get('toolchain'),str) or not compiler['toolchain'] or
                not isinstance(compiler.get('prefix'),str) or not Path(compiler['prefix']).is_absolute() or
                set(binaries)!={'lean','lake'} or not config or
                any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for h in [*binaries.values(),*config.values()])):
            raise ValueError('missing incremental tool identity')
        return True,'controller_result_matched'
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as error:
        return False,str(error)
