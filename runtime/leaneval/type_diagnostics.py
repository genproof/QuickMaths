"""Extract complete #check diagnostics without treating them as proof evidence."""

from __future__ import annotations

import json


def _group_end(text: str, start: int) -> int | None:
    pairs = {'(': ')', '[': ']', '{': '}', '⦃': '⦄'}
    stack = [pairs[text[start]]]
    quote = None
    escaped = False
    for pos in range(start + 1, len(text)):
        char = text[pos]
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', '«'):
            quote = '"' if char == '"' else '»'
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if char != stack.pop():
                return None
            if not stack:
                return pos + 1
    return None


def is_type_diagnostic(data: str, declaration: str) -> bool:
    if not declaration or not data.startswith(declaration):
        return False
    pos = len(declaration)
    if data[pos:pos + 2] == '.{':
        end = _group_end(data, pos + 1)
        if end is None or not data[pos + 2:end - 1].strip():
            return False
        pos = end
    # A qualified-name prefix must never match a different declaration.
    if pos < len(data) and not (data[pos].isspace() or data[pos] == ':'):
        return False
    while True:
        while pos < len(data) and data[pos].isspace():
            pos += 1
        if pos == len(data):
            return False
        if data[pos] == ':':
            return bool(data[pos + 1:].strip())
        if data[pos] not in '([{⦃':
            return False
        end = _group_end(data, pos)
        if end is None or not data[pos + 1:end - 1].strip():
            return False
        pos = end


def exact_types(stdout: str, declaration: str) -> list[str]:
    """Keep all matching records; the caller must reject conflicting types."""
    result = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get('severity') != 'information':
            continue
        data = event.get('data')
        if isinstance(data, str) and is_type_diagnostic(data, declaration):
            result.append(data)
    return result
