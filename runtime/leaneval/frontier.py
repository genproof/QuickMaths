"""Source-bound research contracts and admission; never proof acceptance.

Compiler feedback is diagnostic evidence, including when its source hashes match.
Only the independent official verifier/registry can establish a proof result.
"""
from __future__ import annotations

import argparse
import hashlib
import fnmatch
from functools import lru_cache
import json
import time
from pathlib import Path
from typing import Any

from leaneval.artifacts import digest, git_tree
from leaneval.planning import profile
from leaneval.verifier import atomic_json
from leaneval.feedback_records import read_feedback
from leaneval.contract_io import load as load_contract, read as read_contract

from leaneval.incremental_queries import equivalent as same_query, observation as query_observation


class ProgressBlocked(ValueError):
    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


SCHEMA = 1


def read(path: Path, limit: int = 4_000_000) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('unsafe or oversized research metadata: ' + str(path))
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('research metadata must be an object')
    return value


def inventory(workspace: Path) -> dict[str, str]:
    from leaneval.worker import proof_files
    return {name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(proof_files(workspace).items())}


def policy(config: dict[str, Any]) -> dict[str, Any]:
    assignment = config.get('campaign_assignment') or {}
    value = assignment.get('research_frontier') or {}
    if value and value.get('schema_version') != SCHEMA:
        raise ValueError('unknown research frontier schema')
    lane = value.get('lane_kind', 'research')
    if lane not in {'research', 'integration'}:
        raise ValueError('lane_kind must be research or integration')
    scope = value.get('edit_scope') or ['Submission.lean', 'Submission/**/*.lean']
    excluded = value.get('exclude_scope') or []
    if not isinstance(scope, list) or not scope or not isinstance(excluded, list) or any(
        not isinstance(pattern, str) or '..' in pattern or not (
            pattern == 'Submission.lean' or pattern.startswith('Submission/') and pattern.endswith('.lean')
        ) for pattern in scope + excluded
    ):
        raise ValueError('edit scope must stay inside the proof-only overlay')
    return {'schema_version': SCHEMA, 'lane_kind': lane,
            'owner': value.get('owner') or (assignment.get('auto_continue') or {}).get('base') or config.get('attempt_id') or config.get('run_id'),
            'edit_scope': scope, 'exclude_scope': excluded,
            'dependencies': value.get('dependencies') or []}


def path_matches(name: str, pattern: str) -> bool:
    """Path-segment glob: * never crosses '/', ** matches zero or more segments."""
    names, patterns = name.split('/'), pattern.split('/')
    @lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(patterns):
            return i == len(names)
        if patterns[j] == '**':
            return match(i, j + 1) or i < len(names) and match(i + 1, j)
        return (i < len(names) and fnmatch.fnmatchcase(names[i], patterns[j]) and
                match(i + 1, j + 1))
    return match(0, 0)


def enforce_scope(job: Path, current: dict[str, str]) -> None:
    config = read(job / 'config.json')
    transferred = []
    if config:
        checks, transferred = transfer_checks(job, config, current)
        if policy(config)['lane_kind'] == 'integration' and any(
                not check['source_matches'] for check in checks):
            raise ValueError('Transferred integration dependency changed or lost its receipt')
    baseline = read(job / 'research-frontier.json')
    if not baseline:
        return
    before = dict(baseline.get('source_files', {}))
    # A registered controller addition becomes a read-only baseline file. This
    # never licenses a model-created file, a replacement, or later edits/deletion.
    for item in transferred:
        if item['file'] not in before and item.get('controller_addition_sha256'):
            before[item['file']] = item['controller_addition_sha256']
    scope = baseline['edit_scope']
    excluded = baseline.get('exclude_scope', [])
    changed = {name for name in set(before) | set(current) if before.get(name) != current.get(name)}
    illegal = [name for name in changed if not (
        any(path_matches(name, pattern) for pattern in scope) and
        not any(path_matches(name, pattern) for pattern in excluded))]
    if illegal:
        raise ValueError('Changed file outside assigned edit scope: ' + ', '.join(sorted(illegal)))


