"""Export receipt-bound local Lean import closures; never grants acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time

from leaneval.costs import imports, NAME, HEADER_BYTES

MAX_RECEIPT = 32 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 4096
HEX = re.compile('[0-9a-f]{64}')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def regular(path, limit):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink input')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('nonregular or oversized input')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(data) > limit or identity(before) != identity(after) or identity(after) != identity(path.stat()):
        raise ValueError('input changed while reading')
    return data


def proof_name(name):
    p = PurePosixPath(name)
    if (not isinstance(name, str) or '\0' in name or p.is_absolute() or '..' in p.parts
            or p.as_posix() != name or not (name == 'Submission.lean' or
            name.startswith('Submission/') and name.endswith('.lean'))):
        raise ValueError('unsafe proof source name')
    return name


def receipt(raw, expected, root):
    proof_name(root)
    if not isinstance(expected, str) or not HEX.fullmatch(expected) or sha(raw) != expected:
        raise ValueError('producer receipt digest mismatch')
    value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or value.get('schema') != 1 or
            value.get('kind') != 'lean-build-and-module-axioms' or
            value.get('official_verification') is not False or value.get('tree_stable') is not True):
        raise ValueError('not a stable native module receipt')
    tree = value.get('checked_tree')
    if not isinstance(tree, dict) or len(tree) > 100000:
        raise ValueError('invalid checked tree')
    if (value.get('results', {}).get(root) != 'verified' or
            not HEX.fullmatch(str(tree.get(root, ''))) or
            value.get('source_sha256', {}).get(root) != tree[root] or
            not HEX.fullmatch(str(value.get('artifact_sha256', {}).get(root, '')))):
        raise ValueError('root not bound to successful native check')
    return value


def closure(root, tree, read):
    pending = [proof_name(root)]
    files, edges, external = {}, {}, set()
    total = 0
    while pending:
        name = pending.pop()
        if name in files:
            continue
        if len(files) >= MAX_FILES:
            raise ValueError('closure file limit')
        expected = tree.get(name)
        if not isinstance(expected, str) or not HEX.fullmatch(expected):
            raise ValueError('import absent from producer tree: ' + name)
        data = read(name, expected)
        if sha(data) != expected:
            raise ValueError('source does not match producer check: ' + name)
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError('closure byte limit')
        modules, complete = imports(data[:HEADER_BYTES], truncated=len(data) > HEADER_BYTES)
        if not complete:
            raise ValueError('import header incomplete: ' + name)
        local = []
        for module in modules:
            if not NAME.fullmatch(module):
                raise ValueError('unsupported import name')
            if module == 'Submission' or module.startswith('Submission.'):
                child = proof_name(module.replace('.', '/') + '.lean')
                local.append(child)
            else:
                external.add(module)
        files[name] = data
        edges[name] = sorted(set(local))
        pending.extend(reversed(edges[name]))
    return files, edges, sorted(external)


def export(receipt_path, expected, root, source_roots, output):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('output already exists')
    raw = regular(receipt_path, MAX_RECEIPT)
    native = receipt(raw, expected, root)
    roots = [Path(p).absolute() for p in source_roots]
    if not roots:
        raise ValueError('missing source roots')
    selected = {}
    def read(name, wanted):
        for directory in roots:
            path = directory/name
            try:
                data = regular(path, MAX_FILE)
            except FileNotFoundError:
                continue
            if sha(data) == wanted:
                selected[name] = str(path)
                return data
        raise ValueError('no exact checked source available: ' + name)
    files, edges, external = closure(root, native['checked_tree'], read)
    manifest = {'schema_version': 1, 'kind': 'receipt-bound-local-import-closure',
        'root': root, 'producer_receipt_sha256': expected,
        'files': {n: sha(d) for n, d in sorted(files.items())}, 'imports': edges,
        'external_modules': external, 'file_count': len(files),
        'bytes': sum(map(len, files.values())), 'source_locations': selected,
        'created_at': time.time(), 'accepted': False,
        'scope': 'Lexical Submission imports only. External libraries and semantic compatibility require native consumer compilation.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.'+output.name+'.', dir=output.parent))
    try:
        for name, data in files.items():
            path = staging/'sources'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (staging/'producer-receipt.json').write_bytes(raw)
        (staging/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        # Rename is atomic; never overwrite an existing destination.
        if output.exists() or output.is_symlink():
            raise ValueError('output appeared during export')
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return manifest


def verify(bundle):
    bundle = Path(bundle)
    value = json.loads(regular(bundle/'manifest.json', MAX_RECEIPT), object_pairs_hook=unique)
    if (value.get('schema_version') != 1 or value.get('kind') != 'receipt-bound-local-import-closure'
            or value.get('accepted') is not False):
        raise ValueError('invalid bundle manifest')
    raw = regular(bundle/'producer-receipt.json', MAX_RECEIPT)
    native = receipt(raw, value.get('producer_receipt_sha256'), value.get('root'))
    files, edges, external = closure(value['root'], native['checked_tree'],
        lambda name, _: regular(bundle/'sources'/name, MAX_FILE))
    if ({name: sha(data) for name, data in files.items()} != value.get('files') or
            edges != value.get('imports') or external != value.get('external_modules') or
            len(files) != value.get('file_count') or sum(map(len, files.values())) != value.get('bytes')):
        raise ValueError('bundle closure or manifest mismatch')
    return value


def audit(bundle, workspace):
    value = verify(bundle)
    matched, missing, changed = [], [], []
    workspace = Path(workspace)
    def identity(name):
        try:
            s = (workspace/name).lstat()
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        except FileNotFoundError:
            return None
    before = {name: identity(name) for name in value['files']}
    for name, expected in sorted(value['files'].items()):
        try:
            data = regular(workspace/name, MAX_FILE)
        except FileNotFoundError:
            missing.append(name)
            continue
        if sha(data) == expected:
            matched.append(name)
        else:
            changed.append(name)
    unstable = sorted(name for name, previous in before.items() if identity(name) != previous)
    return {'schema_version': 1, 'root': value['root'],
        'producer_receipt_sha256': value['producer_receipt_sha256'],
        'status': ('observation_unstable' if unstable else
                   'matched' if not missing and not changed else 'context_differs'),
        'matched': matched, 'missing': missing, 'changed': changed,
        'changed_during_observation': unstable,
        'external_modules_unchecked': value['external_modules'],
        'native_consumer_check_required': True, 'accepted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('export')
    make.add_argument('--receipt', type=Path, required=True)
    make.add_argument('--receipt-sha256', required=True)
    make.add_argument('--root', required=True)
    make.add_argument('--source-root', type=Path, action='append', required=True)
    make.add_argument('--output', type=Path, required=True)
    check = commands.add_parser('audit')
    check.add_argument('--bundle', type=Path, required=True)
    check.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'export':
        value = export(args.receipt, args.receipt_sha256, args.root, args.source_root, args.output)
        print(json.dumps({key: value[key] for key in ['root','producer_receipt_sha256','file_count','bytes','accepted']}))
    else:
        print(json.dumps(audit(args.bundle, args.workspace)))


if __name__ == '__main__':
    main()
