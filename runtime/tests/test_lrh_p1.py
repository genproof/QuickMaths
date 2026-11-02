"""Offline tests for the lrh P1 runtime changes (2026-10-14).

Light `./lean check`, READY footer, separate official-check lock, compiler threads and OOM
classification, identical-failure cache, stale-compile abort, round-wall refusal.
"""
import importlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from leaneval import build_cache, compact, compile_policy  # noqa: E402


def import_worker():
    return importlib.import_module('leaneval.worker')


worker = import_worker()

SORRY_FREE = {'Submission.lean': b'import Submission.A\nnamespace Submission\ntheorem t : True := A.a\nend Submission\n',
              'Submission/A.lean': b'namespace Submission.A\ntheorem a : True := trivial\nend Submission.A\n'}
AXIOMS_OK = '\n'.join(json.dumps({'severity': 'information', 'data': f"'{n}' depends on axioms: [propext, Classical.choice, Quot.sound]",
                                  'fileName': '/x/ResearchLightCheck.lean', 'pos': {'line': 2, 'column': 0}})
                      for n in ('t1', 't2'))


def inventory(files):
    import hashlib
    return {n: hashlib.sha256(d).hexdigest() for n, d in sorted(files.items())}


class Job:
    """A prepared-looking job directory with a local-transport worker config."""

    def __init__(self, files=SORRY_FREE, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.job = self.root / 'job'
        self.workspace = self.job / 'workspace'
        (self.workspace / 'Submission').mkdir(parents=True)
        self.write(files)
        self.feedback_dir = self.workspace / '.feedback'
        self.feedback_dir.mkdir()
        self.cfg = {'transport': 'local', 'problem_id': 'p', 'local_workspace': str(self.workspace),
                    'remote_workspace': str(self.workspace / '.compiler/p'),
                    'remote_workbench': '/nonexistent/workbench', 'remote_agent': '/nonexistent/agent',
                    'feedback_dir': str(self.feedback_dir), 'timeout_seconds': 1800, **cfg}

    def write(self, files):
        for name, data in files.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def steps(self):
        return [json.loads(p.read_text()) for p in sorted(self.feedback_dir.glob('step-*.json'))]

    def close(self):
        self.tmp.cleanup()


class FakeRemote:
    """Stands in for request_remote; records requests, returns scripted results."""

    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def __call__(self, cfg, request, watch=None):
        self.requests.append({'cfg': dict(cfg), 'request': request, 'watch': watch})
        result = self.results.pop(0) if self.results else ok_build()
        return result(request) if callable(result) else dict(result)


def ok_build(**extra):
    return {'exitcode': 0, 'stdout': '', 'stderr': '', 'timed_out': False, 'phase': 'module_build',
            'scope': 'module-only', 'elapsed_seconds': 3.0, **extra}


def light_ok(request):
    files = {n: __import__('base64').b64decode(d) for n, d in request['files'].items()}
    return ok_build(light_check=True, hole_names=['t1', 't2'], statement_module='Solution',
                    checked_files=inventory(files), checked_files_unchanged=True,
                    solution_build={'exitcode': 0, 'stdout': '', 'stderr': '', 'timed_out': False},
                    axiom_probe={'exitcode': 0, 'stdout': AXIOMS_OK, 'stderr': '', 'timed_out': False})


def lean_error(file='Submission/A.lean'):
    return {'exitcode': 1, 'stdout': f'error: {file}:2:22: type mismatch\n', 'stderr': '',
            'timed_out': False, 'phase': 'module_build', 'scope': 'module-only', 'elapsed_seconds': 4.0}


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.saved_remote = worker.request_remote

    def tearDown(self):
        worker.request_remote = self.saved_remote
        for job in self.jobs:
            job.close()

    def job(self, files=SORRY_FREE, **cfg):
        job = Job(files, **cfg)
        self.jobs.append(job)
        return job

    def remote(self, *results):
        fake = FakeRemote(*results)
        worker.request_remote = fake
        return fake


class LightCheckTests(WorkerCase):
    def test_modes_and_caps(self):
        self.assertEqual(compact.check_mode('check', ''), 'light')
        self.assertEqual(compact.check_mode('check', 'full'), 'full')
        self.assertEqual(compact.check_mode('check', '--full'), 'full')
        self.assertEqual(compact.check_mode('build', ''), 'full')  # legacy query-less build
        self.assertIsNone(compact.check_mode('build', 'Submission'))
        cfg = {'timeout_seconds': 1800}
        self.assertEqual(worker.call_seconds(cfg, 'check', ''), 1800)
        self.assertEqual(worker.call_seconds(cfg, 'check', 'full'), compact.FULL_CHECK_SECONDS)
        self.assertEqual(worker.call_seconds(dict(cfg, full_check_seconds=9000), 'check', 'full'), 9000)
        # a smaller configured cap never lowers the runtime default
        self.assertEqual(worker.call_seconds(dict(cfg, full_check_seconds=60), 'check', 'full'),
                         compact.FULL_CHECK_SECONDS)

    def test_light_check_passes_and_never_accepts(self):
        job = self.job()
        fake = self.remote(light_ok)
        result = worker.feedback(job.cfg, 'check', '', _controller=True)
        self.assertEqual(fake.requests[0]['request']['action'], 'light_check')
        self.assertEqual(result['outcome_class'], 'check_succeeded')
        self.assertEqual(result['check_mode'], 'light')
        self.assertEqual(result['phase'], 'light_check')
        self.assertIs(result['accepted'], False)
        self.assertTrue(result['axiom_audit']['diagnostic_foundational'])
        self.assertIs(result['axiom_audit']['accepted'], False)
        self.assertEqual(result['feedback_queue'], 'compiler')
        self.assertEqual(result['lrh_version'], compact.LRH_VERSION)
        self.assertEqual(fake.requests[0]['cfg']['timeout_seconds'], 1800)  # not the full-check cap
        self.assertIn('Check passed', compact.render(result))
        step = job.steps()[0]
        self.assertEqual(step['revision'], 1)
        self.assertEqual(step['check_mode'], 'light')

    def test_light_check_refused_while_sorry_remains(self):
        job = self.job(dict(SORRY_FREE, **{'Submission/A.lean': b'theorem a : True := sorry\n'}))
        fake = self.remote()
        result = worker.feedback(job.cfg, 'check', '', _controller=True)
        self.assertEqual(fake.requests, [])
        self.assertEqual(result['phase'], 'full_check_gate')
        self.assertIn('Check refused', result['stderr'])

    def test_light_verdicts(self):
        base = dict(ok_build(), hole_names=['t1'], statement_module='Solution',
                    solution_build={'exitcode': 0, 'stdout': '', 'stderr': '', 'timed_out': False})
        self.assertEqual(compact.light_verdict(dict(base, exitcode=1)), {})  # build failed: as is
        statement = compact.light_verdict(dict(base, solution_build={
            'exitcode': 1, 'stdout': 'error: Solution.lean:3:2: type mismatch', 'stderr': '', 'timed_out': False}))
        self.assertEqual(statement['phase'], 'light_check_statement')
        self.assertIn('statement', statement['required_next_action'])
        sorry_ax = json.dumps({'severity': 'information', 'data': "'t1' depends on axioms: [propext, sorryAx]"})
        axioms = compact.light_verdict(dict(base, axiom_probe={'exitcode': 0, 'stdout': sorry_ax,
                                                               'stderr': '', 'timed_out': False}))
        self.assertEqual(axioms['exitcode'], 1)
        self.assertEqual(axioms['stdout'], '')  # axiom reports are summarised, not dumped
        self.assertEqual(axioms['phase'], 'light_check_axioms')
        self.assertIn('t1 uses sorryAx', axioms['required_next_action'])
        missing = compact.light_verdict(dict(base, axiom_probe={'exitcode': 0, 'stdout': '', 'stderr': '',
                                                                'timed_out': False}))
        self.assertIn('t1: no axiom report', missing['required_next_action'])
        no_names = compact.light_verdict(dict(base, hole_names=[]))
        self.assertEqual(no_names['phase'], 'request_validation')
        self.assertEqual(worker.classify_outcome(dict(base, **no_names)), 'invalid_request')

    def test_full_check_gate_accepts_a_light_check(self):
        sha = 'S'
        light = {'action': 'check', 'check_mode': 'light', 'source_sha256': sha, 'outcome_class': 'check_succeeded'}
        build = {'action': 'build', 'query': 'Submission', 'source_sha256': sha, 'outcome_class': 'check_succeeded'}
        self.assertIsNone(compact.full_check_gate([light], sha, SORRY_FREE))
        self.assertIsNone(compact.full_check_gate([build], sha, SORRY_FREE))
        self.assertIsNotNone(compact.full_check_gate([build, dict(light, outcome_class='lean_diagnostic')], sha, SORRY_FREE))
        self.assertIsNotNone(compact.full_check_gate([], sha, SORRY_FREE))
        self.assertIsNone(compact.full_check_gate([], sha, SORRY_FREE, light=True))


class ReadyTests(WorkerCase):
    def steps_for(self, files, **extra):
        return {'source_files': inventory(files), 'source_current': True, 'outcome_class': 'check_succeeded',
                **extra}

    def test_ready_state(self):
        built = self.steps_for(SORRY_FREE, action='build', query='Submission')
        self.assertEqual(compact.ready_state([built], inventory(SORRY_FREE), SORRY_FREE), 'built')
        light = self.steps_for(SORRY_FREE, action='check', query='', check_mode='light')
        self.assertEqual(compact.ready_state([built, light], inventory(SORRY_FREE), SORRY_FREE), 'checked')
        module = self.steps_for(SORRY_FREE, action='build', query='Submission.A')
        self.assertEqual(compact.ready_state([built, module], inventory(SORRY_FREE), SORRY_FREE), 'built')
        # the latest whole build saw other files, or failed: not ready
        other = dict(built, source_files={'Submission.lean': 'x'})
        self.assertIsNone(compact.ready_state([built, other], inventory(SORRY_FREE), SORRY_FREE))
        self.assertIsNone(compact.ready_state([dict(built, outcome_class='lean_diagnostic')],
                                              inventory(SORRY_FREE), SORRY_FREE))
        # a full check that timed out is not a rejection; one that failed with Lean errors is
        full = self.steps_for(SORRY_FREE, action='check', query='full', check_mode='full')
        self.assertEqual(compact.ready_state([built, dict(full, outcome_class='timeout')],
                                             inventory(SORRY_FREE), SORRY_FREE), 'built')
        self.assertIsNone(compact.ready_state([built, dict(full, outcome_class='lean_diagnostic')],
                                              inventory(SORRY_FREE), SORRY_FREE))
        self.assertEqual(compact.ready_state([full], inventory(SORRY_FREE), SORRY_FREE), 'checked')
        # refused or superseded calls carry no verdict and do not hide an earlier passing build
        refused = dict(built, outcome_class='invalid_request', phase='round_wall_gate')
        light_refused = dict(light, outcome_class='invalid_request')
        stopped = dict(built, outcome_class='superseded')
        self.assertEqual(compact.ready_state([built, refused, light_refused, stopped],
                                             inventory(SORRY_FREE), SORRY_FREE), 'built')
        self.assertIsNone(compact.full_check_gate([dict(built, source_sha256='S'), refused], 'S', SORRY_FREE))
        sorry = dict(SORRY_FREE, **{'Submission/A.lean': b'theorem a : True := sorry\n'})
        self.assertIsNone(compact.ready_state([self.steps_for(sorry, action='build', query='Submission')],
                                              inventory(sorry), sorry))

    def test_ready_note_texts(self):
        self.assertIsNone(compact.ready_note(None))
        for state in ('built', 'checked'):
            note = compact.ready_note(state)
            self.assertIn('READY_FOR_VERIFICATION', note)
            self.assertIn('outside this session', note)
        self.assertIn('--full` is not needed', compact.ready_note('built'))

    def test_broker_footer_from_feedback_records(self):
        from leaneval import feedback_broker
        job = self.job()
        self.remote(ok_build())
        worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        note = feedback_broker.ready_footer(job.feedback_dir, worker.proof_files(job.workspace))
        self.assertIn('`./lean build Submission` succeeded on exactly these', note)
        job.write({'Submission/A.lean': SORRY_FREE['Submission/A.lean'] + b'-- edited\n'})
        self.assertIsNone(feedback_broker.ready_footer(job.feedback_dir, worker.proof_files(job.workspace)))
        source = (Path(feedback_broker.__file__)).read_text()
        self.assertIn("result['ready_note'] = ready", source)
        self.assertIn("result.get('ready_note')", Path(worker.__file__).read_text())

    def test_full_check_timeout_is_not_a_rejection(self):
        job = self.job()
        self.remote(ok_build(), {'exitcode': -9, 'stdout': 'nanoda ...', 'stderr': '', 'timed_out': True,
                                 'phase': 'official_lake_test'},
                    {'exitcode': None, 'stdout': '', 'stderr': '', 'timed_out': True, 'phase': 'ssh_timeout'},
                    {'exitcode': 1, 'stdout': 'error: Lean exited with code 137', 'stderr': '',
                     'timed_out': False, 'phase': 'official_lake_test'})
        worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        result = worker.feedback(job.cfg, 'check', 'full', _controller=True)
        self.assertEqual(result['check_mode'], 'full')
        self.assertEqual(result['outcome_class'], 'timeout')
        self.assertEqual(result['feedback_queue'], 'official')
        self.assertTrue(result['not_a_rejection'])
        self.assertIn('NOT a rejection', result['required_next_action'])
        self.assertIn('READY_FOR_VERIFICATION', compact.render(result))
        # the transport timeout and a host kill of the official test say the same
        for _ in range(2):
            again = worker.feedback(job.cfg, 'check', 'full', _controller=True)
            self.assertTrue(again['not_a_rejection'])
        self.assertIn('stopped by the host', again['required_next_action'])
        # still READY-worthy: the latest whole build passed on these files
        self.assertEqual(compact.ready_state(job.steps(), inventory(SORRY_FREE), SORRY_FREE), 'built')


class LockTests(WorkerCase):
    def test_lock_names(self):
        self.assertEqual(worker.lock_names('check', 'full'), ('official.lock',))
        self.assertEqual(worker.lock_names('build', ''), ('official.lock',))
        self.assertEqual(worker.lock_names('check', ''), ('controller.lock',))
        self.assertEqual(worker.lock_names('build', 'Submission'), ('controller.lock',))
        self.assertEqual(worker.lock_names('inspect', 'Submission:x'), ('controller.lock',))
        self.assertEqual(worker.lock_names('preflight', ''), ('controller.lock', 'official.lock'))
        self.assertEqual(worker.lock_names('search', 'x'), ())

    def test_builds_run_while_a_full_check_holds_its_lock(self):
        job = self.job()
        release, entered = threading.Event(), threading.Event()

        def slow_full_check(request):
            entered.set()
            release.wait(30)
            return ok_build(phase='official_lake_test', scope=None)
        self.remote(ok_build(), slow_full_check, lean_error())
        worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(
            worker.feedback(job.cfg, 'check', 'full', _controller=True)))
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            started = time.monotonic()
            build = worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(build['outcome_class'], 'lean_diagnostic')
            self.assertEqual(build['revision'], 2)
        finally:
            release.set()
            thread.join(30)
        self.assertEqual(outcome['outcome_class'], 'check_succeeded')
        self.assertEqual(outcome['revision'], 3)  # numbered when it finished
        steps = job.steps()
        self.assertEqual([s['revision'] for s in steps], [1, 2, 3])
        self.assertEqual(steps[2]['previous_step_ids'], [steps[0]['step_id'], steps[1]['step_id']])


