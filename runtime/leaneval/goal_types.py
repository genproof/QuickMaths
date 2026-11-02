"""Identity and shape checks for diagnostic types of frozen benchmark goals."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from leaneval.type_diagnostics import is_type_diagnostic

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def identity(original: dict[str, bytes], benchmark: str, problem: str) -> dict[str, Any]:
    holes = json.loads(original['holes.json'])
    if holes.get('id') != problem:
        raise ValueError('Frozen goal metadata belongs to a different problem')
    declarations = [h['basename'] for h in holes['holes']]
    if (not declarations or len(declarations) > 100 or
            any(not isinstance(n, str) or not NAME.fullmatch(n) for n in declarations) or
            len(set(declarations)) != len(declarations)):
        raise ValueError('Frozen goal declarations are missing, ambiguous or unsupported')
    return {'benchmark_commit': benchmark, 'problem_id': problem, 'module': 'Challenge',
            'source_files': {n: hashlib.sha256(original[n]).hexdigest()
                             for n in ('Challenge.lean', 'holes.json')},
            'declarations': sorted(declarations)}


def checked_record(value: Any, expected: Any) -> dict[str, Any] | None:
    """Fail closed; a goal type is never an accepted proof or residual lemma."""
    if (not isinstance(value, dict) or not isinstance(expected, dict) or
            value.get('frozen_identity') != expected or value.get('accepted') is not False or
            value.get('kind') != 'frozen-official-goal-types'):
        return None
    goals = value.get('goals')
    if not isinstance(goals, list) or not goals:
        return None
    seen = set()
    for goal in goals:
        if not isinstance(goal, dict):
            return None
        name, typ = goal.get('declaration'), goal.get('exact_type')
        if (not isinstance(name, str) or name not in expected.get('declarations', []) or
                name in seen or not isinstance(typ, str) or not is_type_diagnostic(typ, name) or
                not goal.get('step_id') or goal.get('step_id') != goal.get('invocation_id') or
                goal.get('accepted') is not False):
            return None
        seen.add(name)
    return value
