"""Retain bounded, source-conditional compiler failures across continuations.

These records describe failed checks, not impossible mathematical strategies.
They never reject admission, block a compiler request, or establish acceptance.
"""
from pathlib import Path
import hashlib
import json
import math
import os
import re

from leaneval.artifacts import digest
from leaneval.checkpoint_contract import safe
from leaneval.verifier import atomic_json

MAX_RECORDS = 16
MAX_SCAN = 4096
MAX_BYTES = 65536
HEX = r'[0-9a-f]{64}'
MAX_GOALS = 3
MAX_GOAL_BYTES = 4096


def goal_frames(result):
    """Retain reported goal text, not a reconstructed elaborator/tactic state."""
    frames = []
    source = result['source_files']
    for diagnostic in result.get('diagnostics', []):
        if not isinstance(diagnostic, dict) or diagnostic.get('severity') != 'error':
            continue
        text = diagnostic.get('message')
        filename = diagnostic.get('file')
        if (not isinstance(text, str) or text.splitlines()[:1] != ['unsolved goals'] or
                not re.search(r'(?m)^\s*⊢', text) or not isinstance(filename, str) or
                diagnostic.get('source_sha256') != result['source_sha256'] or
                type(diagnostic.get('line')) is not int or diagnostic['line'] <= 0 or
                type(diagnostic.get('column')) is not int or diagnostic['column'] < 0):
            continue
        names = [name for name in source if filename == name or filename.endswith('/'+name)]
        if len(names) != 1:
            continue
        raw = text.encode('utf-8')
        truncated = len(raw) > MAX_GOAL_BYTES
        retained = raw[-MAX_GOAL_BYTES:].decode('utf-8', errors='ignore') if truncated else text
        frames.append({'kind':'unsolved-goals-diagnostic','file':names[0],
            'file_sha256':source[names[0]], 'line':diagnostic['line'], 'column':diagnostic['column'],
            'message':retained,'message_sha256':hashlib.sha256(raw).hexdigest(),
            'truncated':truncated,'retained_region':'tail' if truncated else 'complete',
            'authority':'source-bound diagnostic text; not an elaborator checkpoint'})
        if len(frames) >= MAX_GOALS:
            break
    return frames


def checked(value):
    if not isinstance(value, dict): return None
    unsigned = {k:v for k,v in value.items() if k != 'record_digest'}
    try:
        if value.get('record_digest') != digest(unsigned): return None
    except (TypeError, ValueError): return None
    if (value.get('schema_version') != 1 or value.get('kind') != 'failed-compiler-check'
            or value.get('accepted') is not False
            or value.get('outcome_class') != 'lean_diagnostic'
            or value.get('action') not in ('build', 'check', 'inspect', 'incremental')
            or type(value.get('exitcode')) is not int or value['exitcode'] == 0
            or not isinstance(value.get('step_id'), str)
            or not re.fullmatch('[0-9a-f]{32}', value['step_id'])
            or not isinstance(value.get('job'), str)
            or not Path(value['job']).is_absolute()
            or not isinstance(value.get('query'), str) or len(value['query']) > 1000
            or not isinstance(value.get('finished_at'), (int, float))
            or isinstance(value['finished_at'], bool) or not math.isfinite(value['finished_at'])):
        return None
    for key in ('source_sha256', 'tool_identity_sha256', 'diagnostics_sha256'):
        if not isinstance(value.get(key), str) or not re.fullmatch(HEX, value[key]): return None
    errors = value.get('errors')
    if not isinstance(errors, list) or not 1 <= len(errors) <= 3: return None
    if any(not isinstance(e, str) or not e or len(e) > 1500 for e in errors): return None
    frames = value.get('reported_goals', [])
    if not isinstance(frames, list) or len(frames) > MAX_GOALS: return None
    for frame in frames:
        if not isinstance(frame, dict): return None
        name = frame.get('file')
        if (not isinstance(name, str) or '\x00' in name or '\\' in name or
                '..' in Path(name).parts or Path(name).is_absolute() or
                not (name == 'Submission.lean' or name.startswith('Submission/') and name.endswith('.lean'))):
            return None
        text = frame.get('message')
        if (frame.get('kind') != 'unsolved-goals-diagnostic' or not isinstance(text,str) or
                len(text.encode('utf-8')) > MAX_GOAL_BYTES or not text or
                type(frame.get('truncated')) is not bool or
                frame.get('retained_region') != ('tail' if frame['truncated'] else 'complete') or
                frame.get('authority') != 'source-bound diagnostic text; not an elaborator checkpoint' or
                type(frame.get('line')) is not int or frame['line'] <= 0 or
                type(frame.get('column')) is not int or frame['column'] < 0): return None
        for name in ('file_sha256','message_sha256'):
            if not isinstance(frame.get(name),str) or not re.fullmatch(HEX,frame[name]): return None
        if not frame['truncated'] and hashlib.sha256(text.encode()).hexdigest() != frame['message_sha256']:
            return None
    return value