class ParallelismTests(WorkerCase):
    def test_threads_and_lake_jobs(self):
        self.assertEqual(compile_policy.lean_threads(None, 32), 8)
        self.assertEqual(compile_policy.lean_threads(None, 4), 4)
        self.assertEqual(compile_policy.lean_threads('12', 32), 12)
        self.assertEqual(compile_policy.lean_threads('junk', 2), 2)
        env = {'LEAN_NUM_THREADS': '6', 'LRH_LAKE_JOBS': '3'}
        self.assertEqual(compile_policy.parallelism({}, env), {'lean_num_threads': 6, 'lake_jobs': 3})
        self.assertEqual(compile_policy.parallelism({}, dict(env, LRH_LEAN_NUM_THREADS='5'))['lean_num_threads'], 5)
        self.assertEqual(compile_policy.parallelism({'lean_num_threads': 4, 'lake_jobs': 2}, env),
                         {'lean_num_threads': 4, 'lake_jobs': 2})
        self.assertEqual(compile_policy.parallelism({}, {'LEAN_NUM_THREADS': '0'}),
                         {'lean_num_threads': None, 'lake_jobs': None})
        self.assertEqual(compile_policy.lake_jobs_args(4, 'USAGE: lake [OPTIONS]\n  --dir, -d=file'), [])
        self.assertEqual(compile_policy.lake_jobs_args(4, '  --jobs, -j=N   max parallel jobs'), ['--jobs=4'])
        self.assertEqual(compile_policy.lake_jobs_args(None, '--jobs'), [])

    def test_settings_reach_the_remote_request(self):
        job = self.job(lean_num_threads=3)
        fake = self.remote(ok_build())
        worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
        self.assertEqual(fake.requests[0]['request']['config']['lean_num_threads'], 3)
        self.assertIn('lake_jobs', fake.requests[0]['request']['config'])

    def test_remote_program_passes_threads_into_landrun(self):
        source = Path(worker.__file__).read_text()
        program = source.split("REMOTE_PROGRAM = r'''")[1].split("'''")[0]
        compile(program, 'remote', 'exec')
        self.assertIn("env['LEAN_NUM_THREADS'] = str(lean_threads)", program)
        landruns = [part for part in program.split("landrun = ['landrun'")[1:]]
        self.assertEqual(len(landruns), 3)  # preflight, light check, scoped builds
        for part in landruns:
            self.assertIn("'--env', 'LEAN_NUM_THREADS'", part.split(']')[0])
        self.assertNotIn("'lake', 'build'", program)  # every build goes through lake_build()
        # SIGUSR1 handling is installed before the request is read
        self.assertLess(program.index('signal.signal(signal.SIGUSR1, _supersede)'),
                        program.index('request = json.load(sys.stdin)'))

    def test_oom_kill_is_an_environment_error(self):
        killed = {'exitcode': 1, 'stdout': 'error: Lean exited with code 137\n', 'stderr': '',
                  'timed_out': False, 'phase': 'module_build'}
        self.assertEqual(worker.classify_outcome(killed), 'environment_error')
        self.assertEqual(worker.classify_outcome({'exitcode': -9, 'timed_out': False, 'phase': 'module_build'}),
                         'environment_error')
        self.assertEqual(worker.classify_outcome({'exitcode': 137, 'timed_out': False, 'phase': 'module_build'}),
                         'environment_error')
        oom = {'exitcode': 1, 'stdout': 'error: x', 'timed_out': False, 'phase': 'module_build',
               'stage_measurements': [{'isolated_cgroup': {'observation': {'after': {'counters': {
                   'memory.events': {'oom_kill': 2}}}}}}]}
        self.assertIn('OOM', compile_policy.resource_killed(oom))
        # our own timeout kill stays a timeout; ordinary Lean errors stay diagnostics
        self.assertEqual(worker.classify_outcome({'exitcode': -9, 'timed_out': True, 'phase': 'module_build'}),
                         'timeout')
        self.assertEqual(worker.classify_outcome(lean_error()), 'lean_diagnostic')

    def test_oom_kill_never_opens_the_environment_circuit(self):
        job = self.job()
        killed = {'exitcode': 1, 'stdout': 'error: Lean exited with code 137\n', 'stderr': '',
                  'timed_out': False, 'phase': 'module_build', 'scope': 'module-only'}
        self.remote(killed, dict(killed), dict(killed))
        for _ in range(3):
            result = worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
            self.assertEqual(result['outcome_class'], 'environment_error')
            self.assertIn('not an error in your proof', result['required_next_action'])
        self.assertFalse((job.feedback_dir / 'environment-circuit.json').exists())


