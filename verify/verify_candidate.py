"""One bounded source-only verification with preserved sources and the official oracle.

Runs inside a run directory made by verify_run.py prepare (cwd = that directory):

  config.json            problem_id, commit, benchmark (a clean checkout), benchmark_commit, timeout_seconds,
                         evaluation_flow, tools_bin and binary_sha256 (lake-comparator flow: the pinned bwrap;
                         comparator flow: the comparator, nanoda_bin, lean4export, landrun binaries),
                         manager_sha256, official_private_lean_pool, evaluator_sha256, lean_pool_commit,
                         optional lean_num_threads
  workspace_manager.py   the workspace manager, checked against manager_sha256
  evaluate_submission.py the organizers' evaluator at its pinned commit, checked against evaluator_sha256
  source/, source-sha256.json
                         the trimmed proof files (Submission.lean, Submission/**/*.lean) and their hashes

It copies the sources into a fresh workspace of the benchmark's generated/<problem>, lets the manager
run the official `lake test` (comparator with the Lean kernel and Nanoda) in another fresh workspace,
then checks that sources, protected files, binaries and Lean Pool sources did not change. Acceptance is the
official exit code 0 (what the organizers score) and both kernels' acceptance lines in the log. status.json
tracks the phase; result.json holds the outcome. Exit 0 only when accepted.

The manager and the binaries come from the run's config; LEAN_NUM_THREADS reaches `lake test` only when
the config sets lean_num_threads (the CI envelope).
"""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, data):
    p = BASE / name
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(p)


def main():
    config = json.loads((BASE / 'config.json').read_text())
    start = time.time()
    save('status.json', {'phase': 'prepare', 'accepted': False, 'started_at': start})
    manager_path = BASE / 'workspace_manager.py'
    assert sha(manager_path) == config['manager_sha256']
    spec = importlib.util.spec_from_file_location('workspace_manager', manager_path)
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    manager.BASE = BASE
    manager.BENCHMARK = Path(config['benchmark'])
    manager.BENCHMARK_COMMIT = config['benchmark_commit']
    if config.get('lean_num_threads'):
        manager.EXTRA_ENV = {'LEAN_NUM_THREADS': str(int(config['lean_num_threads']))}
    manager.check_benchmark()
    dependency_sources = {}
    if config.get('official_private_lean_pool'):
        evaluator_path = BASE / 'evaluate_submission.py'
        assert sha(evaluator_path) == config['evaluator_sha256']
        es = importlib.util.spec_from_file_location('official_evaluator', evaluator_path)
        evaluator = importlib.util.module_from_spec(es)
        sys.modules[es.name] = evaluator
        es.loader.exec_module(evaluator)
        packages = manager.BENCHMARK / '.lake/packages'
        pool = (packages / 'lean-pool').resolve()
        assert subprocess.check_output(['git', '-C', str(pool), 'rev-parse', 'HEAD'], text=True).strip() == config['lean_pool_commit']
        assert not subprocess.check_output(['git', '-C', str(pool), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
        names = subprocess.check_output(['git', '-C', str(pool), 'ls-files', '-z']).decode().split('\0')
        dependency_sources = {str(pool / n): sha(pool / n) for n in names if n}
        save('dependency-source-sha256.json', dependency_sources)
        original_initialize = manager.initialize

        def initialize_private(problem, target):
            workspace = original_initialize(problem, target)
            evaluator._check_requires_match_manifest(workspace / 'lakefile.toml', manager.BENCHMARK / 'lake-manifest.json')
            reason = evaluator._share_packages(workspace, packages)
            assert reason is None, reason
            return workspace

        manager.initialize = initialize_private
    tools = Path(config['tools_bin'])
    binaries = config['binary_sha256']
    assert all(sha(tools / n) == h for n, h in binaries.items())
    for name in ['workspaces', 'verification', 'logs', 'receipts']:
        (BASE / name).mkdir()
    candidate = manager.initialize(config['problem_id'], BASE / 'workspaces/candidate')
    expected = json.loads((BASE / 'source-sha256.json').read_text())
    if (candidate / 'Submission').exists():
        shutil.rmtree(candidate / 'Submission')
    for relative, digest in expected.items():
        source = BASE / 'source' / relative
        assert sha(source) == digest
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    protected = {str(p.relative_to(candidate)): sha(p) for p in candidate.rglob('*')
                 if p.is_file() and '.lake' not in p.relative_to(candidate).parts
                 and p.relative_to(candidate).parts[0] != 'Submission'
                 and p.name != 'Submission.lean'}
    save('protected-sha256.json', protected)
    maps = Path('/proc/sys/vm/max_map_count')
    save('status.json', {'phase': 'official_verify', 'accepted': False,
                        'started_at': start, 'source_count': len(expected),
                        'commit': config['commit'], 'problem_id': config['problem_id'],
                        'benchmark_commit': config['benchmark_commit'],
                        'timeout_seconds': config['timeout_seconds'],
                        'lean_num_threads': config.get('lean_num_threads'),
                        'map_limit_observed': maps.read_text().strip() if maps.exists() else None,
                        'map_limit_modified_by_this_unit': False})
    code = manager.verify('candidate', config['timeout_seconds'])
    receipts = list((BASE / 'receipts').glob('verifier-*.json'))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    workspace = Path(receipt['verification_workspace'])
    assert receipt['source_sha256'] == expected
    for root in [candidate, workspace]:
        actual = {str(p.relative_to(root)): sha(p) for p in manager.submission_paths(root)}
        assert actual == expected
        assert all(sha(root / p) == h for p, h in protected.items())
    manager.check_benchmark()
    assert all(sha(Path(p)) == h for p, h in dependency_sources.items())
    assert all(sha(tools / n) == h for n, h in binaries.items())
    log = Path(receipt['log'])
    assert sha(log) == receipt['log_sha256']
    illegal = 'Illegal axiom' in log.read_text(errors='replace')
    accepted = bool(code == 0 and receipt['accepted'] and not illegal)
    result = {'phase': 'finished', 'accepted': accepted, 'receipt': str(receipts[0]),
              'receipt_sha256': sha(receipts[0]), 'elapsed_seconds': time.time() - start,
              'sources_unchanged': True, 'protected_unchanged': True,
              'binaries_unchanged': True, 'illegal_axiom': illegal, 'exit_code': code,
              'official_private_lean_pool': bool(config.get('official_private_lean_pool')),
              'dependency_sources_unchanged': bool(dependency_sources) if config.get('official_private_lean_pool') else None,
              'lean_num_threads': config.get('lean_num_threads'),
              'evaluation_flow': config.get('evaluation_flow', 'comparator'),
              'map_limit_modified_by_this_unit': False}
    save('result.json', result)
    save('status.json', result)
    return 0 if accepted else (code or 1)


if __name__ == '__main__':
    try:
        result = main()
    except BaseException as exc:
        save('status.json', {'phase': 'error', 'accepted': False,
                             'error': type(exc).__name__ + ': ' + str(exc)})
        raise
    sys.exit(result)