def record(job, result):
    source = result.get('source_files')
    tools = result.get('tool_identity')
    if (result.get('action') not in ('build', 'check', 'inspect', 'incremental')
            or result.get('outcome_class') != 'lean_diagnostic'
            or type(result.get('exitcode')) is not int or result['exitcode'] == 0
            or result.get('timed_out') or result.get('source_current') is not True
            or result.get('compiler_invoked') is not True
            or result.get('checked_files_unchanged') is not True
            or not isinstance(source, dict) or not source
            or result.get('checked_files') != source
            or result.get('source_sha256') != digest(source)
            or not isinstance(tools, dict) or not tools):
        return None
    diagnostics = result.get('diagnostics')
    if not isinstance(diagnostics, list): return None
    errors = [d['message'][:1500] for d in diagnostics if isinstance(d, dict)
              and d.get('severity') == 'error' and isinstance(d.get('message'), str) and d['message']][:3]
    if not errors: return None
    value = {'schema_version':1, 'kind':'failed-compiler-check', 'accepted':False,
        'job':str(safe(job)), 'step_id':result.get('step_id'), 'action':result['action'],
        'query':result.get('query', ''), 'source_sha256':result['source_sha256'],
        'tool_identity_sha256':digest(tools), 'diagnostics_sha256':digest(diagnostics),
        'errors':errors, 'finished_at':result.get('finished_at'), 'exitcode':result['exitcode'],
        'outcome_class':'lean_diagnostic'}
    frames = goal_frames(result)
    if frames:
        value['reported_goals'] = frames
    value['record_digest'] = digest(value)
    if not checked(value): return None
    directory = safe(Path(job)/'research-failed-checks')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = safe(directory/(value['step_id']+'.json'))
    if path.exists():
        previous = json.loads(path.read_bytes())
        if previous != value: raise ValueError('Compiler step already has a different failure record')
    else: atomic_json(path, value)
    return {'path':str(path), 'record_digest':value['record_digest']}


def snapshot(job, inherited):
    """Ignore malformed inputs, expose scan bounds, never promote text notes."""
    records = {}; invalid = 0; truncated = False
    if inherited:
        unsigned = {k:v for k,v in inherited.items() if k != 'checkpoint_digest'}
        if digest(unsigned) == inherited.get('checkpoint_digest'):
            memory = inherited.get('failed_routes') or {}
            candidates = memory.get('observations', []) if isinstance(memory, dict) else []
            if isinstance(candidates, list):
                for value in candidates[:MAX_RECORDS]:
                    if checked(value): records[(value['job'],value['step_id'])] = value
                    else: invalid += 1
    directory = safe(Path(job)/'research-failed-checks')
    if directory.is_dir():
        entries = []
        with os.scandir(directory) as stream:
            for count, entry in enumerate(stream):
                if count >= MAX_SCAN:
                    truncated = True; break
                if entry.is_file(follow_symlinks=False) and entry.name.endswith('.json'):
                    entries.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
        # Read only the most recent bounded candidates. Original records stay on disk.
        for _, name in sorted(entries, reverse=True)[:MAX_RECORDS]:
            try:
                path = safe(Path(name))
                if path.stat().st_size > MAX_BYTES: raise ValueError('Oversized failure record')
                value = checked(json.loads(path.read_bytes()))
                if value and value['job'] == str(job): records[(value['job'],value['step_id'])] = value
                else: invalid += 1
            except (OSError, ValueError, TypeError): invalid += 1
    ordered = sorted(records.values(), key=lambda r:(r['finished_at'],r['job'],r['step_id']), reverse=True)
    return {'schema_version':1, 'observations':ordered[:MAX_RECORDS],
            'retained_limit':MAX_RECORDS, 'scan_truncated':truncated,
            'invalid_records_ignored':invalid, 'accepted':False}


def brief(memory, source):
    records = memory.get('observations', []) if isinstance(memory, dict) else []
    lines = []
    rendered_goal = False
    for value in records[:3]:
        if not checked(value): continue
        same = value['source_sha256'] == digest(source)
        line = {'action':value['action'], 'query':value['query'],
            'source_matches':same, 'source_sha256':value['source_sha256'],
            'tool_identity_sha256':value['tool_identity_sha256'],
            'step_id':value['step_id'], 'error':value['errors'][0][:500]}
        frames = value.get('reported_goals', [])
        if same and frames and not rendered_goal:
            frame = frames[0]
            raw = frame['message'].encode('utf-8')
            line['reported_goal'] = {k:frame[k] for k in
                ('file','file_sha256','line','column','message_sha256','authority')}
            line['reported_goal'].update(message=raw[-2048:].decode('utf-8',errors='ignore'),
                truncated=frame['truncated'] or len(raw)>2048,
                retained_region='tail' if frame['truncated'] or len(raw)>2048 else 'complete')
            rendered_goal = True
        elif frames:
            line['reported_goals_not_shown'] = len(frames)
        lines.append(line)
    if not lines: return ''
    return ('\nPrevious failed compiler checks (conditional observations, not impossible proof routes):\n'
        + json.dumps(lines, ensure_ascii=False)
        + '\nBefore repeating one, identify what changed in its source, pinned tools, or check. '
          'A changed source needs a fresh check; an old error does not prove the new route fails. '
          'Reported goals are diagnostic text from that exact failed check, not restored tactic state. '
          'Full retained evidence is in research-inherited.json and controller research-failed-checks records.\n')
