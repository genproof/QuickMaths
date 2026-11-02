"""Select diagnostic import roots from the captured, controller-scoped bundle."""
from __future__ import annotations

import hashlib
from typing import Any

from leaneval.costs import NAME
from leaneval.frontier import path_matches, policy
from leaneval.artifacts import digest, proof_path

MAX_OWNED_SEEDS = 4096


def select(overlay: dict[str, bytes], contract: dict[str, Any] | None
           ) -> tuple[list[str], dict[str, Any]]:
    # Older clients and integration lanes retain the full submission root.
    if contract is None:
        return ['Submission'], {'mode': 'submission', 'complete': True}
    p = policy({'campaign_assignment': {'research_frontier': contract}})
    if p['lane_kind'] != 'research':
        return ['Submission'], {'mode': 'submission', 'complete': True}
    owned = sorted(name for name in overlay if proof_path(name)
                   and any(path_matches(name, pattern) for pattern in p['edit_scope'])
                   and not any(path_matches(name, pattern) for pattern in p['exclude_scope']))
    modules = [name[:-5].replace('/', '.') for name in owned]
    valid = [module for module in modules if NAME.fullmatch(module)]
    selected = valid[:MAX_OWNED_SEEDS]
    seeds = list(dict.fromkeys(['Submission', *selected]))
    invalid = len(modules) - len(valid)
    omitted = len(valid) - len(selected)
    return seeds, {
        'mode': 'research_scope', 'owner': p['owner'],
        'edit_scope': p['edit_scope'], 'exclude_scope': p['exclude_scope'],
        'owned_modules': len(owned), 'selected_owned_modules': len(selected),
        'omitted_owned_modules': omitted, 'invalid_module_names': invalid,
        'seed_limit': MAX_OWNED_SEEDS, 'seed_count': len(seeds),
        'owned_source_digest': digest({name: hashlib.sha256(overlay[name]).hexdigest()
                                       for name in owned}),
        'complete': bool(owned) and not (invalid or omitted),
        'scope': 'Owned modules and submission root; lexical imports, not proof acceptance',
    }
