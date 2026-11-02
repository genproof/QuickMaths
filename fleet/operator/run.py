#!/usr/bin/env python3
"""Operator machine: run one host-side tool of this directory on a fleet host, as
`ssh HOST python3 - ARGS < TOOL`, with the ssh options of local/fleet.py (StrictHostKeyChecking and
BatchMode always on, LEH_SSH_CONFIG, LEH_SSH_KNOWN_HOSTS) and every argument quoted for the remote shell
(a bare `ssh HOST python3 - PID "a reason"` splits the reason into words on the host).

Usage: run.py [--upload FILE] [--print] HOST TOOL [ARGS ...]

  run.py HOST TOOL [ARGS ...]                  e.g. run.py host-1 lanes.py my_problem
  run.py --upload FILE HOST TOOL [ARGS ...]    first copies FILE to <own root>/uploads/<name> on HOST;
                                               tools that take a file accept uploads/<name>
  run.py --print HOST TOOL [ARGS ...]          print the ssh command instead of running it

stdout and stderr of the tool pass through unchanged (export_prog.py writes a tarball to stdout), and
the exit status is the tool's. On the host, LEH_OWN_ROOT and LEH_CAMPAIGNS_ROOT are set from
LEH_REMOTE_OWN_ROOT and LEH_REMOTE_CAMPAIGNS_ROOT when those are set here (relative to the remote home).
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet = load("leh_fleet", HERE.parent / "local/fleet.py")


def remote_path(value: str) -> str:
    """A remote path for the remote shell: relative paths are relative to the remote home."""
    return shlex.quote(value) if value.startswith("/") else '"$HOME"/' + shlex.quote(value)


def remote_env() -> list[str]:
    env = []
    for local, remote in (("LEH_REMOTE_OWN_ROOT", "LEH_OWN_ROOT"), ("LEH_REMOTE_CAMPAIGNS_ROOT", "LEH_CAMPAIGNS_ROOT")):
        if os.environ.get(local):
            env.append(f"{remote}={remote_path(os.environ[local])}")
    return env


def tool_path(name: str) -> Path:
    path = Path(name) if "/" in name else HERE / name
    if not path.is_file() or path.suffix != ".py" or path.resolve() == Path(__file__).resolve():
        raise SystemExit(f"no host-side tool {name}")
    return path


def commands(host: str, tool: str, args: list[str], upload: Path | None) -> list[tuple[list[str], Path]]:
    """(ssh argv, stdin file) pairs to run in order."""
    own = os.environ.get("LEH_REMOTE_OWN_ROOT") or "leh-fleet"
    steps = []
    if upload is not None:
        dest = f"{own}/uploads/{upload.name}"
        steps.append(([*fleet.SSH, host, f"mkdir -p {remote_path(own + '/uploads')} && cat > {remote_path(dest)}"],
                      upload))
    remote = " ".join([*remote_env(), "python3", "-", *(shlex.quote(a) for a in args)])
    steps.append(([*fleet.SSH, host, remote], tool_path(tool)))
    return steps


def main(argv: list[str]) -> int:
    upload, dry = None, False
    while argv[:1] in (["--upload"], ["--print"]):
        if argv[0] == "--print":
            dry, argv = True, argv[1:]
        else:
            if len(argv) < 2 or not Path(argv[1]).is_file():
                raise SystemExit("--upload needs an existing file")
            upload, argv = Path(argv[1]), argv[2:]
    if len(argv) < 2 or argv[0].startswith("-"):
        raise SystemExit(__doc__)
    steps = commands(argv[0], argv[1], argv[2:], upload)
    for cmd, stdin in steps:
        if dry:
            print(shlex.join(cmd), "<", stdin)
            continue
        with stdin.open("rb") as stream:
            code = subprocess.run(cmd, stdin=stream).returncode
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