def proof_feedback(job: Path) -> dict[str, Any]:
    ignored = {'cost', 'search', 'preflight', 'repair', 'inspect_partial'}
    latest = read_feedback(job / 'workspace/.feedback/latest.json')
    def relevant(entry):
        if (not entry or entry.get('action') in ignored or
                entry.get('action') == 'inspect' and entry.get('query', '').startswith('Challenge:')):
            return False
        if entry.get('action') == 'incremental':
            from leaneval.incremental_progress import validate
            return validate(job, entry)[0]
        return True
    if relevant(latest):
        return latest
    directory = job / 'workspace/.feedback'
    # Incremental filenames contain invocation UUIDs, not an ordered step number.
    # Modification time selects candidates only; source and controller-result
    # validation remain authoritative before an incremental receipt is used.
    candidates = [*directory.glob('step-*.json'), *directory.glob('incremental-*.json')]
    def modified(path):
        try:
            return path.stat().st_mtime_ns, path.name
        except OSError:
            return -1, path.name
    for path in sorted(candidates, key=modified, reverse=True)[:100]:
        try:
            entry = read_feedback(path)
        except ValueError:
            continue  # lrh: an oversized or changing record is skipped, not fatal to the job
        if relevant(entry):
            return entry
    return {}


def semantic_contract(contract: dict[str, Any]) -> dict[str, Any]:
    return {k: contract.get(k) for k in ('target', 'remaining_type', 'source_files',
            'prerequisites', 'next_check', 'blocked_by', 'owner') if k in contract}


def feedback_matches_source(feedback: dict[str, Any], source: dict[str, str]) -> bool:
    return (bool(feedback) and feedback.get('source_current') is True and
            feedback.get('source_files') == source and
            feedback.get('source_sha256') == digest(source))


def nudge_observation(source: dict[str, str], feedback: dict[str, Any],
                      contract: dict[str, Any]) -> dict[str, Any]:
    return {'source_sha256': digest(source),
            'feedback_source_current': feedback_matches_source(feedback, source),
            'check': query_observation({'check': {k: feedback.get(k) for k in ('action', 'query', 'outcome_class', 'exitcode')}})['check'],
            'diagnostic_digest': digest(feedback.get('diagnostics', [])),
            'contract_digest': digest(semantic_contract(contract))}


def guard_next(job: Path, source: dict[str, str], action: str, query: str) -> None:
    from leaneval.feedback_progress import blocks_repeat
    if blocks_repeat(job, source, action, query):
        raise ProgressBlocked('Unchanged source and three identical compiler failures in this session: '
                              'change the source or run a different concrete scoped test before retrying',
                              'identical_compiler_failures')
    state = read(job / 'research-nudge.json')
    prior = proof_feedback(job)
    contract = load_contract(job)
    if (state.get('unchanged_observations', 0) >= 2 and
            feedback_matches_source(prior, source) and
            query_observation(state.get('observation', {})) == nudge_observation(source, prior, contract) and
            prior.get('outcome_class') == 'lean_diagnostic' and
            prior.get('action') == action and same_query(action, prior.get('query', ''), query)):
        raise ProgressBlocked('Unchanged source and repeated diagnostic after no-delta checkpoints: '
                              'perform a different scoped check or change the proof route before retrying',
                              'repeated_checkpoint_diagnostic')


