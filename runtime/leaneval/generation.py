"""Advisory generation contracts. Byte/syntax assertions never establish a proof."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

from leaneval.artifacts import digest, proof_path
from leaneval.verifier import atomic_json


def regular(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Expected a workspace-relative path')
    target = root / path
    if any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise ValueError('Symlink in generation path')
    if not target.is_file():
        raise ValueError('Missing regular input/output: ' + name)
    return target


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def masked(source: str) -> str:
    """Remove comments and strings while preserving offsets and line boundaries."""
    chars = list(source)
    i = 0
    depth = 0
    string = False
    while i < len(source):
        if depth:
            if source.startswith('/-', i):
                depth += 1
                chars[i:i+2] = '  '
                i += 2
            elif source.startswith('-/', i):
                depth -= 1
                chars[i:i+2] = '  '
                i += 2
            else:
                if chars[i] != '\n':
                    chars[i] = ' '
                i += 1
        elif string:
            c = source[i]
            if c != '\n':
                chars[i] = ' '
            if c == '\\' and i + 1 < len(source):
                i += 1
                if chars[i] != '\n':
                    chars[i] = ' '
            elif c == '"':
                string = False
            i += 1
        elif source.startswith('/-', i):
            chars[i:i+2] = '  '
            depth = 1
            i += 2
        elif source.startswith('--', i):
            while i < len(source) and source[i] != '\n':
                chars[i] = ' '
                i += 1
        elif source[i] == '"':
            chars[i] = ' '
            string = True
            i += 1
        else:
            i += 1
    if depth or string:
        raise ValueError('Unclosed comment or string')
    return ''.join(chars)


HEADER = re.compile(r'(?m)^[ \t]*(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*(namespace|section|end|theorem|lemma|def|abbrev|opaque|instance|structure|inductive)\b[ \t]*([^\s(:={]+)?')


def declaration(source: str, name: str) -> str:
    """Conservative line-oriented Lean declaration selector; ambiguity fails closed."""
    text = masked(source)
    headers = list(HEADER.finditer(text))
    scopes: list[str] = []
    matches = []
    for index, header in enumerate(headers):
        kind, token = header.group(1), header.group(2) or ''
        if kind in {'namespace', 'section'}:
            scopes.append(token if kind == 'namespace' else '')
        elif kind == 'end':
            if scopes:
                scopes.pop()
        else:
            full = token.removeprefix('_root_.') if token.startswith('_root_.') else '.'.join([s for s in scopes if s] + [token])
            if full == name or ('.' not in name and token == name):
                finish = headers[index + 1].start() if index + 1 < len(headers) else len(text)
                matches.append(text[header.start():finish])
    if len(matches) != 1:
        raise ValueError('Declaration absent or ambiguous: ' + name)
    return matches[0]


def assertions(data: bytes, name: str, contains: list[str], absent: list[str]) -> None:
    if not contains and not absent:
        raise ValueError('At least one declaration assertion is required')
    body = declaration(data.decode('utf-8'), name)
    if any(not value or value not in body for value in contains):
        raise ValueError('Expected declaration content missing')
    if any(not value or value in body for value in absent):
        raise ValueError('Forbidden declaration content present')


def validate_bytes(files: dict[str, bytes], receipts: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for record in receipts:
        output = record['output']
        if output in seen or not proof_path(output):
            raise ValueError('Invalid/duplicate generation output')
        seen.add(output)
        if record.get('status') != 'generated':
            raise ValueError('Last generation failed: ' + output)
        if output not in files or sha(files[output]) != record['output_sha256']:
            raise ValueError('Generated output changed: ' + output)
        assertions(files[output], record['declaration'], record['contains'], record['absent'])
        result.append({'output': output, 'receipt_id': record['id'], 'receipt_sha256': digest(record),
                       'output_sha256': record['output_sha256']})
    return result


def validate(root: Path, files: dict[str, bytes]) -> list[dict]:
    folder = root / '.generation'
    if not folder.exists():
        return []
    registry = json.loads(regular(root, '.generation/index.json').read_text())
    records = []
    for output, name in registry.items():
        record = json.loads(regular(root, '.generation/' + name).read_text())
        if record['output'] != output:
            raise ValueError('Generation registry mismatch')
        for path, expected in record['inputs'].items():
            if sha(regular(root, path).read_bytes()) != expected:
                raise ValueError('Generation input changed: ' + path)
        records.append(record)
    validate_bytes(files, records)
    return records


def _generate(root: Path, args: argparse.Namespace) -> dict:
    if not proof_path(args.output):
        raise ValueError('Output must be a proof source')
    # Validate contract before starting user code; do not accept an empty assertion.
    if not args.contains and not args.absent:
        raise ValueError('At least one declaration assertion is required')
    inputs = {name: sha(regular(root, name).read_bytes()) for name in args.input + args.part}
    if not inputs:
        raise ValueError('Declare generator/input files with --input')
    folder = root / '.generation'
    folder.mkdir(exist_ok=True)
    index = folder / 'index.json'
    if folder.is_symlink() or index.is_symlink():
        raise ValueError('Generation metadata cannot be symlinked')
    registry = json.loads(index.read_text()) if index.exists() else {}
    record = {'schema': 1, 'id': uuid.uuid4().hex, 'output': args.output,
              'declaration': args.declaration, 'contains': args.contains, 'absent': args.absent,
              'inputs': inputs, 'parts': args.part, 'argv': args.argv,
              'started_at': time.time(), 'status': 'failed', 'accepted': False,
              'scope': 'syntactic generation contract; independent Lean verification required'}
    filename = record['id'] + '.json'
    # Register failure before execution so interrupted/failed regeneration invalidates old output.
    atomic_json(folder / filename, record)
    registry[args.output] = filename
    atomic_json(index, registry)
    before = sha(regular(root, args.output).read_bytes()) if (root / args.output).exists() else None
    try:
        if not args.argv:
            raise ValueError('Generator argv required after --')
        process = subprocess.Popen(args.argv, cwd=root, start_new_session=True)
        try:
            code = process.wait(timeout=args.seconds)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError('Generator timeout') from None
        record['exitcode'] = code
        if code != 0:
            raise ValueError('Generator failed')
        data = regular(root, args.output).read_bytes()
        record['output_sha256'] = sha(data)
        if before == record['output_sha256'] and not args.allow_unchanged:
            raise ValueError('Generator did not change output')
        for name, expected in inputs.items():
            if sha(regular(root, name).read_bytes()) != expected:
                raise ValueError('Input changed during generation: ' + name)
        assertions(data, args.declaration, args.contains, args.absent)
        record['status'] = 'generated'
    except (ValueError, OSError, UnicodeError) as exc:
        record['error'] = str(exc)
    record['finished_at'] = time.time()
    atomic_json(folder / filename, record)
    return record


def generate(root: Path, args: argparse.Namespace) -> dict:
    folder = root / '.generation'
    if folder.is_symlink():
        raise ValueError('Generation metadata cannot be symlinked')
    folder.mkdir(exist_ok=True)
    lock_path = folder / 'lock'
    if lock_path.is_symlink():
        raise ValueError('Generation lock cannot be symlinked')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _generate(root, args)


def main(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--declaration', required=True)
    parser.add_argument('--contains', action='append', default=[])
    parser.add_argument('--absent', action='append', default=[])
    parser.add_argument('--input', action='append', default=[])
    parser.add_argument('--part', action='append', default=[])
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--allow-unchanged', action='store_true')
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.argv[:1] == ['--']:
        args.argv = args.argv[1:]
    if not 0 < args.seconds <= 3600:
        parser.error('--seconds must be in (0, 3600]')
    result = generate(root, args)
    print(json.dumps(result, sort_keys=True))
    return int(result['status'] != 'generated')