class CacheTests(WorkerCase):
    def test_identical_failure_is_served_from_history(self):
        job = self.job()
        fake = self.remote(lean_error(), lean_error())
        first = worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
        self.assertEqual(first['outcome_class'], 'lean_diagnostic')
        second = worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(second['cached_from_step'], first['step_id'])
        self.assertIs(second['compiler_invoked'], False)
        self.assertEqual(second['diagnostics'], first['diagnostics'])
        self.assertIn('Identical to step', compact.render(second))
        third = worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
        self.assertEqual(third['cached_from_step'], first['step_id'])  # not a copy of a copy
        # another query or changed files compile again
        worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        job.write({'Submission/A.lean': SORRY_FREE['Submission/A.lean'] + b'-- edit\n'})
        worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
        self.assertEqual(len(fake.requests), 3)

    def test_cache_rules(self):
        inv = inventory(SORRY_FREE)
        fail = {'action': 'build', 'query': 'Submission.A', 'source_files': inv, 'outcome_class': 'lean_diagnostic',
                'compiler_invoked': True, 'source_current': True, 'exitcode': 1, 'step_id': 'f',
                'diagnostics': [{'severity': 'error'}]}
        self.assertEqual(compile_policy.cached_failure([fail], 'build', 'Submission.A', inv)['step_id'], 'f')
        ok = dict(fail, outcome_class='check_succeeded', exitcode=0, step_id='o')
        self.assertIsNone(compile_policy.cached_failure([fail, ok], 'build', 'Submission.A', inv))
        self.assertIsNone(compile_policy.cached_failure([dict(fail, source_current=False)], 'build', 'Submission.A', inv))
        self.assertIsNone(compile_policy.cached_failure([dict(fail, diagnostics=[])], 'build', 'Submission.A', inv))
        self.assertIsNone(compile_policy.cached_failure([dict(fail, timed_out=True)], 'build', 'Submission.A', inv))
        full = dict(fail, action='check', query='full', check_mode='full')
        self.assertIsNone(compile_policy.cached_failure([full], 'check', 'full', inv))
        self.assertIsNone(compile_policy.cached_failure([fail], 'build', '', inv))


