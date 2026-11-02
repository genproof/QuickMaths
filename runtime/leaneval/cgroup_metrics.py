"""Read-only cgroup-v2 envelopes. Shared counters are never per-check costs."""
from pathlib import Path, PurePosixPath
import os
import time

MAX_BYTES = 65536
MAX_DEPTH = 64
FILES = ('cpu.stat', 'memory.current', 'memory.peak', 'memory.events',
         'memory.swap.current', 'memory.swap.peak', 'pids.current')
LIMITS = ('cpu.max', 'memory.max', 'memory.swap.max', 'pids.max',
          'cpuset.cpus.effective')


def read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        data = os.read(descriptor, MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('oversized cgroup metadata')
        return data.decode().strip()
    finally:
        os.close(descriptor)


def available(path):
    try:
        return read(path)
    except FileNotFoundError:
        return None


def number(value):
    if value is None:
        return None
    if not value.isdecimal():
        raise ValueError('invalid nonnegative cgroup counter')
    return int(value)


def counters(value):
    if value is None:
        return None
    result = {}
    for line in value.splitlines():
        key, raw = line.split()
        if key in result:
            raise ValueError('duplicate cgroup counter')
        result[key] = number(raw)
    return result


def identity(path):
    stat = path.stat()
    if path.is_symlink() or not path.is_dir():
        raise ValueError('invalid cgroup directory')
    return {'device': stat.st_dev, 'inode': stat.st_ino}


class Observation:
    """Bind a PID/start to its initial cgroup, then read that same cgroup at finish."""
    def __init__(self, pid, proc=Path('/proc'), root=Path('/sys/fs/cgroup')):
        self.proc, self.root = Path(proc), Path(root)
        self.pid, self.path, self.initial = pid, None, None
        self.error = None
        try:
            if type(pid) is not int or pid <= 0:
                raise ValueError('no measurable process')
            process = self.proc / str(pid)
            start = read(process / 'stat').rsplit(')', 1)[1].split()[19]
            memberships = [line[3:] for line in read(process / 'cgroup').splitlines()
                           if line.startswith('0::')]
            if len(memberships) != 1:
                raise ValueError('cgroup v2 membership unavailable')
            relative = PurePosixPath(memberships[0])
            if not relative.is_absolute() or '..' in relative.parts or len(relative.parts) > MAX_DEPTH:
                raise ValueError('invalid cgroup membership')
            self.path = self.root.joinpath(*relative.parts[1:])
            # Refuse symlink traversal outside the actual cgroup mount.
            if self.path.resolve() != self.path or self.root.resolve() != self.root:
                raise ValueError('symlink in cgroup path')
            self.binding = {'pid': pid, 'start_ticks': start, 'membership': str(relative),
                            'cgroup': identity(self.path),
                            'boot_id': available(self.proc / 'sys/kernel/random/boot_id')}
            self.initial = self.snapshot()
            if read(process / 'stat').rsplit(')', 1)[1].split()[19] != start:
                raise ValueError('process identity changed during capture')
            if read(process / 'cgroup').splitlines().count('0::' + str(relative)) != 1:
                raise ValueError('process moved during capture')
        except (OSError, ValueError, IndexError, UnicodeError) as error:
            self.error = type(error).__name__ + ': ' + str(error)

    def snapshot(self):
        before = identity(self.path)
        values = {}
        for name in FILES:
            raw = available(self.path / name)
            values[name] = counters(raw) if name in {'cpu.stat', 'memory.events'} else number(raw)
        hierarchy = []
        node = self.path
        while True:
            entry = {'membership': '/' + str(node.relative_to(self.root)) if node != self.root else '/',
                     'identity': identity(node), 'limits': {name: available(node / name) for name in LIMITS}}
            hierarchy.append(entry)
            if node == self.root:
                break
            if len(hierarchy) >= MAX_DEPTH:
                raise ValueError('cgroup hierarchy depth exceeded')
            node = node.parent
        if identity(self.path) != before or before != self.binding['cgroup']:
            raise ValueError('cgroup identity changed')
        memory, swap, quotas = [], [], []
        for row in hierarchy:
            for key, output in [('memory.max', memory), ('memory.swap.max', swap)]:
                raw = row['limits'][key]
                if raw is not None and raw != 'max':
                    output.append(number(raw))
            raw = row['limits']['cpu.max']
            if raw is not None:
                quota, period = raw.split()
                if number(period) == 0:
                    raise ValueError('zero CPU quota period')
                if quota != 'max':
                    quotas.append(number(quota) / number(period))
        return {'at': time.time(), 'counters': values, 'hierarchy': hierarchy,
                'effective_bounds': {'memory_max_bytes': min(memory) if memory else None,
                                     'swap_max_bytes': min(swap) if swap else None,
                                     'cpu_quota_cores': min(quotas) if quotas else None},
                'bounds_note': 'Null means no finite bound was found in the visible hierarchy. Hidden namespace ancestors are not inspected; affinity and host contention may impose tighter CPU constraints.'}

    def finish(self):
        result = {'schema_version': 1, 'status': 'unavailable', 'exclusive_to_check': False,
                  'scope': 'Observed cgroup and descendants, potentially shared with unrelated work.',
                  'per_check_peak_memory_bytes': None, 'per_check_cpu_seconds': None,
                  'accepted': False}
        if self.error:
            return dict(result, error=self.error)
        result.update(binding=self.binding, before=self.initial)
        try:
            after = self.snapshot()
            result.update(status='observed', after=after)
            # Deltas belong to this cgroup, never to the selected compiler alone.
            before_cpu = self.initial['counters']['cpu.stat'] or {}
            after_cpu = after['counters']['cpu.stat'] or {}
            deltas = {key: after_cpu[key] - before_cpu[key]
                      for key in before_cpu.keys() & after_cpu.keys()}
            if any(value < 0 for value in deltas.values()):
                raise ValueError('cgroup CPU counter reset')
            result['scope_cpu_counter_deltas'] = deltas
            result['limits_changed'] = self.initial['hierarchy'] != after['hierarchy']
            result['peak_note'] = 'memory.peak is cumulative for this cgroup lifetime, not the peak of this check or interval. No peak counter is reset.'
        except (OSError, ValueError, IndexError, UnicodeError) as error:
            result.update(status='unavailable', error=type(error).__name__ + ': ' + str(error))
            result.pop('scope_cpu_counter_deltas', None)
        return result


def summary(value, phase):
    """Bounded planner view; full counter provenance stays in the stage receipt."""
    before, after = value.get('before') or {}, value.get('after') or {}
    return {'phase': phase, 'status': value.get('status'), 'binding': value.get('binding'),
            'scope': value.get('scope'), 'exclusive_to_check': False,
            'effective_bounds': after.get('effective_bounds'),
            'limits_changed': value.get('limits_changed'),
            'scope_memory_peak_before_bytes': (before.get('counters') or {}).get('memory.peak'),
            'scope_memory_peak_after_bytes': (after.get('counters') or {}).get('memory.peak'),
            'scope_cpu_counter_deltas': value.get('scope_cpu_counter_deltas'),
            'per_check_peak_memory_bytes': None, 'per_check_cpu_seconds': None,
            'peak_note': value.get('peak_note'), 'error': value.get('error'), 'accepted': False}
