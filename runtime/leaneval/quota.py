"""Shared, credential-group quota observations; never a proof outcome or invoice.

Provider reset times and local cooldown estimates stay distinct. Synchronization
replays original observations without extending their timestamps or deadlines.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import time

from leaneval.runtime import RuntimeBlocked


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def state_root():
    return Path(os.environ.get('LEANEVAL_QUOTA_STATE', str(Path.home() / 'projects/lean-eval-quota-state')))


def load_policy():
    path = Path(os.environ.get('LEANEVAL_QUOTA_POLICY', str(state_root() / 'groups.json')))
    if not path.exists():
        return {'groups': {}, 'credentials': {}}
    value = json.loads(path.read_text())
    if not isinstance(value.get('groups'), dict) or not isinstance(value.get('credentials'), dict):
        raise RuntimeBlocked('Invalid controller quota group policy')
    return value


def group_for(name: str, policy=None):
    policy = load_policy() if policy is None else policy
    known = policy['credentials'].get(name)
    if known:
        return known
    # A newly added, not-yet-mapped credential must not inherit another host's
    # quota merely because its filename matches. Discovery may map it later.
    return 'unmapped-' + hashlib.sha256((socket.gethostname() + '/' + name).encode()).hexdigest()[:24]


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path or state_root() / 'quota.sqlite3'
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise RuntimeBlocked('Quota state must not be symlinked')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT)')

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def validate(row):
        if set(row) != {'group', 'kind', 'window', 'origin', 'observed_at', 'until', 'reset_at', 'utilization', 'basis', 'model_scope'}:
            raise ValueError('Invalid quota record fields')
        if any(not isinstance(row[k], str) or not row[k] or len(row[k]) > 160 for k in ('group', 'window', 'origin', 'basis', 'model_scope')):
            raise ValueError('Invalid quota record identity')
        if row['kind'] not in {'limit', 'window'} or row['model_scope'] != 'all_models_in_group':
            raise ValueError('Invalid quota scope')
        if not number(row['observed_at']) or row['observed_at'] < 0:
            raise ValueError('Invalid observation time')
        for key in ('until', 'reset_at', 'utilization'):
            if row[key] is not None and (not number(row[key]) or row[key] < 0):
                raise ValueError('Invalid quota quantity')
        if row['kind'] == 'limit' and row['until'] is None:
            raise ValueError('A limit requires a deadline')

    def merge(self, rows):
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError('Oversize quota exchange')
        for row in rows:
            self.validate(row)
        with self.tx() as db:
            for row in rows:
                key = canonical([row[k] for k in ('group', 'kind', 'window', 'origin')])
                old = db.execute('SELECT value FROM records WHERE key=?', (key,)).fetchone()
                if old:
                    previous = json.loads(old[0])
                    # Replays never become fresh observations. New native observations retain their original timestamps.
                    old_order = (previous['observed_at'], previous['until'] or 0)
                    new_order = (row['observed_at'], row['until'] or 0)
                    if new_order <= old_order:
                        continue
                db.execute('INSERT OR REPLACE INTO records VALUES (?,?)', (key, canonical(row)))

    def export(self):
        with self.tx() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT value FROM records ORDER BY key')]

    def blocked_until(self, group, now, reserve=None):
        values = []
        for row in self.export():
            if row['group'] != group:
                continue
            if row['kind'] == 'limit' and row['until'] > now:
                values.append(row['until'])
            elif (reserve is not None and row['kind'] == 'window' and row['reset_at'] is not None
                  and row['reset_at'] > now and row['utilization'] is not None and row['utilization'] >= reserve):
                values.append(row['reset_at'] + 90)
        return max(values, default=0)

    def observe(self, group, info, *, origin, now=None, margin=90):
        now = time.time() if now is None else now
        rows = []
        def row(kind, window, until=None, reset=None, utilization=None, basis='native_rate_limit_event'):
            rows.append({'group': group, 'kind': kind, 'window': window, 'origin': origin,
                         'observed_at': now, 'until': until, 'reset_at': reset,
                         'utilization': utilization, 'basis': basis, 'model_scope': 'all_models_in_group'})
        windows = info.get('unifiedWindows') or {}
        for name, value in windows.items() if isinstance(windows, dict) else []:
            if not isinstance(name, str) or not isinstance(value, dict):
                continue
            used, reset = value.get('utilization'), value.get('resetsAt')
            if not number(used) or used < 0 or not number(reset) or reset <= now:
                continue
            row('window', name, reset=reset, utilization=used)
            if used >= 1:
                row('limit', name, until=reset + margin, reset=reset, utilization=used)
        # overageStatus=rejected while status=allowed is NOT an exhausted subscription.
        if info.get('status') == 'rejected':
            reset = info.get('resetsAt')
            if number(reset) and reset > now:
                row('limit', str(info.get('rateLimitType') or 'primary'), until=reset + margin, reset=reset)
            elif not any(x['kind'] == 'limit' for x in rows):
                row('limit', 'unknown', until=now + 900, basis='native_rejection_unknown_reset_cooldown')
        self.merge(rows)
        return rows

    def cooldown(self, group, until, *, origin, now=None, basis='api_rate_limit_unknown_reset'):
        now = time.time() if now is None else now
        row = {'group': group, 'kind': 'limit', 'window': 'cooldown', 'origin': origin,
               'observed_at': now, 'until': until, 'reset_at': None, 'utilization': None,
               'basis': basis, 'model_scope': 'all_models_in_group'}
        self.merge([row])
        return row

    def import_legacy(self, path, policy=None):
        if not path.is_file():
            return
        with path.open() as stream:
            value = json.load(stream)
            observed_at = os.fstat(stream.fileno()).st_mtime
        if not isinstance(value, dict):
            raise ValueError('Invalid legacy account quota state')
        deadlines = {}
        for key, until in value.items():
            if isinstance(key, str) and '|' in key and number(until):
                name = key.split('|', 1)[0]
                deadlines[name] = max(deadlines.get(name, 0), until)
        prefix = 'legacy/' + socket.gethostname() + '/'
        old = {row['origin'][len(prefix):]:row for row in self.export()
               if row['kind'] == 'limit' and row['window'] == 'legacy' and row['origin'].startswith(prefix)}
        rows = []
        for name in deadlines.keys() | old.keys():
            until = deadlines.get(name, 0)
            group = group_for(name, policy)
            previous = old.get(name)
            if previous and previous['until'] == until and previous['group'] == group:
                continue
            # Administrative gates are mutable. Removing or shortening a local
            # gate must publish a newer tombstone/deadline instead of leaving a
            # stale, possibly years-long gate cached by peers.
            rows.append({'group':group,'kind':'limit','window':'legacy',
                         'origin':prefix+name, 'observed_at':observed_at,
                         'until':until, 'reset_at':None, 'utilization':None,
                         'basis':'existing_account_schedule','model_scope':'all_models_in_group'})
        self.merge(rows)


def current():
    ledger = Ledger()
    legacy = Path(os.environ.get('LEANEVAL_CLAUDE_STATE', str(Path.home()/'projects/lean-eval-claude-state'))) / 'account-limits.json'
    ledger.import_legacy(legacy)
    return ledger


def selection(names, *, now=None, reserve=None, legacy=None, ledger=None, policy=None):
    now = time.time() if now is None else now
    ledger = ledger or current()
    policy = load_policy() if policy is None else policy
    deadlines = {}
    group_legacy = {}
    for name, until in (legacy or {}).items():
        group = group_for(name, policy)
        group_legacy[group] = max(group_legacy.get(group, 0), until)
    for name in names:
        group = group_for(name, policy)
        until = ledger.blocked_until(group, now, reserve)
        until = max(until, group_legacy.get(group, 0))
        deadlines[name] = {'group':group,'blocked_until':until}
    choice = next((name for name in names if deadlines[name]['blocked_until'] <= now), None)
    wake = min((x['blocked_until'] for x in deadlines.values()), default=now)
    return {'choice':choice,'wake_at':wake,'accounts':deadlines,'observed_at':now}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export','merge','status'))
    args = parser.parse_args()
    ledger = current()
    if args.action == 'merge':
        import sys
        ledger.merge(json.load(sys.stdin))
    if args.action == 'export':
        print(canonical(ledger.export()))
    else:
        policy=load_policy()
        now=time.time()
        print(canonical(selection(list(policy['credentials']),now=now,ledger=ledger,policy=policy)))


if __name__ == '__main__':
    main()