class RoundWallTests(WorkerCase):
    def test_refuses_a_call_that_cannot_finish(self):
        job = self.job()
        fake = self.remote(ok_build(), ok_build())
        worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        step = job.feedback_dir / 'step-000001.json'  # that build took 10 min
        step.write_text(json.dumps(dict(json.loads(step.read_text()), elapsed_seconds=600.0)))
        (job.job / 'round-clock.json').write_text(json.dumps({'deadline': time.time() + 300}))
        refused = worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(refused['phase'], 'round_wall_gate')
        self.assertEqual(refused['outcome_class'], 'invalid_request')
        self.assertIn('killed at the round end', refused['required_next_action'])
        # other calls without history in this round still run; plenty of time runs again
        worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
        (job.job / 'round-clock.json').write_text(json.dumps({'deadline': time.time() + 3600}))
        worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        self.assertEqual(len(fake.requests), 3)

    def test_expected_seconds_is_the_fastest_recent_run(self):
        steps = [{'action': 'build', 'query': 'Submission', 'compiler_invoked': True,
                  'outcome_class': 'check_succeeded', 'elapsed_seconds': s} for s in (900, 30, 700)]
        self.assertEqual(compile_policy.expected_seconds(steps, 'build', 'Submission'), 30)
        cached = dict(steps[0], cached_from_step='x', elapsed_seconds=0)
        self.assertEqual(compile_policy.expected_seconds([cached], 'build', 'Submission'), None)
        timeout = dict(steps[0], timed_out=True, outcome_class='timeout', elapsed_seconds=10,
                       lineage_compiler_allowance_seconds=1800)
        self.assertEqual(compile_policy.expected_seconds([timeout], 'build', 'Submission'), 1800)


