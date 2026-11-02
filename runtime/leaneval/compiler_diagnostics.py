"""Parse compiler diagnostics; retain raw output separately as the authority."""
import json
import re

PLAIN = re.compile(r'^(?P<file>.+?\.lean):(?P<line>\d+):(?P<column>\d+):\s*'
                   r'(?P<severity>error|warning|information|info):\s*(?P<message>.*)$')
LAKE = re.compile(r'^(?P<severity>error|warning|information|info):\s*'
                  r'(?P<file>.+?\.lean):(?P<line>\d+):(?P<column>\d+):\s*(?P<message>.*)$')
META = re.compile(r'^(?:trace:|error: (?:build failed|Lean exited)|warning: build|'
                  r'Build |Some required targets|[✖✔ℹ⚠]|\[\d+/\d+\])')


def parse(stdout, stderr, source_sha256):
    entries = []
    current = None
    lake_block = False
    for line in (stdout + '\n' + stderr).splitlines():
        event = None
        if line.startswith('{'):
            try:
                event = json.loads(line)
            except ValueError:
                pass
        if isinstance(event, dict):
            current = None
            pos = event.get('pos')
            if (event.get('severity') in {'error', 'warning', 'information', 'info'} and
                    isinstance(event.get('fileName'), str) and event['fileName'].endswith('.lean') and
                    isinstance(event.get('data'), str) and isinstance(pos, dict) and
                    type(pos.get('line')) is int and pos['line'] > 0 and
                    type(pos.get('column')) is int and pos['column'] >= 0):
                entries.append({'file': event['fileName'], 'line': pos['line'], 'column': pos['column'],
                                'severity': event['severity'], 'message': event['data'],
                                'source_sha256': source_sha256, 'prefix_verified': False})
            continue
        match = PLAIN.match(line)
        lake = LAKE.match(line) if not match else None
        if match or lake:
            match = match or lake
            current = {**match.groupdict(), 'line': int(match['line']), 'column': int(match['column']),
                       'source_sha256': source_sha256, 'prefix_verified': False}
            entries.append(current)
            lake_block = bool(lake)
        elif current and not META.match(line) and (lake_block or line[:1].isspace()):
            current['message'] += '\n' + line
        else:
            current = None
    for entry in entries:
        entry['message'] = entry['message'].rstrip()
    return entries
