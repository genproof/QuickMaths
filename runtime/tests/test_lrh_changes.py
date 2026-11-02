"""Offline tests for the lrh changes to the upstream runtime."""
import contextlib
import http.client
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from leaneval import build_cache, compact  # noqa: E402


class BuildCacheTests(unittest.TestCase):
    def test_identity_ignores_module_paths(self):
        tools, template = {'lean': 'v4'}, {'Challenge.lean': b'x'}
        self.assertEqual(build_cache.identity(tools, template),
                         build_cache.identity(tools, template, {'Submission/A.lean'}))
        self.assertNotEqual(build_cache.identity(tools, template),
                            build_cache.identity({'lean': 'v5'}, template))

    def test_prune_removes_only_deleted_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            keep = ['lib/lean/Submission.olean', 'lib/lean/Submission/A.olean',
                    'lib/lean/Submission/A.ilean', 'ir/Submission/A.c', 'lib/lean/Challenge.olean']
            gone = ['lib/lean/Submission/B.olean', 'lib/lean/Submission/B.olean.hash',
                    'ir/Submission/B.setup.json', 'lib/lean/Submission/Sub/C.trace']
            for name in keep + gone:
                (build / name).parent.mkdir(parents=True, exist_ok=True)
                (build / name).write_text('x')
            removed = build_cache.prune_removed_modules(build, {'Submission.lean', 'Submission/A.lean'})
            self.assertEqual(removed, len(gone))
            for name in keep:
                self.assertTrue((build / name).exists(), name)
            for name in gone:
                self.assertFalse((build / name).exists(), name)

    def test_stat_seal_detects_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()  # macOS /var is a symlink; checked() rejects symlinks
            path = root / 'a.olean'
            path.write_text('x')
            first = build_cache.tree(root)
            self.assertEqual(first, build_cache.tree(root))
            os.utime(path, ns=(1, 1))
            self.assertNotEqual(first, build_cache.tree(root))


    def test_seal_ignores_lake_setup_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'ir').mkdir()
            (root / 'a.olean').write_text('x')
            setup = root / 'ir' / 'a.setup.json'
            setup.write_text('{}')
            first = build_cache.tree(root)
            old = build_cache.tree(root, skip_setup=False)
            setup.unlink()
            self.assertEqual(first, build_cache.tree(root))
            self.assertNotEqual(old, build_cache.tree(root, skip_setup=False))
            # schema-1 seals keep their old meaning
            self.assertNotEqual(build_cache.sealed_tree(root, {'tree': old}), old)
            self.assertEqual(build_cache.sealed_tree(root, {'tree': first, 'tree_schema': 2}), first)
            os.utime(root / 'a.olean', ns=(1, 1))
            self.assertNotEqual(first, build_cache.tree(root))


class WarmRoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.saved = (build_cache.WORK_ROOT, build_cache.SEAL_ROOT, build_cache.SEED_RESERVE_BYTES)
        build_cache.WORK_ROOT, build_cache.SEAL_ROOT = base/'work', base/'seals'
        build_cache.SEED_RESERVE_BYTES = 0
        self.jobs = base/'campaign'/'jobs'

    def tearDown(self):
        build_cache.WORK_ROOT, build_cache.SEAL_ROOT, build_cache.SEED_RESERVE_BYTES = self.saved
        self.tmp.cleanup()

    def original(self, attempt):
        return self.jobs/attempt/'workspace'/'.compiler'/'linnik'

    def sealed_round(self, attempt, outcome='check_succeeded'):
        ws = build_cache.workspace_for(self.original(attempt), 'build', 'Submission.Lanes.X.Main')
        build = ws/'.lake'/'build'/'lib'/'lean'/'Submission'
        build.mkdir(parents=True)
        (build/'A.olean').write_text('olean')
        evidence = {'eligible': True, 'identity': 'ID'}
        build_cache.finish(ws, evidence, {'exitcode': 0, 'checked_files_unchanged': True})
        self.assertTrue(evidence['sealed'])
        return ws

    def test_previous_rounds_of_a_lineage(self):
        names = [p.parts[-4] for p in build_cache.previous_rounds(self.original('full-w1-007-linnik-laneX-r3'))]
        self.assertEqual(names, ['full-w1-007-linnik-laneX-r2', 'full-w1-007-linnik-laneX'])
        self.assertEqual(build_cache.previous_rounds(self.original('full-w1-007-linnik-laneX')), [])

    def test_continuation_round_reuses_the_sealed_build(self):
        old = self.sealed_round('full-w1-007-linnik-laneX')
        new = build_cache.workspace_for(self.original('full-w1-007-linnik-laneX-r2'), 'build', 'Submission')
        self.assertTrue((new/'.lake'/'build'/'lib'/'lean'/'Submission'/'A.olean').is_file())
        self.assertFalse((old/'.lake'/'build').exists())
        package_root = Path(self.tmp.name).resolve()/'packages'
        package_root.mkdir()
        evidence = build_cache.prepare(new, package_root, 'ID', True)
        self.assertEqual((evidence['reused'], evidence['reason']), (True, 'verified_previous_scoped_build'))
        # a different trusted environment is not reused
        self.assertFalse(build_cache.prepare(new, package_root, 'OTHER', True)['reused'])

    def test_a_cleaned_setup_json_keeps_the_sealed_build(self):
        ws = self.sealed_round('full-w1-009-linnik-laneZ')
        ir = ws/'.lake'/'build'/'ir'/'Submission'
        ir.mkdir(parents=True)
        (ir/'A.setup.json').write_text('{}')
        evidence = {'eligible': True, 'identity': 'ID'}
        build_cache.finish(ws, evidence, {'exitcode': 0, 'checked_files_unchanged': True})
        (ir/'A.setup.json').unlink()  # a host disk cleaner
        package_root = Path(self.tmp.name).resolve()/'packages'
        package_root.mkdir()
        evidence = build_cache.prepare(ws, package_root, 'ID', True)
        self.assertEqual((evidence['reused'], evidence['reason']), (True, 'verified_previous_scoped_build'))

    def test_tampered_or_unsealed_build_is_not_carried(self):
        old = self.sealed_round('full-w1-008-linnik-laneY')
        os.utime(old/'.lake'/'build'/'lib'/'lean'/'Submission'/'A.olean', ns=(1, 1))
        new = build_cache.workspace_for(self.original('full-w1-008-linnik-laneY-r2'), 'build', 'Submission')
        self.assertFalse((new/'.lake'/'build').exists())
        self.assertTrue((old/'.lake'/'build').is_dir())
        # a round whose workspace already exists keeps it
        again = build_cache.workspace_for(self.original('full-w1-008-linnik-laneY-r2'), 'inspect', '')
        self.assertEqual(again, new)

    def sibling(self, runner, attempt, problem='linnik'):
        return self.jobs.parent.parent/runner/'jobs'/attempt/'workspace'/'.compiler'/problem

    def sealed_sibling(self, runner, attempt, problem='linnik', text='olean'):
        self.sibling(runner, attempt, problem).mkdir(parents=True)  # job dirs exist on a host
        ws = build_cache.workspace_for(self.sibling(runner, attempt, problem), 'build', 'Submission.Lanes.X.Main')
        build = ws/'.lake'/'build'/'lib'/'lean'/'Submission'
        build.mkdir(parents=True, exist_ok=True)  # may already be seeded from a sibling
        (build/'A.olean').write_text(text)
        evidence = {'eligible': True, 'identity': 'ID'}
        build_cache.finish(ws, evidence, {'exitcode': 0, 'checked_files_unchanged': True})
        self.assertTrue(evidence['sealed'])
        return ws

    def test_new_lane_is_seeded_from_the_newest_sibling_build(self):
        self.sealed_sibling('fleet-runner2', 'fleet-1-w2-001-linnik', text='old')
        time.sleep(0.01)
        src = self.sealed_sibling('fleet-runner3', 'fleet-1-w3-004-linnik-laneA', text='new')
        self.sealed_sibling('fleet-runner4', 'fleet-1-w4-002-mandelbrot_dimh-laneB', text='other')
        new = build_cache.workspace_for(self.sibling('fleet-runner5', 'fleet-1-w5-009-linnik-laneC'),
                                        'build', 'Submission.Lanes.C.Main')
        copied = new/'.lake'/'build'/'lib'/'lean'/'Submission'/'A.olean'
        self.assertEqual(copied.read_text(), 'new')
        self.assertTrue((src/'.lake'/'build'/'lib'/'lean'/'Submission'/'A.olean').is_file())  # source kept
        package_root = Path(self.tmp.name).resolve()/'packages'
        package_root.mkdir()
        evidence = build_cache.prepare(new, package_root, 'ID', True)
        self.assertEqual((evidence['reused'], evidence['reason']), (True, 'verified_previous_scoped_build'))
        # the source's own seal still verifies after the copy
        self.assertTrue(build_cache.prepare(src, package_root, 'ID', True)['reused'])

    def test_sibling_seed_prefers_the_largest_build_and_takes_interrupted_ones(self):
        big = self.sealed_sibling('fleet-runner2', 'fleet-1-w2-001-linnik', text='big')
        (big/'.lake'/'build'/'lib'/'lean'/'Submission'/'B.olean').write_text('b')
        evidence = {'eligible': True, 'identity': 'ID'}
        build_cache.finish(big, evidence, {'exitcode': -9, 'timed_out': True, 'process_group_terminated': True,
                                           'checked_files_unchanged': True, 'phase': 'module_build',
                                           'scope': 'module-only', 'measurement': {
                                               'phase': 'module_build',
                                               'reported_nodes': [{'target': 'Submission.A'}]}})
        self.assertEqual(evidence.get('sealed_outcome'), 'interrupted_module_build')
        time.sleep(0.01)
        small = self.sibling('fleet-runner3', 'fleet-1-w3-004-linnik-laneA')
        small.mkdir(parents=True)
        ws = build_cache.scoped(small)
        (ws/'.lake'/'build'/'lib'/'lean'/'Submission').mkdir(parents=True)
        (ws/'.lake'/'build'/'lib'/'lean'/'Submission'/'A.olean').write_text('small')
        build_cache.finish(ws, {'eligible': True, 'identity': 'ID'}, {'exitcode': 0, 'checked_files_unchanged': True})
        new = build_cache.workspace_for(self.sibling('fleet-runner5', 'fleet-1-w5-009-linnik-laneC'),
                                        'build', 'Submission')
        self.assertTrue((new/'.lake'/'build'/'lib'/'lean'/'Submission'/'B.olean').is_file())

    def test_sibling_seed_refuses_tampered_or_other_problems(self):
        src = self.sealed_sibling('fleet-runner3', 'fleet-1-w3-004-linnik-laneA')
        os.utime(src/'.lake'/'build'/'lib'/'lean'/'Submission'/'A.olean', ns=(1, 1))
        self.sealed_sibling('fleet-runner4', 'fleet-1-w4-002-mandelbrot_dimh-laneB')
        new = build_cache.workspace_for(self.sibling('fleet-runner5', 'fleet-1-w5-009-linnik-laneC'),
                                        'build', 'Submission')
        self.assertFalse((new/'.lake'/'build').exists())
        # a continuation round still prefers its own previous round
        own = self.sealed_sibling('fleet-runner6', 'fleet-1-w6-001-linnik-laneD', text='own')
        self.sealed_sibling('fleet-runner7', 'fleet-1-w7-001-linnik-laneE', text='sib')
        nxt = build_cache.workspace_for(self.sibling('fleet-runner6', 'fleet-1-w6-001-linnik-laneD-r2'),
                                        'build', 'Submission')
        self.assertEqual((nxt/'.lake'/'build'/'lib'/'lean'/'Submission'/'A.olean').read_text(), 'own')
        self.assertFalse((own/'.lake'/'build').exists())