def transfer_checks(job: Path, config: dict[str, Any],
                    current: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read only controller-bound receipts, never metadata from the model workspace."""
    imported = config.get('imported_sources') or {}
    sources = list(imported.values()) if isinstance(imported, dict) else []
    if current is None:
        current = inventory(job / 'workspace') if any(
            isinstance(source, dict) and source.get('transfer_receipt') for source in sources) else {}
    index: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or not source.get('transfer_receipt'):
            continue
        path = Path(source['transfer_receipt'])
        from leaneval.transfer_receipts import load
        receipt, receipt_sha = load(job, path, source.get('transfer_receipt_sha256'))
        if (receipt.get('schema') != 1 or receipt.get('status') != 'applied' or
                receipt.get('workspace') != str((job / 'workspace').resolve()) or
                receipt.get('official_verification') is not False or
                not isinstance(receipt.get('files'), dict)):
            raise ValueError('invalid controller transfer receipt')
        for name, entry in receipt['files'].items():
            if not isinstance(entry, dict):
                raise ValueError('invalid transferred file entry')
            evidence = entry.get('source_evidence') or {}
            verified = (entry.get('verification') == 'coordinator_diagnostic' and
                        evidence.get('kind') == 'lean-build-and-module-axioms' and
                        evidence.get('official_verification') is False and
                        evidence.get('sha256') == entry.get('sha256'))
            index[name] = {'file': name, 'sha256': entry.get('sha256'),
                           'receipt_sha256': receipt_sha, 'owner': evidence.get('origin'),
                           'producer_receipt_sha256': evidence.get('receipt_sha256'),
                           'before_sha256': entry.get('before_sha256'),
                           'transfer_mode': receipt.get('mode'),
                           'controller_addition_sha256': (entry.get('sha256') if
                               receipt.get('mode') == 'live_add_only' and entry.get('before_sha256') is None
                               else (index.get(name, {}).get('controller_addition_sha256') if
                                   index.get(name, {}).get('sha256') == entry.get('sha256') else None)),
                           'diagnostic_verified': verified,
                           'source_current': current.get(name) == entry.get('sha256'),
                           'authority': 'controller transfer of diagnostic evidence; not official proof acceptance'}
    checks = []
    for dep in policy(config)['dependencies']:
        if not isinstance(dep, dict):
            raise ValueError('invalid dependency entry')
        if dep.get('kind') not in {'transferred_file', 'transferred_module'}:
            continue
        keys = ('owner', 'file', 'sha256')
        if dep['kind'] == 'transferred_file' or dep.get('receipt_sha256') is not None:
            keys += ('receipt_sha256',)
        for key in keys:
            if not isinstance(dep.get(key), str) or not dep[key]:
                raise ValueError('transferred dependency requires ' + key)
        if dep['kind'] == 'transferred_module':
            name = Path(dep['file'])
            if (name.is_absolute() or '..' in name.parts or not dep['file'].endswith('.lean') or
                    not (dep['file'] == 'Submission.lean' or dep['file'].startswith('Submission/'))):
                raise ValueError('transferred module must name a proof source file')
        item = index.get(dep['file'], {})
        matches = (bool(item) and item.get('diagnostic_verified') is True and
                   item.get('source_current') is True and
                   all(item.get(k) == dep[k] for k in keys if k != 'file'))
        checks.append({**dep, 'resolved_receipt_sha256': item.get('receipt_sha256'),
                       'producer_receipt_sha256': item.get('producer_receipt_sha256'),
                       'source_matches': matches,
                       'authority': 'exact transferred source; not official proof acceptance'})
    return checks, list(index.values())


def dependency_check(parent: Path, dep: dict[str, Any]) -> dict[str, Any]:
    """A dependency is a particular owner's real check, not a successful search.

    Duplicate serializations of one receipt are harmless. Conflicting payloads
    bearing the same step ID cannot establish an unambiguous prerequisite.
    """
    parent_config = read(parent / 'config.json')
    actual_owner = policy(parent_config)['owner'] if parent_config else None
    variants = {}
    for path in sorted((parent / 'workspace/.feedback').glob('step-*.json')):
        try:
            candidate = read_feedback(path)
        except ValueError:
            continue  # lrh: oversized record (cannot serve as a receipt)
        if candidate.get('step_id') == dep['step_id']:
            variants[digest(candidate)] = candidate
    unique = len(variants) == 1
    receipt_digest, receipt = next(iter(variants.items())) if unique else (None, {})
    reasons = []
    if actual_owner != dep['owner']:
        reasons.append('owner_mismatch')
    if not variants:
        reasons.append('missing_receipt')
    elif not unique:
        reasons.append('conflicting_receipt')
    if unique:
        action, query = receipt.get('action'), receipt.get('query', '')
        if (action not in {'check', 'build', 'inspect'} or
                action == 'inspect' and query.startswith('Challenge:')):
            reasons.append('not_candidate_compiler_check')
        if receipt.get('exitcode') != 0 or receipt.get('timed_out'):
            reasons.append('unsuccessful_check')
        if receipt.get('outcome_class') != 'check_succeeded':
            reasons.append('unsuccessful_outcome')
        if dep.get('action') is not None and dep['action'] != action:
            reasons.append('action_mismatch')
        if dep.get('query') is not None and dep['query'] != query:
            reasons.append('query_mismatch')
        if dep.get('feedback_digest') is not None and dep['feedback_digest'] != receipt_digest:
            reasons.append('receipt_digest_mismatch')
        current = inventory(parent / 'workspace')
        if (receipt.get('source_sha256') != dep['source_sha256'] or
                receipt.get('source_files') != current or
                digest(current) != dep['source_sha256'] or
                receipt.get('source_current') is not True):
            reasons.append('source_mismatch')
        if dep.get('require_axiom_audit') and (receipt.get('axiom_audit') or {}).get(
                'diagnostic_foundational') is not True:
            reasons.append('axiom_audit_missing_or_failed')
    return {'owner': dep['owner'], 'actual_owner': actual_owner, 'job': str(parent),
            'step_id': dep['step_id'], 'source_sha256': dep['source_sha256'],
            'feedback_digest': receipt_digest, 'receipt_variants': len(variants),
            'action': receipt.get('action'), 'query': receipt.get('query'),
            'source_matches': not reasons, 'reasons': reasons,
            'authority': 'source-bound compiler diagnostic; not official proof acceptance'}


def admission(job: Path, config: dict[str, Any], *, source_only: bool = False) -> dict[str, Any]:
    """Gate explicitly declared integration dependencies before model reservation."""
    p = policy(config)
    dependencies = p['dependencies']
    if not isinstance(dependencies, list) or len(dependencies) > 100:
        raise ValueError('invalid dependency frontier')
    checks, transferred = transfer_checks(job, config)
    for dep in dependencies:
        if dep.get('kind') in {'transferred_file', 'transferred_module'}:
            continue
        for key in ('owner', 'job', 'step_id', 'source_sha256'):
            if not isinstance(dep.get(key), str) or not dep[key]:
                raise ValueError('dependency requires ' + key)
        parent = Path(dep['job']).resolve()
        checks.append(dependency_check(parent, dep))
    if not source_only:
        for check in checks:
            if check.get('require_compile') is True:
                compiled = consumer_dependency_check(job, config, check)
                check['transfer_source_matches'] = check['source_matches']
                check['source_matches'] = check['source_matches'] and compiled['source_matches']
                check['consumer_check'] = compiled
    blocked = [r for r in checks if not r['source_matches']]
    result = {'schema_version': SCHEMA, **p, 'checks': checks, 'transferred_files': transferred,
              'blocked_by': blocked, 'status': 'blocked' if blocked and p['lane_kind'] == 'integration' else 'admitted',
              'accepted': False, 'checked_at': time.time()}
    atomic_json(job / 'frontier-admission.json', result)
    if result['status'] == 'blocked':
        raise ValueError('Integration frontier blocked: ' + ', '.join(r['owner'] for r in blocked))
    return result


def compile_spec(dep: dict[str, Any]) -> tuple[str, str, int]:
    if dep.get('kind') not in {'transferred_file', 'transferred_module'}:
        raise ValueError('consumer compilation requires a transferred proof module')
    name = dep.get('file', '')
    path = Path(name)
    if (path.is_absolute() or '..' in path.parts or not name.endswith('.lean') or
            not (name == 'Submission.lean' or name.startswith('Submission/'))):
        raise ValueError('consumer compilation requires a proof source path')
    seconds = dep.get('compile_timeout_seconds', 180)
    if type(seconds) is not int or not 1 <= seconds <= 600:
        raise ValueError('consumer compile timeout must be 1..600 seconds')
    from leaneval.dependency_retry import budget
    budget(dep, seconds)
    module = name[:-5].replace('/', '.')
    declaration = dep.get('declaration')
    if declaration is not None:
        if not isinstance(declaration, str) or not declaration or ':' in declaration:
            raise ValueError('consumer declaration must be one exact name')
        return 'inspect', module + ':' + declaration, seconds
    return 'build', module, seconds


def consumer_dependency_check(job: Path, config: dict[str, Any], dep: dict[str, Any]) -> dict[str, Any]:
    action, query, _ = compile_spec(dep)
    # Ignore derived admission fields when locating the original descriptor.
    key = digest({k: dep[k] for k in ('kind', 'owner', 'file', 'sha256', 'receipt_sha256',
                  'require_compile', 'compile_timeout_seconds', 'compile_total_timeout_seconds', 'declaration') if k in dep})
    record = read(job / 'research-dependency-checks.json').get('checks', {}).get(key, {})
    if not all(isinstance(record.get(k), str) and record[k] for k in
               ('step_id', 'source_sha256', 'feedback_digest')):
        return {'source_matches': False, 'reasons': ['consumer_check_missing'],
                'action': action, 'query': query}
    check = dependency_check(job, {'owner': policy(config)['owner'], 'job': str(job),
        'step_id': record['step_id'], 'source_sha256': record['source_sha256'],
        'feedback_digest': record['feedback_digest'], 'action': action, 'query': query,
        'require_axiom_audit': action == 'inspect'})
    if action == 'inspect' and not record.get('exact_types'):
        check['source_matches'] = False
        check['reasons'].append('consumer_exact_type_missing')
    check['exact_types'] = record.get('exact_types', [])
    return check


def check_consumer_dependencies(job: Path, config: dict[str, Any]) -> None:
    """Budgeted native checks before model allocation, reused only with current evidence."""
    from leaneval import worker
    checks, _ = transfer_checks(job, config)
    book = read(job / 'research-dependency-checks.json') or {'schema_version': 1, 'checks': {}}
    for dep in policy(config)['dependencies']:
        if dep.get('require_compile') is not True:
            continue
        action, query, seconds = compile_spec(dep)
        structural = next((c for c in checks if all(c.get(k) == dep.get(k)
                           for k in ('kind', 'owner', 'file', 'sha256'))), {})
        from leaneval import dependency_contexts
        context = dependency_contexts.record(job, config, dep, structural)
        context_key = dependency_contexts.key(dep)
        book.setdefault('producer_contexts', {})[context_key] = context
        atomic_json(job / 'research-dependency-checks.json', book)
        if consumer_dependency_check(job, config, dep)['source_matches']:
            continue
        if not structural.get('source_matches'):
            continue
        cfg = worker.load_config(job / 'worker.json')
        from leaneval import dependency_retry
        total = dependency_retry.budget(dep, seconds)
        # A carried worker.json may still name the parent's old runtime.
        cfg['remote_agent'] = str(Path(__file__).resolve().parents[1])
        result, batch = dependency_retry.run(job, cfg, action, query, seconds, total)
        if result is None:
            book.setdefault('preparation_batches', {})[context_key] = batch
            atomic_json(job / 'research-dependency-checks.json', book)
            continue
        key = digest({k: dep[k] for k in ('kind', 'owner', 'file', 'sha256', 'receipt_sha256',
                     'require_compile', 'compile_timeout_seconds', 'compile_total_timeout_seconds', 'declaration') if k in dep})
        book['checks'][key] = {'step_id': result.get('step_id'),
            'source_sha256': result.get('source_sha256'), 'feedback_digest': digest(result),
            'action': action, 'query': query, 'exact_types': result.get('exact_types', []),
            'outcome_class': result.get('outcome_class'), 'checked_at': time.time(),
            'preparation_batch': batch, 'accepted': False}
        atomic_json(job / 'research-dependency-checks.json', book)


def prepare(job: Path, workbench: Path, config: dict[str, Any], *, progress=None) -> str:
    if progress is not None:
        progress.phase("consumer_dependencies")
    check_consumer_dependencies(job, config)
    if progress is not None:
        progress.phase("dependency_admission")
    gate = admission(job, config)
    if progress is not None:
        progress.phase("frontier_assembly")
    original = git_tree(workbench / 'vendor/lean-eval', profile()['benchmark']['commit'],
                        'generated/' + config['problem_id'])
    holes = json.loads(original['holes.json'])
    from leaneval.goal_types import identity
    try:
        frozen_identity = identity(original, profile()['benchmark']['commit'], config['problem_id'])
    except (KeyError, TypeError, ValueError):
        frozen_identity = None
    source = inventory(job / 'workspace')
    from leaneval import dependency_contexts
    contexts = read(job / 'research-dependency-checks.json').get('producer_contexts', {})
    record = {'schema_version': SCHEMA, 'problem_id': config['problem_id'],
              'statement_revision': config.get('statement_revision'),
              'benchmark_commit': profile()['benchmark']['commit'], 'owner': gate['owner'],
              'lane_kind': gate['lane_kind'], 'edit_scope': gate['edit_scope'],
              'exclude_scope': gate['exclude_scope'],
              'source_files': source, 'source_sha256': digest(source),
              'official_obligations': holes['holes'], 'frozen_identity': frozen_identity,
              'blocked_by': gate['blocked_by'],
              'checked_prerequisites': [{**{k: c.get(k) for k in
                  ('file', 'owner', 'sha256', 'source_matches', 'consumer_check')},
                  'producer_context': contexts.get(dependency_contexts.key(c))}
                  for c in gate['checks'] if c.get('kind') in {'transferred_file', 'transferred_module'}],
              'accepted': False}
    atomic_json(job / 'research-frontier.json', record)
    inherited = read(job / 'research-inherited.json')
    inherited_text = ''
    if inherited:
        from leaneval.goal_types import checked_record
        official = inherited.get('official_goals')
        if official and not checked_record(official, frozen_identity):
            raise ValueError('Inherited official goal types differ from the frozen task')
        view = residual_view(job, source, load_contract(job), inherited)
        inherited_text = '\nCaptured parent checkpoint (diagnostic claims, not proved goals):\n' + json.dumps({
            'checkpoint_digest': inherited.get('checkpoint_digest'),
            'artifact_digest': inherited.get('artifact_digest'), 'contracts': view['contracts'],
            'official_goals': compact_goals(official),
            'pending_contract': view['pending_contract'],
            'unverified_handoff': inherited.get('unverified_handoff'),
            'full_evidence': str(job / 'research-inherited.json')}, ensure_ascii=False)
    from leaneval.failed_routes import brief
    inherited_text += brief(inherited.get('failed_routes') or {}, source)
    from leaneval import type_history
    active = residual_view(job, source, load_contract(job), inherited)['contracts']
    inherited_text += type_history.brief(type_history.collect(job, inherited), source,
                                         [item['step_id'] for item in active])
    return ('\nOwned research frontier:\n' + json.dumps({
        k: record[k] for k in ('problem_id', 'owner', 'lane_kind', 'edit_scope', 'exclude_scope', 'blocked_by', 'checked_prerequisites')}) +
        '\nProducer context reports compare local import sources with the producer receipt. '
        'A matched context does not prove the prerequisite; read consumer_check for the native result. '
        'Unknown or changed context requires investigation, not a claim of success. '
        'Official obligations remain exactly those in holes.json. Use ./research goals '
        'to capture their pinned compiler types even when Submission does not build. '
        'These describe the original task, not proof progress or residual lemmas. Use ./research capture '
        '--module Submission.Module --declaration fully.qualified.name '
        '--next-check "a specific next check" to save the exact compiler type and axiom diagnostic. '
        'If an unrelated error prevents the module from building, add --partial to capture '
        'the elaborated type from that exact source. Partial capture never certifies the module. '
        'Use ./lean build Submission.Module for a bounded module check; ./lean check is the light '
        'readiness check (no sorry, Submission and statement wrapper build, standard hole axioms) '
        'and ./lean check --full the complete official test. A local module check does not '
        'discharge the official task.\n' + inherited_text)


def residual_view(job: Path, source: dict[str, str], contract: dict[str, Any],
                  inherited: dict[str, Any]) -> dict[str, Any]:
    """Project only authenticated, current-source types into continuation advice."""
    from leaneval.checkpoint_contract import resolve
    contracts, _, pending = resolve(job, contract, source, inherited)
    compact = []
    for value in contracts[:3]:
        item = {k: value.get(k) for k in ('target', 'module', 'remaining_type', 'owner',
            'next_check', 'step_id', 'source_sha256', 'kind', 'candidate_checked', 'module_exitcode')}
        item['compiler_type_verified'] = True
        item['accepted'] = False
        if len(item.get('remaining_type') or '') > 5000:
            item['remaining_type'] = 'Full compiler-recorded type remains in the source contract/checkpoint on disk.'
        compact.append(item)
    return {'contracts':compact, 'pending_contract':pending, 'accepted':False}


def capture(job: Path, module: str, declaration: str, next_check: str,
            blocked_by: list[str] | None = None, *, partial: bool = False) -> dict[str, Any]:
    from leaneval import worker
    if module == 'Challenge':
        raise ValueError('Use research goals for frozen task types; capture records residual declarations')
    if not next_check.strip() or len(next_check) > 2000:
        raise ValueError('a bounded concrete next check is required')
    cfg = worker.load_config(job / 'worker.json')
    result = worker.feedback(cfg, 'inspect_partial' if partial else 'inspect', module + ':' + declaration)
    types = result.get('exact_types', [])
    valid_partial = (partial and result.get('phase') == 'partial_declaration_inspection'
                     and result.get('candidate_checked') is False
                     and result.get('partial_source_prefix_exact') is True
                     and result.get('invocation_id') == result.get('step_id')
                     and result.get('partial_probe_sha256')
                     and not result.get('timed_out')
                     and result.get('exitcode') in (0, 1))
    if ((not valid_partial and (partial or result.get('exitcode') != 0)) or not result.get('source_current') or
            len(set(types)) != 1 or not result.get('checked_files_unchanged')):
        raise ValueError('No unambiguous fresh compiler type; inspect the feedback receipt')
    config = read(job / 'config.json')
    p = policy(config)
    contract = {'schema_version': SCHEMA, 'target': declaration, 'module': module,
                'remaining_type': types[0], 'owner': p['owner'], 'next_check': next_check,
                'edit_scope': p['edit_scope'], 'blocked_by': blocked_by or [],
                'source_files': result['source_files'], 'source_sha256': result['source_sha256'],
                'step_id': result['step_id'], 'invocation_id': result['invocation_id'],
                'axiom_audit': result.get('axiom_audit'),
                'controller_type_receipt': result.get('controller_type_receipt'),
                'kind': 'partial-compiler-type' if partial else 'compiler-type-and-diagnostic', 'accepted': False,
                'prerequisites': [{'file': name, 'sha256': sha} for name, sha in result['source_files'].items()
                    if name in {module.replace('.', '/') + '.lean', 'Submission.lean'}]}
    if partial:
        contract.update(candidate_checked=False, module_exitcode=result.get('module_exitcode'),
                        partial_probe_sha256=result['partial_probe_sha256'])
    # This is model-visible metadata; it never enters the trusted proof bundle.
    atomic_json(job / 'workspace/research-contract.json', contract)
    return contract


def compact_goals(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    goals = []
    for g in value['goals'][:3]:
        typ = g['exact_type']
        goals.append({'declaration': g['declaration'], 'exact_type': typ if len(typ) <= 5000
                      else 'Full exact type is stored in research-inherited.json.',
                      'step_id': g['step_id'], 'accepted': False})
    return {'kind': value['kind'], 'frozen_identity': value['frozen_identity'],
            'goals': goals, 'captured_goal_count': len(value['goals']), 'accepted': False}


def collect_goals(job: Path, declaration: str | None = None) -> dict[str, Any]:
    from leaneval import worker
    from leaneval.goal_types import checked_record
    frontier = read(job / 'research-frontier.json')
    expected = frontier.get('frozen_identity')
    if not expected:
        raise ValueError('Prepared frontier has no supported frozen goal identity')
    config = read(job / 'config.json')
    if (expected['problem_id'] != config['problem_id'] or
            expected['benchmark_commit'] != profile()['benchmark']['commit']):
        raise ValueError('Prepared frontier differs from the active task')
    selected = [declaration] if declaration else expected['declarations']
    if any(d not in expected['declarations'] for d in selected):
        raise ValueError('Choose an exact declaration from the frozen task')
    old = checked_record(read(job / 'research-goals.json'), expected)
    goals = {g['declaration']: g for g in old['goals']} if old else {}
    cfg = worker.load_config(job / 'worker.json')
    for name in selected:
        result = worker.feedback(cfg, 'inspect', 'Challenge:' + name)
        types = result.get('exact_types', [])
        if (result.get('exitcode') != 0 or result.get('timed_out') or
                result.get('scope') != 'frozen-official-goal-type' or
                result.get('frozen_goal_identity') != expected or
                result.get('frozen_goal_current') is not True or
                result.get('invocation_id') != result.get('step_id') or
                len(set(types)) != 1):
            raise ValueError('No fresh unambiguous frozen goal type; inspect the compiler receipt')
        goals[name] = {'declaration': name, 'exact_type': types[0],
                       'step_id': result['step_id'], 'invocation_id': result['invocation_id'],
                       'axiom_audit': result.get('axiom_audit'), 'accepted': False}
        value = {'schema_version': SCHEMA, 'kind': 'frozen-official-goal-types',
                 'frozen_identity': expected, 'goals': [goals[n] for n in sorted(goals)],
                 'complete': len(goals) == len(expected['declarations']), 'accepted': False}
        if not checked_record(value, expected):
            raise ValueError('Malformed frozen goal diagnostic')
        atomic_json(job / 'research-goals.json', value)
    return value


def checkpoint(job: Path, directory: Path, source_files: dict[str, str], artifact: str) -> dict[str, Any]:
    config = read(job / 'config.json')
    contract, contract_source = read_contract(job, archive=directory)
    from leaneval.checkpoint_contract import resolve
    inherited = read(job / 'research-inherited.json')
    contracts, type_receipts, pending_contract = resolve(job, contract, source_files, inherited)
    from leaneval.failed_routes import snapshot
    failed_routes = snapshot(job, inherited)
    from leaneval import type_history
    historical_types = type_history.collect(job, inherited)
    bound = bool(contracts)
    notes = job / 'workspace/NOTES.md'
    handoff = notes.read_text(errors='replace')[:4000] if notes.is_file() and not notes.is_symlink() else ''
    from leaneval.goal_types import checked_record
    goal_record = (read(job / 'research-goals.json') or
                   read(job / 'research-inherited.json').get('official_goals') or {})
    official = checked_record(goal_record, read(job / 'research-frontier.json').get('frozen_identity'))
    value = {'schema_version': SCHEMA, 'parent_job': str(job), 'artifact_digest': artifact,
             'source_files': source_files, 'problem_id': config.get('problem_id'),
             'statement_revision': config.get('statement_revision'),
             'owner': policy(config)['owner'], 'contracts': contracts, 'type_receipts': type_receipts,
             'stale_contract_present': bool(contract) and not bound,
             'pending_contract': pending_contract, 'failed_routes': failed_routes,
             'historical_types': historical_types,
             'contract_source': contract_source,
             'unverified_handoff': handoff, 'official_goals': official,
             'stale_official_goals_present': bool(goal_record) and not official, 'accepted': False}
    value['checkpoint_digest'] = digest(value)
    atomic_json(directory / 'research-checkpoint.json', value)
    return value


def inherit(snapshot_dir: Path, snapshot: dict[str, Any], new_job: Path) -> None:
    expected = snapshot.get('research_checkpoint_digest')
    if not expected:
        return
    value = read(snapshot_dir / 'research-checkpoint.json')
    claimed = value.pop('checkpoint_digest', None)
    if claimed != expected or digest(value) != expected:
        raise ValueError('research checkpoint identity mismatch')
    if value.get('artifact_digest') != snapshot['artifact_digest']:
        raise ValueError('research checkpoint belongs to a different proof artifact')
    value['checkpoint_digest'] = claimed
    atomic_json(new_job / 'research-inherited.json', value)


def nudge(job: Path) -> str:
    source = inventory(job / 'workspace')
    feedback = proof_feedback(job)
    contract = load_contract(job)
    observation = nudge_observation(source, feedback, contract)
    state = read(job / 'research-nudge.json')
    count = state.get('unchanged_observations', 0) + 1 if state.get('observation') == observation else 0
    atomic_json(job / 'research-nudge.json', {'observation': observation, 'unchanged_observations': count})
    instruction = ('No source, diagnostic or residual-contract delta was observed across repeated '
                   'checkpoints. Choose a different explicit check or proof route before the next '
                   'continuation; do not repeat a generic continue request.\n') if count >= 2 else ''
    if feedback and not feedback_matches_source(feedback, source):
        instruction += 'Prior check is not bound to the current source; run a fresh scoped check.\n'
    inherited = read(job / 'research-inherited.json')
    view = residual_view(job, source, contract, inherited)
    for item in view['contracts']:
        partial = item['kind'] == 'partial-compiler-type'
        label = ('Compiler-recorded residual type (partial elaboration; module not certified):'
                 if partial else 'Compiler-recorded residual type (diagnostic; not a proved goal):')
        instruction += (label + '\n' + str(item['remaining_type']) +
                        '\nOwner: ' + str(item['owner']) +
                        '\nNext falsifiable check (worker plan): ' + str(item['next_check']) + '\n')
    if view['pending_contract']:
        instruction += ('Unverified residual note; capture a fresh type before relying on it:\n' +
                        json.dumps(view['pending_contract'], ensure_ascii=False) + '\n')
    from leaneval.failed_routes import brief, snapshot
    instruction += brief(snapshot(job, inherited), source)
    from leaneval import type_history
    instruction += type_history.brief(type_history.collect(job, inherited), source,
                                      [item['step_id'] for item in view['contracts']])
    return instruction


def main(job: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('--module', required=True)
    cap.add_argument('--declaration', required=True)
    cap.add_argument('--next-check', required=True)
    cap.add_argument('--blocked-by', action='append', default=[])
    cap.add_argument('--partial', action='store_true')
    goals = sub.add_parser('goals')
    goals.add_argument('--declaration')
    sub.add_parser('frontier')
    args = parser.parse_args(argv)
    if args.command == 'capture':
        result = capture(job, args.module, args.declaration, args.next_check, args.blocked_by, partial=args.partial)
    elif args.command == 'goals':
        result = collect_goals(job, args.declaration)
    else:
        result = read(job / 'research-frontier.json')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
