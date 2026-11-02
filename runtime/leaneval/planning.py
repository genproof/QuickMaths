"""Read the frozen benchmark profile and catalog bundled with the runtime.

Only the official benchmark verifier may produce a solved verdict.
"""

from __future__ import annotations

import json
import tomllib
from importlib.resources import files
from typing import Any


def resource_text(name: str) -> str:
    """Read a bundled, caller-selected resource (never an agent-provided path)."""
    return files("leaneval").joinpath("resources", name).read_text(encoding="utf-8")


def profile() -> dict[str, Any]:
    return tomllib.loads(resource_text("profile.toml"))


def benchmark() -> dict[str, Any]:
    return json.loads(resource_text("benchmark.json"))
