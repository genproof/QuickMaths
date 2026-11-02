"""Select only the appended diagnostic commands in a partial source probe.

These records describe elaborated types; they never certify the candidate.
"""
from __future__ import annotations

import json
from leaneval.type_diagnostics import is_type_diagnostic


def probe(source: bytes, declaration: str) -> tuple[bytes, int]:
    trailer = ('\n\n#check _root_.' + declaration + '\n#print axioms _root_.' + declaration + '\n').encode()
    return source + trailer, source.count(b'\n') + 3


def select(stdout: str, path: str, first_line: int, declaration: str) -> tuple[list[str], str]:
    selected = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (not isinstance(item, dict) or item.get('severity') != 'information'
                or item.get('fileName') != path
                or not isinstance(item.get('pos'), dict)
                or item['pos'].get('line') not in (first_line, first_line + 1)):
            continue
        selected.append(item)
    types = [item['data'] for item in selected
             if item['pos']['line'] == first_line and isinstance(item.get('data'), str)
             and is_type_diagnostic(item['data'], declaration)]
    axiom_output = '\n'.join(json.dumps(item) for item in selected
                             if item['pos']['line'] == first_line + 1)
    return types, axiom_output
