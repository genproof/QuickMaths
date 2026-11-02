"""Fresh incremental compiler cgroups inside an already-delegated broker unit."""
from pathlib import Path
import os
import re
import subprocess
import sys
import uuid

from leaneval import cgroup_metrics as cg

ROOT = Path('/sys/fs/cgroup')
_root = None
_setup = {'status': 'not_initialized'}
UNBOUNDED = {'cpu.max': 'max 100000', 'memory.max': 'max',
             'memory.high': 'max', 'memory.swap.max': 'max', 'pids.max': 'max',
             'cpuset.cpus': '', 'cpuset.mems': '', 'io.max': '',
             'cpu.max.burst': '0', 'memory.swap.high': 'max', 'memory.zswap.max': 'max',
             'memory.low': '0', 'memory.min': '0'}


def membership(pid):
    lines = cg.read(Path('/proc') / str(pid) / 'cgroup').splitlines()
    paths = [line[3:] for line in lines if line.startswith('0::')]
    if len(paths) != 1:
        raise ValueError('No unique cgroup-v2 membership')
    relative = Path(paths[0])
    if not relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid cgroup path')
    result = ROOT / str(relative).lstrip('/')
    if result.resolve() != result:
        raise ValueError('Symlink in cgroup path')
    return result


def limits(path):
    return {name: cg.available(path / name) for name in UNBOUNDED}


def prepare():
    """Run before serving requests, only inside a fresh delegated broker service."""
    global _root, _setup
    if _root is not None:
        return _setup
    try:
        root = membership(os.getpid())
        if root.parent != ROOT / 'system.slice' or not re.fullmatch(
                r'lean-(?:feedback-r66|cgroup-control-r107)-[0-9a-f]{16}\.service', root.name):
            raise ValueError('Not a supported isolated broker service')
        if root.stat().st_uid != os.geteuid() or not os.access(root, os.W_OK):
            raise ValueError('Broker cgroup is not delegated to this user')
        if cg.read(root / 'cgroup.procs').split() != [str(os.getpid())]:
            raise ValueError('Broker cgroup has other processes before initialization')
        supported = set(cg.read(root / 'cgroup.controllers').split())
        if not {'cpu', 'memory', 'pids'} <= supported:
            raise ValueError('Required delegated controllers unavailable')
        original = limits(root)
        controller = root / 'controller'
        controller.mkdir(exist_ok=True)
        if cg.read(controller / 'cgroup.procs'):
            raise ValueError('Controller leaf already has processes')
        (controller / 'cgroup.procs').write_text(str(os.getpid()))
        # Enabling controllers requires the parent domain to have no direct tasks.
        if cg.read(root / 'cgroup.procs'):
            raise ValueError('Broker parent still has direct processes')
        (root / 'cgroup.subtree_control').write_text('+cpu +memory +pids')
        if limits(root) != original:
            raise ValueError('Broker limits changed during initialization')
        _root = root
        _setup = {'status': 'ready', 'root': str(root), 'identity': cg.identity(root),
                  'limits': original, 'accepted': False}
    except (OSError, ValueError) as error:
        _setup = {'status': 'unavailable', 'error': str(error), 'accepted': False}
    return _setup


class Scope:
    def __init__(self, path=None, reason=None):
        self.path = path
        self.reason = reason
        self.identity = cg.identity(path) if path is not None else None
        self.observation = None
        self.active = False

    def kill(self):
        """Only this newly-created compiler group, never the broker or a solver."""
        if not self.active or self.path is None or cg.identity(self.path) != self.identity:
            return False
        kill = self.path / 'cgroup.kill'
        if not kill.exists():
            return False
        kill.write_text('1')
        return True

    def finish(self):
        result = {'status': 'unavailable', 'exclusive_to_check': False,
                  'per_check_peak_memory_bytes': None, 'per_check_cpu_seconds': None,
                  'accepted': False, 'scope': 'One gated compiler command and all its descendants.'}
        if not self.active:
            result['reason'] = self.reason or _setup
            if self.path is not None:
                try:
                    if cg.identity(self.path) == self.identity:
                        self.path.rmdir()
                except OSError:
                    pass
            return result
        try:
            if cg.identity(self.path) != self.identity:
                raise ValueError('Compiler cgroup identity changed')
            observation = self.observation.finish()
            result['observation'] = observation
            if observation['status'] != 'observed':
                raise ValueError('Compiler cgroup counters unavailable')
            events = cg.counters(cg.read(self.path / 'cgroup.events'))
            if events.get('populated') != 0:
                raise ValueError('Compiler descendants are still running')
            values = observation['after']['counters']
            peak = values['memory.peak']
            usage = (values['cpu.stat'] or {}).get('usage_usec')
            if peak is None or usage is None:
                raise ValueError('Missing memory peak or CPU accounting')
            result.update(status='complete', exclusive_to_check=True,
                          per_check_peak_memory_bytes=peak,
                          per_check_cpu_seconds=usage / 1_000_000,
                          fresh_cgroup=True, identity=self.identity,
                          accounting_note='Includes the gated launcher after assignment and compiler descendants; excludes parent broker and pre-assignment launcher startup.')
        except (OSError, ValueError) as error:
            result.update(status='unavailable', reason=str(error))
        finally:
            try:
                if cg.identity(self.path) == self.identity:
                    self.path.rmdir()
                    result['empty_cgroup_removed'] = True
            except OSError:
                result['empty_cgroup_removed'] = False
        return result