class SupersedeTests(WorkerCase):
    FAKE_REMOTE = r'''
import json, os, signal, sys, time
request = json.load(sys.stdin)
mode = request['mode']
if mode == 'handler':
    def stop(signum, frame):
        print(json.dumps({'exitcode': -9, 'stdout': '', 'stderr': '', 'timed_out': False,
                          'superseded': True, 'phase': 'module_build'}))
        sys.stdout.flush()
        os._exit(0)
    signal.signal(signal.SIGUSR1, stop)
time.sleep(20)
print(json.dumps({'exitcode': 0, 'stdout': '', 'stderr': '', 'timed_out': False}))
'''

    def setUp(self):
        super().setUp()
        self.saved = (worker._remote_argv, compile_policy.SUPERSEDE_MIN_SECONDS,
                      compile_policy.SUPERSEDE_POLL_SECONDS)
        worker._remote_argv = lambda cfg: [sys.executable, '-c', self.FAKE_REMOTE]
        compile_policy.SUPERSEDE_MIN_SECONDS = 0.2
        compile_policy.SUPERSEDE_POLL_SECONDS = 0.1

    def tearDown(self):
        (worker._remote_argv, compile_policy.SUPERSEDE_MIN_SECONDS,
         compile_policy.SUPERSEDE_POLL_SECONDS) = self.saved
        super().tearDown()

    def run_with_edit(self, mode):
        job = self.job()
        files = worker.proof_files(job.workspace)
        closure = compile_policy.import_closure(files, 'Submission.A')
        watch = compile_policy.SourceWatch(job.workspace, closure, inventory(files))
        threading.Timer(0.5, lambda: job.write({'Submission/A.lean': b'-- edited\n'})).start()
        started = time.monotonic()
        result = worker.request_remote({'transport': 'local', 'timeout_seconds': 60}, {'mode': mode}, watch=watch)
        self.assertLess(time.monotonic() - started, 10)
        return result

    def test_edit_stops_the_compile(self):
        result = self.run_with_edit('handler')
        self.assertTrue(result['superseded'])
        self.assertEqual(result['superseded_files'], ['Submission/A.lean'])
        self.assertEqual(worker.classify_outcome(result), 'superseded')

    def test_signal_before_the_handler_is_still_superseded(self):
        result = self.run_with_edit('default')
        self.assertEqual(result['phase'], 'superseded')
        self.assertEqual(worker.classify_outcome(result), 'superseded')

    def test_unrelated_edit_or_same_content_does_not_stop(self):
        job = self.job()
        files = worker.proof_files(job.workspace)
        watch = compile_policy.SourceWatch(job.workspace, {'Submission/A.lean'}, inventory(files))
        self.assertEqual(watch.changed(), [])
        job.write({'Submission/A.lean': SORRY_FREE['Submission/A.lean']})  # rewritten, same bytes
        job.write({'Submission/B.lean': b'theorem b : True := trivial\n'})  # outside the closure
        self.assertEqual(watch.changed(), [])
        (job.workspace / 'Submission/A.lean').unlink()
        self.assertEqual(watch.changed(), ['Submission/A.lean'])

    def test_import_closure_and_watched_module(self):
        files = {'Submission.lean': b'import Mathlib\nimport Submission.A Submission.B\n',
                 'Submission/A.lean': b'-- import Submission.C\nimport Submission.D\n',
                 'Submission/B.lean': b'', 'Submission/C.lean': b'', 'Submission/D.lean': b''}
        self.assertEqual(compile_policy.import_closure(files, 'Submission'),
                         {'Submission.lean', 'Submission/A.lean', 'Submission/B.lean', 'Submission/D.lean'})
        self.assertEqual(compile_policy.import_closure(files, 'Submission.A'),
                         {'Submission/A.lean', 'Submission/D.lean'})
        self.assertIsNone(compile_policy.import_closure(files, 'Submission.Missing'))
        self.assertEqual(compile_policy.watched_module('build', 'Submission.A', False), 'Submission.A')
        self.assertEqual(compile_policy.watched_module('inspect', 'Submission.A:x', False), 'Submission.A')
        self.assertEqual(compile_policy.watched_module('check', '', True), 'Submission')
        self.assertIsNone(compile_policy.watched_module('check', 'full', False))  # never stops the official test
        self.assertIsNone(compile_policy.watched_module('inspect', 'Challenge:x', False))

    def test_feedback_passes_the_closure_watch(self):
        job = self.job()
        fake = self.remote(ok_build(), ok_build())
        worker._remote_argv = self.saved[0]
        worker.feedback(job.cfg, 'build', 'Submission.A', _controller=True)
        self.assertEqual(fake.requests[0]['watch'].names, ['Submission/A.lean'])
        superseded = ok_build(exitcode=-9, superseded=True, superseded_files=['Submission/A.lean'],
                              superseded_after_seconds=12.0)
        self.remote(superseded)
        result = worker.feedback(job.cfg, 'build', 'Submission', _controller=True)
        self.assertEqual(result['outcome_class'], 'superseded')
        self.assertIn('Stopped: Submission/A.lean changed', result['required_next_action'])
        self.assertNotIn('NOTE: files changed', compact.render(result))

    def test_superseded_build_keeps_the_scoped_cache(self):
        killed = {'exitcode': -9, 'timed_out': False, 'superseded': True, 'process_group_terminated': True,
                  'checked_files_unchanged': True, 'phase': 'module_build', 'scope': 'module-only',
                  'stdout': '', 'stderr': '', 'measurement': {'phase': 'module_build', 'reported_nodes': []}}
        self.assertEqual(build_cache.interrupted_outcome(killed), 'interrupted_module_build')
        timeout = dict(killed, superseded=False, timed_out=True)
        self.assertIsNone(build_cache.interrupted_outcome(timeout))  # upstream rule unchanged
        self.assertIsNone(build_cache.completed_outcome(killed))


class TextTests(unittest.TestCase):
    def test_hints_say_ready_after_the_light_check(self):
        from leaneval import agent_runner, jobs
        for text in (agent_runner.NUDGE, agent_runner.NUDGE_HANDOFF):
            self.assertIn('./lean check', text)
            self.assertIn('light', text)
            self.assertIn('READY_FOR_VERIFICATION', text)
        self.assertIn('./lean check --full', jobs.AGENT_INSTRUCTIONS)
        self.assertIn('NOT a rejection', compact.not_a_rejection(5400))
        self.assertIn('out of memory', compact.not_a_rejection(5400, 'out of memory: 1 process(es)'))


if __name__ == '__main__':
    unittest.main()
