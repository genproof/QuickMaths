"""Bounded import planning and sampled resource observations; never proof authority."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any

from leaneval.artifacts import digest
from leaneval.cgroup_metrics import summary as cgroup_summary

NAME = re.compile(r"[\w']+(?:\.[\w']+)*", re.UNICODE)
HEADER_BYTES = 2097152
HEADER_TOKEN = re.compile(r"/-|-/|--|\n")


def imports(data: bytes, *, truncated: bool = False) -> tuple[list[str], bool]:
    # Stop at the first body declaration. Body strings/characters are not header syntax.
    if truncated:
        # A cut import token is not an unresolved module; defer its entire line.
        data = data[:data.rfind(b'\n') + 1]
    text = data.decode('utf-8')
    dependencies: list[str] = []
    prelude = False
    def line(value: str) -> bool:
        nonlocal prelude
        value = value.strip()
        if not value or value == 'module':
            return True
        if value == 'prelude':
            prelude = True
            return True
        match = re.fullmatch(r'(?:(?:public|private|meta)\s+)*import\s+(?:all\s+)?(.+)', value)
        if not match:
            return False
        names = match.group(1).split()
        if any(not NAME.fullmatch(name) for name in names):
            raise ValueError('Unsupported import syntax')
        dependencies.extend(names)
        return True
    chars: list[str] = []
    i = depth = 0
    ended = False
    while i < len(text):
        token = HEADER_TOKEN.search(text, i)
        if token is None:
            if not depth:
                chars.append(text[i:])
            break
        if not depth:
            chars.append(text[i:token.start()])
        value = token.group()
        i = token.end()
        if value == '/-':
            depth += 1
            chars.append(' ')
        elif value == '-/' and depth:
            depth -= 1
        elif value == '--' and not depth:
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
        elif value == '\n':
            if not line(''.join(chars)):
                ended = True
                break
            chars = []
        elif not depth:
            chars.append(value)
    if not ended and chars:
        ended = not line(''.join(chars))
    if not prelude:
        dependencies.insert(0, 'Init')
    return list(dict.fromkeys(dependencies)), ended or (not truncated and depth == 0)


def plan(overlay: dict[str, bytes], roots: list[Path], seeds: list[str],
         cache: Path, identity: dict[str, Any], *, seconds: float = 2,
         max_modules: int = 20000, selection: dict[str, Any] | None = None,
         max_total_modules: int | None = None) -> dict[str, Any]:
    """Pin-scoped header cache; candidate headers are always read from captured bytes."""
    absolute_limit = max_modules if max_total_modules is None else max_total_modules
    if (isinstance(max_modules, bool) or not isinstance(max_modules, int) or max_modules < 1
            or isinstance(absolute_limit, bool) or not isinstance(absolute_limit, int)
            or not max_modules <= absolute_limit <= 100000):
        raise ValueError('Node limits must be positive integers with initial <= total <= 100000')
    effective_limit = max_modules
    roots = list(roots) + [root / 'lake' for root in roots if (root / 'lake').is_dir()]
    started = time.monotonic()
    scope = digest({'tool_identity': identity, 'import_parser_revision': 5,
                    'roots': [str(root) for root in roots]})
    resume_scope = digest({'scope': scope, 'seeds': seeds,
                           'overlay': {name: hashlib.sha256(data).hexdigest()
                                       for name, data in sorted(overlay.items())}})
    cache.mkdir(parents=True, exist_ok=True)
    cache_file = cache / 'headers.json'
    try:
        prior = json.loads(cache_file.read_text())
        headers = prior['headers'] if prior['identity'] == scope else {}
    except (OSError, ValueError, KeyError, TypeError):
        headers = {}
    graph: dict[str, list[str]] = {}
    sources = {}
    unresolved = []
    uncertain = []
    pending = list(seeds)
    resumed = 0
    try:
        prior_graph = json.loads((cache / 'resume.json').read_text())
        if prior_graph['resume_scope'] == resume_scope and not prior_graph['unresolved'] and not prior_graph['uncertain_headers']:
            # Reuse only under the same candidate bytes and pinned environment.
            # Dependency stamps have the same contract as the header cache.
            for info in prior_graph['sources'].values():
                if info['origin'] == 'pinned_dependency':
                    stat = Path(info['path']).stat()
                    if info['stamp'] != [stat.st_size, stat.st_mtime_ns, stat.st_ino]:
                        raise ValueError('Dependency changed since partial traversal')
            if len(prior_graph['imports']) > absolute_limit:
                raise ValueError('Cached graph exceeds current node budget')
            graph = prior_graph['imports']
            sources = prior_graph['sources']
            pending = prior_graph['pending']
            resumed = len(graph)
            effective_limit = min(absolute_limit, max(max_modules,
                ((resumed + max_modules - 1) // max_modules) * max_modules))
            if any(name not in graph for name in pending) and resumed >= effective_limit:
                effective_limit = min(absolute_limit, effective_limit + max_modules)
    except (OSError, ValueError, KeyError, TypeError):
        graph, sources, pending = {}, {}, list(seeds)
    while pending and len(graph) < effective_limit and time.monotonic() - started < seconds:
        name = pending.pop()
        if name in graph:
            continue
        if not NAME.fullmatch(name):
            uncertain.append(name)
            continue
        relative = name.replace('.', '/') + '.lean'
        try:
            if relative in overlay:
                data = overlay[relative]
                deps, complete = imports(data[:HEADER_BYTES], truncated=len(data) > HEADER_BYTES)
                info = {'origin': 'captured_source', 'bytes': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()}
            else:
                path = next((r / relative for r in roots if (r / relative).is_file()), None)
                if path is None:
                    graph[name] = []
                    unresolved.append(name)
                    continue
                stat = path.stat()
                key = str(path)
                stamp = [stat.st_size, stat.st_mtime_ns, stat.st_ino]
                row = headers.get(key)
                if not isinstance(row, dict) or row.get('stamp') != stamp:
                    with path.open('rb') as stream:
                        data = stream.read(HEADER_BYTES)
                    deps, complete = imports(data, truncated=stat.st_size > HEADER_BYTES)
                    row = {'stamp': stamp, 'imports': deps, 'complete': complete,
                           'header_sha256': hashlib.sha256(data).hexdigest()}
                    headers[key] = row
                deps, complete = row['imports'], row['complete']
                info = {'origin': 'pinned_dependency', 'bytes': stat.st_size,
                        'path': str(path), 'stamp': stamp,
                        'header_sha256': row['header_sha256']}
            if name == 'Init':
                deps = [d for d in deps if d != 'Init']
            graph[name] = deps
            sources[name] = info
            if not complete:
                uncertain.append(name)
            pending.extend(d for d in deps if d not in graph)
        except (OSError, ValueError, UnicodeError) as exc:
            graph[name] = []
            uncertain.append(name + ': ' + type(exc).__name__)
    remainder = sorted(set(pending) - graph.keys())
    stop_reasons = []
    if remainder:
        stop_reasons.append('node_limit' if len(graph) >= effective_limit else 'time_budget')
    if unresolved:
        stop_reasons.append('unresolved_imports')
    if uncertain:
        stop_reasons.append('uncertain_headers')
    if selection is not None and selection.get('complete') is not True:
        stop_reasons.append('incomplete_selection')
    report = {'schema_version': 1, 'seeds': seeds, 'identity': scope,
              'limits': {'initial_nodes': max_modules, 'effective_nodes': effective_limit,
                         'absolute_nodes': absolute_limit, 'seconds': seconds},
              'stop_reasons': stop_reasons,
              'imports': graph, 'sources': sources, 'unresolved': sorted(set(unresolved)),
              'uncertain_headers': sorted(set(uncertain)), 'unvisited': remainder,
              'complete': not (unresolved or uncertain or remainder) and (selection is None or selection.get('complete') is True),
              'modules': len(sources), 'source_bytes': sum(x['bytes'] for x in sources.values()),
              'planning_seconds': round(time.monotonic() - started, 6),
              'accepted': False, 'kind': 'lexical_import_graph',
              'wall_prediction_seconds': None, 'memory_prediction_bytes': None,
              'scope': 'Captured source and pinned dependency headers; not a semantic closure proof'}
    if selection is not None:
        report['selection'] = selection
    report['resumed_modules'] = resumed
    report['newly_visited_modules'] = len(graph) - resumed
    graph_payload = {key: value for key, value in report.items()
                     if key not in {'planning_seconds', 'resumed_modules', 'newly_visited_modules'}}
    report['graph_digest'] = digest(graph_payload)
    # Cache and report persistence are diagnostic only, never a compiler prerequisite.
    try:
        tmp = cache / 'headers.tmp'
        tmp.write_text(json.dumps({'identity': scope, 'headers': headers}, separators=(',', ':')))
        tmp.replace(cache_file)
        with gzip.open(cache / 'latest-graph.json.gz', 'wt') as stream:
            json.dump(report, stream, separators=(',', ':'))
        resume = dict(report, resume_scope=resume_scope, pending=pending)
        temporary = cache / ('resume-' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(resume, separators=(',', ':')))
        temporary.replace(cache / 'resume.json')
        archive = cache / 'graphs'
        archive.mkdir(exist_ok=True)
        target = archive / (report['graph_digest'] + '.json.gz')
        temporary = archive / (uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('wb') as raw:
                with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as stream:
                    stream.write(json.dumps(graph_payload, sort_keys=True, separators=(',', ':')).encode())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            with gzip.open(target, 'rt') as stream:
                if digest(json.load(stream)) != report['graph_digest']:
                    raise OSError('Archived graph digest mismatch')
            report['immutable_graph_path'] = str(target)
            report['immutable_graph_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError):
        report['persistence_failed'] = True
    return report


def summary(report: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in report.items() if key not in {'imports', 'sources'}}
    if len(report['seeds']) > 20:
        result['seed_count'] = len(report['seeds'])
        result['seeds'] = report['seeds'][:20]
        result['seeds_truncated'] = True
    result['unvisited_count'] = len(report['unvisited'])
    result['unvisited'] = report['unvisited'][:20]
    result['unresolved'] = report['unresolved'][:20]
    result['uncertain_headers'] = report['uncertain_headers'][:20]
    result['_timing_graph'] = report['imports']
    reasons = report.get('stop_reasons', [])
    if 'unresolved_imports' in reasons or 'uncertain_headers' in reasons:
        advice = 'Inspect unresolved imports or incomplete headers before comparing costs.'
    elif 'node_limit' in reasons:
        limits = report['limits']
        advice = ('Repeat cost on unchanged source to extend the cached graph within its total node budget.'
                  if limits['effective_nodes'] < limits['absolute_nodes'] else
                  'The total graph node budget is exhausted; request a narrower module graph.')
    elif 'time_budget' in reasons:
        advice = 'Repeat cost on unchanged source to continue the cached graph within the time budget.'
    elif not report['complete']:
        advice = 'The selected source scope is incomplete; inspect its limits before comparing costs.'
    else:
        advice = 'Use measured check costs; source size alone does not predict verification time.'
    result['route_advice'] = advice
    return result


def host_resources() -> dict[str, Any]:
    """Read-only context, never an admission decision or a memory prediction."""
    result: dict[str, Any] = {'logical_cpus': os.cpu_count(), 'load_average': None,
                              'affinity_cpus': None, 'memory_available_bytes': None}
    try:
        result['load_average'] = list(os.getloadavg())
    except (AttributeError, OSError):
        pass
    try:
        result['affinity_cpus'] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        pass
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                result['memory_available_bytes'] = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    return result


class Probe:
    """Sample a process tree. RSS sum double-counts shared pages; short processes can be missed."""
    def __init__(self, pid: int | None, interval: float = .25):
        self.pid = pid
        self.interval = interval
        self.started = time.monotonic()
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.samples = 0
        self.peak = 0
        self.processes: dict[tuple[int, str], dict[str, Any]] = {}
        self.truncated = False
        self.process_table_truncated = False
        self.thread_children_truncated = False
        self.host_start = host_resources()
        from leaneval.cgroup_metrics import Observation
        self.cgroup = Observation(pid)
        try:
            self.clock_ticks = int(os.sysconf('SC_CLK_TCK'))
        except (OSError, ValueError):
            self.clock_ticks = None

    def cpu_sample(self, item: dict[str, Any], fields: list[str]) -> None:
        """Only count deltas actually observed within one PID/start identity."""
        try:
            ticks = int(fields[11]) + int(fields[12])
            if ticks < 0:
                raise ValueError('negative CPU counter')
            previous = item.get('cpu_last_ticks')
            if previous is not None and ticks < previous:
                item['cpu_counter_valid'] = False
                return
            item.setdefault('cpu_first_ticks', ticks)
            item['cpu_last_ticks'] = ticks
            item['cpu_samples'] = item.get('cpu_samples', 0) + 1
            item.setdefault('cpu_counter_valid', True)
            states = item.setdefault('observed_states', {})
            states[fields[0]] = states.get(fields[0], 0) + 1
        except (ValueError, IndexError):
            item['cpu_counter_valid'] = False

    def sample(self) -> None:
        if not self.pid or not Path('/proc').is_dir():
            return
        pending = [self.pid]
        seen = set()
        total = 0
        observed = False
        elapsed = time.monotonic() - self.started
        sample_deadline = time.monotonic() + .2
        while pending and len(seen) < 2048 and time.monotonic() < sample_deadline:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            root = Path('/proc') / str(pid)
            try:
                stat = (root / 'stat').read_text().rsplit(')', 1)[1].split()
                generation = stat[19]
                if stat[0] == 'Z':
                    continue
                # A multithreaded parent can spawn a child from any task, not
                # just its thread-group leader. Bound both work and elapsed time.
                thread_count = 0
                for task in (root / 'task').iterdir():
                    if thread_count >= 256 or time.monotonic() >= sample_deadline:
                        self.thread_children_truncated = True
                        break
                    thread_count += 1
                    try:
                        pending.extend(int(child) for child in (task / 'children').read_text().split())
                    except (OSError, ValueError):
                        self.thread_children_truncated = True
                status = (root / 'status').read_text()
                match = re.search(r'^VmRSS:\s+(\d+)\s+kB', status, re.M)
                if match is None:
                    continue
                rss = int(match.group(1)) * 1024
                observed = True
                total += rss
                key = (pid, generation)
                if key not in self.processes and len(self.processes) < 512:
                    # Basename only: argv can carry user data and credentials.
                    command = os.path.basename((root / 'cmdline').read_bytes().split(b'\0')[0].decode(errors='replace'))
                    self.processes[key] = {'command': command, 'first_seen_seconds': elapsed,
                                           'last_seen_seconds': elapsed, 'sampled_peak_rss_bytes': rss,
                                           'pid': pid, 'start_ticks': generation}
                elif key in self.processes:
                    item = self.processes[key]
                    item['last_seen_seconds'] = elapsed
                    item['sampled_peak_rss_bytes'] = max(item['sampled_peak_rss_bytes'], rss)
                else:
                    self.process_table_truncated = True
                if key in self.processes:
                    self.cpu_sample(self.processes[key], stat)
            except (OSError, ValueError, IndexError):
                continue
        self.truncated |= bool(pending)
        if observed:
            self.samples += 1
            self.peak = max(self.peak, total)

    def start(self) -> 'Probe':
        self.sample()
        def loop() -> None:
            while not self.stop_event.wait(self.interval):
                self.sample()
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def finish(self) -> dict[str, Any]:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        rows = list(self.processes.values())
        observed_ticks = 0
        usable = 0
        for item in rows:
            valid = item.get('cpu_counter_valid') is True and item.get('cpu_samples', 0) >= 2 and self.clock_ticks
            seconds = ((item['cpu_last_ticks'] - item['cpu_first_ticks']) / self.clock_ticks) if valid else None
            item['observed_cpu_seconds'] = seconds
            if valid:
                observed_ticks += item['cpu_last_ticks'] - item['cpu_first_ticks']
                usable += 1
        cpu = observed_ticks / self.clock_ticks if self.clock_ticks and usable else None
        return {'schema_version': 2, 'started_at': self.started_at, 'finished_at': time.time(),
                'wall_seconds': round(time.monotonic() - self.started, 6), 'samples': self.samples,
                'sample_interval_seconds': self.interval,
                'sampled_peak_tree_rss_bytes': self.peak if self.samples else None,
                'processes': rows, 'truncated': self.truncated or self.process_table_truncated or self.thread_children_truncated,
                'process_table_truncated': self.process_table_truncated,
                'thread_children_truncated': self.thread_children_truncated,
                'observed_cpu_seconds_lower_bound': cpu,
                'cpu_observed_processes': usable, 'cpu_clock_ticks_per_second': self.clock_ticks,
                'host_resources_start': self.host_start, 'host_resources_end': host_resources(),
                'cgroup_envelope': self.cgroup.finish(),
                'cpu_scope': 'Sum of observed per-PID/start CPU deltas only; first-sample work, unsampled children and unretained processes are excluded. Not total CPU or wait time.',
                'scope': 'Sampled RSS sum, shared pages counted per process; short-lived children may be missed',
                'accepted': False}


def built_nodes(stdout: str) -> list[dict[str, Any]]:
    result = []
    # Lake reports per-node elapsed time. These are observed log values, not predictions.
    pattern = re.compile(r'Built\s+([\w.\'/-]+)\s+\((\d+(?:\.\d+)?)(ms|s)\)')
    for match in pattern.finditer(stdout):
        seconds = float(match[2]) / (1000 if match[3] == 'ms' else 1)
        result.append({'target': match[1], 'reported_wall_seconds': seconds})
    return result


def attach_history(report: dict[str, Any], source_files: dict[str, str],
                   tool_identity: dict[str, Any], history: list[dict[str, Any]]) -> None:
    measured = []
    for record in reversed(history):
        if record.get('source_files') != source_files or record.get('tool_identity') != tool_identity:
            continue
        if not record.get('source_current') or record.get('action') in {'cost', 'search', 'preflight', 'repair'}:
            continue
        stages = record.get('stage_measurements', [])
        if not stages:
            continue
        peaks = [s['sampled_peak_tree_rss_bytes'] for s in stages if s.get('sampled_peak_tree_rss_bytes') is not None]
        cpu = [s['observed_cpu_seconds_lower_bound'] for s in stages
               if s.get('observed_cpu_seconds_lower_bound') is not None]
        measured.append({'step_id': record.get('step_id'), 'action': record.get('action'),
                         'query': record.get('query'), 'exitcode': record.get('exitcode'),
                         'outcome_class': record.get('outcome_class'),
                         'bridge_wall_seconds': record.get('elapsed_seconds'),
                         'stage_wall_seconds': sum(s['wall_seconds'] for s in stages),
                         'observed_cpu_seconds_lower_bound': sum(cpu) if cpu else None,
                         'cpu_measured_stages': len(cpu), 'stage_count': len(stages),
                         'cpu_scope': 'Observed process deltas only; missing stages and unsampled work excluded',
                         'sampled_peak_tree_rss_bytes': max(peaks) if peaks else None,
                         'isolated_cgroup_measurements': [s['isolated_cgroup'] for s in stages
                             if isinstance(s.get('isolated_cgroup'), dict)],
                         'cgroup_envelopes': [cgroup_summary(s['cgroup_envelope'], s.get('phase'))
                                              for s in stages if isinstance(s.get('cgroup_envelope'), dict)]})
        if len(measured) >= 8:
            break
    report['same_source_environment_measurements'] = measured
    if measured:
        report['route_advice'] = ('Use these same-source/environment observations to choose the next scoped check. '
                                  'Runtime and cache state can change; these are not future duration guarantees. '
                                  'Full official verification remains required for final acceptance.')
    attach_node_costs(report, source_files, tool_identity, history)


def attach_node_costs(report: dict[str, Any], source_files: dict[str, str],
                      tool_identity: dict[str, Any], history: list[dict[str, Any]]) -> None:
    """Observed node costs on the captured lexical DAG, never a wall-time guarantee."""
    graph = report.pop('_timing_graph', {})
    report['critical_path_prediction_seconds'] = None
    graph_valid = (bool(graph) and isinstance(graph, dict)
                   and all(isinstance(k, str) and isinstance(v, list)
                           and all(isinstance(n, str) for n in v) for k, v in graph.items()))
    if not graph_valid:
        report['node_costs'] = {'status': 'missing_graph', 'measured_nodes': 0}
        return
    candidates = {name for name in graph if name.replace('.', '/') + '.lean' in source_files}
    weights: dict[str, dict[str, Any]] = {}
    records = sorted(history, key=lambda r: r.get('finished_at', 0)
                     if isinstance(r.get('finished_at', 0), (int, float)) else 0)
    for record in records:
        if (record.get('source_files') != source_files or record.get('tool_identity') != tool_identity
                or record.get('source_current') is not True or record.get('exitcode') != 0
                or type(record.get('exitcode')) is not int or record.get('timed_out')
                or record.get('compiler_invoked') is False
                or record.get('outcome_class') != 'check_succeeded'
                or record.get('action') not in {'build', 'check', 'inspect', 'incremental'}
                or record.get('checked_files', source_files) != source_files
                or record.get('checked_files_unchanged') is False):
            continue
        for stage in record.get('stage_measurements', []):
            if stage.get('exitcode') != 0 or stage.get('timed_out'):
                continue
            for node in stage.get('reported_nodes', []):
                name, seconds = node.get('target'), node.get('reported_wall_seconds')
                if (name not in graph or type(seconds) not in (int, float)
                        or not math.isfinite(seconds) or seconds < 0):
                    continue
                weights[name] = {'seconds': seconds, 'step_id': record.get('step_id'),
                                 'phase': stage.get('phase')}
    # Topological order is iterative to support deep Lean import chains.
    indegree = {name: 0 for name in graph}
    dependents: dict[str, list[str]] = {name: [] for name in graph}
    open_edges = set()
    for name, deps in graph.items():
        for dep in set(deps):
            if dep not in graph:
                open_edges.add(dep)
                continue
            indegree[name] += 1
            dependents[dep].append(name)
    ready = sorted(name for name, degree in indegree.items() if not degree)
    order = []
    while ready:
        name = ready.pop()
        order.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    valid = report.get('complete') is True and not open_edges and len(order) == len(graph)
    def path_for(selected: set[str]) -> dict[str, Any]:
        missing = sorted(selected - weights.keys())
        value: dict[str, Any] = {'seconds': None, 'required_nodes': len(selected),
                                 'measured_nodes': len(selected & weights.keys()),
                                 'missing_nodes': missing[:20], 'missing_count': len(missing)}
        if not valid or missing or not selected:
            value['status'] = ('incomplete_or_cyclic_graph' if not valid else
                               'missing_measurements' if missing else 'empty_scope')
            return value
        distance: dict[str, float] = {}
        parent: dict[str, str | None] = {}
        for name in order:
            if name not in selected:
                continue
            deps = [d for d in graph[name] if d in selected]
            longest = max(deps, key=lambda d: distance[d], default=None)
            parent[name] = longest
            distance[name] = weights[name]['seconds'] + (distance[longest] if longest else 0)
        tip = max(distance, key=distance.get)
        path = []
        node: str | None = tip
        while node is not None:
            path.append({'module': node, **weights[node]})
            node = parent[node]
        value.update(status='observed_cost_estimate', seconds=distance[tip],
                     total_node_seconds=sum(weights[n]['seconds'] for n in selected),
                     path=list(reversed(path)))
        return value
    full = path_for(set(graph))
    warm = path_for(candidates)
    report['node_costs'] = {'status': 'graph_bound_observations', 'graph_digest': report.get('graph_digest'),
                            'measured_nodes': len(weights), 'full_graph': full,
                            'warm_dependencies_candidate_graph': warm,
                            'assumptions': ['Same captured source inventory and tool identity.',
                                            'Durations are prior Lake-reported node observations.',
                                            'Warm estimate excludes prebuilt dependency work and assumes candidate nodes compile.',
                                            'Scheduling, transport and cache changes are not predicted.'],
                            'per_node_memory_prediction': None, 'accepted': False}
    report['critical_path_prediction_seconds'] = full['seconds']
    if warm['seconds'] is not None:
        report['route_advice'] = ('Candidate compilation has a measured import-path estimate with warm dependencies. '
                                  'Inspect node_costs for missing full-graph costs and assumptions; '
                                  'this is not an end-to-end duration guarantee. Full official verification remains required.')
