"""Read bounded compiler feedback separately from small configuration metadata."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

MAX_FEEDBACK_BYTES = 32 * 1024 * 1024


def read_feedback(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError('feedback must be a regular file: ' + str(path))
    before = path.stat()
    if before.st_size > MAX_FEEDBACK_BYTES:
        raise ValueError('feedback exceeds 32MiB: ' + str(path))
    raw = path.read_bytes()
    after = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after) or len(raw) != after.st_size:
        raise ValueError('feedback changed during read: ' + str(path))
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('feedback must be an object: ' + str(path))
    return value