def attach_existing():
    """Recognize an inherited controller leaf without moving any process."""
    global _root, _setup
    if _root is not None:
        return _setup
    try:
        current = membership(os.getpid())
        root = current.parent
        if (current.name != 'controller' or root.parent != ROOT / 'system.slice'
                or not re.fullmatch(r'lean-(?:feedback-r66|cgroup-control-r107)-[0-9a-f]{16}\.service', root.name)):
            raise ValueError('Not an inherited broker controller leaf')
        if root.stat().st_uid != os.geteuid() or not os.access(root, os.W_OK):
            raise ValueError('Inherited broker root is not delegated to this user')
        if cg.read(root / 'cgroup.procs'):
            raise ValueError('Inherited root has direct processes')
        if str(os.getpid()) not in cg.read(current / 'cgroup.procs').split():
            raise ValueError('Process is not in the inherited controller leaf')
        if not {'cpu', 'memory', 'pids'} <= set(cg.read(root / 'cgroup.subtree_control').split()):
            raise ValueError('Inherited controllers unavailable')
        if membership(os.getpid()) != current:
            raise ValueError('Process moved during inherited-root capture')
        _root = root
        _setup = {'status': 'attached', 'root': str(root), 'identity': cg.identity(root),
                  'limits': limits(root), 'accepted': False}
    except (OSError, ValueError) as error:
        _setup = {'status': 'unavailable', 'error': str(error), 'accepted': False}
    return _setup


def spawn(argv, **kwargs):
    """Same Popen contract; no second compiler execution on setup failure."""
    if _root is None:
        attach_existing()
    if _root is None:
        return subprocess.Popen(argv, **kwargs), Scope(reason=_setup)
    path = None
    try:
        current = membership(os.getpid())
        if current != _root / 'controller':
            raise ValueError('Broker moved outside its controller leaf')
        # A child-specific cap on the controller leaf must not be bypassed by siblings.
        if any(value not in (None, UNBOUNDED[name]) for name, value in limits(current).items()):
            raise ValueError('Controller leaf has additional resource limits')
        path = _root / ('check-' + uuid.uuid4().hex)
        path.mkdir()
        if cg.read(path / 'cgroup.procs'):
            raise ValueError('New compiler cgroup is populated')
        scope = Scope(path)
    except (OSError, ValueError) as error:
        if path is not None:
            try:
                path.rmdir()
            except OSError:
                pass
        return subprocess.Popen(argv, **kwargs), Scope(reason=str(error))
    read_fd, write_fd = os.pipe()
    try:
        options = dict(kwargs)
        options['pass_fds'] = tuple(options.get('pass_fds', ())) + (read_fd,)
        command = [sys.executable, '-I', str(Path(__file__).with_name('stage_exec.py')), str(read_fd), *argv]
        process = subprocess.Popen(command, **options)
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        path.rmdir()
        raise
    os.close(read_fd)
    try:
        (path / 'cgroup.procs').write_text(str(process.pid))
        if membership(process.pid) != path:
            raise ValueError('Compiler cgroup assignment failed')
        scope.observation = cg.Observation(process.pid)
        if scope.observation.error:
            raise ValueError(scope.observation.error)
        scope.active = True
    except (OSError, ValueError) as error:
        scope.reason = str(error)
    finally:
        # Release exactly the already-created process, even when accounting failed.
        try:
            os.write(write_fd, b'1')
        except BrokenPipeError:
            scope.active = False
            scope.reason = 'Compiler launcher exited before release'
        finally:
            os.close(write_fd)
    return process, scope
