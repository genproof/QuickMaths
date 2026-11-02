"""Secret references: the one place where a credential reference is resolved.

A reference names where a secret lives, never the secret itself:

  file:PATH  a regular file readable only by its owner (mode 600); `~` is the home of the
             reading process, so one reference works on every host of a fleet
  env:NAME   an environment variable of the reading process

A bare absolute path is read as ``file:PATH`` (the older token-file setting). The account label
of a reference (quota blocks, account order, status) is the file name without a ``.token``
suffix, or the variable name. Nothing here prints, logs or copies a secret.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SecretError(ValueError):
    """A reference is malformed, or its secret is missing or not private."""


def parse(ref: str, home: Path | None = None) -> tuple[str, str]:
    """("file", absolute path) or ("env", variable name)."""
    if not isinstance(ref, str) or not ref or "\0" in ref:
        raise SecretError("empty or invalid secret reference")
    if ref.startswith("env:"):
        name = ref[4:]
        if not ENV_NAME.fullmatch(name):
            raise SecretError("env: reference needs a variable name")
        return "env", name
    path = ref[5:] if ref.startswith("file:") else ref
    if path.startswith("~/"):
        path = str((home or Path.home()) / path[2:])
    if not path.startswith("/"):
        raise SecretError("file: reference needs an absolute or ~/ path")
    return "file", path


def path_of(ref: str, home: Path | None = None) -> Path | None:
    """The file of a file: reference, else None."""
    kind, value = parse(ref, home)
    return Path(value) if kind == "file" else None


def label(ref: str, home: Path | None = None) -> str:
    kind, value = parse(ref, home)
    if kind == "env":
        return value
    name = Path(value).name
    return name[:-len(".token")] if name.endswith(".token") and len(name) > len(".token") else name


def resolve(ref: str, home: Path | None = None) -> str:
    """The secret, stripped of surrounding whitespace."""
    kind, value = parse(ref, home)
    if kind == "env":
        secret = os.environ.get(value, "").strip()
        if not secret:
            raise SecretError("secret environment variable is unset or empty")
        return secret
    path = Path(value)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise SecretError("secret file missing or not private (mode 600 required)")
    secret = path.read_text().strip()
    if not secret:
        raise SecretError("secret file is empty")
    return secret
