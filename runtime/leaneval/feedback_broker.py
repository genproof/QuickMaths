"""Controller-owned feedback executor for model sandboxes with read-only job state.

The RPC accepts only a feedback action and query for one fixed job. It exposes no
ledger mutation API, shell command, path override, or accounting settlement API.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import urllib.error
import urllib.request

from leaneval.runtime import RuntimeBlocked
from leaneval.verifier import atomic_json

ACTIONS = {'build', 'check', 'search', 'preflight', 'repair', 'inspect', 'inspect_partial', 'cost', 'incremental'}


def request(job: Path, action: str, query: str, timeout: float):
    endpoint = json.loads((job / 'feedback-broker.json').read_text())
    if endpoint.get('job') != str(job.resolve()) or endpoint.get('host') != '127.0.0.1':
        raise RuntimeBlocked('Invalid controller feedback endpoint')
    body = json.dumps({'token': endpoint['token'], 'action': action, 'query': query}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{int(endpoint['port'])}/feedback", data=body,
                                 headers={'Content-Type': 'application/json'})
    # Ignore user proxy settings for a strictly loopback controller endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout + 45) as response:
            result = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeBlocked('Controller feedback unavailable; no local fallback') from exc
    if not isinstance(result, dict) or 'exitcode' not in result:
        raise RuntimeBlocked('Invalid controller feedback result')
    return result


def ready_footer(feedback_dir: Path, files: dict[str, bytes], limit: int = 60) -> str | None:
    """lrh: compact.ready_note for the current files, from the newest feedback records."""
    import hashlib
    from leaneval import compact
    if not files or compact.code_sorry(files):
        return None
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    steps = []
    for path in sorted(Path(feedback_dir).glob('step-*.json'))[-limit:]:
        try:
            steps.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return compact.ready_note(compact.ready_state(steps, inventory, files))


@contextmanager
def serve(job: Path):
    from leaneval import worker
    cfg = worker.load_config(job / 'worker.json')
    token = secrets.token_hex(32)
    active: set[threading.Thread] = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            active.add(threading.current_thread())
            try:
                self._post()
            finally:
                active.discard(threading.current_thread())

        def _post(self):
            self.connection.settimeout(10)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.path != '/feedback' or not 0 < size <= 65536:
                    raise ValueError('Invalid feedback request')
                data = json.loads(self.rfile.read(size))
                if set(data) != {'token', 'action', 'query'} or not hmac.compare_digest(data['token'], token):
                    raise ValueError('Invalid feedback capability')
                if data['action'] not in ACTIONS or not isinstance(data['query'], str) or len(data['query']) > 32768:
                    raise ValueError('Invalid feedback action')
                if data['action'] == 'incremental':
                    from leaneval.incremental import execute
                    result = execute(job, data['query'])
                else:
                    result = worker.feedback(cfg, data['action'], data['query'], _controller=True)
                # lrh: the stall clock lives in the job directory, which the sandboxed `./lean`
                # client can only read; the broker keeps it and returns the note. It also says
                # when only the official verification is left (READY footer).
                try:
                    import time
                    from leaneval.compact import stall_note
                    files = worker.proof_files(Path(cfg['local_workspace']))
                    note = stall_note(job, files, time.time())
                    if note and isinstance(result, dict):
                        result['stall_note'] = note
                    ready = ready_footer(Path(cfg['feedback_dir']), files)
                    if ready and isinstance(result, dict):
                        result['ready_note'] = ready
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                code = 200
            except RuntimeBlocked as exc:
                code = 200
                result = {'exitcode': None, 'accepted': False, 'compiler_invoked': False,
                          'phase': 'lineage_budget', 'outcome_class': 'budget_blocked',
                          'stderr': str(exc)}
            except Exception as exc:
                code = 400
                result = {'exitcode': None, 'accepted': False, 'compiler_invoked': False,
                          'phase': 'controller_feedback', 'outcome_class': 'controller_error',
                          'stderr': f'{type(exc).__name__}: {exc}'}
            body = json.dumps(result).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # The executed check and its accounting remain on disk.

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    # lrh: request threads are daemons and close_server() waits for them only CLOSE_GRACE_SECONDS;
    # with non-daemon threads one stuck request kept a finished round's process (and its runner)
    # alive for hours (2026-10-18).
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    endpoint = job / 'feedback-broker.json'
    atomic_json(endpoint, {'job': str(job.resolve()), 'host': '127.0.0.1',
                          'port': server.server_port, 'token': token, 'pid': os.getpid()})
    thread.start()
    try:
        yield
    finally:
        # lrh: the round is over; a build still running for it cannot be used and would keep
        # this runner busy (server_close joins the request threads) until its own timeout.
        from leaneval import inflight
        inflight.cancel_all()
        close_server(server, thread, active)
        if endpoint.exists() and json.loads(endpoint.read_text()).get('token') == token:
            endpoint.unlink()


CLOSE_GRACE_SECONDS = 300


def close_server(server, thread, active, grace: float = CLOSE_GRACE_SECONDS) -> int:
    """lrh: stop the broker and give its running requests `grace` seconds to finish their
    accounting; returns how many are still running (daemon threads, they end with the process)."""
    import time
    server.shutdown()
    server.server_close()
    thread.join()
    deadline = time.monotonic() + grace
    for request in list(active):
        request.join(max(0.0, deadline - time.monotonic()))
    return sum(1 for request in list(active) if request.is_alive())
