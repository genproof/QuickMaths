"""Controller-owned cumulative allowances for a family of fresh proof sessions.

Tokens are stopping thresholds: native CLI calls can overrun between observations.
Unknown historical usage is conservatively booked, never silently reset to zero.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any

from leaneval.artifacts import digest
from leaneval.runtime import RunLedger, RuntimeBlocked
from leaneval.verifier import atomic_json

FIELDS = ('tokens', 'model_seconds', 'compiler_seconds')


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError('Lineage metadata must not be symlinked')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Lineage metadata must be an object')
    return value


def validate_limits(value: dict[str, Any]) -> dict[str, int | float]:
    if set(value) != set(FIELDS):
        raise ValueError('Lineage needs tokens, model_seconds, compiler_seconds limits')
    for name, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ValueError('Invalid lineage limit: ' + name)
        if name == 'tokens' and type(number) is not int:
            raise ValueError('Lineage tokens must be an integer')
    return value


class Ledger:
    def __init__(self, path: Path, policy: dict[str, Any] | None = None):
        self.path = path
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Lineage ledger path contains symlinks')
        if policy is None and not path.is_file():
            raise RuntimeBlocked('Lineage ledger is missing; refusing a reset')
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.execute('CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, value TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS members (job TEXT PRIMARY KEY, value TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS compiler (id TEXT PRIMARY KEY, value TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS bootstrap (id INTEGER PRIMARY KEY, complete INTEGER)')
            row = db.execute('SELECT value FROM policy WHERE id=1').fetchone()
            if row is None:
                if policy is None:
                    raise RuntimeBlocked('Missing lineage policy')
                validate_limits(policy['limits'])
                db.execute('INSERT INTO policy VALUES (1,?)', (json.dumps(policy, sort_keys=True),))
            elif policy is not None and json.loads(row[0]) != policy:
                raise RuntimeBlocked('Lineage policy differs from persisted limits')

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=30)
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
    def policy(db):
        return json.loads(db.execute('SELECT value FROM policy WHERE id=1').fetchone()[0])

    @staticmethod
    def totals(db):
        total = dict.fromkeys(FIELDS, 0)
        for (raw,) in db.execute('SELECT value FROM members'):
            row = json.loads(raw)
            booked = row['spent'] if row['closed'] else row['reserved']
            total['tokens'] += max(booked['tokens'], row['spent']['tokens'])
            total['model_seconds'] += max(booked['model_seconds'], row['spent']['model_seconds'])
        for (raw,) in db.execute('SELECT value FROM compiler'):
            row = json.loads(raw)
            total['compiler_seconds'] += row['spent'] if row['closed'] else max(row['reserved'], row['spent'])
        return total

    def member(self, job: Path):
        with self.tx() as db:
            row = db.execute('SELECT value FROM members WHERE job=?', (str(job),)).fetchone()
            return json.loads(row[0]) if row else None

    def reserve_model(self, job: Path, tokens: int, seconds: float) -> dict[str, Any]:
        if tokens <= 0 or seconds <= 0:
            raise RuntimeBlocked('No positive model allowance requested')
        with self.tx() as db:
            row = db.execute('SELECT value FROM members WHERE job=?', (str(job),)).fetchone()
            if row:
                return json.loads(row[0])
            policy = self.policy(db)
            total = self.totals(db)
            tokens = min(tokens, int(policy['limits']['tokens'] - total['tokens']))
            seconds = min(seconds, policy['limits']['model_seconds'] - total['model_seconds'])
            if tokens <= 0 or seconds <= 0:
                raise RuntimeBlocked('Cumulative lineage model budget exhausted')
            value = {'job': str(job), 'reserved': {'tokens': tokens, 'model_seconds': seconds},
                     'spent': {'tokens': 0, 'model_seconds': 0}, 'closed': False,
                     'created_at': time.time()}
            db.execute('INSERT INTO members VALUES (?,?)', (str(job), json.dumps(value, sort_keys=True)))
            return value

    def settle_model(self, job: Path, usage: dict[str, Any], *, evidence: str):
        tokens, seconds = usage.get('tokens'), usage.get('wall_seconds')
        if type(tokens) is not int or tokens < 0 or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise RuntimeBlocked('Unknown parent usage cannot release lineage allowance')
        with self.tx() as db:
            row = db.execute('SELECT value FROM members WHERE job=?', (str(job),)).fetchone()
            if not row:
                raise RuntimeBlocked('Parent is not a lineage member')
            value = json.loads(row[0])
            spent = {'tokens': tokens, 'model_seconds': seconds}
            if value['closed'] and value['spent'] != spent:
                raise RuntimeBlocked('Sealed lineage usage changed')
            value.update(spent=spent, closed=True, evidence=evidence)
            if value.get('legacy'):
                bound=value['legacy_compiler_bound']
                prior={'job':str(job),'reserved':bound,'spent':bound,'closed':True,
                       'basis':'historical allowance charged in full; actual usage unavailable'}
                db.execute('INSERT OR IGNORE INTO compiler VALUES (?,?)',('legacy/'+digest(str(job)),json.dumps(prior,sort_keys=True)))
            db.execute('UPDATE members SET value=? WHERE job=?', (json.dumps(value, sort_keys=True),str(job)))

    def reserve_compiler(self, job: Path, key: str, seconds: float):
        with self.tx() as db:
            if db.execute('SELECT 1 FROM compiler WHERE id=?', (key,)).fetchone():
                raise RuntimeBlocked('Compiler reservation already exists; reconcile instead of replay')
            if not db.execute('SELECT 1 FROM members WHERE job=?', (str(job),)).fetchone():
                raise RuntimeBlocked('Compiler job is not a lineage member')
            policy, total = self.policy(db), self.totals(db)
            seconds = min(seconds, policy['limits']['compiler_seconds'] - total['compiler_seconds'])
            if seconds < 1:
                raise RuntimeBlocked('Cumulative lineage compiler budget exhausted')
            value = {'job': str(job), 'reserved': seconds, 'spent': 0, 'closed': False}
            db.execute('INSERT INTO compiler VALUES (?,?)', (key,json.dumps(value,sort_keys=True)))
            return seconds

    def finish_compiler(self, key: str, seconds: float | None):
        with self.tx() as db:
            row = db.execute('SELECT value FROM compiler WHERE id=?',(key,)).fetchone()
            if not row:
                raise RuntimeBlocked('Missing compiler reservation')
            value = json.loads(row[0])
            if seconds is None:
                return  # Observation loss keeps the full reservation booked.
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError('Invalid compiler usage')
            if value['closed'] and value['spent'] != seconds:
                raise RuntimeBlocked('Compiler usage replay differs')
            value.update(spent=seconds,closed=True)
            db.execute('UPDATE compiler SET value=? WHERE id=?',(json.dumps(value,sort_keys=True),key))

    def initialized(self):
        with self.tx() as db:
            row=db.execute('SELECT complete FROM bootstrap WHERE id=1').fetchone()
            return bool(row and row[0])

    def import_legacy(self, job: Path, config: dict[str, Any]):
        closed=(job/'termination.json').is_file() and (job/'ledger.sqlite3').is_file()
        usage=RunLedger(job/'ledger.sqlite3').snapshot()['accounted'] if closed else None
        if usage is not None and (type(usage.get('tokens')) is not int or not isinstance(usage.get('wall_seconds'),(int,float))):
            raise RuntimeBlocked('Unknown historical model usage')
        with self.tx() as db:
            if db.execute('SELECT 1 FROM members WHERE job=?',(str(job),)).fetchone():
                return
            spent={'tokens':usage['tokens'],'model_seconds':usage['wall_seconds']} if closed else {'tokens':0,'model_seconds':0}
            value={'job':str(job),'reserved':{'tokens':config['token_limit'],'model_seconds':config['wall_seconds']},
                   'spent':spent,'closed':closed,'legacy':True,'created_at':time.time(),
                   'legacy_compiler_bound':float(config['wall_seconds'])+float((config.get('campaign_assignment') or {}).get('verification_seconds',300))+2*float((config.get('campaign_assignment') or {}).get('worker_timeout_seconds',300))}
            db.execute('INSERT INTO members VALUES (?,?)',(str(job),json.dumps(value,sort_keys=True)))
            if closed:
                assignment=config.get('campaign_assignment') or {}
                bound=float(config['wall_seconds'])+float(assignment.get('verification_seconds',300))+2*float(assignment.get('worker_timeout_seconds',300))
                compiler={'job':str(job),'reserved':bound,'spent':bound,'closed':True,'basis':'historical allowance charged in full; actual usage unavailable'}
                db.execute('INSERT INTO compiler VALUES (?,?)',('legacy/'+digest(str(job)),json.dumps(compiler,sort_keys=True)))

    def mark_initialized(self):
        with self.tx() as db:
            db.execute('INSERT OR REPLACE INTO bootstrap VALUES (1,1)')

    def snapshot(self):
        with self.tx() as db:
            policy = self.policy(db)
            totals = self.totals(db)
            return {'policy':policy,'policy_digest':digest(policy),'booked':totals,
                    'remaining':{k:policy['limits'][k]-totals[k] for k in FIELDS},
                    'members':db.execute('SELECT count(*) FROM members').fetchone()[0]}


def reference(job: Path) -> tuple[Ledger, dict[str, Any]] | None:
    path = job / 'lineage.json'
    if not path.exists():
        return None
    ref = read(path)
    if ref.get('job') != str(job.resolve()):
        raise RuntimeBlocked('Lineage reference belongs to another job')
    ledger = Ledger(Path(ref['ledger']))
    snapshot = ledger.snapshot()
    if not ledger.initialized() or snapshot['policy_digest'] != ref['policy_digest'] or not ledger.member(job):
        raise RuntimeBlocked('Lineage reference does not match controller ledger')
    return ledger, ref


def bind(job: Path, ledger: Ledger):
    snapshot = ledger.snapshot()
    ref = {'schema_version':1,'job':str(job.resolve()),'ledger':str(ledger.path),
           'policy_digest':snapshot['policy_digest'],'lineage_id':snapshot['policy']['id']}
    atomic_json(job/'lineage.json',ref)
    return ref


def _derive(job: Path, config: dict[str, Any], explicit: dict[str, Any] | None = None):
    assignment = config.get('campaign_assignment') or {}
    rule = assignment.get('auto_continue') or {}
    limits = explicit or config.get('lineage_limits') or assignment.get('lineage_limits')
    campaign = job.parent.parent
    if limits is None and not rule:
        return None  # Legacy manually granted tranches keep their explicit semantics.
    base = rule.get('base') or config['run_id']
    max_rounds = int(rule.get('max_rounds',1))
    peers = []
    if rule:
        for path in sorted(job.parent.glob('*/config.json')):
            other = read(path)
            other_rule = (other.get('campaign_assignment') or {}).get('auto_continue') or {}
            if (other_rule.get('base') or other.get('run_id')) == base:
                peers.append((path.parent,other))
    if not peers:
        peers = [(job,config)]
    if limits is None:
        specs = [c.get('campaign_assignment') or c for _,c in peers]
        manifest = campaign/'manifest.json'
        if manifest.is_file():
            for spec in read(manifest).get('assignments',[]):
                if ((spec.get('auto_continue') or {}).get('base') or spec.get('attempt_id')) == base:
                    specs.append(spec)
        specs += [{**s, **((s.get('auto_continue') or {}).get('next') or {})} for s in specs.copy()]
        tokens = max(int(s.get('token_limit',config['token_limit'])) for s in specs)
        model = max(float(s.get('wall_seconds',config['wall_seconds'])) for s in specs)
        verification = max(float(s.get('verification_seconds',300)) for s in specs)
        worker = max(float(s.get('worker_timeout_seconds',300)) for s in specs)
        limits = {'tokens':max_rounds*tokens,'model_seconds':max_rounds*model,
                  'compiler_seconds':max_rounds*(model+verification+2*worker)}
        basis = 'Existing maximum rounds times declared per-round allowances; historical compiler allowance charged in full (actual usage unavailable)'
    else:
        basis = 'Explicit total-lineage limits'
    validate_limits(limits)
    policy={'id':str(base),'problem_id':config['problem_id'],'limits':limits,'basis':basis,
            'wall_basis':'cumulative active model seconds and cumulative compiler wall seconds',
            'tokens_basis':'input plus output; cached input is included, native-call overshoot remains charged'}
    root = campaign if rule else job.parent
    path = root/'.research-lineages'/(digest({'base':base,'problem':config['problem_id']})+'.sqlite3')
    ledger = Ledger(path) if path.exists() else Ledger(path,policy)
    existing=ledger.snapshot()['policy']
    if explicit is not None and existing['limits']!=limits:
        raise RuntimeBlocked('Explicit lineage limits differ from existing policy')
    if ledger.initialized():
        return ledger
    for peer,cfg in peers:
        if (peer/'ledger.sqlite3').is_file():
            ledger.import_legacy(peer,cfg)
    ledger.mark_initialized()
    return ledger


def derive(job: Path, config: dict[str, Any], explicit: dict[str, Any] | None = None):
    assignment=config.get('campaign_assignment') or {}
    rule=assignment.get('auto_continue') or {}
    if explicit is None and not config.get('lineage_limits') and not assignment.get('lineage_limits') and not rule:
        return None
    root=job.parent.parent if rule else job.parent
    directory=root/'.research-lineages'
    if any(p.is_symlink() for p in (directory,*directory.parents)):
        raise ValueError('Lineage directory contains symlinks')
    directory.mkdir(parents=True,exist_ok=True)
    lock=directory/(digest({'base':rule.get('base') or config['run_id'],'problem':config['problem_id']})+'.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        return _derive(job,config,explicit)


def prepare(job: Path, config: dict[str, Any]):
    old = reference(job)
    if old:
        return old[0]
    ledger=derive(job,config)
    if ledger is None:
        return None
    ledger.reserve_model(job,config['token_limit'],config['wall_seconds'])
    bind(job,ledger)
    return ledger


def continuation_ticket(snapshot: dict[str, Any], grant: dict[str, Any], new_job: Path):
    parent=Path(snapshot['parent_job']).resolve()
    existing=reference(parent)
    saved=snapshot.get('lineage')
    if existing:
        ledger=existing[0]
    elif saved:
        ref=saved['reference']
        ledger=Ledger(Path(ref['ledger']))
        if ref.get('job') != str(parent) or ledger.snapshot()['policy_digest'] != ref['policy_digest']:
            raise RuntimeBlocked('Snapshot lineage reference mismatch')
    elif (parent/'config.json').is_file():
        ledger=derive(parent,read(parent/'config.json'),grant.get('lineage_limits'))
    elif grant.get('lineage_limits'):
        usage=snapshot['usage']
        cfg={'run_id':snapshot['run_id'],'problem_id':snapshot['problem_id'],
             'token_limit':max(1,usage['tokens']), 'wall_seconds':max(1,usage['wall_seconds'])}
        ledger=derive(parent,cfg,grant['lineage_limits'])
    else:
        return None
    if ledger is None:
        return None
    if grant.get('lineage_limits') is not None and ledger.snapshot()['policy']['limits']!=grant['lineage_limits']:
        raise RuntimeBlocked('Continuation cannot change shared lineage limits')
    if ledger.member(parent) is None:
        if (parent/'config.json').is_file():
            cfg=read(parent/'config.json')
            tokens,seconds=cfg['token_limit'],cfg['wall_seconds']
        else:
            tokens,seconds=max(1,snapshot['usage']['tokens']),max(1,snapshot['usage']['wall_seconds'])
        ledger.import_legacy(parent,{'token_limit':tokens,'wall_seconds':seconds})
    ledger.settle_model(parent,snapshot['usage'],evidence='snapshot/'+snapshot['artifact_digest'])
    ledger.reserve_model(new_job,grant['token_limit'],grant['wall_seconds'])
    return ledger


def model_allowance(job: Path, config: dict[str, Any], accounted: dict[str, Any]):
    ledger=prepare(job,config)
    if ledger is None:
        return config['token_limit']-accounted['tokens'],config['wall_seconds']-accounted['wall_seconds']
    row=ledger.member(job)
    if row['closed']:
        raise RuntimeBlocked('A snapshotted lineage member cannot restart; use a fresh continuation')
    return min(config['token_limit'],row['reserved']['tokens'])-accounted['tokens'],min(config['wall_seconds'],row['reserved']['model_seconds'])-accounted['wall_seconds']


def settle(job: Path, usage: dict[str, Any]):
    current=reference(job)
    if current:
        current[0].settle_model(job,usage,evidence='finished-controller-invocation')
        return current[0].snapshot()
    return None


def compiler_reservation(job: Path, key: str, seconds: float):
    current=reference(job)
    if current is None:
        return None
    ledger,_=current
    allowed=ledger.reserve_compiler(job,key,seconds)
    return {'ledger':str(ledger.path),'key':key,'seconds':allowed}


def compiler_finished(ticket: dict[str, Any] | None, seconds: float | None):
    if ticket:
        Ledger(Path(ticket['ledger'])).finish_compiler(ticket['key'],seconds)
