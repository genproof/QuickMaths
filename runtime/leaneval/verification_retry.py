"""Refuse unchanged official timeout retries using controller-owned evidence.

This policy never accepts a proof. A different candidate, verifier strategy, or
strictly larger *reserved* wall allowance can proceed to normal verification.
"""
from __future__ import annotations

from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

from leaneval.artifacts import digest, file_digest


class RetryBlocked(RuntimeError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__('Unchanged candidate already timed out with this verification strategy; '
                         'change proof inputs, verifier strategy, or provide a larger bounded allowance')


def read(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Retry evidence must not traverse symlinks')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Retry evidence must be an object')
    return value


def candidate(manifest):
    # Git snapshot commits and capture paths vary even when every proof byte is identical.
    return {k: manifest[k] for k in ('problem_id', 'statement_revision', 'benchmark_commit', 'files')}


def strategy(tools):
    reviewed = read(Path(__file__).with_name('retry-strategy.json'))
    runner = dict(tools['runner'])
    if runner['verifier.py'] in reviewed['equivalent_verifier_sha256']:
        runner['verifier.py'] = reviewed['strategy_id']
    return {'pins': tools['pins'], 'binaries': tools['binaries'], 'runner': runner}


def prior_jobs(job, config):
    seen = set()
    for _ in range(256):
        job = job.resolve(strict=True)
        if job in seen:
            raise ValueError('Cyclic verification retry ancestry')
        seen.add(job)
        yield job, config
        provenance = config.get('continuation_provenance')
        if not provenance:
            return
        parent = Path(provenance['parent_job'])
        if not parent.is_absolute() or parent == job or parent.is_relative_to(job / 'workspace'):
            raise ValueError('Invalid verification retry parent')
        parent_config = parent / 'config.json'
        if file_digest(parent_config) != provenance['parent_config_sha256']:
            raise ValueError('Verification retry parent configuration changed')
        job, config = parent, read(parent_config)
    raise ValueError('Verification retry ancestry exceeds 256 jobs')


def assess(job, manifest, tools, runs, seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Retry policy requires a positive actual verification allowance')
    wanted, current_strategy = candidate(manifest), strategy(tools)
    report = {'schema_version': 1, 'status': 'allowed', 'candidate_digest': digest(wanted),
              'strategy_digest': digest(current_strategy), 'actual_allowance_seconds': seconds,
              'checked_jobs': 0, 'matched_timeouts': [], 'proof_accepted': False}
    database = runs.resolve() / 'receipts.sqlite3'
    for prior, config in prior_jobs(job, read(job / 'config.json')):
        report['checked_jobs'] += 1
        path = prior / 'verification.json'
        if not path.exists():
            continue
        result = read(path)
        receipt = result.get('receipt')
        if not receipt or receipt.get('status') != 'timeout':
            continue
        if not database.is_file():
            raise ValueError('Native timeout registry is missing')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT artifact,payload FROM receipts WHERE run_id=?',
                             (receipt['run_id'],)).fetchone()
        artifact = receipt['artifact']
        if (not row or json.loads(row[1]) != receipt or row[0] != digest(artifact)
                or receipt['artifact_digest'] != row[0]
                or result.get('artifact_digest') != row[0]
                or result.get('status') != 'timeout' or result.get('verified') is not False
                or config['problem_id'] != artifact['problem_id']
                or config['statement_revision'] != artifact['statement_revision']
                or config['benchmark_commit'] != artifact['benchmark_commit']):
            raise ValueError('Unbound or conflicting native timeout evidence')
        if candidate(artifact) != wanted or strategy(receipt['verifier']) != current_strategy:
            continue
        # Older receipts lack the reservation field. Their measured elapsed time
        # is a conservative lower bound, never the possibly edited assignment.
        bound = receipt.get('lineage_compiler_allowance_seconds', receipt.get('wall_seconds'))
        if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound) or bound <= 0:
            raise ValueError('Native timeout has no usable allowance evidence')
        match = {'job': str(prior), 'run_id': receipt['run_id'],
                 'receipt_digest': digest(receipt), 'artifact_digest': row[0],
                 'prior_allowance_seconds': bound}
        report['matched_timeouts'].append(match)
        if seconds <= bound:
            report.update(status='blocked_unchanged_timeout', blocked_by=match)
            raise RetryBlocked(report)
    return report
