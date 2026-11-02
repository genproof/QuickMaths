"""Source-bound startup observations. No proof, model, or restart authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

LIMIT = 65536
PHASES = frozenset(('starting', 'source_admission', 'lineage_budget',
    'init_preflight', 'environment_preflight', 'workspace_wrappers',
    'source_inventory', 'continuation_material', 'consumer_dependencies',
    'dependency_admission', 'frontier_assembly'))


def config_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def process(pid):
    if type(pid) is not int or pid <= 0:
        return {'state': 'unknown'}
    try:
        fields = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start_ticks': fields[19],
                'state': 'terminal' if fields[0] in ('Z', 'X') else 'live'}
    except FileNotFoundError:
        return {'pid': pid, 'state': 'absent'}
    except (OSError, IndexError):
        return {'pid': pid, 'state': 'unknown'}


def _write(path, value):
    data = (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n').encode()
    if len(data) > LIMIT:
        raise ValueError('preparation record exceeds bound')
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Session:
    def __init__(self, job, config):
        self.job = Path(job).absolute()
        self.record = {'schema_version': 1, 'invocation': uuid.uuid4().hex,
            'job': str(self.job), 'run_id': config.get('run_id'),
            'attempt_id': config.get('attempt_id'), 'config_sha256': config_digest(config),
            'owner': process(os.getpid()), 'status': 'running', 'phase': 'starting',
            'started_at': time.time(), 'stages': [], 'accepted': False,
            'authority': 'preparation-observation-only'}
        self.since = time.monotonic()
        self.enabled = True

    def _publish(self):
        if not self.enabled:
            return
        try:
            directory = self.job / 'research-preparations'
            directory.mkdir(mode=0o700, exist_ok=True)
            if self.job.is_symlink() or directory.is_symlink():
                raise ValueError('symlink preparation directory')
            self.record['updated_at'] = time.time()
            _write(directory / (self.record['invocation'] + '.json'), self.record)
            _write(self.job / 'research-preparation.json', self.record)
        except (OSError, ValueError):
            # Observability must not change compiler budgets or kill model work.
            self.enabled = False

    def __enter__(self):
        self._publish()
        return self

    def _close_phase(self):
        self.record['stages'].append({'phase': self.record['phase'],
            'wall_seconds': round(time.monotonic() - self.since, 6)})

    def phase(self, name):
        if name not in PHASES:
            raise ValueError('unknown preparation phase')
        self._close_phase()
        self.record['phase'] = name
        self.since = time.monotonic()
        self._publish()

    def __exit__(self, kind, error, traceback):
        self._close_phase()
        self.record.update(status='failed' if kind else 'ready', finished_at=time.time())
        if kind:
            # Class only: exception text may contain credentials or megabytes of diagnostics.
            self.record['error_class'] = kind.__name__[:128]
        self._publish()
        return False


def observe(job, supervisor_pid, config):
    result = {'status': 'unknown', 'reason': 'record_missing', 'accepted': False,
              'authority': 'preparation-observation-only'}
    path = Path(job) / 'research-preparation.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                raise ValueError('invalid record size or type')
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('record grew beyond bound')
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError('record is not an object')
        if (record.get('schema_version') != 1 or record.get('job') != str(Path(job).absolute())
                or record.get('config_sha256') != config_digest(config)
                or record.get('run_id') != config.get('run_id')
                or record.get('attempt_id') != config.get('attempt_id')):
            result['reason'] = 'identity_mismatch'
            return result
        invocation = record.get('invocation')
        if (not isinstance(invocation, str) or len(invocation) != 32
                or any(c not in '0123456789abcdef' for c in invocation)
                or record.get('status') not in ('running', 'ready', 'failed')
                or record.get('phase') not in PHASES or record.get('accepted') is not False):
            raise ValueError('invalid preparation fields')
        owner = record.get('owner')
        live = process(supervisor_pid)
        if (not isinstance(owner, dict) or live.get('state') != 'live'
                or type(owner.get('pid')) is not int or owner.get('pid') != supervisor_pid
                or not owner.get('start_ticks') or owner.get('start_ticks') != live.get('start_ticks')):
            result.update(reason='owner_unconfirmed', invocation=invocation)
            return result
        result.update({key: record.get(key) for key in ('invocation', 'phase', 'started_at',
            'updated_at', 'finished_at', 'stages', 'error_class')})
        result.update(status=record['status'], reason='owner_and_config_matched', owner=live)
        # ready only means prepare returned, never evidence that a model was invoked.
        return result
    except FileNotFoundError:
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        result['reason'] = 'record_unreadable_or_invalid'
        return result
