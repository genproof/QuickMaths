"""lrh: compiler processes running for the agent's feedback requests, cancellable at round end.

The feedback broker joins its request threads when the round ends. A build that was still
running (round time over, external STOP, a lane finished mid-build) used to keep the runner
and its queue busy until the build's own timeout (up to 30-90 minutes) and kept compiling in
the scoped build cache, outside the job workspace the runner reaps. The broker now cancels
the tracked process groups first.
"""
from __future__ import annotations

import contextlib
import os
import signal
import threading
from typing import Any, Iterator

_lock = threading.Lock()
_active: dict[int, tuple[Any, Any]] = {}


@contextlib.contextmanager
def track(process: Any, scope: Any = None) -> Iterator[None]:
    """Register `process` (a session leader, start_new_session=True) and its cgroup scope."""
    with _lock:
        _active[process.pid] = (process, scope)
    try:
        yield
    finally:
        with _lock:
            _active.pop(process.pid, None)


def active() -> int:
    with _lock:
        return len(_active)


def cancel_all(sig: int = signal.SIGTERM) -> int:
    """Signal every tracked process group and kill its cgroup scope; returns how many."""
    with _lock:
        items = list(_active.values())
    for process, scope in items:
        if process.poll() is not None:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sig)
        if scope is not None:
            with contextlib.suppress(OSError, AttributeError):
                scope.kill()
    return len(items)
