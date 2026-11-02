"""The committed runtime/runtime-manifest.json lists exactly the runtime's files and hashes.

The queue controller checks every listed file against its hash, so a runtime change committed without
regenerating the manifest (tools/runtime_manifest.py) would be refused on a host.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_lists_every_tracked_runtime_file_with_its_hash():
    if subprocess.run(["git", "-C", str(ROOT), "rev-parse"], capture_output=True).returncode != 0:
        pytest.skip("not a git checkout (e.g. an exported tarball): tracked files unknown")
    result = subprocess.run([sys.executable, str(ROOT / "tools/runtime_manifest.py"), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
