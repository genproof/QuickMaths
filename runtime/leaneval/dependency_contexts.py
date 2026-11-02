"""Automatic, diagnostic producer-context audits before native consumer checks."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from leaneval import dependency_bundle as bundle
from leaneval.artifacts import digest
from leaneval.verifier import atomic_json

REGISTRY = Path(os.environ.get('LEH_DEPENDENCY_CONTEXTS',
                               str(Path.home() / 'lean-harness-dependency-contexts')))
MAX_CONTEXTS = 32


def key(dep):
    return digest({k: dep[k] for k in ('kind','owner','file','sha256','receipt_sha256',
        'require_compile','compile_timeout_seconds','compile_total_timeout_seconds','declaration') if k in dep})


def index(root):
    path = root/'registry.json'
    if not path.exists():
        return {'schema_version':1,'bundles':[]}
    value = json.loads(bundle.regular(path, 1024*1024), object_pairs_hook=bundle.unique)
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or
            not isinstance(value.get('bundles'), list) or len(value['bundles']) > MAX_CONTEXTS):
        raise ValueError('invalid producer context registry')
    return value


def register(source, root=REGISTRY):
    source, root = Path(source), Path(root)
    value = bundle.verify(source)
    raw = bundle.regular(source/'manifest.json', bundle.MAX_RECEIPT)
    manifest_sha = bundle.sha(raw)
    spec = {'root':value['root'], 'root_sha256':value['files'][value['root']],
        'producer_receipt_sha256':value['producer_receipt_sha256'],
        'manifest_sha256':manifest_sha, 'object':manifest_sha}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError('symlink context registry')
    objects = root/'objects';objects.mkdir(exist_ok=True, mode=0o700)
    if objects.is_symlink():
        raise ValueError('symlink context objects')
    with (root/'register.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = index(root)
        same = [r for r in registry['bundles'] if all(r.get(k)==spec[k]
            for k in ('root','root_sha256','producer_receipt_sha256'))]
        if same and same != [spec]:
            raise ValueError('conflicting registered producer context')
        if not same and len(registry['bundles']) >= MAX_CONTEXTS:
            raise ValueError('producer context registry is full')
        destination = objects/manifest_sha
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix='.context-', dir=objects))
            try:
                (staging/'manifest.json').write_bytes(raw)
                (staging/'producer-receipt.json').write_bytes(
                    bundle.regular(source/'producer-receipt.json', bundle.MAX_RECEIPT))
                for name, expected in value['files'].items():
                    data = bundle.regular(source/'sources'/name, bundle.MAX_FILE)
                    if bundle.sha(data) != expected:
                        raise ValueError('bundle changed during registration')
                    target = staging/'sources'/name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                bundle.verify(staging)
                staging.rename(destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        bundle.verify(destination)
        if bundle.sha(bundle.regular(destination/'manifest.json', bundle.MAX_RECEIPT)) != manifest_sha:
            raise ValueError('registered context identity mismatch')
        if not same:
            registry['bundles'].append(spec)
            atomic_json(root/'registry.json', registry)
    return spec


def inspect(job, dep, structural, root=None):
    started = time.monotonic()
    result = {'status':'unknown','reason':'not_registered', 'accepted':False,
        'native_consumer_check_required':True,'observed_at':time.time(),
        'scope':'local lexical Submission import context; external libraries and proof correctness are unchecked'}
    producer = structural.get('producer_receipt_sha256')
    if not isinstance(producer, str) or not bundle.HEX.fullmatch(producer):
        result['reason'] = 'producer_receipt_unbound'
        return result
    try:
        root = Path(REGISTRY if root is None else root)
        candidates = [r for r in index(root)['bundles'] if r.get('root')==dep.get('file')
            and r.get('root_sha256')==dep.get('sha256') and r.get('producer_receipt_sha256')==producer]
        if not candidates:
            return result
        if len(candidates) != 1:
            raise ValueError('ambiguous producer context')
        spec = candidates[0]
        if (not bundle.HEX.fullmatch(str(spec.get('object',''))) or
                spec.get('object') != spec.get('manifest_sha256')):
            raise ValueError('invalid producer context object')
        path = root/'objects'/spec['object']
        before = bundle.regular(path/'manifest.json', bundle.MAX_RECEIPT)
        if bundle.sha(before) != spec['manifest_sha256']:
            raise ValueError('registered manifest mismatch')
        audited = bundle.audit(path, Path(job)/'workspace')
        if (audited['root'] != dep['file'] or audited['producer_receipt_sha256'] != producer or
                bundle.regular(path/'manifest.json', bundle.MAX_RECEIPT) != before):
            raise ValueError('producer context changed during observation')
        result.update(audited, reason='producer_receipt_matched',
                      manifest_sha256=spec['manifest_sha256'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        result.update(status='unknown',reason='context_unavailable',error_class=type(exc).__name__)
    result['wall_seconds'] = round(time.monotonic()-started,6)
    return result


def record(job, config, dep, structural):
    job = Path(job)
    result = inspect(job, dep, structural)
    summary = {k:result.get(k) for k in ('status','reason','observed_at','wall_seconds',
        'producer_receipt_sha256','manifest_sha256','native_consumer_check_required')}
    for name in ('matched','missing','changed','changed_during_observation'):
        summary[name+'_count'] = len(result.get(name,[]))
        if name != 'matched':
            summary[name] = result.get(name,[])[:20]
    summary.update(accepted=False, scope=result['scope'])
    try:
        directory = job/'research-dependency-contexts'
        directory.mkdir(exist_ok=True,mode=0o700)
        if directory.is_symlink():
            raise ValueError('symlink audit directory')
        event = directory/(uuid.uuid4().hex+'.json')
        value = {'schema_version':1,'job':str(job.absolute()), 'config_sha256':digest(config),
                 'dependency':dep,'structural':structural,'observation':result,'accepted':False}
        atomic_json(event,value)
        summary.update(event=str(event),event_sha256=bundle.sha(event.read_bytes()))
    except (OSError, ValueError):
        summary.update(record_status='unavailable')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['register'])
    parser.add_argument('--bundle',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(register(args.bundle),sort_keys=True))


if __name__=='__main__':
    main()