class CompactTests(unittest.TestCase):
    def test_full_check_gate(self):
        ok = {'action': 'build', 'query': 'Submission', 'source_sha256': 'S', 'outcome_class': 'check_succeeded'}
        self.assertIsNone(compact.full_check_gate([ok], 'S'))
        self.assertIsNotNone(compact.full_check_gate([ok], 'T'))
        failed = dict(ok, outcome_class='lean_diagnostic')
        self.assertIsNotNone(compact.full_check_gate([ok, failed], 'S'))
        module = {'action': 'build', 'query': 'Submission.A', 'source_sha256': 'S', 'outcome_class': 'check_succeeded'}
        self.assertIsNone(compact.full_check_gate([ok, module], 'S'))
        # P1: plain `./lean check` is the light check; `--full` (query 'full') the official test
        self.assertFalse(compact.full_check('check', ''))
        self.assertTrue(compact.full_check('check', 'full'))
        self.assertTrue(compact.full_check('build', ''))
        self.assertFalse(compact.full_check('build', 'Submission'))
        # a sorry in code refuses the check before any build lookup; comments do not count
        files = {'Submission.lean': b'-- sorry\n/- sorry -/\ntheorem a : True := trivial\n'}
        self.assertIsNone(compact.full_check_gate([ok], 'S', files))
        files['Submission/B.lean'] = b'theorem b : True := by\n  sorry\n'
        refusal = compact.full_check_gate([ok], 'S', files)
        self.assertIn('Submission/B.lean', refusal)
        self.assertIn('fails on any sorry', refusal)

    def test_round_clock_footer(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            self.assertIsNone(compact.round_clock(job, 0))
            (job / 'round-clock.json').write_text('{"deadline": 7200, "handoff_seconds": 900}')
            self.assertEqual(compact.round_clock(job, 3600), 'Round time left: 60 min.')
            self.assertIn('HANDOFF', compact.round_clock(job, 6700))
            self.assertIn('Last failure', compact.round_clock(job, 6700))
            self.assertIn('0 min', compact.round_clock(job, 9000))

    def test_stall_note_after_an_hour_without_a_closed_sorry(self):
        files = {'Submission.lean': b'theorem a : True := sorry\n-- sorry\ntheorem b : True := trivial\n',
                 'Submission/X.lean': b'/- sorry -/\nlemma c (n : Nat) : n = n := by\n  sorry\n',
                 'NOTES.md': b'sorry'}
        self.assertEqual(compact.frontier(files), ['Submission.lean:a', 'Submission/X.lean:c'])
        with tempfile.TemporaryDirectory() as tmp:
            job, t0 = Path(tmp), 1000.0
            self.assertIsNone(compact.stall_note(job, files, t0))
            self.assertIsNone(compact.stall_note(job, files, t0 + 59 * 60))
            note = compact.stall_note(job, files, t0 + 61 * 60)
            self.assertIn('No sorry closed for 61 min (2 open)', note)
            self.assertIsNone(compact.stall_note(job, files, t0 + 70 * 60))  # not repeated at once
            self.assertIsNotNone(compact.stall_note(job, files, t0 + 82 * 60))
            # a decomposition (new open pieces) does not restart the clock ...
            split = dict(files, **{'Submission/Y.lean': b'theorem d : True := sorry\n'})
            self.assertIsNotNone(compact.stall_note(job, split, t0 + 103 * 60))
            # ... closing any open piece does
            closed = dict(split, **{'Submission/Y.lean': b'theorem d : True := trivial\n'})
            self.assertIsNone(compact.stall_note(job, closed, t0 + 104 * 60))
            self.assertIsNone(compact.stall_note(job, closed, t0 + 160 * 60))
            self.assertIsNotNone(compact.stall_note(job, closed, t0 + 165 * 60))
            # no open sorry: never a note
            done = {'Submission.lean': b'theorem a : True := trivial\n'}
            self.assertIsNone(compact.stall_note(job, done, t0 + 999 * 60))

    def test_full_check_gets_long_timeout(self):
        try:
            from leaneval import worker
        except ModuleNotFoundError as exc:  # the verifier's `gate` package exists only on hosts
            self.skipTest(str(exc))
        cfg = {'timeout_seconds': 1800}
        self.assertEqual(worker.call_seconds(cfg, 'check', 'full'), compact.FULL_CHECK_SECONDS)
        self.assertEqual(worker.call_seconds(cfg, 'check', ''), 1800)  # light check
        self.assertEqual(worker.call_seconds(cfg, 'build', 'Submission.A'), 1800)

    def test_render_errors_sorry_warnings(self):
        result = {'action': 'build', 'query': 'Submission.A', 'step_id': 'abcdef1234',
                  'outcome_class': 'lean_diagnostic', 'end_to_end_seconds': 12.3, 'revision': 7,
                  'build_cache': {'eligible': True, 'reused': True},
                  'stdout': 'trace: huge LEAN_PATH line\n', 'stderr': '',
                  'diagnostics': [
                      {'severity': 'error', 'file': 'Submission/A.lean', 'line': 3, 'column': 2,
                       'message': "unknown identifier 'Nat.foo'", 'hint': 'existing names like ...'},
                      {'severity': 'warning', 'file': 'Submission/A.lean', 'line': 9, 'column': 0,
                       'message': "declaration uses 'sorry'"},
                      {'severity': 'warning', 'file': 'Submission/A.lean', 'line': 1, 'column': 0,
                       'message': 'unused variable'}]}
        text = compact.render(result)
        self.assertIn('FAILED (lean_diagnostic), 12 s, cache warm', text)
        self.assertIn("error Submission/A.lean:3:2: unknown identifier 'Nat.foo'", text)
        self.assertIn('hint: existing names like', text)
        self.assertIn('sorry: Submission/A.lean:9', text)
        self.assertIn('warnings: 1 hidden', text)
        self.assertNotIn('LEAN_PATH', text)
        self.assertIn('step-000007.json', text)

    def test_render_gate_refusal_once(self):
        text = compact.render({'action': 'check', 'query': '', 'outcome_class': 'invalid_request',
                               'stdout': '', 'stderr': 'Full check refused: x',
                               'required_next_action': 'Full check refused: x'})
        self.assertEqual(text.count('Full check refused'), 1)

    def test_render_failure_without_diagnostics_shows_tail(self):
        text = compact.render({'action': 'check', 'query': '', 'outcome_class': 'timeout',
                               'stdout': 'trace: x\nline one\n', 'stderr': 'boom'})
        self.assertIn('line one', text)
        self.assertIn('boom', text)
        self.assertNotIn('trace: x', text)

    def test_name_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / 'names.txt'
            index.write_text('Nat.succ_le_iff\nInt.succ_le_iff\nNat.le_succ\n')
            os.environ['LRH_NAME_INDEX'] = str(index)
            try:
                module = importlib.reload(compact)
                hint = module.name_hint("unknown identifier 'Real.succ_le_iff'")
                self.assertIn('Nat.succ_le_iff', hint)
                self.assertIn('Int.succ_le_iff', hint)
                self.assertIsNone(module.name_hint('type mismatch'))
                close = module.name_hint("unknown constant 'Nat.le_succc'")
                self.assertIn('Nat.le_succ', close)
                lean435 = module.name_hint('Unknown constant `Nat.le_succc`')
                self.assertIn(': Nat.le_succ', lean435)  # same namespace ranked first
            finally:
                del os.environ['LRH_NAME_INDEX']
                importlib.reload(compact)

    def test_build_name_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '.lake' / 'packages' / 'lib'  # real packages live under .lake
            root.mkdir(parents=True)
            (root / '.lake').mkdir()
            (root / '.lake' / 'Skip.lean').write_text('theorem skipped : True := trivial\n')
            (root / 'A.lean').write_text('namespace Foo\ntheorem bar : True := trivial\n'
                                         '@[simp] lemma baz : True := trivial\nend Foo\n'
                                         'def _root_.qux : Nat := 1\nprotected theorem top : True := trivial\n')
            out = Path(tmp) / 'names.txt'
            self.assertEqual(compact.build_name_index([root], out), 4)
            self.assertEqual(out.read_text().split(), ['Foo.bar', 'Foo.baz', 'qux', 'top'])


class NudgeTests(unittest.TestCase):
    def test_nudge_until_five_minutes(self):
        from leaneval import agent_runner as runner
        round_s = 14400
        text = runner.nudge_text(3600, 10**9, round_s, 10**9)
        self.assertIn('60 minutes', text)
        self.assertIn('Keep proving', text)
        self.assertIn('critical path', text)
        self.assertIn('HANDOFF', runner.nudge_text(600, 10**9, round_s, 10**9))
        for field in ('Next step', 'Active tactic', 'Last failure', 'Do not retry', 'TACTICS'):
            self.assertIn(field, runner.nudge_text(600, 10**9, round_s, 10**9))
        self.assertIsNone(runner.nudge_text(299, 10**9, round_s, 10**9))
        self.assertIsNone(runner.nudge_text(3600, 10, round_s, 10**9))
        # tiny budgets keep the upstream proportional floor
        self.assertIsNotNone(runner.nudge_text(25, 10**9, 30, 10**9))
        self.assertIsNone(runner.nudge_text(4, 10**9, 30, 10**9))

    def test_round_ends_after_handoff_or_idle_replies(self):
        from leaneval import agent_runner as runner
        self.assertEqual(runner.end_after_turn(5, 1, False), (0, False))   # real work resets
        self.assertEqual(runner.end_after_turn(0, 0, False), (1, False))   # one idle reply: nudge
        self.assertEqual(runner.end_after_turn(0, 1, False), (2, True))    # second in a row: end
        self.assertEqual(runner.end_after_turn(7, 0, True), (0, True))     # handoff answered: end
        self.assertTrue(runner.handoff_due(600, 14400))
        self.assertFalse(runner.handoff_due(3600, 14400))


class InflightTests(unittest.TestCase):
    def test_cancel_kills_the_tracked_build_group(self):
        import signal
        import subprocess
        import time
        from leaneval import inflight
        # a "build" that starts its own child, like lake starting lean
        script = 'import subprocess, time; subprocess.Popen(["sleep", "60"]); time.sleep(60)'
        process = subprocess.Popen([sys.executable, '-c', script], start_new_session=True)
        try:
            with inflight.track(process):
                time.sleep(0.5)
                self.assertEqual(inflight.active(), 1)
                self.assertEqual(inflight.cancel_all(), 1)
                process.wait(timeout=10)
            self.assertEqual(inflight.active(), 0)
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:  # macOS: a not-yet-reaped zombie member answers EPERM
                    pass
                time.sleep(0.2)
            else:
                self.fail('the build group outlived the cancel')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
        self.assertEqual(inflight.cancel_all(), 0)

    def test_remote_program_and_broker_cancel_on_round_end(self):
        root = Path(__file__).resolve().parents[1] / 'leaneval'
        source = (root / 'worker.py').read_text()
        program = source.split("REMOTE_PROGRAM = r'''")[1].split("'''")[0]
        compile(program, 'remote', 'exec')
        self.assertIn('signal.signal(signal.SIGTERM, _terminate)', program)
        self.assertIn('_running.remove((p, stage_scope))', program)
        broker = (root / 'feedback_broker.py').read_text()
        self.assertLess(broker.index('inflight.cancel_all()'), broker.index('server.server_close()'))
        # the stall clock is kept by the broker (the sandboxed client cannot write the job dir)
        self.assertIn("result['stall_note'] = note", broker)
        self.assertIn("result.get('stall_note')", source)
        self.assertIn('inflight.track(process, stage_scope)', (root / 'incremental.py').read_text())


class BrokerCloseTests(unittest.TestCase):
    def test_a_stuck_request_does_not_keep_the_broker_open(self):
        import threading
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        try:
            from leaneval import feedback_broker
        except ModuleNotFoundError as exc:  # the host venv provides `gate`; deploy_runtime.sh runs this there
            self.skipTest(f'runtime dependency missing: {exc}')
        release, entered, active = threading.Event(), threading.Event(), set()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                active.add(threading.current_thread())
                try:
                    entered.set()
                    release.wait(30)
                    self.send_response(200)
                    self.end_headers()
                finally:
                    active.discard(threading.current_thread())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/feedback'
        def request():
            # the server goes away under this request on purpose; its error is not the test's subject
            with contextlib.suppress(OSError, http.client.HTTPException):
                urllib.request.urlopen(urllib.request.Request(url, data=b'{}'), timeout=30).read()

        client = threading.Thread(target=request, daemon=True)
        client.start()
        self.assertTrue(entered.wait(10))
        started = time.monotonic()
        left = feedback_broker.close_server(server, thread, active, grace=0.5)
        self.assertEqual(left, 1)
        self.assertLess(time.monotonic() - started, 5)
        release.set()
        broker = (Path(__file__).resolve().parents[1] / 'leaneval' / 'feedback_broker.py').read_text()
        self.assertIn('server.daemon_threads = True', broker)


class RecordBoundTests(unittest.TestCase):
    def imports(self):
        try:
            from leaneval import frontier, worker
        except ModuleNotFoundError as exc:  # host venv only; deploy_runtime.sh runs these there
            self.skipTest(f'runtime dependency missing: {exc}')
        return worker, frontier

    def test_an_oversized_record_is_trimmed_keeping_errors_and_progress(self):
        worker, _ = self.imports()
        warn = {'severity': 'warning', 'message': 'This simp argument is unused: ' + 'x' * 200}
        result = {'diagnostics': [dict(warn) for _ in range(6000)]
                  + [{'severity': 'error', 'message': 'boom'}, {'severity': 'warning', 'message': "uses 'sorry'"}],
                  'stdout': '\n'.join(['w' * 300] * 20000 + ['\u2714 [5/9] Built Submission.A (3s)'] + ['w' * 300] * 20000)}
        worker._bound_record(result, limit=1024 * 1024)
        self.assertIn('record_bounded', result)
        self.assertIn({'severity': 'error', 'message': 'boom'}, result['diagnostics'])
        self.assertTrue(any('sorry' in d['message'] for d in result['diagnostics']))
        self.assertLessEqual(len(result['diagnostics']), 502)
        self.assertIn('Built Submission.A (3s)', result['stdout'])
        small = {'diagnostics': [warn], 'stdout': 'ok'}
        self.assertNotIn('record_bounded', worker._bound_record(small))

    def test_proof_feedback_skips_an_oversized_record(self):
        _, frontier = self.imports()
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp).resolve()
            fb = job / 'workspace' / '.feedback'
            fb.mkdir(parents=True)
            (fb / 'step-000001.json').write_text('{"action": "build", "query": "Submission.A", "step_id": "a"}')
            big = fb / 'step-000002.json'
            with big.open('wb') as handle:
                handle.truncate(33 * 1024 * 1024)
            os.utime(big, ns=(time.time_ns(), time.time_ns()))
            entry = frontier.proof_feedback(job)
            self.assertEqual(entry.get('step_id'), 'a')


if __name__ == '__main__':
    unittest.main()
