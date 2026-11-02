"""The current evaluation flow in the runtime verifier: the bubblewrap wrapper and the PATH it needs."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from leaneval import verifier  # noqa: E402


def test_wrapper_inserts_the_cache_bind_once_and_quotes_paths(tmp_path):
    workbench = tmp_path / "wb it's"
    packages = workbench / "vendor/lean-eval/.lake/packages"
    packages.mkdir(parents=True)
    real = workbench / ".tools/bin/bwrap"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\nfor a do printf '%s\\n' \"$a\"; done\n")
    real.chmod(0o755)
    directory = verifier.bwrap_wrapper(tmp_path / "bin", workbench)
    out = subprocess.run([str(directory / "bwrap"), "--die-with-parent", "--", "lake", "--", "x"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    cache = str(packages.resolve())
    assert out == ["--die-with-parent", "--ro-bind", cache, cache, "--", "lake", "--", "x"]
    before = (directory / "bwrap").stat().st_mtime_ns
    verifier.bwrap_wrapper(tmp_path / "bin", workbench)  # unchanged content: not rewritten
    assert (directory / "bwrap").stat().st_mtime_ns == before


def test_environment_puts_the_wrapper_first_and_drops_comparator_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPARATOR_BWRAP", "/elsewhere/bwrap")
    monkeypatch.setenv("COMPARATOR_BIN", "/elsewhere/comparator")
    env = verifier.environment(tmp_path / "wb", tmp_path / "bin")
    path = env["PATH"].split(os.pathsep)
    assert path[0] == str(tmp_path / "bin") and path[1] == str(verifier.toolchain_bin())
    assert "COMPARATOR_BWRAP" not in env and "COMPARATOR_BIN" not in env
    assert verifier.environment(tmp_path / "wb")["PATH"].split(os.pathsep)[0] == str(verifier.toolchain_bin())
