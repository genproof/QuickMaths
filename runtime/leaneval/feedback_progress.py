"""In-session advisory checkpoints; never turn planning into proof progress."""
from pathlib import Path
import time

from leaneval.incremental_queries import equivalent as same_query, observation as query_observation

THRESHOLD = 3
SEARCH_LIMIT = 6


def _context(job, source, feedback):
    from leaneval import frontier
    contract = frontier.read(job / 'workspace/research-contract.json')
    if contract.get('source_files') != source:
        contract = {}
    observation = frontier.nudge_observation(source, feedback, contract)
    # Missing parsed diagnostics are not evidence that two failures are identical.
    if feedback.get('outcome_class') == 'lean_diagnostic' and not feedback.get('diagnostics'):
        observation['unparsed_failure_step'] = feedback.get('step_id')
    return observation


def observe(job: Path, receipt):
    from leaneval import frontier
    action = receipt.get('action')
    proof = action in {'build', 'check', 'inspect', 'incremental'}
    if action != 'search' and not proof:
        return None
    if action == 'inspect' and receipt.get('query', '').startswith('Challenge:'):
        return None
    if action == 'incremental':
        from leaneval.incremental_progress import validate
        if not validate(job, receipt)[0]:
            return None
    step = receipt.get('step_id')
    if not isinstance(step, str) or not step:
        return None
    path = job / 'research-session-progress.json'
    state = frontier.read(path)
    if state.get('last_step_id') == step:
        return state.get('last_advisory')
    pending = state.get('unsampled_searches', 0)
    # Large proof trees can take seconds to hash. Search calls are sampled at
    # the first checkpoint and every third unchanged step; advice is emitted
    # only after a fresh complete content hash, never from cached metadata.
    if (action == 'search' and state.get('observation') and
            (state.get('unchanged_checkpoints', 0) + pending + 1) % THRESHOLD):
        state.update(last_step_id=step, unsampled_searches=pending + 1,
                     last_advisory=None, observed_at=time.time())
        frontier.atomic_json(path, state)
        return None
    source = frontier.inventory(job / 'workspace')
    if proof:
        if (not frontier.feedback_matches_source(receipt, source) or
                receipt.get('outcome_class') not in {'check_succeeded', 'lean_diagnostic'}):
            return None
        feedback = receipt
    else:
        observation, feedback = _search_observation(job, source, state)
    if proof:
        observation = _context(job, source, feedback)
    unchanged = _checked_context(state.get('observation', {})) == _checked_context(observation)
    count = state.get('unchanged_checkpoints', 0) + pending + 1 if unchanged else 1
    parsed_failure = (proof and receipt.get('outcome_class') == 'lean_diagnostic' and
                      bool(receipt.get('diagnostics')))
    repeat = state.get('identical_failed_checks', 0) if unchanged else 0
    if parsed_failure:
        repeat += 1
    elif proof:
        repeat = 0
    advisory = None
    if count >= THRESHOLD:
        owner = frontier.policy(frontier.read(job / 'config.json'))['owner']
        advisory = {'kind': 'unchanged-proof-checkpoint', 'accepted': False,
                    'source_sha256': frontier.digest(source), 'owner': owner,
                    'unchanged_checkpoints': count, 'identical_failed_checks': repeat,
                    'instruction': 'No source or checked proof change was observed across these feedback steps. '
                    'Run a concrete Lean test, capture the exact residual type, or state a different '
                    'falsifiable route before repeating broad searches. Original task types and search '
                    'results are not proof progress. After six unchanged checkpoints, search is refused until '
                    'the source changes or a different concrete Lean check changes the checked state.'}
    frontier.atomic_json(path, {'schema_version': 1, 'observation': observation,
                        'unchanged_checkpoints': count, 'identical_failed_checks': repeat,
                        'proof_step_id': feedback.get('step_id'),
                        'proof_action': feedback.get('action'), 'proof_query': feedback.get('query'),
                        'unsampled_searches': 0,
                        'last_step_id': step, 'observed_at': time.time(), 'last_advisory': advisory,
                        'accepted': False})
    return advisory


def blocks_repeat(job: Path, source, action, query):
    from leaneval import frontier
    state = frontier.read(job / 'research-session-progress.json')
    if (state.get('identical_failed_checks', 0) < THRESHOLD or
            state.get('proof_action') != action or not same_query(action, state.get('proof_query'), query)):
        return False
    feedback = frontier.proof_feedback(job)
    return (frontier.feedback_matches_source(feedback, source) and
            bool(feedback.get('diagnostics')) and
            feedback.get('outcome_class') == 'lean_diagnostic' and
            state.get('proof_step_id') == feedback.get('step_id') and
            _checked_context(state.get('observation', {})) == _checked_context(_context(job, source, feedback)))


def _checked_context(observation):
    # Editing a research note cannot substitute for a compiler observation.
    return {key: value for key, value in query_observation(observation).items() if key != 'contract_digest'}


def search_barrier(job: Path):
    """Require a fresh concrete step after repeated unchanged search checkpoints."""
    from leaneval import frontier
    state = frontier.read(job / 'research-session-progress.json')
    if state.get('unchanged_checkpoints', 0) < SEARCH_LIMIT:
        return None
    source = frontier.inventory(job / 'workspace')
    observation, _ = _search_observation(job, source, state)
    if _checked_context(state.get('observation', {})) != _checked_context(observation):
        return None
    return {'source_files': source, 'source_sha256': frontier.digest(source),
            'blocked_by': state.get('last_step_id'),
            'unchanged_checkpoints': state['unchanged_checkpoints'],
            'required_next_action': 'Change the proof source, run a different scoped Lean build/check, '
                                    'or inspect a different residual declaration before another search. '
                                    'Editing research notes alone does not satisfy this gate.'}


def _search_observation(job, source, state):
    from leaneval import frontier
    feedback = frontier.proof_feedback(job)
    if (frontier.feedback_matches_source(feedback, source) and
            feedback.get('outcome_class') in {'check_succeeded', 'lean_diagnostic'}):
        return _context(job, source, feedback), feedback
    previous = state.get('observation', {})
    if previous.get('source_sha256') == frontier.digest(source):
        # A timeout or stale receipt is not a new checked state. Preserve the
        # controller's previous checkpoint until a qualified check or source change.
        return previous, {}
    return _context(job, source, {}), {}
