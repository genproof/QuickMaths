"""Resolve local active time after verified quiescence, never vendor token usage."""
import hashlib
import json
import math
import os
from pathlib import Path
import stat


def number(value):
    return type(value) in (int,float) and math.isfinite(value) and value >= 0


def read(path):
    path=Path(path)
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Symlinked closure evidence')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>8*1024*1024:
            raise ValueError('Nonregular or oversized closure evidence')
        raw=stream.read(8*1024*1024+1)
        after=os.fstat(stream.fileno())
    stamp=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if stamp(before)!=stamp(after) or stamp(after)!=stamp(path.stat(follow_symlinks=False)):
        raise ValueError('Closure evidence changed during read')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Expected closure evidence object')
    return value,hashlib.sha256(raw).hexdigest()


def resolve_local_time(job,state,final,quiescence):
    """Only called after finalization independently proves PID/group/workspace quiet.

    Keep the native ledger unchanged. The returned controller snapshot can settle
    a lineage's local active seconds while its token/invoice uncertainty persists.
    """
    original=dict(state['accounted'])
    unknown=dict(status='unresolved',scope='cumulative local active model seconds; not provider billing')
    try:
        if (quiescence.get('pid_absent') is not True or quiescence.get('group_absent') is not True or
            quiescence.get('basis')!='terminal_result+kernel_absence+same_uid_proc_scan'):
            raise ValueError('No verified quiescence')
        attempt_id=final['attempt_id']
        terminal,terminal_sha=read(Path(job)/'termination.json')
        result,result_sha=read(Path(job)/'result.json')
        if (terminal.get('attempt_id')!=attempt_id or result.get('attempt_id')!=attempt_id or
            terminal.get('generation')!=final['generation'] or final['generation']!=state['generation'] or
            terminal.get('wait_completed') is not True or
            type(terminal.get('exit_code')) is not int or terminal['exit_code']!=final['result']['exit_code'] or
            terminal.get('stop_reason')!=final['result']['reason'] or
            terminal.get('stop_reason') not in {'token_limit','wall_limit'} or
            result.get('stop_reason')!=terminal['stop_reason'] or
            result.get('exit_code')!=terminal['exit_code'] or
            result.get('run_id')!=state['identity']['config']['run_id'] or
            result.get('configuration_confirmed') is not True or
            terminal.get('pid')!=quiescence.get('pid') or
            terminal.get('process_group')!=quiescence.get('process_group')):
            raise ValueError('Terminal identity mismatch')
        attempts=state['attempts']
        current=attempts[attempt_id]
        if (current['status']!='ambiguous' or current.get('usage')!=final['usage'] or
            current['reservation']['generation']!=final['generation'] or
            current['reservation']['checkpoint_hash']!=final['checkpoint_hash']):
            raise ValueError('Current attempt does not match terminal usage')
        elapsed=final['usage'].get('wall_seconds')
        if (not number(elapsed) or result.get('elapsed_seconds')!=elapsed or
            result.get('observed_tokens')!=final['usage'].get('tokens')):
            raise ValueError('Active time or token observation differs')
        invocations=result.get('invocations')
        if (not isinstance(invocations,list) or not invocations or
            len(invocations)!=terminal.get('invocations') or
            any(not number(i.get('started_at')) or not number(i.get('ended_at')) or
                i['ended_at']<i['started_at'] or type(i.get('exit_code')) is not int for i in invocations)):
            raise ValueError('Incomplete invocation closure')
        total=elapsed
        for name,attempt in attempts.items():
            if name==attempt_id:continue
            value=(attempt.get('usage') or {}).get('wall_seconds')
            if attempt['status']!='settled' or not number(value):
                raise ValueError('An earlier attempt has unresolved local time')
            total+=value
        if not number(original.get('wall_seconds')) or total>original['wall_seconds']:
            raise ValueError('Resolved time exceeds native booked time')
        original['wall_seconds']=total
        return original,dict(status='resolved',scope=unknown['scope'],basis='terminal local timings after verified process quiescence',
            native_booked_wall_seconds=state['accounted']['wall_seconds'],local_active_seconds=total,
            terminal_sha256=terminal_sha,result_sha256=result_sha,attempt_id=attempt_id,
            tokens_remain_unresolved=True,invoice_remains_unresolved=True,native_ledger_unchanged=True)
    except (OSError,ValueError,KeyError,TypeError) as error:
        unknown['reason']=str(error)
        return original,unknown
