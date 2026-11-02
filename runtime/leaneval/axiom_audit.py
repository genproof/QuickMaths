"""Strict same-invocation diagnostic folding; never proof acceptance."""
from __future__ import annotations
import json
import re

ALLOWED = {'propext', 'Classical.choice', 'Quot.sound'}


def fold(raw: str, expected: list[str]) -> dict:
    closures = {}
    identical_duplicates, conflicts, malformed = [], [], []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            malformed.append('non-JSON output')
            continue
        if not isinstance(item, dict):
            malformed.append('invalid diagnostic')
            continue
        if item.get('severity') == 'error':
            malformed.append('compiler error')
        if item.get('severity') != 'information':
            continue
        data = item.get('data')
        if not isinstance(data, str):
            malformed.append('missing diagnostic data')
            continue
        match = re.fullmatch(r"'([^']+)' depends on axioms: \[([^]]*)\]", data.strip())
        empty = re.fullmatch(r"'([^']+)' does not depend on any axioms", data.strip())
        if not (match or empty):
            continue
        name = (match or empty).group(1)
        axioms = frozenset(v.strip() for v in match.group(2).split(',') if v.strip()) if match else frozenset()
        if name in closures:
            if closures[name] == axioms:
                identical_duplicates.append(name)
            else:
                conflicts.append(name)
        else:
            closures[name] = axioms
    valid = bool(expected) and not malformed and not conflicts and all(
        name in closures and closures[name] <= ALLOWED for name in expected)
    return {'closures': {k: sorted(v) for k, v in closures.items()},
            'expected': expected, 'identical_duplicates': identical_duplicates,
            'conflicts': conflicts, 'malformed': malformed,
            'diagnostic_foundational': valid, 'accepted': False}

