#!/usr/bin/env python3
"""Regenerate or check runtime/runtime-manifest.json, the file list the queue controller verifies.

    tools/runtime_manifest.py           rewrite the "files" map from the tracked runtime files
    tools/runtime_manifest.py --check   exit 1 when the committed manifest differs (CI, tests)

The controller refuses a runtime whose files differ from their hashes, so every runtime change is
committed together with a regenerated manifest. fleet/server/deploy_runtime.sh rehashes the deployed
copy the same way and adds its content-addressed runtime id.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
MANIFEST = RUNTIME / "runtime-manifest.json"


def tracked_files() -> dict[str, str]:
    names = subprocess.run(["git", "-C", str(RUNTIME), "ls-files", "-z", "."], check=True, capture_output=True,
                           text=True).stdout.split("\0")
    return {name: hashlib.sha256((RUNTIME / name).read_bytes()).hexdigest()
            for name in sorted(names) if name and name != MANIFEST.name and (RUNTIME / name).is_file()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text())
    files = tracked_files()
    if args.check:
        if manifest["files"] != files:
            changed = sorted(set(files) ^ set(manifest["files"]) | {k for k in files if manifest["files"].get(k) != files[k]})
            print("runtime-manifest.json is stale: " + ", ".join(changed), file=sys.stderr)
            print("run tools/runtime_manifest.py and commit the result", file=sys.stderr)
            return 1
        return 0
    manifest["files"] = files
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"{len(files)} files hashed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
