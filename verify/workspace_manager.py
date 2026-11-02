"""Fresh, source-only workspaces of the pinned benchmark and one official `lake test` on them.

The caller sets BASE (the run directory: workspaces/, verification/, logs/, receipts/ go there),
BENCHMARK (a clean benchmark checkout whose .lake/packages holds the built dependencies) and
BENCHMARK_COMMIT. EXTRA_ENV adds variables to the `lake test` environment (the CI envelope sets
LEAN_NUM_THREADS); the official run passes none.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

BASE = Path('.')
BENCHMARK = Path('benchmark-clean')
BENCHMARK_COMMIT = ''
EXTRA_ENV: dict = {}
ALLOWED_ENV = ['HOME', 'USER', 'PATH', 'TMPDIR', 'ELAN_HOME', 'COMPARATOR_BIN',
               'COMPARATOR_LANDRUN', 'COMPARATOR_LEAN4EXPORT', 'COMPARATOR_NANODA']


def slug(text):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', text):
        raise ValueError('Invalid problem/workspace identifier')
    return text


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_benchmark():
    head = subprocess.check_output(['git', '-C', str(BENCHMARK), 'rev-parse', 'HEAD'], text=True).strip()
    if head != BENCHMARK_COMMIT:
        raise ValueError('Unexpected benchmark HEAD')
    changed = subprocess.check_output(['git', '-C', str(BENCHMARK), 'status', '--porcelain', '--untracked-files=no'], text=True)
    if changed.strip():
        raise ValueError('Tracked benchmark files modified')


def initialize(problem, target):
    check_benchmark()
    source = BENCHMARK / 'generated' / slug(problem)
    if not source.is_dir():
        raise ValueError('Unknown problem')
    if target.exists():
        raise ValueError('Target already exists; use another workspace label')
    shutil.copytree(source, target)
    (target / '.lake').mkdir(exist_ok=True)
    (target / '.lake/packages').symlink_to(BENCHMARK / '.lake/packages', target_is_directory=True)
    shutil.copy2(BENCHMARK / 'lake-manifest.json', target / 'lake-manifest.json')
    (target / '.campaign.json').write_text(json.dumps({'problem_id': problem, 'benchmark_commit': BENCHMARK_COMMIT}, indent=2) + '\n')
    return target


def submission_paths(workspace):
    files = [workspace / 'Submission.lean']
    helper = workspace / 'Submission'
    if helper.is_symlink():
        raise ValueError('Submission symlinks are forbidden')
    if helper.exists():
        for path in helper.rglob('*'):
            if path.is_symlink():
                raise ValueError('Submission symlinks are forbidden')
            if path.is_file():
                if path.suffix != '.lean':
                    raise ValueError('Only Lean source files are allowed in Submission/')
                files.append(path)
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or symlinked Submission.lean')
    return sorted(files)


def verify(label, timeout):
    check_benchmark()
    candidate = BASE / 'workspaces' / slug(label)
    metadata = json.loads((candidate / '.campaign.json').read_text())
    problem = slug(metadata['problem_id'])
    if metadata['benchmark_commit'] != BENCHMARK_COMMIT:
        raise ValueError('Candidate benchmark pin mismatch')
    source = BENCHMARK / 'generated' / problem
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if relative.parts[0] == 'Submission' or str(relative) == 'Submission.lean':
            continue
        if path.is_file() and digest(path) != digest(candidate / relative):
            raise ValueError('Candidate changed trusted file: ' + str(relative))
    sources = submission_paths(candidate)
    now = datetime.datetime.now(datetime.timezone.utc)
    run_id = label + '-' + now.strftime('%Y%m%dT%H%M%S%fZ')
    workspace = initialize(problem, BASE / 'verification' / run_id)
    # Preserve no build outputs: verification starts with trusted generated files.
    if (workspace / 'Submission').exists():
        shutil.rmtree(workspace / 'Submission')
    hashes = {}
    for path in sources:
        relative = path.relative_to(candidate)
        dest = workspace / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        hashes[str(relative)] = digest(dest)
    log = BASE / 'logs' / ('verifier-' + run_id + '.log')
    receipt = BASE / 'receipts' / ('verifier-' + run_id + '.json')
    env = {k: os.environ[k] for k in ALLOWED_ENV if k in os.environ}
    env.update({str(k): str(v) for k, v in EXTRA_ENV.items()})
    start = time.monotonic()
    with log.open('w') as output:
        process = subprocess.Popen(['lake', 'test'], cwd=workspace, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code = 124
            timed_out = True
            output.write('\nHarness timeout: acceptance not established.\n')
    body = log.read_text(errors='replace')
    record = {'problem_id': problem, 'candidate': str(candidate), 'verification_workspace': str(workspace),
              'benchmark_commit': BENCHMARK_COMMIT, 'started_at': now.isoformat(),
              'elapsed_seconds': round(time.monotonic() - start, 3), 'command': ['lake', 'test'],
              'exit_code': code, 'timed_out': timed_out, 'source_sha256': hashes,
              'extra_env': dict(EXTRA_ENV),
              'nanoda_accepted': 'nanoda kernel accepts the solution' in body,
              'lean_kernel_accepted': 'Lean default kernel accepts the solution' in body,
              'illegal_sorry_axiom': "Illegal axiom detected: 'sorryAx'" in body,
              'log': str(log), 'log_sha256': digest(log)}
    record['accepted'] = code == 0 and record['nanoda_accepted'] and record['lean_kernel_accepted']
    receipt.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(dict(record, receipt=str(receipt)), indent=2))
    return 0 if record['accepted'] else (code if code else 1)
